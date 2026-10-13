
import os
import re
import asyncio
import sqlite3
import logging
import threading
import hashlib
import urllib.request

from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, parse_qs
from datetime import datetime, date

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

# 22.000 € brutos/año es solo una referencia aproximada.
# El neto real depende de las circunstancias personales y las pagas.
SALARIO_BRUTO_REFERENCIA = 22000

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

# Puestos compatibles con la experiencia y los permisos indicados.
# No se incluyen limpieza, soldadura ni especialidades técnicas.
PUESTOS = [
    "conductor", "conductora",
    "repartidor", "repartidora",
    "transportista",
    "conductor de furgoneta",
    "conductor repartidor",
    "conductor de camion",
    "reparto",
    "carretillero", "carretillera",
    "mozo de almacen", "mozo/a de almacen",
    "auxiliar de almacen", "auxiliar de almacén",
    "operario de almacen", "operario de almacén",
    "almacenero", "almacenera",
    "almacen", "logistica", "expediciones",
    "preparador de pedidos", "preparadora de pedidos",
    "encargado de almacen", "encargada de almacen",
    "operario", "operaria",
    "operario de produccion", "operaria de produccion",
    "operador de maquinaria", "operadora de maquinaria",
    "produccion", "fabrica", "peon industrial",
    "peon de fabrica", "peon", "peona",
    "conserje", "control de accesos",
    "controlador de accesos", "auxiliar de servicios",
    "recepcionista",
    "reponedor", "reponedora",
    "dependiente", "dependienta",
    "supermercado",
    "mantenimiento general",
    "mecanico", "mecanica",
    "administrativo de logistica",
    "administrativa de logistica",
    "montador", "montadora",
]

# Estos términos se comprueban sobre el texto de la oferta.
FRASES_NOCHE = [
    "turno de noche",
    "turno nocturno",
    "turno fijo de noche",
    "horario nocturno",
    "jornada nocturna",
    "solo noches",
    "trabajo nocturno",
    "turno de noche obligatorio",
]

TAREA_AUTOMATICA = None
BUSQUEDA_EN_CURSO = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("empleo")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

web = Flask(__name__)


# ============================================================
# BASE DE DATOS
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


def guardar_enviadas(ids):
    if not ids:
        return
    fecha = datetime.now().isoformat()
    with db() as conexion:
        conexion.executemany(
            "INSERT OR IGNORE INTO enviadas(id, fecha) VALUES (?, ?)",
            [(identificador, fecha) for identificador in ids],
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

        if tag in ("br", "p", "div", "tr", "td", "li",
                   "h1", "h2", "h3"):
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
            enlace = self.href_actual.strip()
            titulo = " ".join(self.texto_enlace).strip()
            if enlace:
                self.enlaces.append((enlace, titulo))
            self.href_actual = None
            self.texto_enlace = []

        if tag in ("br", "p", "div", "tr", "td", "li",
                   "h1", "h2", "h3"):
            self.textos.append(" ")

    def texto_completo(self):
        return re.sub(
            r"\s+", " ", unescape(" ".join(self.textos))
        ).strip()


def normalizar(texto):
    texto = (texto or "").lower()
    return texto.translate(str.maketrans("áéíóúü", "aeiouu"))


def descargar_pagina(url):
    peticion = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9",
        },
    )
    with urllib.request.urlopen(
        peticion, timeout=TIMEOUT_WEB
    ) as respuesta:
        contenido = respuesta.read()
        if respuesta.status != 200:
            raise RuntimeError(
                "El portal respondió con HTTP " + str(respuesta.status)
            )
        charset = respuesta.headers.get_content_charset() or "utf-8"
    return contenido.decode(charset, errors="replace")


def fecha_es(texto):
    if not texto:
        return None
    for formato in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(texto.strip(), formato).date()
        except ValueError:
            pass
    return None


# ============================================================
# FILTROS DE OFERTAS
# ============================================================

def titulo_interesante(titulo):
    t = normalizar(titulo)

    # Exclusiones prioritarias para evitar falsos positivos.
    prohibidos = [
        "limpieza", "limpiador", "limpiadora",
        "soldador", "soldadora", "fontanero",
        "fontanera", "electricista", "fisioterapeuta",
        "ingeniero", "ingeniera", "tecnico instalador",
        "tecnica instaladora", "vigilante de seguridad",
    ]
    if any(p in t for p in prohibidos):
        return False

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


def comprobar_localidad(localidad, texto_completo):
    lugar = normalizar(localidad)

    for clave, nombre in ZONAS.items():
        if clave in lugar:
            return nombre

    # Murcia capital: se admite con salario publicado que alcance
    # una referencia bruta orientativa; no garantiza 1.500 € netos.
    if re.match(r"^murcia(?:\s*\(murcia\))?$", lugar):
        salario = extraer_salario_anual(texto_completo)
        if salario is not None and salario >= SALARIO_BRUTO_REFERENCIA:
            return "Murcia capital"

    return None


def motivo_descartada(titulo, texto, inicio, fin):
    if fin is None:
        return "no se pudo verificar la fecha final"

    if fin < date.today():
        return "la fecha límite ya ha pasado"

    localidad = extraer_localidad(texto)
    if not localidad:
        return "la ficha no muestra una localidad reconocible"

    if not comprobar_localidad(localidad, texto):
        return "la localidad no coincide con las zonas configuradas"

    texto_normalizado = normalizar(texto)
    titulo_normalizado = normalizar(titulo)

    if "vigilante de seguridad" in titulo_normalizado:
        return "puesto de vigilante de seguridad"
    if "tip en vigor" in texto_normalizado:
        return "exige TIP en vigor"
    if any(frase in texto_normalizado for frase in FRASES_NOCHE):
        return "indica expresamente turno nocturno"

    return None


def extraer_detalle(url, titulo_listado):
    html = descargar_pagina(url)
    lector = LectorHTML()
    lector.feed(html)
    texto = lector.texto_completo()

    inicio, fin = extraer_fechas(texto)
    titulo = titulo_listado.strip()

    if not titulo or normalizar(titulo) in (
        "ver oferta", "mas informacion"
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

    if not titulo_interesante(titulo):
        logger.info("Oferta descartada | %s | puesto no compatible", titulo)
        return None

    motivo = motivo_descartada(titulo, texto, inicio, fin)
    if motivo:
        logger.info("Oferta descartada | %s | %s", titulo, motivo)
        return None

    localidad = extraer_localidad(texto)
    zona = comprobar_localidad(localidad, texto)

    identificador_match = re.search(
        r"Datos de la oferta número\s*:?\s*(\d+)",
        texto, re.IGNORECASE,
    )
    identificador = (
        identificador_match.group(1)
        if identificador_match
        else hashlib.sha256(url.encode("utf-8")).hexdigest()
    )

    return {
        "id": identificador,
        "titulo": titulo,
        "zona": zona,
        "url": url,
        "inicio": inicio,
        "fin": fin,
        "texto": texto,
    }


# ============================================================
# BUSQUEDA Y PAGINACION
# ============================================================

def parece_pagina_de_resultados(href, url_actual):
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

    query = normalizar(parsed.query)
    return any(
        palabra in query
        for palabra in ("pagina=", "page=", "numpagina=", "paginacion=")
    )


async def recoger_enlaces_listado():
    pendientes = [URL_LISTADO]
    paginas_visitadas = set()
    enlaces_ofertas = {}

    while pendientes and len(paginas_visitadas) < MAX_PAGINAS:
        url_pagina = pendientes.pop(0)
        if url_pagina in paginas_visitadas:
            continue

        paginas_visitadas.add(url_pagina)

        try:
            html = await asyncio.to_thread(descargar_pagina, url_pagina)
        except Exception as error:
            logger.warning(
                "No se pudo consultar el listado: %s (%s)",
                type(error).__name__, url_pagina,
            )
            continue

        lector = LectorHTML()
        lector.feed(html)

        for href, titulo in lector.enlaces:
            url = urljoin(url_pagina, href)

            if "detalleOferta.do" in href:
                enlaces_ofertas[url] = titulo.strip()
                continue

            if parece_pagina_de_resultados(href, url_pagina):
                parametros = parse_qs(urlparse(url).query)
                provincia = parametros.get("provincia", ["30"])[0]
                if provincia not in ("30", ""):
                    continue
                if url not in paginas_visitadas and url not in pendientes:
                    pendientes.append(url)

        logger.info(
            "Página revisada: %s; fichas acumuladas: %s",
            len(paginas_visitadas), len(enlaces_ofertas),
        )
        await asyncio.sleep(0.2)

    logger.info(
        "Paginación finalizada: páginas=%s, fichas=%s",
        len(paginas_visitadas), len(enlaces_ofertas),
    )
    return list(enlaces_ofertas.items())


async def consultar_ofertas():
    logger.info("Consultando ofertas del Sistema Nacional de Empleo.")

    try:
        enlaces = await recoger_enlaces_listado()
    except Exception:
        logger.exception("Error recopilando las páginas de resultados.")
        return []

    if not enlaces:
        logger.warning("No se encontraron fichas en el listado.")
        return []

    enlaces.sort(
        key=lambda elemento: (
            0 if titulo_interesante(elemento[1]) else 1
        )
    )

    candidatos = enlaces[:MAX_CANDIDATOS]
    resultados = []

    for url, titulo in candidatos:
        try:
            oferta = await asyncio.to_thread(
                extraer_detalle, url, titulo
            )
            if oferta:
                resultados.append(oferta)
        except Exception as error:
            logger.warning(
                "No se pudo leer la ficha %s: %s",
                titulo or "(sin título)", type(error).__name__,
            )
        await asyncio.sleep(0.2)

    unicas = {oferta["id"]: oferta for oferta in resultados}
    resultados = list(unicas.values())
    resultados.sort(
        key=lambda oferta: oferta["inicio"] or date.min,
        reverse=True,
    )

    logger.info(
        "Resumen: fichas revisadas=%s; ofertas válidas=%s.",
        len(candidatos), len(resultados),
    )
    return resultados


# ============================================================
# MENSAJES DE TELEGRAM
# ============================================================

def crear_mensaje(oferta):
    inicio = (
        oferta["inicio"].strftime("%d/%m/%Y")
        if oferta["inicio"] else "No indicada"
    )
    fin = oferta["fin"].strftime("%d/%m/%Y")

    salario = extraer_salario_anual(oferta["texto"])
    if salario is not None:
        salario_texto = (
            f"{salario:,} € brutos/año".replace(",", ".")
        )
        if salario < SALARIO_BRUTO_REFERENCIA:
            aviso_salario = (
                "⚠️ El bruto publicado puede quedarse por debajo "
                "del objetivo de 1.500 € netos/mes."
            )
        else:
            aviso_salario = (
                "ℹ️ Confirma el neto real y el número de pagas."
            )
    else:
        salario_texto = "No publicado"
        aviso_salario = (
            "⚠️ No se puede comprobar si llega a 1.500 € netos/mes. "
            "Consulta el salario antes de aceptar."
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
        "📍 Zona: " + oferta["zona"],
        "📅 Publicada desde: " + inicio,
        "⏳ Fecha límite: " + fin,
        "💶 Salario: " + salario_texto,
        aviso_salario,
    ]

    if requisitos_texto:
        partes.extend(["", "📋 Requisitos:", requisitos_texto])

    partes.extend([
        "",
        "🔗 Ver oferta e instrucciones para inscribirte:",
        oferta["url"],
        "",
        "⚠️ Comprueba que la vacante siga activa antes de inscribirte.",
    ])

    return "\n".join(partes)[:3900]


async def enviar_oferta(bot, chat_id, oferta):
    await bot.send_message(
        chat_id=chat_id,
        text=crear_mensaje(oferta),
        disable_web_page_preview=True,
    )


# ============================================================
# COMANDOS
# ============================================================

async def comando_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    activar_chat(update.effective_chat.id)
    await update.message.reply_text(
        "✅ AVISOS DE EMPLEO ACTIVADOS\n\n"
        "Te avisaré cuando encuentre ofertas nuevas.\n\n"
        "📍 San Javier, San Pedro del Pinatar, Los Alcázares, "
        "Torre-Pacheco y Cartagena.\n"
        "📍 Murcia capital: se requiere salario bruto publicado "
        "de referencia de al menos 22.000 €/año.\n"
        "🚚 Reparto, carnet B, furgoneta, carretilla, almacén, "
        "logística y producción.\n"
        "🏢 Conserjería y control de accesos.\n"
        "🧹 Limpieza descartada.\n"
        "🌙 Sin ofertas que indiquen expresamente turno nocturno.\n"
        "💶 Objetivo: aproximadamente 1.500 € netos al mes; "
        "si el salario no aparece, te lo indicaré.\n\n"
        "Usa /buscar para consultar ahora.\n"
        "Usa /parar para cancelar los avisos."
    )


async def comando_parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desactivar_chat(update.effective_chat.id)
    await update.message.reply_text(
        "Avisos desactivados. Puedes reactivarlos con /start."
    )


async def comando_buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BUSQUEDA_EN_CURSO

    if BUSQUEDA_EN_CURSO:
        await update.message.reply_text(
            "Ya hay una búsqueda en marcha. Espera a que termine."
        )
        return

    BUSQUEDA_EN_CURSO = True
    mensaje = await update.message.reply_text(
        "🔎 Buscando ofertas que encajen contigo. Puede tardar un poco..."
    )

    try:
        ofertas = await consultar_ofertas()

        if not ofertas:
            await mensaje.edit_text(
                "No he encontrado ofertas que cumplan los filtros "
                "en esta consulta. Puedes intentarlo más tarde."
            )
            return

        await mensaje.edit_text(
            f"✅ He encontrado {len(ofertas)} ofertas compatibles. "
            "Te envío las primeras."
        )

        for oferta in ofertas[:MAX_OFERTAS_ENVIO]:
            await enviar_oferta(
                context.bot, update.effective_chat.id, oferta
            )
            await asyncio.sleep(0.3)

    except Exception:
        logger.exception("Error durante la búsqueda manual")
        await mensaje.edit_text(
            "Ha ocurrido un error al consultar las ofertas. "
            "Revisa los registros de Render."
        )
    finally:
        BUSQUEDA_EN_CURSO = False


async def comando_portales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 FUENTE ACTUAL\n\n"
        "Sistema Nacional de Empleo — ofertas de Murcia:\n"
        + URL_LISTADO
        + "\n\nEsta consulta no utiliza créditos de JOA."
    )


async def comando_preferencias(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "⚙️ TUS FILTROS\n\n"
        "📍 San Javier\n"
        "📍 San Pedro del Pinatar\n"
        "📍 Los Alcázares\n"
        "📍 Torre-Pacheco\n"
        "📍 Cartagena\n"
        "📍 Murcia capital: referencia bruta publicada de 22.000 €/año.\n\n"
        "💶 Objetivo aproximado: 1.500 € netos al mes.\n"
        "🚚 Reparto, conducción B, furgoneta, carretilla, "
        "almacén, logística y producción.\n"
        "🏢 Conserjería y control de accesos.\n"
        "🧹 Limpieza descartada.\n"
        "🌙 Turnos nocturnos anunciados descartados.\n"
        "🔧 Sin especialidades técnicas excluidas.\n\n"
        "Si una oferta no publica salario, se indicará para "
        "que puedas comprobarlo antes de inscribirte."
    )


async def comando_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 COMANDOS\n\n"
        "/start - Activar avisos\n"
        "/buscar - Buscar ofertas ahora\n"
        "/scan - Buscar ofertas ahora\n"
        "/ofertas - Buscar ofertas ahora\n"
        "/portales - Ver la fuente\n"
        "/preferencias - Ver tus filtros\n"
        "/parar - Desactivar avisos\n"
        "/ayuda - Mostrar ayuda"
    )


# ============================================================
# BUSQUEDA AUTOMATICA
# ============================================================

async def revisar_nuevas(application):
    chats = suscriptores()

    if not chats:
        logger.info("No hay suscriptores activos. Usa /start.")
        return

    ofertas = await consultar_ofertas()
    if not ofertas:
        logger.info("La revisión automática no encontró ofertas válidas.")
        return

    ids_vistas = obtener_enviadas()
    consulta_inicial = primera_consulta()

    if consulta_inicial:
        nuevas = ofertas[:MAX_PRIMER_ENVIO]
    else:
        nuevas = [
            oferta for oferta in ofertas
            if oferta["id"] not in ids_vistas
        ][:MAX_OFERTAS_ENVIO]

    if not nuevas:
        logger.info("No hay ofertas nuevas.")
        return

    enviadas_correctamente = []

    for oferta in nuevas:
        enviada_a_alguien = False

        for chat_id in chats:
            try:
                await enviar_oferta(application.bot, chat_id, oferta)
                enviada_a_alguien = True
            except Exception:
                logger.exception(
                    "No se pudo enviar una oferta al chat %s.", chat_id
                )

        if enviada_a_alguien:
            enviadas_correctamente.append(oferta["id"])

        await asyncio.sleep(0.5)

    guardar_enviadas(enviadas_correctamente)

    if consulta_inicial:
        guardar_enviadas([oferta["id"] for oferta in ofertas])
        marcar_consulta_inicial()


async def tarea_automatica(application):
    await asyncio.sleep(30)

    while True:
        try:
            logger.info("Iniciando búsqueda automática.")
            await revisar_nuevas(application)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error en la búsqueda automática.")

        await asyncio.sleep(INTERVALO_BUSQUEDA)


async def al_iniciar(application):
    global TAREA_AUTOMATICA

    # Evita mantener un webhook configurado mientras se usa polling.
    await application.bot.delete_webhook(drop_pending_updates=False)

    if TAREA_AUTOMATICA is None or TAREA_AUTOMATICA.done():
        TAREA_AUTOMATICA = asyncio.create_task(
            tarea_automatica(application),
            name="busqueda-automatica-empleo",
        )

    logger.info("Tarea automática preparada.")


# ============================================================
# SERVIDOR RENDER
# ============================================================

@web.route("/")
def inicio():
    return "Bot de empleo activo."


@web.route("/health")
def health():
    return {
        "estado": "ok",
        "fuente": "Sistema Nacional de Empleo",
        "usa_creditos_joa": False,
    }


def iniciar_web():
    web.run(
        host="0.0.0.0",
        port=PORT,
        use_reloader=False,
    )


# ============================================================
# INICIO DEL BOT
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
    aplicacion.add_handler(CommandHandler("preferencias", comando_preferencias))
    aplicacion.add_handler(CommandHandler("parar", comando_parar))
    aplicacion.add_handler(CommandHandler("ayuda", comando_ayuda))

    logger.info("Bot iniciado. Fuente: Sistema Nacional de Empleo.")
    logger.info("No se utilizan créditos de JOA.")
    logger.info("Búsqueda automática cada %s segundos.", INTERVALO_BUSQUEDA)

    aplicacion.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
