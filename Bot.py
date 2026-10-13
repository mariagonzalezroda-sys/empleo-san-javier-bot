
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
from urllib.parse import urljoin
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
MAX_CANDIDATOS = 60
TIMEOUT_WEB = 25

# Busqueda regional del Sistema Nacional de Empleo.
# No utiliza la API de JOA.
URL_LISTADO = (
    "https://www.sistemanacionalempleo.es/"
    "OfertaDifusionWEB/busquedaOfertas.do?CA=14&modo=inicio"
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
    "transportista", "carretillero", "carretillera",
    "mozo de almacen", "mozo/a de almacen", "almacenero",
    "almacenera", "almacen", "logistica", "expediciones",
    "encargado de almacen", "encargada de almacen",
    "operario", "operaria", "operador de maquinaria",
    "operador/a de maquinaria", "maquinaria agricola",
    "produccion", "fabrica", "conserje", "control de accesos",
    "controlador de accesos", "auxiliar de servicios",
    "recepcionista", "mantenimiento", "reponedor", "reponedora",
    "preparador de pedidos", "preparadora de pedidos",
    "dependiente", "dependienta", "supermercado",
    "limpieza", "limpiador", "limpiadora", "montador",
    "montadora", "mecanico", "mecanica",
    "administrativo de logistica", "administrativa de logistica",
    "auxiliar de almacen", "auxiliar de almacén",
    "operario de almacen", "operario de almacén",
    "peon", "peona", "peon de fabrica", "peon industrial",
    "conductor de camion", "conductor de furgoneta",
    "conductor repartidor", "reparto",
]

FRASES_NOCHE = [
    "turno de noche",
    "turno nocturno",
    "turno fijo de noche",
    "horario nocturno",
    "jornada nocturna",
    "solo noches",
    "trabajo nocturno",
]

# Mantiene una referencia a la tarea automatica.
TAREA_AUTOMATICA = None

# ============================================================
# REGISTROS
# ============================================================

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
        filas = conexion.execute(
            "SELECT id FROM enviadas"
        ).fetchall()
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
        conexion.execute(
            """
            INSERT OR REPLACE INTO ajustes(clave, valor)
            VALUES ('iniciado', 'si')
            """
        )


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

        if tag in ("br", "p", "div", "tr", "td", "li", "h1", "h2", "h3"):
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

        if tag in ("br", "p", "div", "tr", "td", "li", "h1", "h2", "h3"):
            self.textos.append(" ")

    def texto_completo(self):
        return re.sub(
            r"\s+",
            " ",
            unescape(" ".join(self.textos)),
        ).strip()


def normalizar(texto):
    texto = (texto or "").lower()
    traducciones = str.maketrans(
        "áéíóúü",
        "aeiouu",
    )
    return texto.translate(traducciones)


def descargar_pagina(url):
    peticion = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "es-ES,es;q=0.9",
        },
    )

    with urllib.request.urlopen(
        peticion,
        timeout=TIMEOUT_WEB,
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
    titulo_normalizado = normalizar(titulo)
    return any(
        normalizar(puesto) in titulo_normalizado
        for puesto in PUESTOS
    )


def extraer_fechas(texto):
    inicio = re.search(
        r"Fecha de inicio\s*:?\s*(\d{2}/\d{2}/\d{4})",
        texto,
        re.IGNORECASE,
    )
    fin = re.search(
        r"Fecha de fin\s*:?\s*(\d{2}/\d{2}/\d{4})",
        texto,
        re.IGNORECASE,
    )

    fecha_inicio = fecha_es(inicio.group(1)) if inicio else None
    fecha_fin = fecha_es(fin.group(1)) if fin else None

    return fecha_inicio, fecha_fin


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
        coincidencia = re.search(
            patron,
            texto,
            re.IGNORECASE,
        )
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
                cantidades.append(
                    int(re.sub(r"[.\s]", "", cantidad))
                )
            except ValueError:
                pass

    return min(cantidades) if cantidades else None


def comprobar_localidad(localidad, texto_completo):
    lugar = normalizar(localidad)

    for clave, nombre in ZONAS.items():
        if clave in lugar:
            return nombre

    # Murcia capital solo si se publica un salario anual >= 24.000 euros.
    if re.match(r"^murcia(?:\s*\(murcia\))?$", lugar):
        salario = extraer_salario_anual(texto_completo)
        if salario is not None and salario >= 24000:
            return "Murcia capital"

    return None


def motivo_descartada(titulo, texto, inicio, fin):
    if fin is None:
        return "no se pudo verificar la fecha final"

    if fin < date.today():
        return "la fecha límite ya ha pasado"

    localidad = extraer_localidad(texto)
    zona = comprobar_localidad(localidad, texto)

    if not localidad:
        return "la ficha no muestra una localidad reconocible"

    if not zona:
        return "la localidad no coincide con las zonas configuradas"

    texto_normalizado = normalizar(texto)
    titulo_normalizado = normalizar(titulo)

    if "vigilante de seguridad" in titulo_normalizado:
        return "es un puesto de vigilante de seguridad"

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
    motivo = motivo_descartada(titulo_listado, texto, inicio, fin)

    if motivo:
        logger.info(
            "Oferta descartada | %s | Motivo: %s",
            titulo_listado or "(sin título)",
            motivo,
        )
        return None

    localidad = extraer_localidad(texto)
    zona = comprobar_localidad(localidad, texto)

    identificador_match = re.search(
        r"Datos de la oferta número\s*:?\s*(\d+)",
        texto,
        re.IGNORECASE,
    )

    if identificador_match:
        identificador = identificador_match.group(1)
    else:
        identificador = hashlib.sha256(
            url.encode("utf-8")
        ).hexdigest()

    titulo = titulo_listado.strip()

    # Si el enlace no tiene título útil, intentamos identificarlo
    # mediante el texto de la ficha.
    if not titulo or titulo.lower() in ("ver oferta", "más información", "mas informacion"):
        titulo_ficha = re.search(
            r"(?:Puesto de trabajo|Ocupación|Ocupacion|Puesto)\s*:?\s*"
            r"(.{3,100}?)(?=\s+(?:Localidad|Provincia|Fecha de inicio)\b|$)",
            texto,
            re.IGNORECASE,
        )
        if titulo_ficha:
            titulo = titulo_ficha.group(1).strip()

    if not titulo:
        titulo = "Oferta de empleo"

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
# CONSULTA DEL PORTAL OFICIAL
# ============================================================

async def consultar_ofertas():
    logger.info("Consultando el listado regional oficial de empleo de Murcia.")
    logger.info("URL consultada: %s", URL_LISTADO)

    try:
        html = await asyncio.to_thread(descargar_pagina, URL_LISTADO)
    except Exception as error:
        logger.error(
            "No se pudo abrir el listado oficial: %s: %s",
            type(error).__name__,
            str(error)[:250],
        )
        return []

    lector = LectorHTML()
    lector.feed(html)

    enlaces = []
    vistos = set()

    for href, titulo in lector.enlaces:
        if "detalleOferta.do" not in href:
            continue

        url = urljoin(URL_LISTADO, href)

        if url in vistos:
            continue

        vistos.add(url)
        enlaces.append((url, titulo.strip()))

    logger.info(
        "El portal respondió. Enlaces HTML: %s. Fichas de ofertas: %s.",
        len(lector.enlaces),
        len(enlaces),
    )

    if not enlaces:
        logger.warning(
            "No se han encontrado enlaces detalleOferta.do. "
            "El portal podría requerir otra búsqueda o haber cambiado."
        )
        fragmento = normalizar(lector.texto_completo())[:500]
        logger.info("Texto inicial recibido del portal: %s", fragmento)
        return []

    # Se revisan primero las ofertas cuyo título coincide con los puestos
    # buscados. También se inspeccionan algunos enlaces con título genérico,
    # por si el portal no incluye el nombre del puesto en el enlace.
    enlaces.sort(
        key=lambda elemento: 0 if titulo_interesante(elemento[1]) else 1
    )

    candidatos = enlaces[:MAX_CANDIDATOS]
    resultados = []
    validas = 0

    for url, titulo in candidatos:
        try:
            oferta = await asyncio.to_thread(
                extraer_detalle,
                url,
                titulo,
            )

            if oferta:
                # Si el título del enlace es informativo, exigimos que
                # corresponda a los puestos que buscamos.
                if titulo and titulo.lower() not in (
                    "ver oferta", "más información", "mas informacion"
                ) and not titulo_interesante(titulo):
                    logger.info(
                        "Oferta descartada | %s | Motivo: título fuera "
                        "de los puestos buscados.",
                        titulo,
                    )
                    continue

                resultados.append(oferta)
                validas += 1

        except Exception as error:
            logger.warning(
                "No se pudo leer una ficha | %s | Error: %s",
                titulo or "(sin título)",
                type(error).__name__,
            )

        await asyncio.sleep(0.2)

    logger.info(
        "Resumen de búsqueda: fichas revisadas=%s, ofertas válidas=%s.",
        len(candidatos),
        validas,
    )

    # Elimina posibles duplicados del propio portal.
    unicas = {}
    for oferta in resultados:
        unicas[oferta["id"]] = oferta

    resultados = list(unicas.values())
    resultados.sort(
        key=lambda oferta: oferta["inicio"] or date.min,
        reverse=True,
    )

    return resultados


# ============================================================
# MENSAJES DE TELEGRAM
# ============================================================

def crear_mensaje(oferta):
    inicio = (
        oferta["inicio"].strftime("%d/%m/%Y")
        if oferta["inicio"]
        else "No indicada"
    )
    fin = oferta["fin"].strftime("%d/%m/%Y")

    salario = extraer_salario_anual(oferta["texto"])
    salario_texto = (
        f"{salario:,} € brutos/año".replace(",", ".")
        if salario is not None
        else "No publicado"
    )

    requisitos = re.search(
        r"(?:REQUISITOS|REQUERIMIENTOS)\s*:?(.*?)(?:"
        r"CONDICIONES|INSCRIPCION|INSCRIPCIÓN|Datos de contacto|$)",
        oferta["texto"],
        re.IGNORECASE,
    )

    requisitos_texto = ""
    if requisitos:
        requisitos_texto = requisitos.group(1).strip()
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
        "Te enviaré ofertas nuevas cuando las encuentre.\n\n"
        "📍 San Javier, San Pedro del Pinatar, Los Alcázares, "
        "Torre-Pacheco y Cartagena.\n"
        "🚚 Conducción, reparto, carretilla, almacén, logística, "
        "producción, conserjería, mantenimiento y puestos similares.\n"
        "🌙 Se descartan anuncios que indiquen expresamente turnos de noche.\n"
        "💶 Murcia capital solo con salario anual publicado de al menos 24.000 €.\n\n"
        "Usa /buscar para consultar ahora.\n"
        "Usa /parar para cancelar los avisos."
    )


async def comando_parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desactivar_chat(update.effective_chat.id)
    await update.message.reply_text(
        "Avisos desactivados. Puedes reactivarlos con /start."
    )


async def comando_buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = await update.message.reply_text(
        "🔎 Consultando las ofertas oficiales de empleo de Murcia..."
    )

    try:
        ofertas = await consultar_ofertas()

        if not ofertas:
            await mensaje.edit_text(
                "No he encontrado ofertas que cumplan los filtros en esta consulta.\n\n"
                "He registrado en Render cuántas fichas se han revisado "
                "y por qué se han descartado. No significa necesariamente "
                "que no haya empleo disponible."
            )
            return

        await mensaje.edit_text(
            f"✅ He encontrado {len(ofertas)} ofertas que pasan los filtros. "
            "Te envío los resultados."
        )

        for oferta in ofertas[:MAX_OFERTAS_ENVIO]:
            await enviar_oferta(context.bot, update.effective_chat.id, oferta)
            await asyncio.sleep(0.3)

    except Exception:
        logger.exception("Error durante la búsqueda manual")
        await mensaje.edit_text(
            "Ha ocurrido un error al consultar las ofertas. "
            "Revisa los registros de Render."
        )


async def comando_portales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 FUENTE ACTUAL\n\n"
        "Sistema Nacional de Empleo — ofertas de la Región de Murcia:\n"
        + URL_LISTADO
        + "\n\nEsta consulta no utiliza créditos de JOA."
    )


async def comando_preferencias(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "⚙️ FILTROS ACTUALES\n\n"
        "📍 San Javier\n"
        "📍 San Pedro del Pinatar\n"
        "📍 Los Alcázares\n"
        "📍 Torre-Pacheco\n"
        "📍 Cartagena\n"
        "📍 Murcia capital: salario anual publicado de al menos 24.000 €\n\n"
        "Puestos: conducción, reparto, carretilla, almacén, logística, "
        "expediciones, producción, conserjería, control de accesos, "
        "mantenimiento, limpieza y puestos similares.\n\n"
        "Se descartan anuncios que mencionen expresamente turnos nocturnos "
        "y los puestos de vigilante de seguridad."
    )


async def comando_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 COMANDOS\n\n"
        "/start - Activar avisos\n"
        "/buscar - Consultar ofertas ahora\n"
        "/scan - Consultar ofertas ahora\n"
        "/ofertas - Consultar ofertas ahora\n"
        "/portales - Ver la fuente\n"
        "/preferencias - Ver filtros\n"
        "/parar - Desactivar avisos\n"
        "/ayuda - Mostrar ayuda"
    )


# ============================================================
# BUSQUEDA AUTOMATICA
# ============================================================

async def revisar_nuevas(application):
    chats = suscriptores()

    if not chats:
        logger.info(
            "No hay suscriptores activos. El usuario debe enviar /start."
        )
        return

    ofertas = await consultar_ofertas()

    if not ofertas:
        logger.info("La revisión automática no encontró ofertas válidas.")
        return

    ids_vistas = obtener_enviadas()
    consulta_inicial = primera_consulta()

    if consulta_inicial:
        nuevas = ofertas[:MAX_PRIMER_ENVIO]
        logger.info(
            "Primera consulta automática: se enviarán hasta %s ofertas.",
            len(nuevas),
        )
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
                    "No se pudo enviar una oferta al chat %s.",
                    chat_id,
                )

        if enviada_a_alguien:
            enviadas_correctamente.append(oferta["id"])

        await asyncio.sleep(0.5)

    guardar_enviadas(enviadas_correctamente)

    if consulta_inicial:
        # No mandar como nuevas en la próxima revisión las ofertas
        # que ya estaban visibles durante el primer envío.
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

    # Elimina un webhook anterior antes de usar polling.
    # Esto no puede detener otra instancia que use el mismo token.
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

    aplicacion.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
