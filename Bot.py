
import os
import asyncio
import logging
import threading
import json
import unicodedata
from datetime import datetime
from urllib.parse import urlencode, quote_plus
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

from flask import Flask, jsonify
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ============================================================
# CONFIGURACIÓN
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
JOA_API_KEY = os.getenv("JOA_API_KEY")
PORT = int(os.getenv("PORT", "10000"))

API_URL = "https://api.jobopportunitiesapi.org/v1/jobs"

ZONAS = [
    "San Javier",
    "San Pedro del Pinatar",
    "Los Alcázares",
    "Torre-Pacheco",
    "Cartagena",
    "Murcia",
]

# Una consulta de 25 registros al día ayuda a controlar
# el límite mensual del plan gratuito.
LIMITE_API = 25
INTERVALO_ALERTAS = 24 * 60 * 60
MAX_OFERTAS_TELEGRAM = 8

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

app_web = Flask(__name__)

# Los usuarios quedan registrados mientras el proceso
# del bot permanezca activo.
suscriptores = set()
ofertas_vistas = set()


# ============================================================
# SERVIDOR WEB PARA RENDER
# ============================================================

@app_web.route("/")
def inicio_web():
    return jsonify({
        "bot": "Bot de empleo San Javier",
        "estado": "funcionando",
        "api_configurada": bool(JOA_API_KEY),
    })


@app_web.route("/health")
def estado_web():
    return jsonify({"estado": "ok"}), 200


def iniciar_servidor_web():
    app_web.run(
        host="0.0.0.0",
        port=PORT,
        use_reloader=False,
    )


# ============================================================
# UTILIDADES
# ============================================================

def normalizar(texto):
    texto = str(texto or "").lower().strip()
    texto = unicodedata.normalize("NFD", texto)
    return "".join(
        caracter
        for caracter in texto
        if unicodedata.category(caracter) != "Mn"
    )


def crear_enlace_google(busqueda):
    return "https://www.google.com/search?q=" + quote_plus(busqueda)


def crear_enlace_indeed(busqueda):
    return (
        "https://es.indeed.com/jobs?q="
        + quote_plus(busqueda)
        + "&l="
        + quote_plus("San Javier, Murcia")
    )


def crear_enlace_infojobs(busqueda):
    return crear_enlace_google(
        "site:infojobs.net "
        + busqueda
        + " San Javier Murcia"
    )


def es_zona_cercana(oferta):
    ubicacion = normalizar(
        " ".join([
            str(oferta.get("location") or ""),
            str(oferta.get("city") or ""),
            str(oferta.get("state") or ""),
        ])
    )

    return any(
        normalizar(zona) in ubicacion
        for zona in ZONAS
    )


def es_trabajo_interesante(oferta):
    titulo = normalizar(oferta.get("title", ""))

    palabras = [
        "conductor",
        "repartidor",
        "carretillero",
        "carretilla",
        "transportista",
        "conserje",
        "control de acceso",
        "controlador de acceso",
        "mozo de almacen",
        "almacen",
        "logistica",
        "operario",
        "mantenimiento",
        "reponedor",
        "supermercado",
        "vigilante",
        "recepcionista",
        "auxiliar de servicios",
        "limpieza",
        "carnet b",
        "chofer",
        "conductor de furgoneta",
    ]

    if not any(palabra in titulo for palabra in palabras):
        return False

    # Se descartan ofertas cuyo título indique claramente
    # trabajo nocturno. El horario no siempre figura en la API.
    terminos_noche = [
        "turno de noche",
        "turno nocturno",
        "turno noche",
        "nocturno",
        "nocturna",
        "night shift",
        "overnight",
    ]

    if any(termino in titulo for termino in terminos_noche):
        return False

    return True


# ============================================================
# CONSULTA DE OFERTAS REALES
# ============================================================

def consultar_api_empleo():
    if not JOA_API_KEY:
        raise RuntimeError(
            "Falta JOA_API_KEY en las variables de Render."
        )

    # La API busca en título, empresa y ubicación.
    # Después verificamos la zona y el tipo de trabajo.
    consulta_zonas = (
        '"San Javier" OR '
        '"San Pedro del Pinatar" OR '
        '"Los Alcázares" OR '
        '"Torre-Pacheco" OR '
        'Cartagena OR Murcia'
    )

    parametros = urlencode({
        "country": "ES",
        "q": consulta_zonas,
        "limit": LIMITE_API,
    })

    peticion = Request(
        API_URL + "?" + parametros,
        headers={
            "Authorization": "Bearer " + JOA_API_KEY,
            "Accept": "application/json",
            "User-Agent": "EmpleoSanJavierBot/1.0",
        },
        method="GET",
    )

    try:
        with urlopen(peticion, timeout=25) as respuesta:
            datos = json.loads(
                respuesta.read().decode("utf-8")
            )

    except HTTPError as error:
        codigo = error.code

        if codigo == 401:
            raise RuntimeError(
                "La API ha rechazado la clave. "
                "Revisa JOA_API_KEY en Render."
            )

        if codigo == 402:
            raise RuntimeError(
                "La API indica que se ha alcanzado "
                "un límite del plan o de registros."
            )

        if codigo == 429:
            raise RuntimeError(
                "La API ha limitado temporalmente las "
                "consultas. Inténtalo más tarde."
            )

        raise RuntimeError(
            "La API de empleo respondió con el error "
            + str(codigo)
            + "."
        )

    except (URLError, TimeoutError):
        raise RuntimeError(
            "No se ha podido conectar con la API de empleo."
        )

    ofertas = datos.get("data", [])

    if not isinstance(ofertas, list):
        raise RuntimeError(
            "La respuesta de la API no tiene el formato esperado."
        )

    resultado = []

    for oferta in ofertas:
        if not isinstance(oferta, dict):
            continue

        if oferta.get("status", "live") != "live":
            continue

        if not es_zona_cercana(oferta):
            continue

        if not es_trabajo_interesante(oferta):
            continue

        if not oferta.get("apply_url"):
            continue

        resultado.append(oferta)

    # Primero las ofertas más recientes.
    resultado.sort(
        key=lambda oferta: (
            oferta.get("posted_at")
            or oferta.get("first_seen_at")
            or ""
        ),
        reverse=True,
    )

    return resultado


async def buscar_ofertas():
    return await asyncio.to_thread(consultar_api_empleo)


# ============================================================
# PRESENTACIÓN DE OFERTAS
# ============================================================

def formatear_fecha(fecha_texto):
    if not fecha_texto:
        return "Fecha de publicación no indicada"

    try:
        fecha = datetime.fromisoformat(
            fecha_texto.replace("Z", "+00:00")
        )

        fecha = fecha.astimezone(
            ZoneInfo("Europe/Madrid")
        )

        return fecha.strftime("%d/%m/%Y %H:%M")

    except (ValueError, TypeError):
        return str(fecha_texto)


def formatear_salario(oferta):
    minimo = oferta.get("salary_min_annual_eur")
    maximo = oferta.get("salary_max_annual_eur")

    if minimo is not None and maximo is not None:
        try:
            return (
                f"{float(minimo):,.0f}–"
                f"{float(maximo):,.0f} € brutos/año"
            )
        except (ValueError, TypeError):
            pass

    if minimo is not None:
        try:
            return f"Desde {float(minimo):,.0f} € brutos/año"
        except (ValueError, TypeError):
            pass

    return "Salario no publicado"


async def enviar_una_oferta(bot, chat_id, oferta):
    titulo = oferta.get("title") or "Puesto sin título"
    empresa = oferta.get("company") or "Empresa no indicada"

    ubicacion = (
        oferta.get("location")
        or oferta.get("city")
        or "Ubicación no detallada"
    )

    fecha = formatear_fecha(
        oferta.get("posted_at")
        or oferta.get("first_seen_at")
    )

    salario = formatear_salario(oferta)

    texto = (
        "🆕 OFERTA DE EMPLEO\n\n"
        f"💼 {titulo}\n"
        f"🏢 Empresa: {empresa}\n"
        f"📍 Zona: {ubicacion}\n"
        f"💶 Salario: {salario}\n"
        f"📅 Publicada: {fecha}\n\n"
        "⚠️ Comprueba el horario, las condiciones "
        "y que la vacante siga disponible."
    )

    enlace = oferta.get("apply_url")

    teclado = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📩 Ver oferta y solicitar",
                url=enlace,
            )
        ]
    ])

    await bot.send_message(
        chat_id=chat_id,
        text=texto,
        reply_markup=teclado,
        disable_web_page_preview=True,
    )


async def enviar_resultados(
    bot,
    chat_id,
    ofertas,
    solo_nuevas=False,
):
    if not ofertas:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "🔎 No he encontrado ofertas que cumplan "
                "los filtros en la consulta actual.\n\n"
                "Esto no significa que no haya empleo en "
                "la zona: la cobertura de la API puede ser "
                "limitada. Puedes probar los portales de empleo "
                "desde /portales."
            ),
        )
        return

    enviadas = 0

    for oferta in ofertas:
        identificador = oferta.get("id") or oferta.get("apply_url")

        if not identificador:
            continue

        if solo_nuevas and identificador in ofertas_vistas:
            continue

        if identificador in ofertas_vistas:
            continue

        if enviadas >= MAX_OFERTAS_TELEGRAM:
            break

        try:
            await enviar_una_oferta(
                bot,
                chat_id,
                oferta,
            )
            ofertas_vistas.add(identificador)
            enviadas += 1

        except Exception:
            logging.exception(
                "No se ha podido enviar una oferta."
            )

    if enviadas == 0:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "✅ He revisado las ofertas disponibles, "
                "pero no hay nuevas vacantes que mostrar "
                "respecto a las que ya te envié."
            ),
        )


# ============================================================
# COMANDOS DE TELEGRAM
# ============================================================

async def comando_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = update.effective_chat.id
    suscriptores.add(chat_id)

    teclado = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔎 Buscar ofertas reales",
                callback_data="buscar",
            )
        ],
        [
            InlineKeyboardButton(
                "🌐 Portales de empleo",
                callback_data="portales",
            )
        ],
        [
            InlineKeyboardButton(
                "⚙️ Mis preferencias",
                callback_data="preferencias",
            )
        ],
        [
            InlineKeyboardButton(
                "❓ Ayuda",
                callback_data="ayuda",
            )
        ],
    ])

    await update.effective_message.reply_text(
        "👋 ¡Bienvenida al BOT DE EMPLEO SAN JAVIER!\n\n"
        "Ahora consultaré una fuente de ofertas de empleo "
        "mediante una API.\n\n"
        "🎯 Prioridades:\n"
        "🚚 Conductor y repartidor con carnet B\n"
        "📦 Carretillero y trabajos de almacén\n"
        "🏢 Conserje y control de accesos\n"
        "🔧 Operario y mantenimiento\n"
        "☀️ Intentaré descartar ofertas que indiquen "
        "claramente turno de noche\n\n"
        "📍 San Javier, San Pedro del Pinatar, Los Alcázares, "
        "Torre-Pacheco, Cartagena y Murcia.\n\n"
        "Pulsa «Buscar ofertas reales» para empezar. "
        "El bot intentará revisar nuevas ofertas una vez al día.",
        reply_markup=teclado,
    )


async def comando_buscar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = update.effective_chat.id
    suscriptores.add(chat_id)

    mensaje = await update.effective_message.reply_text(
        "🔎 Consultando las ofertas disponibles...\n"
        "Puede tardar unos segundos."
    )

    try:
        ofertas = await buscar_ofertas()
        await enviar_resultados(
            context.bot,
            chat_id,
            ofertas,
        )

    except RuntimeError as error:
        logging.warning("Error de API: %s", error)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ No he podido consultar las ofertas.\n\n"
                f"Motivo: {error}"
            ),
        )

    except Exception:
        logging.exception("Error al buscar ofertas")
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ Ha ocurrido un error al buscar ofertas. "
                "Inténtalo de nuevo más tarde."
            ),
        )

    try:
        await mensaje.delete()
    except Exception:
        pass


async def comando_preferencias(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "⚙️ TUS PREFERENCIAS\n\n"
        "📍 San Javier y alrededores.\n"
        "🚗 Carnet de conducir B.\n"
        "📦 Más de 10 años de experiencia con carretilla.\n"
        "🏢 También conserjería y control de accesos.\n"
        "☀️ Preferencia por trabajos sin turno de noche.\n"
        "📅 Disponibilidad los sábados.\n"
        "💶 Prioridad a salarios mejores cuando se publiquen.\n\n"
        "Importante: no todas las empresas indican el salario "
        "ni el horario en la oferta. El bot no puede garantizar "
        "que una vacante cumpla condiciones que no aparecen."
    )


async def comando_portales(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    teclado = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🚚 Conductor y repartidor",
                url=crear_enlace_indeed(
                    "conductor repartidor carnet B"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "📦 Carretillero y almacén",
                url=crear_enlace_indeed(
                    "carretillero almacen"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "🏢 Conserje y accesos",
                url=crear_enlace_indeed(
                    "conserje control accesos"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "🔎 Buscar en InfoJobs",
                url=crear_enlace_infojobs(
                    "conductor repartidor carretillero conserje"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "🔎 Buscar en Google",
                url=crear_enlace_google(
                    "empleo San Javier Cartagena Murcia "
                    "conductor repartidor carretillero"
                ),
            )
        ],
    ])

    await update.effective_message.reply_text(
        "🌐 BÚSQUEDAS EN PORTALES\n\n"
        "Estas búsquedas complementan las ofertas de la API. "
        "Abre el enlace y comprueba que cada vacante siga activa.",
        reply_markup=teclado,
    )


async def comando_ayuda(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "❓ AYUDA\n\n"
        "/start — Abrir el menú.\n"
        "/buscar — Consultar ofertas reales.\n"
        "/portales — Abrir búsquedas en portales.\n"
        "/preferencias — Consultar tus preferencias.\n"
        "/ayuda — Mostrar esta ayuda.\n\n"
        "Las alertas automáticas revisan las ofertas "
        "aproximadamente una vez al día mientras el servicio "
        "esté funcionando. Solo se pueden detectar ofertas "
        "que aparezcan en la fuente consultada."
    )


# ============================================================
# BOTONES
# ============================================================

async def gestionar_boton(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    consulta = update.callback_query
    await consulta.answer()

    dato = consulta.data

    if dato == "buscar":
        chat_id = consulta.message.chat_id
        suscriptores.add(chat_id)

        await consulta.message.reply_text(
            "🔎 Voy a consultar las ofertas reales."
        )

        try:
            ofertas = await buscar_ofertas()
            await enviar_resultados(
                context.bot,
                chat_id,
                ofertas,
            )
        except Exception as error:
            logging.exception("Error en el botón de búsqueda")
            await consulta.message.reply_text(
                "⚠️ No he podido consultar la API.\n"
                f"Motivo: {error}"
            )

    elif dato == "portales":
        await comando_portales(update, context)

    elif dato == "preferencias":
        await comando_preferencias(update, context)

    elif dato == "ayuda":
        await comando_ayuda(update, context)

    elif dato == "menu":
        await comando_start(update, context)


# ============================================================
# ALERTAS AUTOMÁTICAS
# ============================================================

async def tarea_alertas(application):
    # Primera comprobación automática a las 24 horas.
    # Para ver ofertas inmediatamente, usa /buscar.
    while True:
        await asyncio.sleep(INTERVALO_ALERTAS)

        if not suscriptores:
            continue

        try:
            ofertas = await buscar_ofertas()

            for chat_id in list(suscriptores):
                try:
                    await enviar_resultados(
                        application.bot,
                        chat_id,
                        ofertas,
                        solo_nuevas=True,
                    )
                except Exception:
                    logging.exception(
                        "Error enviando alertas automáticas"
                    )

        except Exception:
            logging.exception(
                "No se ha podido completar la búsqueda diaria"
            )


async def al_iniciar(application):
    application.create_task(
        tarea_alertas(application)
    )
    logging.info("Tarea de alertas diarias preparada.")


# ============================================================
# ARRANQUE
# ============================================================

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "Falta TELEGRAM_TOKEN en Render."
        )

    if not JOA_API_KEY:
        raise RuntimeError(
            "Falta JOA_API_KEY en Render."
        )

    threading.Thread(
        target=iniciar_servidor_web,
        daemon=True,
    ).start()

    bot = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(al_iniciar)
        .build()
    )

    bot.add_handler(CommandHandler("start", comando_start))
    bot.add_handler(CommandHandler("buscar", comando_buscar))
    bot.add_handler(CommandHandler("ofertas", comando_buscar))
    bot.add_handler(
        CommandHandler("portales", comando_portales)
    )
    bot.add_handler(
        CommandHandler("preferencias", comando_preferencias)
    )
    bot.add_handler(CommandHandler("ayuda", comando_ayuda))
    bot.add_handler(CallbackQueryHandler(gestionar_boton))

    logging.info("Bot de empleo San Javier iniciado.")
    bot.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
