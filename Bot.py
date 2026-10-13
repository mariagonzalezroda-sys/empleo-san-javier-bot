
import os
import re
import asyncio
import sqlite3
import logging
import threading
import hashlib
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, parse_qs
from datetime import datetime, date
from urllib.request import Request, urlopen

from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# CONFIGURACION
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
DATABASE_PATH = "bot_empleo.sqlite3"

INTERVALO_BUSQUEDA = 2 * 60 * 60
MAX_OFERTAS_ENVIO = 8
MAX_PRIMER_ENVIO = 5
MAX_CANDIDATOS = 120
MAX_PAGINAS = 12
TIMEOUT_WEB = 25
SALARIO_BRUTO_REFERENCIA = 24000

URL_LISTADO = (
    "https://www.sistemanacionalempleo.es/"
    "OfertaDifusionWEB/busquedaOfertas.do"
    "?botonNavegacion=Enviar&modo=continuar&provincia=30"
)

ZONAS = {
    "san javier": "San Javier",
    "san pedro del pinatar": "San Pedro del Pinatar",
    "los alcazares": "Los Alcázares",
    "alcazares, los": "Los Alcázares",
    "torre-pacheco": "Torre-Pacheco",
    "torre pacheco": "Torre-Pacheco",
    "cartagena": "Cartagena",
}

PUESTOS = [
    "conductor", "conductora", "repartidor", "repartidora",
    "transportista", "conductor de furgoneta",
    "conductor repartidor", "reparto",
    "carretillero", "carretillera",
    "mozo de almacen", "moza de almacen",
    "auxiliar de almacen", "operario de almacen",
    "operaria de almacen", "almacenero", "almacenera",
    "almacen", "logistica", "expediciones",
    "preparador de pedidos", "preparadora de pedidos",
    "operario", "operaria",
    "operario de produccion", "operaria de produccion",
    "operador de maquinaria", "operadora de maquinaria",
    "produccion", "fabrica", "peon industrial",
    "peon de fabrica", "peon", "peona",
    "conserje", "control de accesos",
    "controlador de accesos", "auxiliar de servicios",
    "recepcionista", "reponedor", "reponedora",
    "dependiente", "dependienta", "supermercado",
    "mantenimiento general",
    "montador", "montadora",
]

TITULOS_EXCLUIDOS = [
    "limpieza", "limpiador", "limpiadora",
    "administrativo agricola", "administrativa agricola",
    "administrativo agrícola", "administrativa agrícola",
    "administrativo contable", "administrativa contable",
    "asesor contable", "asesora contable",
    "asesor fiscal", "asesora fiscal",
    "soldador", "soldadora", "fontanero", "fontanera",
    "electricista", "fisioterapeuta",
    "ingeniero", "ingeniera",
    "tecnico instalador", "tecnica instaladora",
    "técnico instalador", "técnica instaladora",
    "vigilante de seguridad",
    "mecanico", "mecanica", "mecánico", "mecánica",
]

FRASES_NOCHE = [
    "turno de noche", "turno nocturno",
    "turno fijo de noche", "horario nocturno",
    "jornada nocturna", "solo noches",
    "trabajo nocturno", "turnos de noche",
    "turno rotativo mañana tarde noche", "turno rotativo de mañana tarde y noche",
    "disponibilidad para trabajar de noche", "disponibilidad nocturna",
]

TAREA_AUTOMATICA = None
BUSQUEDA_EN_CURSO = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("empleo")
logging.getLogger("httpx").setLevel(logging.WARNING)

web = Flask(__name__)


# ============================================================
# BASE DE DATOS: SUSCRIPTORES Y OFERTAS YA ENVIADAS
# ============================================================

def db():
    conexion = sqlite3.connect(DATABASE_PATH, timeout=20)
    conexion.execute("PRAGMA busy_timeout=20000")
    return conexion


def inicializar_db():
    with db() as conexion:
        conexion.execute("""
            CREATE TABLE IF NOT EXISTS suscriptores (
                chat_id INTEGER PRIMARY KEY
            )
        """)
        conexion.execute("""
            CREATE TABLE IF NOT EXISTS enviadas (
                id TEXT PRIMARY KEY,
                fecha TEXT
            )
        """)
        conexion.execute("""
            CREATE TABLE IF NOT EXISTS ajustes (
                clave TEXT PRIMARY KEY,
                valor TEXT
            )
        """)


def suscriptores():
    with db() as conexion:
        filas = conexion.execute(
            "SELECT chat_id FROM suscriptores"
        ).fetchall()
    return [fila[0] for fila in filas]


def activar_chat(chat_id):
    with db() as conexion:
        conexion.execute(
            "INSERT OR IGNORE INTO suscriptores(chat_id) VALUES (?)",
            (chat_id,),
        )


def desactivar_chat(chat_id):
    with db() as conexion:
        conexion.execute(
            "DELETE FROM suscriptores WHERE chat_id = ?",
            (chat_id,),
        )


def obtener_enviadas():
    with db() as conexion:
        filas = conexion.execute("SELECT id FROM enviadas").fetchall()
    return {fila[0] for fila in filas}


def guardar_enviada(identificador):
    with db() as conexion:
        conexion.execute(
            "INSERT OR IGNORE INTO enviadas(id, fecha) VALUES (?, ?)",
            (identificador, datetime.now().isoformat()),
        )


def primera_consulta():
    with db() as conexion:
        fila = conexion.execute(
            "SELECT valor FROM ajustes WHERE clave = 'iniciado'"
        ).fetchone()
    return fila is None


def marcar_consulta_inicial():
    with db() as conexion:
        conexion.execute("""
            INSERT OR REPLACE INTO ajustes(clave, valor)
            VALUES ('iniciado', 'si')
        """)


# ============================================================
# LECTOR HTML
# ============================================================

class LectorHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.textos = []
        self.enlaces = []
        self.href_actual = None
        self.texto_enlace = []

    def handle_starttag(self, tag, attrs):
        atributos = dict(attrs)
        tag = tag.lower()

        if tag == "a":
            self.href_actual = atributos.get("href", "")
            self.texto_enlace = []

        if tag in (
            "br", "p", "div", "tr", "td", "li",
            "h1", "h2", "h3",
        ):
            self.textos.append(" ")

    def handle_data(self, data):
        texto = data.strip()
        if not texto:
            return

        self.textos.append(texto)

        if self.href_actual is not None:
            self.texto_enlace.append(texto)

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag == "a" and self.href_actual is not None:
            href = self.href_actual.strip()
            titulo = " ".join(self.texto_enlace).strip()

            if href:
                self.enlaces.append((href, titulo))

            self.href_actual = None
            self.texto_enlace = []

        if tag in (
            "br", "p", "div", "tr", "td", "li",
            "h1", "h2", "h3",
        ):
            self.textos.append(" ")

    def texto_completo(self):
        return re.sub(
            r"\s+", " ", unescape(" ".join(self.textos))
        ).strip()


def normalizar(texto):
    texto = (texto or "").lower()
    return texto.translate(str.maketrans("áéíóúü", "aeiouu"))


def descargar_pagina(url):
    peticion = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
            ),
            "Accept-Language": "es-ES,es;q=0.9",
        },
    )

    with urlopen(peticion, timeout=TIMEOUT_WEB) as respuesta:
        contenido = respuesta.read()
        charset = respuesta.headers.get_content_charset() or "utf-8"

    return contenido.decode(charset, errors="replace")


def fecha_es(texto):
    if not texto:
        return None

    for formato in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(texto.strip(), formato).date()
        except ValueError:
            continue

    return None


def identificador_estable(url, texto=""):
    # El ID de la ficha del portal es más fiable que la URL completa.
    parametros = parse_qs(urlparse(url).query)
    ids = parametros.get("id", [])

    if ids and ids[0].strip():
        return ids[0].strip()

    coincidencia = re.search(
        r"Datos de la oferta número\s*:?\s*(\d+)",
        texto,
        re.IGNORECASE,
    )

    if coincidencia:
        return coincidencia.group(1)

    return hashlib.sha256(url.encode("utf-8")).hexdigest()


# ============================================================
# FILTROS
# ============================================================

def titulo_interesante(titulo):
    t = normalizar(titulo)

    if any(normalizar(p) in t for p in TITULOS_EXCLUIDOS):
        return False

    # No admitir títulos administrativos genéricos.
    if "administrativ" in t:
        return False

    # Evitar que una mención secundaria al almacén haga válida
    # una oferta cuyo puesto principal no corresponde.
    return any(normalizar(puesto) in t for puesto in PUESTOS)


def extraer_fechas(texto):
    inicio = re.search(
        r"Fecha de inicio\s*:?\s*(\d{2}/\d{2}/\d{4})",
        texto, re.IGNORECASE,
    )
    fin = re.search(
        r"Fecha de fin\s*:?\s*(\d{2}/\d{2}/\d{4})",
        texto, re.IGNORECASE,
    )

    return (
        fecha_es(inicio.group(1)) if inicio else None,
        fecha_es(fin.group(1)) if fin else None,
    )


def extraer_localidad(texto):
    patrones = [
        r"Localidad de Ubicación del Puesto\s*:?\s*(.*?)"
        r"(?=\s+(?:Datos adicionales|INSCRIPCION|INSCRIPCIÓN|"
        r"Requerimientos|Requisitos|Fecha de inicio|Fecha de fin|"
        r"Datos de la oferta|Condiciones)\b|$)",
        r"Localidad\s*:?\s*(.*?)"
        r"(?=\s+(?:Datos adicionales|INSCRIPCION|INSCRIPCIÓN|"
        r"Requerimientos|Requisitos|Fecha de inicio|Fecha de fin)\b|$)",
    ]

    for patron in patrones:
        coincidencia = re.search(patron, texto, re.IGNORECASE)
        if coincidencia:
            return coincidencia.group(1).strip(" .,-")

    return ""


def extraer_salario_anual(texto):
    texto = normalizar(texto)
    patrones = [
        r"(\d{2,3}(?:[.\s]\d{3})+|\d{5,6})\s*euros?"
        r".{0,50}(?:anuales|al ano|anual|brutos al ano)",
        r"(\d{2,3}(?:[.\s]\d{3})+|\d{5,6})\s*€"
        r".{0,50}(?:anuales|al ano|anual|brutos al ano)",
    ]

    cantidades = []

    for patron in patrones:
        for cantidad in re.findall(patron, texto):
            try:
                cantidades.append(int(re.sub(r"[.\s]", "", cantidad)))
            except ValueError:
                pass

    return min(cantidades) if cantidades else None


def comprobar_localidad(localidad, texto):
    lugar = normalizar(localidad)

    for clave, nombre in ZONAS.items():
        if clave in lugar:
            return nombre

    if re.match(r"^murcia(?:\s*\(murcia\))?$", lugar):
        salario = extraer_salario_anual(texto)

        if salario is not None and salario >= SALARIO_BRUTO_REFERENCIA:
            return "Murcia capital"

    return None


def motivo_descartada(titulo, texto, fin):
    if fin is None:
        return "no se pudo verificar la fecha límite"

    if fin < date.today():
        return "la fecha límite ya ha pasado"

    if not titulo_interesante(titulo):
        return "puesto fuera de los filtros"

    if any(
        normalizar(frase) in normalizar(texto)
        for frase in FRASES_NOCHE
    ):
        return "indica expresamente turno nocturno"

    localidad = extraer_localidad(texto)

    if not localidad:
        return "no se pudo identificar la localidad"

    if not comprobar_localidad(localidad, texto):
        return "localidad fuera de las zonas configuradas"

    texto_normalizado = normalizar(texto)

    if "tip en vigor" in texto_normalizado:
        return "exige TIP en vigor"

    # Descartar ofertas reservadas a personas desempleadas o inscritas
    # como demandantes de empleo, porque la persona está trabajando.
    requisitos_desempleo = [
        r"desemplead[oa]s? de larga duracion",
        r"parad[oa]s? de larga duracion",
        r"estar (?:en situacion de )?desempleo",
        r"estar desemplead[oa]s?",
        r"imprescindible (?:estar )?desemplead[oa]s?",
        r"requisito.{0,80}desemplead[oa]s?",
        r"requisito.{0,80}inscrit[oa]s? como demandante de empleo",
        r"inscrit[oa]s? como demandante de empleo.{0,100}(?:requisito|imprescindible|obligatorio)",
        r"(?:necesari[oa]|obligatori[oa]|imprescindible|requisito).{0,100}inscrit[oa]s? como demandante",
        r"(?:estar|deber|tener que).{0,80}inscrit[oa]s? como demandante de empleo",
        r"no estar trabajand[oa]",
        r"no tener empleo",
        r"personas desempleadas",
        r"personas paradas de larga duracion",
        r"inscripcion como demandante de empleo.{0,100}(?:obligatori|imprescindible|requisito)",
    ]

    for patron in requisitos_desempleo:
        if re.search(patron, texto_normalizado, re.IGNORECASE):
            return "exige estar desempleado/a o inscrito/a como demandante"

    return None


def extraer_detalle(url, titulo_listado):
    html = descargar_pagina(url)
    lector = LectorHTML()
    lector.feed(html)
    texto = lector.texto_completo()

    inicio, fin = extraer_fechas(texto)
    titulo = (titulo_listado or "").strip()

    # Si el enlace no trae un título útil, intentar extraerlo de la ficha.
    if not titulo or normalizar(titulo) in (
        "ver oferta", "mas informacion", "más información",
    ):
        coincidencia = re.search(
            r"(?:Puesto de trabajo|Ocupación|Ocupacion|Puesto)\s*:?\s*"
            r"(.{3,100}?)(?=\s+(?:Localidad|Provincia|Fecha de inicio)\b|$)",
            texto, re.IGNORECASE,
        )
        if coincidencia:
            titulo = coincidencia.group(1).strip()

    if not titulo:
        titulo = "Oferta de empleo"

    motivo = motivo_descartada(titulo, texto, fin)

    if motivo:
        logger.info("Descartada | %s | %s", titulo, motivo)
        return None

    localidad = extraer_localidad(texto)
    zona = comprobar_localidad(localidad, texto)

    return {
        "id": identificador_estable(url, texto),
        "titulo": titulo,
        "zona": zona,
        "url": url,
        "inicio": inicio,
        "fin": fin,
        "texto": texto,
    }


# ============================================================
# CONSULTA DEL PORTAL
# ============================================================

def es_pagina_resultados(href, url_actual):
    url = urljoin(url_actual, href)
    parsed = urlparse(url)

    if "sistemanacionalempleo.es" not in parsed.netloc:
        return False

    if not parsed.path.endswith("busquedaOfertas.do"):
        return False

    parametros = parse_qs(parsed.query)
    modo = " ".join(parametros.get("modo", [])).lower()

    if "continuar" in modo:
        return True

    return any(
        clave in parametros
        for clave in ("pagina", "page", "numPagina", "paginacion")
    )


async def recoger_enlaces_listado():
    pendientes = [URL_LISTADO]
    visitadas = set()
    fichas = {}

    while pendientes and len(visitadas) < MAX_PAGINAS:
        url_pagina = pendientes.pop(0)

        if url_pagina in visitadas:
            continue

        visitadas.add(url_pagina)

        try:
            html = await asyncio.to_thread(
                descargar_pagina, url_pagina
            )
        except Exception:
            logger.exception("Error al consultar una página del listado.")
            continue

        lector = LectorHTML()
        lector.feed(html)

        for href, titulo in lector.enlaces:
            url = urljoin(url_pagina, href)

            if "detalleOferta.do" in href:
                fichas[url] = titulo
            elif es_pagina_resultados(href, url_pagina):
                if url not in visitadas and url not in pendientes:
                    pendientes.append(url)

        logger.info(
            "Listado %s/%s; fichas acumuladas: %s",
            len(visitadas), MAX_PAGINAS, len(fichas),
        )

    return list(fichas.items())


async def consultar_ofertas():
    logger.info("Iniciando consulta de ofertas.")

    enlaces = await recoger_enlaces_listado()

    if not enlaces:
        logger.warning("El portal no ha devuelto fichas.")
        return []

    # Procesar primero los títulos que parecen compatibles.
    enlaces.sort(
        key=lambda elemento: (
            0 if titulo_interesante(elemento[1]) else 1
        )
    )

    resultados = []

    for url, titulo in enlaces[:MAX_CANDIDATOS]:
        try:
            oferta = await asyncio.to_thread(
                extraer_detalle, url, titulo
            )
            if oferta:
                resultados.append(oferta)
        except Exception:
            logger.exception("Error leyendo una ficha: %s", titulo)

        await asyncio.sleep(0.2)

    unicas = {}

    for oferta in resultados:
        unicas[oferta["id"]] = oferta

    ofertas = list(unicas.values())
    ofertas.sort(
        key=lambda oferta: oferta["inicio"] or date.min,
        reverse=True,
    )

    logger.info(
        "Consulta terminada: fichas=%s; ofertas válidas=%s.",
        min(len(enlaces), MAX_CANDIDATOS), len(ofertas),
    )

    return ofertas


# ============================================================
# MENSAJES
# ============================================================

def crear_mensaje(oferta):
    inicio = (
        oferta["inicio"].strftime("%d/%m/%Y")
        if oferta["inicio"] else "No indicada"
    )
    fin = oferta["fin"].strftime("%d/%m/%Y")

    salario = extraer_salario_anual(oferta["texto"])

    if salario is None:
        salario_texto = "No publicado"
        aviso_salario = (
            "⚠️ No se puede comprobar si alcanza los 1.500 € "
            "netos al mes. Consulta el sueldo antes de aceptar."
        )
    else:
        salario_texto = (
            f"{salario:,} € brutos/año".replace(",", ".")
        )
        aviso_salario = (
            "ℹ️ El salario bruto no equivale al neto. "
            "Confirma el neto y el número de pagas."
        )

    requisitos = re.search(
        r"(?:REQUISITOS|REQUERIMIENTOS)\s*:?(.*?)(?:"
        r"CONDICIONES|INSCRIPCION|INSCRIPCIÓN|Datos de contacto|$)",
        oferta["texto"], re.IGNORECASE,
    )
    requisitos_texto = requisitos.group(1).strip() if requisitos else ""

    if len(requisitos_texto) > 450:
        requisitos_texto = requisitos_texto[:447] + "..."

    partes = [
        "🆕 OFERTA DE EMPLEO",
        "",
        "💼 " + oferta["titulo"],
        "📍 Zona: " + str(oferta["zona"]),
        "📅 Publicada desde: " + inicio,
        "⏳ Fecha límite: " + fin,
        "💶 Salario: " + salario_texto,
        aviso_salario,
    ]

    if requisitos_texto:
        partes.extend(["", "📋 Requisitos:", requisitos_texto])

    partes.extend([
        "",
        "🔗 Inscripción y detalles:",
        oferta["url"],
    ])

    return "\n".join(partes)[:3900]


async def enviar_oferta(bot, chat_id, oferta):
    await bot.send_message(
        chat_id=chat_id,
        text=crear_mensaje(oferta),
        disable_web_page_preview=True,
    )


# ============================================================
# BUSQUEDA MANUAL SIN REPETIDOS
# ============================================================

async def ejecutar_busqueda_manual(update, context):
    global BUSQUEDA_EN_CURSO

    if BUSQUEDA_EN_CURSO:
        await update.message.reply_text(
            "Ya hay una búsqueda en marcha. Espera a que termine."
        )
        return

    BUSQUEDA_EN_CURSO = True
    mensaje = await update.message.reply_text(
        "🔎 Buscando ofertas nuevas que encajen contigo..."
    )

    try:
        ofertas = await consultar_ofertas()
        ya_enviadas = obtener_enviadas()

        # FILTRO IMPORTANTE: nunca reenviar ofertas registradas.
        nuevas = [
            oferta for oferta in ofertas
            if oferta["id"] not in ya_enviadas
        ]

        if not nuevas:
            await mensaje.edit_text(
                "No he encontrado ofertas nuevas que cumplan tus filtros.\n\n"
                "No te repetiré las ofertas anteriores. "
                "Volveré a comprobar el portal en la próxima revisión."
            )
            return

        enviadas_correctamente = 0

        for oferta in nuevas[:MAX_OFERTAS_ENVIO]:
            try:
                await enviar_oferta(
                    context.bot, update.effective_chat.id, oferta
                )

                # Guardar el ID solo tras un envío correcto.
                guardar_enviada(oferta["id"])
                enviadas_correctamente += 1

            except Exception:
                logger.exception("No se pudo enviar una oferta.")
            await asyncio.sleep(0.3)

        if enviadas_correctamente == 0:
            await mensaje.edit_text(
                "No he podido enviar las ofertas. "
                "Comprueba los registros de Render."
            )
        else:
            await mensaje.edit_text(
                f"✅ Te he enviado {enviadas_correctamente} "
                "oferta(s) nueva(s). Las enviadas quedan registradas "
                "para no repetirlas."
            )

    except Exception:
        logger.exception("Error en búsqueda manual.")
        await mensaje.edit_text(
            "Ha ocurrido un error al consultar el portal. "
            "Revisa los registros de Render."
        )
    finally:
        BUSQUEDA_EN_CURSO = False


# ============================================================
# COMANDOS TELEGRAM
# ============================================================

async def comando_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    activar_chat(update.effective_chat.id)

    await update.message.reply_text(
        "✅ AVISOS DE EMPLEO ACTIVADOS\n\n"
        "🚚 Reparto, carnet B, furgoneta, carretilla y logística.\n"
        "🏭 Producción sin especialización obligatoria.\n"
        "🏢 Conserjería y control de accesos.\n"
        "📍 San Javier, San Pedro del Pinatar, Los Alcázares, "
        "Torre-Pacheco y Cartagena.\n"
        "📍 Murcia capital: solo con salario bruto publicado "
        "de referencia de al menos 24.000 €/año.\n"
        "💶 Objetivo aproximado: 1.500 € netos al mes.\n"
        "🧹 Limpieza, mecánica y administración agrícola descartadas.\n"
        "🌙 Sin ofertas que indiquen expresamente turno nocturno.\n\n"
        "/buscar - Buscar ofertas nuevas\n"
        "/preferencias - Ver filtros\n"
        "/parar - Desactivar avisos"
    )


async def comando_parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desactivar_chat(update.effective_chat.id)
    await update.message.reply_text(
        "Avisos desactivados. Puedes reactivarlos con /start."
    )


async def comando_buscar(update, context):
    await ejecutar_busqueda_manual(update, context)


async def comando_portales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Fuente actual: Sistema Nacional de Empleo.\n\n"
        + URL_LISTADO
        + "\n\nEsta consulta no utiliza créditos de JOA."
    )


async def comando_preferencias(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "⚙️ TUS FILTROS\n\n"
        "📍 San Javier, San Pedro del Pinatar, Los Alcázares, "
        "Torre-Pacheco y Cartagena.\n"
        "📍 Murcia capital con salario bruto publicado de referencia "
        "de al menos 24.000 €/año.\n\n"
        "💶 Objetivo aproximado: 1.500 € netos al mes.\n"
        "🚚 Reparto, carnet B, furgoneta, carretilla, logística, "
        "almacén y producción.\n"
        "🏢 Conserjería y control de accesos.\n"
        "🧹 Limpieza, mecánica y administración agrícola descartadas.\n"
        "🌙 Turnos nocturnos expresamente anunciados descartados.\n"
        "🔁 Las ofertas enviadas se guardan para evitar repeticiones.\n\n"
        "El portal puede no publicar el sueldo o el horario en todas "
        "las ofertas; compruébalos antes de inscribirte."
    )


async def comando_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start - Activar avisos\n"
        "/buscar - Buscar ofertas nuevas\n"
        "/scan - Buscar ofertas nuevas\n"
        "/ofertas - Buscar ofertas nuevas\n"
        "/portales - Ver fuente\n"
        "/preferencias - Ver filtros\n"
        "/parar - Desactivar avisos\n"
        "/ayuda - Mostrar ayuda"
    )


# ============================================================
# REVISION AUTOMATICA
# ============================================================

async def revisar_nuevas(application):
    chats = suscriptores()

    if not chats:
        logger.info("No hay suscriptores activos.")
        return

    ofertas = await consultar_ofertas()

    if not ofertas:
        logger.info("No hay ofertas válidas en esta revisión.")
        return

    ya_enviadas = obtener_enviadas()
    nuevas = [
        oferta for oferta in ofertas
        if oferta["id"] not in ya_enviadas
    ]

    # En la primera revisión solo enviar unas pocas ofertas.
    if primera_consulta():
        nuevas = nuevas[:MAX_PRIMER_ENVIO]
    else:
        nuevas = nuevas[:MAX_OFERTAS_ENVIO]

    if not nuevas:
        logger.info("No hay ofertas nuevas; no se envía nada.")
        return

    for oferta in nuevas:
        enviado = False

        for chat_id in chats:
            try:
                await enviar_oferta(application.bot, chat_id, oferta)
                enviado = True
            except Exception:
                logger.exception(
                    "No se pudo enviar oferta al chat %s.", chat_id
                )

        if enviado:
            guardar_enviada(oferta["id"])

        await asyncio.sleep(0.5)

    marcar_consulta_inicial()


async def tarea_automatica(application):
    await asyncio.sleep(30)

    while True:
        try:
            logger.info("Iniciando revisión automática.")
            await revisar_nuevas(application)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error en revisión automática.")

        await asyncio.sleep(INTERVALO_BUSQUEDA)


async def al_iniciar(application):
    global TAREA_AUTOMATICA

    await application.bot.delete_webhook(drop_pending_updates=False)

    if TAREA_AUTOMATICA is None or TAREA_AUTOMATICA.done():
        TAREA_AUTOMATICA = asyncio.create_task(
            tarea_automatica(application)
        )

    logger.info("Bot preparado y revisión automática activada.")


# ============================================================
# SERVIDOR WEB DE RENDER
# ============================================================

@web.route("/")
def inicio():
    return "Bot de empleo activo."


@web.route("/health")
def health():
    return {
        "estado": "ok",
        "fuente": "Sistema Nacional de Empleo",
    }


def iniciar_web():
    web.run(
        host="0.0.0.0",
        port=PORT,
        use_reloader=False,
    )


# ============================================================
# ARRANQUE
# ============================================================

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "Falta TELEGRAM_TOKEN en las variables de entorno de Render."
        )

    inicializar_db()

    threading.Thread(
        target=iniciar_web,
        daemon=True,
    ).start()

    aplicacion = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(al_iniciar)
        .build()
    )

    aplicacion.add_handler(CommandHandler("start", comando_start))
    aplicacion.add_handler(CommandHandler("buscar", comando_buscar))
    aplicacion.add_handler(CommandHandler("scan", comando_buscar))
    aplicacion.add_handler(CommandHandler("ofertas", comando_buscar))
    aplicacion.add_handler(CommandHandler("portales", comando_portales))
    aplicacion.add_handler(
        CommandHandler("preferencias", comando_preferencias)
    )
    aplicacion.add_handler(CommandHandler("parar", comando_parar))
    aplicacion.add_handler(CommandHandler("ayuda", comando_ayuda))

    logger.info("Bot de empleo iniciado.")
    logger.info("Fuente: Sistema Nacional de Empleo.")
    logger.info("Sin créditos de JOA ni API de pago.")

    aplicacion.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
