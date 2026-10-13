
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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ============================================================
# CONFIGURACION
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
JOA_API_KEY = os.getenv("JOA_API_KEY")
PORT = int(os.getenv("PORT", "10000"))

API_URL = "https://api.jobopportunitiesapi.org/v1/jobs"

# Una sola consulta diaria de 25 registros para controlar
# el consumo del plan gratuito.
LIMITE_API = 25
INTERVALO_ALERTAS = 24 * 60 * 60
MAX_OFERTAS_TELEGRAM = 8

# Zonas prioritarias. Murcia capital se trata aparte.
ZONAS_PRIORITARIAS = [
    "san javier",
    "san pedro del pinatar",
    "los alcazares",
    "torre pacheco",
    "cartagena",
]

# Murcia capital solo se acepta si hay un salario minimo
# publicado de al menos esta cantidad anual.
SALARIO_MINIMO_MURCIA = 24000

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

app_web = Flask(__name__)

# Los chats se guardan en memoria mientras el proceso siga activo.
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


def enlace_google(busqueda):
    return "https://www.google.com/search?q=" + quote_plus(busqueda)


def enlace_indeed(busqueda):
    return (
        "https://es.indeed.com/jobs?q="
        + quote_plus(busqueda)
        + "&l="
        + quote_plus("San Javier, Murcia")
    )


def enlace_infojobs(busqueda):
    return enlace_google(
        "site:infojobs.net " + busqueda + " San Javier Murcia"
    )


def salario_minimo_publicado(oferta):
    valor = oferta.get("salary_min_annual_eur")

    if valor is None:
        valor = oferta.get("salary_min")

    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def ubicacion_oferta(oferta):
    return normalizar(" ".join([
        str(oferta.get("location") or ""),
        str(oferta.get("city") or ""),
        str(oferta.get("state") or ""),
    ]))


def es_zona_prioritaria(oferta):
    ubicacion = ubicacion_oferta(oferta)

    return any(
        zona in ubicacion
        for zona in ZONAS_PRIORITARIAS
    )


def es_murcia_capital(oferta):
    ubicacion = ubicacion_oferta(oferta)

    # La Alberca y otros barrios pueden aparecer solo como
    # parte de Murcia. Si no hay una zona prioritaria reconocida,
    # Murcia se considera zona ampliada.
    return "murcia" in ubicacion


def es_zona_aceptada(oferta):
    if es_zona_prioritaria(oferta):
        return True

    if es_murcia_capital(oferta):
        salario = salario_minimo_publicado(oferta)

        # Murcia capital solo entra si el salario publicado
        # alcanza el minimo establecido.
        return (
            salario is not None
            and salario >= SALARIO_MINIMO_MURCIA
        )

    return False


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
# CONSULTA DE LA API
# ============================================================

def consultar_api_empleo():
    if not JOA_API_KEY:
        raise RuntimeError(
            "Falta JOA_API_KEY en las variables de Render."
        )

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
            "User-Agent": "EmpleoSanJavierBot/1.1",
        },
        method="GET",
    )

    try:
        with urlopen(peticion, timeout=25) as respuesta:
            datos = json.loads(
                respuesta.read().decode("utf-8")
            )

    except HTTPError as error:
        if error.code == 401:
            raise RuntimeError(
                "La API ha rechazado la clave. "
                "Revisa JOA_API_KEY en Render."
            )

        if error.code == 402:
            raise RuntimeError(
                "La API indica que se ha alcanzado un limite "
                "del plan o de registros."
            )

        if error.code == 422:
            raise RuntimeError(
                "La API no acepta alguno de los filtros "
                "de busqueda. Revisa los Logs de Render."
            )

        if error.code == 429:
            raise RuntimeError(
                "La API ha limitado temporalmente las consultas."
            )

        raise RuntimeError(
            "La API de empleo respondio con el error "
            + str(error.code)
            + "."
        )

    except (URLError, TimeoutError):
        raise RuntimeError(
            "No se ha podido conectar con la API de empleo."
        )

    if not isinstance(datos, dict):
        raise RuntimeError(
            "La API ha devuelto una respuesta inesperada."
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

        if not oferta.get("apply_url"):
            continue

        if not es_zona_aceptada(oferta):
            continue

        if not es_trabajo_interesante(oferta):
            continue

        resultado.append(oferta)

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
# PRESENTACION DE OFERTAS
# ============================================================

def formatear_fecha(fecha_texto):
    if not fecha_texto:
        return "Fecha no indicada"

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

    try:
        if minimo is not None and maximo is not None:
            return (
                f"{float(minimo):,.0f}–"
                f"{float(maximo):,.0f} € brutos/año"
            )

        if minimo is not None:
            return f"Desde {float(minimo):,.0f} € brutos/año"

    except (ValueError, TypeError):
        pass

    return "Salario no publicado"


async def enviar_una_oferta(bot, chat_id, oferta):
    titulo = oferta.get("title") or "Puesto sin titulo"
    empresa = oferta.get("company") or "Empresa no indicada"

    ubicacion = (
        oferta.get("location")
        or oferta.get("city")
        or "Ubicacion no detallada"
    )

    fecha = formatear_fecha(
        oferta.get("posted_at")
        or oferta.get("first_seen_at")
    )

    salario = formatear_salario(oferta)

    zona_texto = "📍 Zona prioritaria"
    if (
        es_murcia_capital(oferta)
        and not es_zona_prioritaria(oferta)
    ):
        zona_texto = "📍 Zona ampliada: Murcia capital"

    texto = (
        "🆕 OFERTA DE EMPLEO\n\n"
        f"💼 {titulo}\n"
        f"🏢 Empresa: {empresa}\n"
        f"{zona_texto}: {ubicacion}\n"
        f"💶 Salario: {salario}\n"
        f"📅 Publicada: {fecha}\n\n"
        "⚠️ Comprueba el horario, las condiciones "
        "y que la vacante siga disponible."
    )

    teclado = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📩 Ver oferta y solicitar",
                url=oferta["apply_url"],
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
                "los filtros en esta consulta.\n\n"
                "He priorizado San Javier, San Pedro del Pinatar, "
                "Los Alcázares, Torre-Pacheco y Cartagena. "
                "Murcia capital solo entra si publica un salario "
                "minimo de 24.000 € brutos al año.\n\n"
                "La API puede tener una cobertura limitada. "
                "Puedes ampliar la busqueda con /portales."
            ),
        )
        return

    enviadas = 0

    for oferta in ofertas:
        identificador = oferta.get("id") or oferta.get("apply_url")

        if not identificador:
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
        mensaje = (
            "✅ No hay nuevas ofertas que mostrar "
            "respecto a las que ya te he enviado."
        )

        if not solo_nuevas:
            mensaje = (
                "✅ Ya habias recibido todas las ofertas "
                "que cumplen los filtros de esta consulta."
            )

        await bot.send_message(
            chat_id=chat_id,
            text=mensaje,
        )


# ============================================================
# COMANDOS
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
        "🎯 Buscare ofertas de empleo relacionadas con:\n"
        "🚚 Conductor y repartidor con carnet B\n"
        "📦 Carretillero y trabajos de almacen\n"
        "🏢 Conserje y control de accesos\n"
        "🔧 Operario y mantenimiento\n\n"
        "📍 PRIORIDAD:\n"
        "San Javier, San Pedro del Pinatar, Los Alcazares, "
        "Torre-Pacheco y Cartagena.\n\n"
        "Murcia capital solo se incluye si publica un salario "
        "minimo de 24.000 € brutos al año.\n\n"
        "☀️ Intentare descartar ofertas que indiquen claramente "
        "turno de noche. El horario no siempre figura en los datos.\n\n"
        "Pulsa «Buscar ofertas reales» para consultar la API.",
        reply_markup=teclado,
    )


async def comando_buscar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = update.effective_chat.id
    suscriptores.add(chat_id)

    mensaje = await update.effective_message.reply_text(
        "🔎 Consultando ofertas reales de empleo..."
    )

    try:
        ofertas = await buscar_ofertas()

        await enviar_resultados(
            context.bot,
            chat_id,
            ofertas,
        )

    except Exception as error:
        logging.exception("Error al buscar ofertas")

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ No he podido consultar las ofertas.\n\n"
                f"Motivo: {error}"
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
        "📍 Zona prioritaria: San Javier, San Pedro del Pinatar, "
        "Los Alcazares, Torre-Pacheco y Cartagena.\n"
        "📍 Murcia capital: solo con salario publicado de "
        "24.000 € brutos/año o mas.\n"
        "🚗 Carnet de conducir B.\n"
        "📦 Mas de 10 años de experiencia con carretilla.\n"
        "🏢 Conserjeria y control de accesos.\n"
        "☀️ Preferencia por trabajos sin turno de noche.\n"
        "📅 Disponibilidad los sabados.\n"
        "💶 Prioridad a mejores condiciones salariales.\n\n"
        "El salario y el horario pueden no estar indicados "
        "en la oferta original."
    )


async def comando_portales(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    teclado = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🚚 Conductor y repartidor",
                url=enlace_indeed(
                    "conductor repartidor carnet B"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "📦 Carretillero y almacen",
                url=enlace_indeed(
                    "carretillero almacen"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "🏢 Conserje y accesos",
                url=enlace_indeed(
                    "conserje control accesos"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "🔎 Buscar en InfoJobs",
                url=enlace_infojobs(
                    "conductor repartidor carretillero conserje"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "🔎 Buscar en Google",
                url=enlace_google(
                    "empleo San Javier Cartagena Murcia "
                    "conductor repartidor carretillero"
                ),
            )
        ],
    ])

    await update.effective_message.reply_text(
        "🌐 BUSQUEDAS COMPLEMENTARIAS\n\n"
        "Estos enlaces abren portales de empleo. Comprueba "
        "la ubicacion, el salario, el horario y que la oferta "
        "siga vigente.",
        reply_markup=teclado,
    )


async def comando_ayuda(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "❓ AYUDA\n\n"
        "/start — Abrir el menu.\n"
        "/buscar — Consultar ofertas reales.\n"
        "/ofertas — Alias de buscar.\n"
        "/portales — Abrir portales de empleo.\n"
        "/preferencias — Ver tus preferencias.\n"
        "/ayuda — Mostrar esta ayuda.\n\n"
        "El bot intenta revisar ofertas nuevas una vez al dia "
        "mientras el servicio esta funcionando."
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
            "🔎 Consultando las ofertas reales..."
        )

        try:
            ofertas = await buscar_ofertas()

            await enviar_resultados(
                context.bot,
                chat_id,
                ofertas,
            )

        except Exception as error:
            logging.exception("Error en el boton de busqueda")

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


# ============================================================
# ALERTAS AUTOMATICAS
# ============================================================

async def tarea_alertas(application):
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
                        "Error enviando alerta automatica"
                    )

        except Exception:
            logging.exception(
                "Error al consultar las ofertas diariamente"
            )


async def al_iniciar(application):
    application.create_task(
        tarea_alertas(application)
    )
    logging.info("Alertas diarias preparadas.")


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
    bot.add_handler(CommandHandler("portales", comando_portales))
    bot.add_handler(
        CommandHandler("preferencias", comando_preferencias)
    )
    bot.add_handler(CommandHandler("ayuda", comando_ayuda))
    bot.add_handler(CallbackQueryHandler(gestionar_boton))

    logging.info("Bot de empleo San Javier iniciado.")
    bot.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
