
import os
import threading
import logging
from urllib.parse import quote_plus

from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ============================================================
# CONFIGURACIÓN
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

ZONA = "San Javier, Murcia"
ZONAS_CERCANAS = (
    "San Javier OR San Pedro del Pinatar OR Los Alcázares "
    "OR Torre-Pacheco OR Cartagena"
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logging.getLogger("httpx").setLevel(logging.WARNING)

app_web = Flask(__name__)


# ============================================================
# SERVIDOR WEB PARA RENDER
# ============================================================

@app_web.route("/")
def inicio_web():
    return jsonify({
        "bot": "Bot de empleo San Javier",
        "estado": "funcionando",
        "zona": ZONA,
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
# BÚSQUEDAS DE EMPLEO
# ============================================================

def crear_enlace_google(busqueda):
    consulta = quote_plus(busqueda)
    return f"https://www.google.com/search?q={consulta}"


def crear_enlace_indeed(busqueda):
    consulta = quote_plus(busqueda)
    ubicacion = quote_plus("San Javier, Murcia")
    return (
        f"https://es.indeed.com/jobs?q={consulta}"
        f"&l={ubicacion}"
    )


def crear_enlace_infojobs(busqueda):
    consulta = quote_plus(busqueda)
    return crear_enlace_google(
        f"site:infojobs.net {busqueda} San Javier Murcia"
    )


def obtener_busquedas():
    return [
        (
            "🚚 Conductores y repartidores",
            (
                "conductor repartidor furgoneta "
                f"{ZONAS_CERCANAS} empleo"
            ),
        ),
        (
            "🚗 Conductor con carnet B",
            (
                "conductor carnet B reparto transporte "
                f"{ZONAS_CERCANAS} empleo"
            ),
        ),
        (
            "📦 Carretillero",
            (
                "carretillero carnet carretilla elevadora "
                f"{ZONAS_CERCANAS} empleo"
            ),
        ),
        (
            "🏭 Operario con experiencia",
            (
                "operario producción logística "
                f"{ZONAS_CERCANAS} empleo"
            ),
        ),
        (
            "🏢 Conserje y control de accesos",
            (
                "conserje controlador accesos "
                f"{ZONAS_CERCANAS} empleo"
            ),
        ),
        (
            "🛒 Reposición y supermercados",
            (
                "reponedor supermercado "
                f"{ZONAS_CERCANAS} empleo"
            ),
        ),
        (
            "🔧 Otros trabajos sin turno de noche",
            (
                "empleo jornada diurna sin turno noche "
                f"{ZONAS_CERCANAS}"
            ),
        ),
    ]


# ============================================================
# MENÚ PRINCIPAL
# ============================================================

async def comando_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    texto = (
        "👋 ¡Bienvenida al BOT DE EMPLEO SAN JAVIER!\n\n"
        "Buscaré contigo opciones de trabajo por San Javier "
        "y localidades cercanas.\n\n"
        "🎯 Prioridades:\n"
        "🚚 Conductor y repartidor con carnet B\n"
        "📦 Carretillero, aprovechando tu experiencia\n"
        "🏢 Conserje y control de accesos\n"
        "☀️ Preferencia por trabajos sin turno de noche\n"
        "💶 Intentaremos priorizar mejores condiciones salariales\n\n"
        "Pulsa «Buscar ofertas» para consultar las búsquedas."
    )

    teclado = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔎 Buscar ofertas",
                callback_data="ofertas",
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
        texto,
        reply_markup=teclado,
    )


# ============================================================
# MOSTRAR OFERTAS Y ENLACES
# ============================================================

async def mostrar_ofertas(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    texto = (
        "🔎 BÚSQUEDA DE EMPLEO\n\n"
        "Zona: San Javier y alrededores de Murcia.\n\n"
        "Elige una categoría para consultar las ofertas "
        "publicadas en portales de empleo.\n\n"
        "ℹ️ Los botones abren búsquedas externas. "
        "Comprueba allí el salario, el horario, la ubicación "
        "y que la oferta siga vigente."
    )

    botones = []

    for titulo, busqueda in obtener_busquedas():
        botones.append([
            InlineKeyboardButton(
                titulo,
                callback_data="categoria_" + str(
                    obtener_busquedas().index((titulo, busqueda))
                ),
            )
        ])

    botones.append([
        InlineKeyboardButton(
            "🔙 Menú principal",
            callback_data="menu",
        )
    ])

    await update.effective_message.reply_text(
        texto,
        reply_markup=InlineKeyboardMarkup(botones),
    )


async def comando_ofertas(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await mostrar_ofertas(update, context)


async def comando_preferencias(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    texto = (
        "⚙️ TUS PREFERENCIAS DE EMPLEO\n\n"
        "📍 Zona principal: San Javier, Murcia.\n"
        "🗺️ Alrededores: San Pedro del Pinatar, Los Alcázares, "
        "Torre-Pacheco y Cartagena.\n"
        "🚗 Carnet de conducir: B.\n"
        "📦 Experiencia: más de 10 años con carretilla elevadora.\n"
        "🌙 Evitar: turnos de noche.\n"
        "📅 Sábados: disponibles.\n"
        "💶 Objetivo: priorizar ofertas con mejores salarios.\n\n"
        "Estas preferencias orientan las búsquedas; todavía "
        "no se verifican automáticamente los salarios ni los "
        "horarios de cada oferta."
    )

    await update.effective_message.reply_text(texto)


async def comando_ayuda(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    texto = (
        "❓ CÓMO UTILIZAR EL BOT\n\n"
        "/start — Abrir el menú principal.\n"
        "/ofertas — Consultar categorías de empleo.\n"
        "/preferencias — Ver tus preferencias.\n"
        "/ayuda — Mostrar esta ayuda.\n\n"
        "Pulsa una categoría y abre los enlaces para consultar "
        "las ofertas disponibles.\n\n"
        "Importante: esta primera versión facilita búsquedas "
        "en portales externos. Aún no recopila ofertas "
        "automáticamente ni envía alertas de nuevas vacantes."
    )

    await update.effective_message.reply_text(texto)


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

    if dato == "menu":
        await consulta.message.reply_text(
            "Escribe /start para volver al menú principal."
        )
        return

    if dato == "ofertas":
        await mostrar_ofertas(update, context)
        return

    if dato == "preferencias":
        await comando_preferencias(update, context)
        return

    if dato == "ayuda":
        await comando_ayuda(update, context)
        return

    if dato.startswith("categoria_"):
        try:
            indice = int(dato.split("_", 1)[1])
            busquedas = obtener_busquedas()
            titulo, busqueda = busquedas[indice]
        except (ValueError, IndexError):
            await consulta.message.reply_text(
                "No he podido abrir esa categoría. "
                "Escribe /ofertas para intentarlo de nuevo."
            )
            return

        texto = (
            f"{titulo}\n\n"
            "Consulta estas búsquedas para revisar las vacantes. "
            "Las ofertas y sus condiciones pueden cambiar."
        )

        teclado = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔎 Buscar en Indeed",
                    url=crear_enlace_indeed(busqueda),
                )
            ],
            [
                InlineKeyboardButton(
                    "🔎 Buscar en Google",
                    url=crear_enlace_google(busqueda),
                )
            ],
            [
                InlineKeyboardButton(
                    "🔎 Buscar en InfoJobs",
                    url=crear_enlace_infojobs(busqueda),
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 Ver categorías",
                    callback_data="ofertas",
                )
            ],
        ])

        await consulta.message.reply_text(
            texto,
            reply_markup=teclado,
        )


# ============================================================
# INICIO
# ============================================================

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "Falta la variable TELEGRAM_TOKEN en Render."
        )

    threading.Thread(
        target=iniciar_servidor_web,
        daemon=True,
    ).start()

    bot = Application.builder().token(TELEGRAM_TOKEN).build()

    bot.add_handler(CommandHandler("start", comando_start))
    bot.add_handler(CommandHandler("ofertas", comando_ofertas))
    bot.add_handler(
        CommandHandler("preferencias", comando_preferencias)
    )
    bot.add_handler(CommandHandler("ayuda", comando_ayuda))
    bot.add_handler(
        __import__(
            "telegram.ext",
            fromlist=["CallbackQueryHandler"],
        ).CallbackQueryHandler(gestionar_boton)
    )

    logging.info("Bot de empleo San Javier iniciado.")
    bot.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
