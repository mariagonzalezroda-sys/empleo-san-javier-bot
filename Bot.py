
import os
import re
import asyncio
import sqlite3
import hashlib
import logging
import threading
import urllib.request
import xml.etree.ElementTree as ET

from html import unescape
from html.parser import HTMLParser
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

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
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot_empleo.sqlite3")

INTERVALO_BUSQUEDA = 2 * 60 * 60
MAX_OFERTAS_POR_ENVIO = 8
MAX_OFERTAS_PRIMER_ENVIO = 5
EDAD_MAXIMA_DIAS = 30

ZONAS_PRIORITARIAS = [
    "san javier",
    "san pedro del pinatar",
    "los alcazares",
    "los alcázares",
    "torre pacheco",
    "torre-pacheco",
    "cartagena",
]

# RSS oficiales del Portal Unico de Empleo.
# No necesitan clave de API ni creditos de JOA.

CANALES_RSS = {
    "Conductores y transporte":
        "https://bnde.sistemanacionalempleo.es/es/portaltrabaja/resources/rss/Conductores_Transporte.xml",

    "Almacenes y reponedores":
        "https://bnde.sistemanacionalempleo.es/es/portaltrabaja/resources/rss/Almacenes_Reponedores.xml",

    "Limpieza y cuidado":
        "https://bnde.sistemanacionalempleo.es/es/portaltrabaja/resources/rss/Limpieza_CuidadoDePersonas.xml",

    "Vigilancia y servicios":
        "https://bnde.sistemanacionalempleo.es/es/portaltrabaja/resources/rss/Vigilancia_Servicios.xml",

    "Administracion":
        "https://bnde.sistemanacionalempleo.es/es/portaltrabaja/resources/rss/Administracion.xml",

    "Comercial y ventas":
        "https://bnde.sistemanacionalempleo.es/es/portaltrabaja/resources/rss/Comercial_Ventas.xml",

    "Industria":
        "https://bnde.sistemanacionalempleo.es/es/portaltrabaja/resources/rss/Fabricas_Industria.xml",

    "Metal y mecanica":
        "https://bnde.sistemanacionalempleo.es/es/portaltrabaja/resources/rss/Metal_Mecanica.xml",
}

PALABRAS_TRABAJO = [
    "conductor",
    "conductora",
    "repartidor",
    "repartidora",
    "transportista",
    "carretillero",
    "carretillera",
    "mozo de almacen",
    "mozo/a de almacen",
    "almacenista",
    "logistica",
    "expediciones",
    "reponedor",
    "reponedora",
    "conserje",
    "control de accesos",
    "controlador de accesos",
    "auxiliar de servicios",
    "recepcionista",
    "mantenimiento",
    "operario",
    "operaria",
    "produccion",
    "limpieza",
    "limpiador",
    "limpiadora",
    "preparador de pedidos",
    "preparadora de pedidos",
    "administrativo de logistica",
    "administrativa de logistica",
    "dependiente",
    "dependienta",
    "supermercado",
    "maquinista",
    "reparto",
]

PALABRAS_NOCHE = [
    "turno de noche",
    "turno nocturno",
    "turno fijo de noche",
    "trabajo nocturno",
    "jornada nocturna",
    "solo noches",
    "solo turno noche",
    "de lunes a domingo en turno de noche",
]

# ============================================================
# REGISTRO Y BASE DE DATOS
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger("bot_empleo")

app_web = Flask(__name__)


def conectar_bd():
    conexion = sqlite3.connect(DATABASE_PATH, timeout=20)
    conexion.execute("PRAGMA busy_timeout=20000")
    return conexion


def inicializar_bd():
    with conectar_bd() as conexion:
        conexion.execute("""
            CREATE TABLE IF NOT EXISTS suscriptores (
                chat_id INTEGER PRIMARY KEY
            )
        """)

        conexion.execute("""
            CREATE TABLE IF NOT EXISTS ofertas_vistas (
                oferta_id TEXT PRIMARY KEY,
                fecha_envio TEXT
            )
        """)

        conexion.execute("""
            CREATE TABLE IF NOT EXISTS configuracion (
                clave TEXT PRIMARY KEY,
                valor TEXT
            )
        """)


def obtener_suscriptores():
    with conectar_bd() as conexion:
        filas = conexion.execute(
            "SELECT chat_id FROM suscriptores"
        ).fetchall()

    return [fila[0] for fila in filas]


def guardar_suscriptor(chat_id):
    with conectar_bd() as conexion:
        conexion.execute(
            "INSERT OR IGNORE INTO suscriptores (chat_id) VALUES (?)",
            (chat_id,),
        )


def borrar_suscriptor(chat_id):
    with conectar_bd() as conexion:
        conexion.execute(
            "DELETE FROM suscriptores WHERE chat_id = ?",
            (chat_id,),
        )


def ofertas_registradas():
    with conectar_bd() as conexion:
        filas = conexion.execute(
            "SELECT oferta_id FROM ofertas_vistas"
        ).fetchall()

    return {fila[0] for fila in filas}


def registrar_ofertas(identificadores):
    if not identificadores:
        return

    fecha = datetime.now(timezone.utc).isoformat()

    with conectar_bd() as conexion:
        conexion.executemany(
            """
            INSERT OR IGNORE INTO ofertas_vistas
            (oferta_id, fecha_envio)
            VALUES (?, ?)
            """,
            [(identificador, fecha) for identificador in identificadores],
        )


def primer_arranque():
    with conectar_bd() as conexion:
        fila = conexion.execute(
            "SELECT valor FROM configuracion WHERE clave = 'inicializado'"
        ).fetchone()

    return fila is None


def marcar_arranque():
    with conectar_bd() as conexion:
        conexion.execute(
            """
            INSERT OR REPLACE INTO configuracion (clave, valor)
            VALUES ('inicializado', 'si')
            """
        )


# ============================================================
# LECTURA DE RSS
# ============================================================

class LimpiadorHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.partes = []

    def handle_data(self, data):
        self.partes.append(data)

    def texto(self):
        return " ".join(self.partes)


def limpiar_html(texto):
    if not texto:
        return ""

    texto = unescape(str(texto))
    limpiador = LimpiadorHTML()

    try:
        limpiador.feed(texto)
        resultado = limpiador.texto()
    except Exception:
        resultado = texto

    return re.sub(r"\s+", " ", resultado).strip()


def valor_elemento(elemento, nombres):
    for hijo in elemento.iter():
        nombre = hijo.tag.split("}")[-1].lower()

        if nombre in nombres and hijo.text:
            return hijo.text.strip()

    return ""


def interpretar_fecha(texto):
    if not texto:
        return None

    try:
        fecha = parsedate_to_datetime(texto)

        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=timezone.utc)

        return fecha.astimezone(timezone.utc)

    except Exception:
        pass

    try:
        fecha = datetime.fromisoformat(
            texto.replace("Z", "+00:00")
        )

        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=timezone.utc)

        return fecha.astimezone(timezone.utc)

    except Exception:
        return None


def leer_un_rss(nombre_canal, url):
    peticion = urllib.request.Request(
        url,
        headers={
            "User-Agent": "BotEmpleoSanJavier/1.0",
            "Accept": "application/rss+xml, application/xml, text/xml",
        },
    )

    with urllib.request.urlopen(peticion, timeout=20) as respuesta:
        contenido = respuesta.read()

    raiz = ET.fromstring(contenido)
    resultados = []

    elementos = [
        elemento for elemento in raiz.iter()
        if elemento.tag.split("}")[-1].lower() in ("item", "entry")
    ]

    ahora = datetime.now(timezone.utc)
    limite = ahora - timedelta(days=EDAD_MAXIMA_DIAS)

    for elemento in elementos:
        titulo = valor_elemento(elemento, {"title"})
        enlace = valor_elemento(elemento, {"link", "guid"})
        descripcion = valor_elemento(
            elemento,
            {"description", "summary", "encoded", "content"},
        )

        fecha_texto = valor_elemento(
            elemento,
            {"pubdate", "published", "updated", "date"},
        )

        # Algunos RSS utilizan link como atributo href.
        if not enlace:
            for hijo in elemento.iter():
                if hijo.tag.split("}")[-1].lower() == "link":
                    enlace = hijo.attrib.get("href", "")
                    if enlace:
                        break

        titulo = limpiar_html(titulo)
        descripcion = limpiar_html(descripcion)
        enlace = unescape(enlace.strip()) if enlace else ""

        if not titulo or not enlace.startswith("http"):
            continue

        fecha = interpretar_fecha(fecha_texto)

        # No enviar anuncios antiguos si el RSS proporciona fecha.
        if fecha and fecha < limite:
            continue

        identificador = hashlib.sha256(
            (enlace or (titulo + descripcion)).encode("utf-8")
        ).hexdigest()

        resultados.append({
            "id": identificador,
            "titulo": titulo,
            "enlace": enlace,
            "descripcion": descripcion,
            "fecha": fecha,
            "canal": nombre_canal,
        })

    logger.info(
        "RSS %s: %s entradas recibidas",
        nombre_canal,
        len(resultados),
    )

    return resultados


async def consultar_rss():
    tareas = [
        asyncio.to_thread(leer_un_rss, nombre, url)
        for nombre, url in CANALES_RSS.items()
    ]

    respuestas = await asyncio.gather(
        *tareas,
        return_exceptions=True,
    )

    ofertas = []
    canales_correctos = 0

    for respuesta in respuestas:
        if isinstance(respuesta, Exception):
            logger.warning("Error leyendo RSS: %s", respuesta)
            continue

        canales_correctos += 1
        ofertas.extend(respuesta)

    # Quitar duplicados entre categorias.
    unicas = {}

    for oferta in ofertas:
        unicas[oferta["id"]] = oferta

    logger.info(
        "Consulta RSS terminada: %s canales correctos, %s ofertas unicas",
        canales_correctos,
        len(unicas),
    )

    return list(unicas.values())


# ============================================================
# FILTRO DE OFERTAS
# ============================================================

def normalizar(texto):
    texto = (texto or "").lower()

    reemplazos = {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ü": "u",
    }

    for original, nuevo in reemplazos.items():
        texto = texto.replace(original, nuevo)

    return texto


def salario_anual_minimo(texto):
    """
    Intenta detectar salarios anuales de 24.000 euros o superiores.
    Si no puede determinarlo, devuelve None.
    """
    texto = normalizar(texto)

    patrones = [
        r"(\d{2,3}(?:[.\s]\d{3})+|\d{5,6})\s*euros?\s*(?:brutos?\s*)?(?:anuales|al ano|\/ano|anual)",
        r"(\d{2,3}(?:[.\s]\d{3})+|\d{5,6})\s*€\s*(?:brutos?\s*)?(?:anuales|al ano|\/ano|anual)",
    ]

    cantidades = []

    for patron in patrones:
        for coincidencia in re.findall(patron, texto):
            numero = re.sub(r"[.\s]", "", coincidencia)

            try:
                cantidades.append(int(numero))
            except ValueError:
                pass

    if not cantidades:
        return None

    return min(cantidades)


def ubicacion_valida(texto):
    texto = normalizar(texto)

    for zona in ZONAS_PRIORITARIAS:
        if normalizar(zona) in texto:
            return zona.title()

    # Murcia capital solo si se publica un salario anual minimo
    # de al menos 24.000 euros.
    menciona_murcia = "murcia" in texto
    menciona_region = "region de murcia" in texto

    if menciona_murcia and not any(
        normalizar(zona) in texto for zona in ZONAS_PRIORITARIAS
    ):
        salario = salario_anual_minimo(texto)

        if salario is not None and salario >= 24000:
            return "Murcia capital"

        return None

    # No aceptar anuncios que solo indiquen la region completa.
    if menciona_region:
        return None

    return None


def oferta_valida(oferta):
    titulo = normalizar(oferta["titulo"])
    descripcion = normalizar(oferta["descripcion"])
    texto = titulo + " " + descripcion

    # Evitar puestos de vigilante de seguridad que suelen requerir TIP.
    # Se mantienen conserjeria, control de accesos y auxiliar de servicios.
    if "vigilante de seguridad" in titulo:
        return False

    if "tip en vigor" in texto and "control de accesos" not in titulo:
        return False

    # Evitar turnos de noche expresamente anunciados.
    if any(palabra in texto for palabra in PALABRAS_NOCHE):
        return False

    # Exigir que el titulo encaje con los trabajos buscados.
    if not any(
        normalizar(palabra) in titulo
        for palabra in PALABRAS_TRABAJO
    ):
        return False

    ubicacion = ubicacion_valida(texto)

    if not ubicacion:
        return False

    oferta["ubicacion_detectada"] = ubicacion
    return True


def ordenar_ofertas(ofertas):
    fecha_antigua = datetime.min.replace(tzinfo=timezone.utc)

    return sorted(
        ofertas,
        key=lambda oferta: oferta["fecha"] or fecha_antigua,
        reverse=True,
    )


def formatear_fecha(fecha):
    if not fecha:
        return "No indicada"

    return fecha.astimezone().strftime("%d/%m/%Y %H:%M")


def crear_mensaje(oferta):
    descripcion = oferta["descripcion"]

    if len(descripcion) > 500:
        descripcion = descripcion[:497].rsplit(" ", 1)[0] + "..."

    partes = [
        "🆕 OFERTA DE EMPLEO",
        "",
        "💼 " + oferta["titulo"],
        "📍 Zona: " + oferta["ubicacion_detectada"],
        "📂 Fuente: SEPE / Portal Único de Empleo",
        "📅 Publicada: " + formatear_fecha(oferta["fecha"]),
    ]

    if descripcion:
        partes.extend(["", "📝 " + descripcion])

    partes.extend([
        "",
        "🔗 Inscripción o información:",
        oferta["enlace"],
        "",
        "⚠️ Comprueba que la oferta siga activa y revisa el horario y las condiciones.",
    ])

    return "\n".join(partes)[:3900]


# ============================================================
# BUSQUEDA Y ENVIO
# ============================================================

async def buscar_ofertas_validas():
    ofertas = await consultar_rss()

    validas = [
        oferta for oferta in ofertas
        if oferta_valida(oferta)
    ]

    return ordenar_ofertas(validas)


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
    chat_id = update.effective_chat.id
    guardar_suscriptor(chat_id)

    await update.message.reply_text(
        "✅ BOT DE EMPLEO ACTIVADO\n\n"
        "Te enviaré automáticamente ofertas nuevas que encajen "
        "con tus preferencias y tu zona.\n\n"
        "📍 San Javier, San Pedro del Pinatar, Los Alcázares, "
        "Torre-Pacheco y Cartagena.\n"
        "🚚 Conductores, repartidores, carretilleros, almacén, "
        "logística, conserjería, accesos y otros puestos similares.\n\n"
        "🌙 Intentaré excluir las ofertas que indiquen turno de noche.\n"
        "💶 Murcia capital solo si se identifica un salario anual "
        "de al menos 24.000 €.\n\n"
        "Usa /buscar para comprobar las ofertas disponibles ahora.\n"
        "Usa /parar si no quieres recibir más avisos."
    )


async def comando_parar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    borrar_suscriptor(update.effective_chat.id)

    await update.message.reply_text(
        "Has desactivado los avisos automáticos. "
        "Puedes volver a activarlos con /start."
    )


async def comando_buscar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    mensaje = await update.message.reply_text(
        "🔎 Consultando los canales RSS gratuitos del SEPE..."
    )

    try:
        ofertas = await buscar_ofertas_validas()

        if not ofertas:
            await mensaje.edit_text(
                "No he encontrado ofertas que cumplan todos los filtros "
                "en los canales consultados.\n\n"
                "Esto no significa que no existan ofertas en la zona. "
                "Probaremos más fuentes gratuitas si hace falta."
            )
            return

        await mensaje.edit_text(
            "✅ He encontrado " + str(len(ofertas)) +
            " ofertas que coinciden con los filtros."
        )

        for oferta in ofertas[:MAX_OFERTAS_POR_ENVIO]:
            await enviar_oferta(
                context.bot,
                update.effective_chat.id,
                oferta,
            )

    except Exception:
        logger.exception("Error en la busqueda manual")

        await mensaje.edit_text(
            "No he podido completar la consulta. "
            "Revisa los registros de Render para ver qué canal ha fallado."
        )


async def comando_portales(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "📚 FUENTES GRATUITAS ACTIVAS\n\n"
        "Los canales RSS oficiales del Portal Único de Empleo del SEPE "
        "que consulta este bot son:\n\n"
        + "\n".join("• " + nombre for nombre in CANALES_RSS)
        + "\n\nNo se utiliza JOA ni se consumen sus créditos."
    )


async def comando_preferencias(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "⚙️ TUS FILTROS ACTUALES\n\n"
        "📍 San Javier\n"
        "📍 San Pedro del Pinatar\n"
        "📍 Los Alcázares\n"
        "📍 Torre-Pacheco\n"
        "📍 Cartagena\n"
        "📍 Murcia capital solo con salario anual mínimo "
        "detectado de 24.000 €\n\n"
        "🚚 Conducción, reparto, almacén, carretilla, logística, "
        "conserjería, control de accesos, limpieza, mantenimiento, "
        "producción y puestos relacionados.\n\n"
        "🌙 Se descartan anuncios que mencionen expresamente "
        "turnos de noche.\n\n"
        "Nota: estos filtros dependen de la información publicada "
        "en cada anuncio."
    )


async def comando_ayuda(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🤖 COMANDOS DEL BOT\n\n"
        "/start - Activar los avisos automáticos\n"
        "/buscar - Buscar ofertas ahora\n"
        "/ofertas - Igual que buscar\n"
        "/scan - Igual que buscar\n"
        "/portales - Ver las fuentes consultadas\n"
        "/preferencias - Ver tus filtros\n"
        "/parar - Desactivar los avisos\n"
        "/ayuda - Mostrar esta ayuda"
    )


# ============================================================
# BUSQUEDA AUTOMATICA
# ============================================================

async def revisar_nuevas_ofertas(application):
    suscriptores = obtener_suscriptores()

    if not suscriptores:
        logger.info("No hay suscriptores; se omite el envio automatico.")
        return

    ofertas = await buscar_ofertas_validas()

    if not ofertas:
        logger.info("No se han encontrado ofertas que pasen los filtros.")
        return

    vistas = ofertas_registradas()
    primera = primer_arranque()

    if primera:
        # Primer arranque: enviar solo unas pocas ofertas para evitar
        # inundar Telegram con anuncios antiguos del canal.
        seleccion = ofertas[:MAX_OFERTAS_PRIMER_ENVIO]

        logger.info(
            "Primera consulta: se enviaran hasta %s ofertas",
            len(seleccion),
        )

        for chat_id in suscriptores:
            for oferta in seleccion:
                try:
                    await enviar_oferta(application.bot, chat_id, oferta)
                except Exception:
                    logger.exception(
                        "No se pudo enviar una oferta a %s", chat_id
                    )

        # Registrar tambien las demas ofertas encontradas en esta primera
        # consulta para no enviarlas una por una en las siguientes rondas.
        registrar_ofertas([oferta["id"] for oferta in ofertas])
        marcar_arranque()
        return

    nuevas = [
        oferta for oferta in ofertas
        if oferta["id"] not in vistas
    ]

    if not nuevas:
        logger.info("No hay ofertas nuevas.")
        return

    nuevas = nuevas[:MAX_OFERTAS_POR_ENVIO]

    for chat_id in suscriptores:
        for oferta in nuevas:
            try:
                await enviar_oferta(application.bot, chat_id, oferta)
                await asyncio.sleep(1)
            except Exception:
                logger.exception(
                    "No se pudo enviar una oferta a %s", chat_id
                )

    # Registrar los avisos para evitar duplicados en la siguiente consulta.
    registrar_ofertas([oferta["id"] for oferta in nuevas])


async def tarea_automatica(application):
    await asyncio.sleep(20)

    while True:
        try:
            logger.info("Iniciando comprobacion automatica de ofertas.")
            await revisar_nuevas_ofertas(application)

        except Exception:
            logger.exception("Error en la comprobacion automatica")

        await asyncio.sleep(INTERVALO_BUSQUEDA)


async def preparar_aplicacion(application):
    application.create_task(tarea_automatica(application))


# ============================================================
# SERVIDOR PARA RENDER
# ============================================================

@app_web.route("/")
def inicio():
    return "Bot de empleo funcionando con RSS gratuitos del SEPE."


@app_web.route("/health")
def salud():
    return {
        "estado": "ok",
        "fuente": "RSS SEPE",
        "creditos_joa": False,
    }


def iniciar_servidor_web():
    app_web.run(
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
            "Falta TELEGRAM_TOKEN en las variables de entorno de Render."
        )

    inicializar_bd()

    threading.Thread(
        target=iniciar_servidor_web,
        daemon=True,
    ).start()

    aplicacion = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(preparar_aplicacion)
        .build()
    )

    aplicacion.add_handler(CommandHandler("start", comando_start))
    aplicacion.add_handler(CommandHandler("buscar", comando_buscar))
    aplicacion.add_handler(CommandHandler("ofertas", comando_buscar))
    aplicacion.add_handler(CommandHandler("scan", comando_buscar))
    aplicacion.add_handler(CommandHandler("portales", comando_portales))
    aplicacion.add_handler(CommandHandler("preferencias", comando_preferencias))
    aplicacion.add_handler(CommandHandler("parar", comando_parar))
    aplicacion.add_handler(CommandHandler("ayuda", comando_ayuda))

    logger.info("Bot de empleo iniciado.")
    logger.info("Fuente de ofertas: RSS oficiales del SEPE.")
    logger.info("Creditos de JOA: no utilizados.")

    aplicacion.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
