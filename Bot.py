
import os
import re
import asyncio
import sqlite3
import logging
import threading
import hashlib
import urllib.request
import urllib.error

from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin
from datetime import datetime, date

from flask import Flask
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ============================================================
# CONFIGURACION
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
DATABASE_PATH = "bot_empleo.sqlite3"

INTERVALO_BUSQUEDA = 2 * 60 * 60
MAX_OFERTAS_ENVIO = 8
MAX_PRIMER_ENVIO = 5
TIMEOUT_WEB = 25

URL_LISTADO = (
    "https://www.sistemanacionalempleo.es/"
    "OfertaDifusionWEB/gestionCesta.do?CA=14&accion=volver"
)

ZONAS = [
    "san javier",
    "san pedro del pinatar",
    "los alcazares",
    "alcazares, los",
    "torre-pacheco",
    "torre pacheco",
    "cartagena",
]

PUESTOS = [
    "conductor",
    "conductora",
    "repartidor",
    "repartidora",
    "transportista",
    "carretillero",
    "carretillera",
    "mozo de almacen",
    "mozo/a de almacen",
    "almacenero",
    "almacenera",
    "almacen",
    "logistica",
    "expediciones",
    "encargado de almacen",
    "encargada de almacen",
    "operario",
    "operaria",
    "operador de maquinaria",
    "operador/a de maquinaria",
    "maquinaria agricola",
    "produccion",
    "fabrica",
    "conserje",
    "control de accesos",
    "controlador de accesos",
    "auxiliar de servicios",
    "recepcionista",
    "mantenimiento",
    "reponedor",
    "reponedora",
    "preparador de pedidos",
    "preparadora de pedidos",
    "dependiente",
    "dependienta",
    "supermercado",
    "limpieza",
    "limpiador",
    "limpiadora",
    "montador",
    "montadora",
    "mecanico",
    "mecanica",
    "administrativo de logistica",
    "administrativa de logistica",
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

# ============================================================
# REGISTROS
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger("empleo")

# Evita que los registros HTTP habituales impriman URLs
# de Telegram que contienen el token del bot.
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
            """
            INSERT OR IGNORE INTO enviadas(id, fecha)
            VALUES (?, ?)
            """,
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
# LECTURA HTML
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

        if tag.lower() == "a":
            self.href_actual = atributos.get("href", "")
            self.texto_enlace = []

        if tag.lower() in ("br", "p", "div", "tr", "td", "li"):
            self.textos.append(" ")

    def handle_data(self, data):
        texto = data.strip()

        if texto:
            self.textos.append(texto)

            if self.href_actual is not None:
                self.texto_enlace.append(texto)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.href_actual is not None:
            enlace = self.href_actual.strip()
            titulo = " ".join(self.texto_enlace).strip()

            if enlace and titulo:
                self.enlaces.append((enlace, titulo))

            self.href_actual = None
            self.texto_enlace = []

        if tag.lower() in ("br", "p", "div", "tr", "td", "li"):
            self.textos.append(" ")

    def texto_completo(self):
        return re.sub(
            r"\s+",
            " ",
            unescape(" ".join(self.textos)),
        ).strip()


def normalizar(texto):
    texto = (texto or "").lower()

    for origen, destino in {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ü": "u",
    }.items():
        texto = texto.replace(origen, destino)

    return texto


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
                "El portal respondió con HTTP "
                + str(respuesta.status)
            )

    return contenido.decode("utf-8", errors="replace")


def fecha_es(texto):
    if not texto:
        return None

    try:
        return datetime.strptime(
            texto.strip(),
            "%d/%m/%Y",
        ).date()
    except ValueError:
        return None


# ============================================================
# DETECCION DE OFERTAS
# ============================================================

def titulo_interesante(titulo):
    titulo = normalizar(titulo)

    return any(
        normalizar(palabra) in titulo
        for palabra in PUESTOS
    )


def extraer_fechas(texto):
    inicio = re.search(
        r"Fecha de inicio\s*:\s*(\d{2}/\d{2}/\d{4})",
        texto,
        re.IGNORECASE,
    )

    fin = re.search(
        r"Fecha de fin\s*:\s*(\d{2}/\d{2}/\d{4})",
        texto,
        re.IGNORECASE,
    )

    fecha_inicio = fecha_es(inicio.group(1)) if inicio else None
    fecha_fin = fecha_es(fin.group(1)) if fin else None

    return fecha_inicio, fecha_fin


def extraer_localidad(texto):
    patron = (
        r"Localidad de Ubicación del Puesto\s*:\s*"
        r"(.*?)(?=\s+Datos adicionales|\s+INSCRIPCION|"
        r"\s+INSCRIPCIÓN|\s+Requerimientos|$)"
    )

    coincidencia = re.search(
        patron,
        texto,
        re.IGNORECASE,
    )

    if not coincidencia:
        return ""

    return coincidencia.group(1).strip(" .,-")


def extraer_salario_anual(texto):
    texto = normalizar(texto)

    patrones = [
        r"(\d{2,3}(?:[.\s]\d{3})+|\d{5,6})\s*euros?"
        r".{0,30}(?:anuales|al ano|anual|brutos al ano)",

        r"(\d{2,3}(?:[.\s]\d{3})+|\d{5,6})\s*€"
        r".{0,30}(?:anuales|al ano|anual|brutos al ano)",
    ]

    cantidades = []

    for patron in patrones:
        for coincidencia in re.findall(patron, texto):
            try:
                cantidades.append(
                    int(re.sub(r"[.\s]", "", coincidencia))
                )
            except ValueError:
                continue

    return min(cantidades) if cantidades else None


def comprobar_localidad(localidad, texto_completo):
    lugar = normalizar(localidad)

    equivalencias = {
        "san javier": "San Javier",
        "san pedro del pinatar": "San Pedro del Pinatar",
        "los alcazares": "Los Alcázares",
        "alcazares, los": "Los Alcázares",
        "torre-pacheco": "Torre-Pacheco",
        "torre pacheco": "Torre-Pacheco",
        "cartagena": "Cartagena",
    }

    for clave, nombre in equivalencias.items():
        if clave in lugar:
            return nombre

    # Murcia capital solo se acepta si se identifica un salario
    # anual de al menos 24.000 euros.
    if re.match(r"^murcia(?:\s*\(murcia\))?$", lugar):
        salario = extraer_salario_anual(texto_completo)

        if salario is not None and salario >= 24000:
            return "Murcia capital"

    return None


def extraer_detalle(url, titulo_listado):
    html = descargar_pagina(url)

    lector = LectorHTML()
    lector.feed(html)
    texto = lector.texto_completo()

    inicio, fin = extraer_fechas(texto)

    # Si no podemos verificar la fecha límite, no arriesgamos
    # enviando una vacante posiblemente caducada.
    if fin is None:
        logger.info(
            "Oferta descartada: no se pudo verificar la fecha final: %s",
            titulo_listado,
        )
        return None

    if fin < date.today():
        logger.info(
            "Oferta caducada descartada: %s (fin %s)",
            titulo_listado,
            fin,
        )
        return None

    localidad = extraer_localidad(texto)
    zona = comprobar_localidad(localidad, texto)

    if not zona:
        return None

    texto_normalizado = normalizar(texto)

    if "vigilante de seguridad" in normalizar(titulo_listado):
        return None

    if "tip en vigor" in texto_normalizado:
        return None

    if any(
        frase in texto_normalizado
        for frase in FRASES_NOCHE
    ):
        return None

    identificador_match = re.search(
        r"Datos de la oferta número\s*:\s*(\d+)",
        texto,
        re.IGNORECASE,
    )

    if identificador_match:
        identificador = identificador_match.group(1)
    else:
        identificador = hashlib.sha256(
            url.encode("utf-8")
        ).hexdigest()

    return {
        "id": identificador,
        "titulo": titulo_listado,
        "zona": zona,
        "url": url,
        "inicio": inicio,
        "fin": fin,
        "texto": texto,
    }


async def consultar_ofertas():
    logger.info("Consultando el listado oficial de empleo de Murcia.")

    try:
        html = await asyncio.to_thread(
            descargar_pagina,
            URL_LISTADO,
        )
    except Exception as error:
        logger.error(
            "No se pudo abrir el listado oficial: %s",
            type(error).__name__,
        )
        return []

    lector = LectorHTML()
    lector.feed(html)

    enlaces = []
    vistos = set()

    for href, titulo in lector.enlaces:
        if "detalleOferta.do" not in href:
            continue

        if not titulo_interesante(titulo):
            continue

        url = urljoin(URL_LISTADO, href)

        if url in vistos:
            continue

        vistos.add(url)
        enlaces.append((url, titulo))

    logger.info(
        "El listado respondió. Enlaces encontrados: %s. "
        "Candidatos por título: %s.",
        len(lector.enlaces),
        len(enlaces),
    )

    if not enlaces:
        logger.warning(
            "No se encontraron enlaces de ofertas compatibles. "
            "Puede haber cambiado la estructura del portal."
        )
        return []

    resultados = []

    # Límite para evitar sobrecargar el portal.
    for url, titulo in enlaces[:30]:
        try:
            oferta = await asyncio.to_thread(
                extraer_detalle,
                url,
                titulo,
            )

            if oferta:
                resultados.append(oferta)

        except Exception as error:
            logger.warning(
                "No se pudo leer una ficha (%s): %s",
                titulo,
                type(error).__name__,
            )

        await asyncio.sleep(0.25)

    logger.info(
        "Ofertas válidas después de revisar fechas, zona y requisitos: %s",
        len(resultados),
    )

    resultados.sort(
        key=lambda oferta: oferta["inicio"] or date.min,
        reverse=True,
    )

    return resultados


# ============================================================
# MENSAJES TELEGRAM
# ============================================================

def crear_mensaje(oferta):
    inicio = (
        oferta["inicio"].strftime("%d/%m/%Y")
        if oferta["inicio"]
        else "No indicada"
    )

    fin = oferta["fin"].strftime("%d/%m/%Y")

    texto = oferta["texto"]

    salario = extraer_salario_anual(texto)
    salario_texto = (
        f"{salario:,} € brutos/año".replace(",", ".")
        if salario is not None
        else "No publicado"
    )

    requisitos = re.search(
        r"(?:REQUISITOS|REQUERIMIENTOS)\s*:?(.*?)(?:"
        r"CONDICIONES|INSCRIPCION|INSCRIPCIÓN|Datos de contacto)",
        texto,
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
        partes.extend([
            "",
            "📋 Requisitos:",
            requisitos_texto,
        ])

    partes.extend([
        "",
        "🔗 Ver oferta e instrucciones para inscribirte:",
        oferta["url"],
        "",
        "⚠️ Comprueba que la vacante siga activa antes de presentar tu candidatura.",
    ])

    return "\n".join(partes)[:3900]


async def enviar_oferta(bot, chat_id, oferta):
    await bot.send_message(
        chat_id=chat_id,
        text=crear_mensaje(oferta),
        disable_web_page_preview=True,
    )


async def comando_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    activar_chat(update.effective_chat.id)

    await update.message.reply_text(
        "✅ AVISOS DE EMPLEO ACTIVADOS\n\n"
        "Te enviaré ofertas nuevas cuando las encuentre.\n\n"
        "📍 San Javier, San Pedro del Pinatar, Los Alcázares, "
        "Torre-Pacheco y Cartagena.\n"
        "🚚 Transporte, reparto, carretilla, almacén, logística, "
        "conserjería, accesos, mantenimiento y puestos similares.\n"
        "🌙 Se excluyen anuncios que indiquen expresamente "
        "turnos de noche.\n"
        "💶 Murcia capital solo si el salario anual publicado "
        "es de al menos 24.000 €.\n\n"
        "Usa /buscar para comprobar ofertas ahora.\n"
        "Usa /parar para cancelar los avisos."
    )


async def comando_parar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    desactivar_chat(update.effective_chat.id)

    await update.message.reply_text(
        "Avisos desactivados. Puedes volver a activarlos con /start."
    )


async def comando_buscar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    mensaje = await update.message.reply_text(
        "🔎 Consultando el portal oficial de empleo de Murcia..."
    )

    try:
        ofertas = await consultar_ofertas()

        if not ofertas:
            await mensaje.edit_text(
                "No he encontrado ofertas válidas en la consulta.\n\n"
                "La consulta puede haber respondido sin anuncios "
                "compatibles o el portal puede haber cambiado. "
                "Revisaremos los registros de Render para distinguirlo."
            )
            return

        await mensaje.edit_text(
            f"✅ He encontrado {len(ofertas)} ofertas que pasan los filtros."
        )

        for oferta in ofertas[:MAX_OFERTAS_ENVIO]:
            await enviar_oferta(
                context.bot,
                update.effective_chat.id,
                oferta,
            )

    except Exception:
        logger.exception("Error durante la búsqueda manual")

        await mensaje.edit_text(
            "Ha ocurrido un error al consultar las ofertas. "
            "Revisa los registros de Render."
        )


async def comando_portales(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "📚 FUENTE ACTUAL\n\n"
        "Sistema Nacional de Empleo — ofertas de la Región de Murcia:\n"
        + URL_LISTADO
        + "\n\nLa consulta no utiliza JOA ni consume sus créditos."
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
        "expediciones, conserjería, control de accesos, producción, "
        "mantenimiento, limpieza y puestos similares.\n\n"
        "Se excluyen las ofertas que mencionan expresamente "
        "turnos nocturnos y los puestos de vigilante de seguridad."
    )


async def comando_ayuda(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
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
# ENVIO AUTOMATICO
# ============================================================

async def revisar_nuevas(application):
    chats = suscriptores()

    if not chats:
        logger.info("No hay suscriptores activos.")
        return

    ofertas = await consultar_ofertas()

    if not ofertas:
        logger.info("La revisión automática no ha encontrado ofertas válidas.")
        return

    ids_vistas = obtener_enviadas()

    if primera_consulta():
        nuevas = ofertas[:MAX_PRIMER_ENVIO]
        logger.info(
            "Primera consulta: se enviarán hasta %s ofertas.",
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
                await enviar_oferta(
                    application.bot,
                    chat_id,
                    oferta,
                )
                enviada_a_alguien = True
            except Exception:
                logger.exception(
                    "No se pudo enviar una oferta al chat %s",
                    chat_id,
                )

        if enviada_a_alguien:
            enviadas_correctamente.append(oferta["id"])

        await asyncio.sleep(1)

    guardar_enviadas(enviadas_correctamente)

    if primera_consulta():
        # Evitar inundar el chat con vacantes antiguas del listado inicial.
        guardar_enviadas([oferta["id"] for oferta in ofertas])
        marcar_consulta_inicial()


async def tarea_automatica(application):
    await asyncio.sleep(30)

    while True:
        try:
            logger.info("Iniciando búsqueda automática.")
            await revisar_nuevas(application)
        except Exception:
            logger.exception("Error en la búsqueda automática")

        await asyncio.sleep(INTERVALO_BUSQUEDA)


async def al_iniciar(application):
    # Elimina un webhook antiguo antes de usar polling.
    # Esto no corrige otra instancia que también esté haciendo polling.
    await application.bot.delete_webhook(drop_pending_updates=False)

    application.create_task(
        tarea_automatica(application)
    )


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
# INICIO
# ============================================================

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "Falta TELEGRAM_TOKEN en las variables de entorno."
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

    aplicacion.run_polling(
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
