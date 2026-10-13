
import os
import re
import json
import time
import sqlite3
import asyncio
import logging
import threading
import unicodedata
from datetime import datetime, timezone, timedelta
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

# Limite para proteger la cuota gratuita.
# Cada registro devuelto consume cuota, tambien en /buscar.
LIMITE_API = 25
INTERVALO_ALERTAS = 24 * 60 * 60
ESPERA_PRIMERA_ALERTA = 30
MAX_OFERTAS_TELEGRAM = 8

# Archivo local para guardar chats y ofertas enviadas.
# Render gratuito puede perderlo al reemplazar el sistema
# de archivos durante determinados despliegues.
RUTA_BD = os.getenv("BOT_DB_PATH", "bot_empleo.sqlite3")

ZONAS_PRIORITARIAS = [
    "san javier",
    "san pedro del pinatar",
    "los alcazares",
    "torre pacheco",
    "cartagena",
]

SALARIO_MINIMO_MURCIA = 24000

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

app_web = Flask(__name__)
suscriptores = set()


# ============================================================
# BASE DE DATOS LOCAL
# ============================================================

def conectar_bd():
    conexion = sqlite3.connect(RUTA_BD, timeout=15)
    conexion.execute("PRAGMA busy_timeout = 15000")
    return conexion


def inicializar_bd():
    with conectar_bd() as conexion:
        conexion.execute("""
            CREATE TABLE IF NOT EXISTS suscriptores (
                chat_id TEXT PRIMARY KEY,
                creado_en TEXT NOT NULL
            )
        """)

        conexion.execute("""
            CREATE TABLE IF NOT EXISTS ofertas_enviadas (
                chat_id TEXT NOT NULL,
                oferta_id TEXT NOT NULL,
                enviada_en TEXT NOT NULL,
                PRIMARY KEY (chat_id, oferta_id)
            )
        """)

        filas = conexion.execute(
            "SELECT chat_id FROM suscriptores"
        ).fetchall()

    suscriptores.clear()

    for fila in filas:
        try:
            suscriptores.add(int(fila[0]))
        except (ValueError, TypeError):
            continue

    logging.info(
        "Base de datos local preparada. Chats guardados: %s",
        len(suscriptores),
    )


def guardar_suscriptor(chat_id):
    suscriptores.add(chat_id)

    with conectar_bd() as conexion:
        conexion.execute(
            """
            INSERT OR IGNORE INTO suscriptores (chat_id, creado_en)
            VALUES (?, ?)
            """,
            (
                str(chat_id),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def oferta_ya_enviada(chat_id, oferta_id):
    with conectar_bd() as conexion:
        fila = conexion.execute(
            """
            SELECT 1 FROM ofertas_enviadas
            WHERE chat_id = ? AND oferta_id = ?
            """,
            (str(chat_id), str(oferta_id)),
        ).fetchone()

    return fila is not None


def marcar_oferta_enviada(chat_id, oferta_id):
    with conectar_bd() as conexion:
        conexion.execute(
            """
            INSERT OR IGNORE INTO ofertas_enviadas
            (chat_id, oferta_id, enviada_en)
            VALUES (?, ?, ?)
            """,
            (
                str(chat_id),
                str(oferta_id),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


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

    texto = "".join(
        caracter
        for caracter in texto
        if unicodedata.category(caracter) != "Mn"
    )

    # Unifica guiones, comas y otros signos como espacios.
    texto = re.sub(r"[^a-z0-9]+", " ", texto)

    return " ".join(texto.split())


def enlace_google(busqueda):
    return (
        "https://www.google.com/search?q="
        + quote_plus(busqueda)
    )


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


def ubicacion_oferta(oferta):
    partes = [
        oferta.get("location"),
        oferta.get("city"),
        oferta.get("region"),
        oferta.get("state"),
    ]

    return normalizar(
        " ".join(str(parte) for parte in partes if parte)
    )


def es_zona_prioritaria(oferta):
    ubicacion = ubicacion_oferta(oferta)

    return any(
        zona in ubicacion
        for zona in ZONAS_PRIORITARIAS
    )


def es_murcia_capital(oferta):
    ciudad = normalizar(oferta.get("city", ""))

    # Si la API identifica la ciudad, usamos ese dato.
    if ciudad:
        return ciudad == "murcia"

    # Si no hay ciudad estructurada, solo aceptamos ubicaciones
    # cuyo texto comienza por Murcia, evitando interpretar
    # cualquier localidad de la provincia como la capital.
    ubicacion = normalizar(oferta.get("location", ""))

    return ubicacion == "murcia" or ubicacion.startswith("murcia ")


def salario_minimo_publicado(oferta):
    valor = oferta.get("salary_min_annual_eur")

    try:
        if valor is None:
            return None

        return float(valor)

    except (TypeError, ValueError):
        return None


def es_zona_aceptada(oferta):
    if es_zona_prioritaria(oferta):
        return True

    if es_murcia_capital(oferta):
        salario = salario_minimo_publicado(oferta)

        return (
            salario is not None
            and salario >= SALARIO_MINIMO_MURCIA
        )

    return False


# ============================================================
# FILTRO DE EMPLEOS Y HORARIOS
# ============================================================

PALABRAS_EMPLEO = [
    "conductor",
    "conductora",
    "repartidor",
    "repartidora",
    "reparto",
    "chofer",
    "chófer",
    "furgoneta",
    "carnet b",
    "carretillero",
    "carretillera",
    "carretilla",
    "transportista",
    "conserje",
    "conserjeria",
    "control de acceso",
    "controlador de acceso",
    "auxiliar de servicios",
    "mozo de almacen",
    "moza de almacen",
    "almacen",
    "logistica",
    "operario",
    "operaria",
    "mantenimiento",
    "reponedor",
    "reponedora",
    "supermercado",
    "vigilante",
    "recepcionista",
    "limpieza",
    "limpiador",
    "limpiadora",
    "preparador de pedidos",
    "preparadora de pedidos",
    "expediciones",
    "mensajero",
    "mensajera",
]

TERMINOS_NOCHE = [
    "turno de noche",
    "turno nocturno",
    "turno noche",
    "turnos de noche",
    "turnos nocturnos",
    "nocturno",
    "nocturna",
    "night shift",
    "overnight",
    "trabajo nocturno",
    "jornada nocturna",
]


def es_trabajo_interesante(oferta):
    titulo = normalizar(oferta.get("title", ""))

    return any(
        normalizar(palabra) in titulo
        for palabra in PALABRAS_EMPLEO
    )


def tiene_turno_nocturno(oferta):
    titulo = normalizar(oferta.get("title", ""))
    descripcion = normalizar(oferta.get("description", ""))

    texto = titulo + " " + descripcion

    return any(
        normalizar(termino) in texto
        for termino in TERMINOS_NOCHE
    )


def fecha_api(fecha_texto):
    if not fecha_texto:
        return None

    try:
        fecha = datetime.fromisoformat(
            str(fecha_texto).replace("Z", "+00:00")
        )

        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=timezone.utc)

        return fecha.astimezone(timezone.utc)

    except (ValueError, TypeError, OverflowError):
        return None


def oferta_sigue_vigente(oferta):
    estado = normalizar(oferta.get("status", "live"))

    if estado != "live":
        return False

    fecha_caducidad = fecha_api(oferta.get("expires_at"))

    if fecha_caducidad is not None:
        if fecha_caducidad < datetime.now(timezone.utc):
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

    # La API busca en título, empresa y ubicación.
    # Su listado ya viene ordenado por fecha de publicación.
    # Se mantiene una única consulta para proteger la cuota.
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
        "include_description": "true",
    })

    peticion = Request(
        API_URL + "?" + parametros,
        headers={
            "Authorization": "Bearer " + JOA_API_KEY,
            "Accept": "application/json",
            "User-Agent": "EmpleoSanJavierBot/2.0",
        },
        method="GET",
    )

    try:
        with urlopen(peticion, timeout=35) as respuesta:
            contenido = respuesta.read().decode("utf-8")
            datos = json.loads(contenido)

    except HTTPError as error:
        try:
            detalle = error.read().decode("utf-8")[:500]
        except Exception:
            detalle = ""

        logging.error(
            "Error API JOA HTTP %s: %s",
            error.code,
            detalle,
        )

        if error.code == 401:
            raise RuntimeError(
                "JOA ha rechazado la clave. "
                "Comprueba JOA_API_KEY en Render."
            )

        if error.code == 402:
            raise RuntimeError(
                "JOA indica que se ha alcanzado un límite "
                "del plan o de registros."
            )

        if error.code == 403:
            raise RuntimeError(
                "JOA ha denegado la consulta. Puede ser un "
                "problema de permisos o de cuota. Revisa "
                "el detalle del error en los Logs de Render."
            )

        if error.code == 422:
            raise RuntimeError(
                "JOA no acepta alguno de los parámetros "
                "de búsqueda. Revisa los Logs de Render."
            )

        if error.code == 429:
            raise RuntimeError(
                "JOA ha limitado temporalmente las consultas."
            )

        if error.code >= 500:
            raise RuntimeError(
                "El servicio de JOA tiene un error temporal "
                "o no ha podido completar la búsqueda."
            )

        raise RuntimeError(
            "JOA ha respondido con el error HTTP "
            + str(error.code)
            + "."
        )

    except (URLError, TimeoutError, OSError) as error:
        logging.warning("Error de conexión con JOA: %s", error)

        raise RuntimeError(
            "No se ha podido conectar con la API de empleo."
        )

    except json.JSONDecodeError:
        raise RuntimeError(
            "La API ha devuelto una respuesta que no es JSON válido."
        )

    if not isinstance(datos, dict):
        raise RuntimeError(
            "La API ha devuelto una respuesta inesperada."
        )

    ofertas = datos.get("data")

    if not isinstance(ofertas, list):
        raise RuntimeError(
            "La respuesta de JOA no contiene la lista de ofertas."
        )

    resultado = []

    for oferta in ofertas:
        if not isinstance(oferta, dict):
            continue

        if not oferta_sigue_vigente(oferta):
            continue

        enlace = str(oferta.get("apply_url") or "").strip()

        if not enlace.startswith(("https://", "http://")):
            continue

        if not es_zona_aceptada(oferta):
            continue

        if not es_trabajo_interesante(oferta):
            continue

        # Solo descartamos horarios nocturnos cuando aparecen
        # claramente en el título o la descripción disponible.
        if tiene_turno_nocturno(oferta):
            continue

        resultado.append(oferta)

    resultado.sort(
        key=lambda oferta: (
            fecha_api(oferta.get("posted_at"))
            or fecha_api(oferta.get("first_seen_at"))
            or datetime.min.replace(tzinfo=timezone.utc)
        ),
        reverse=True,
    )

    logging.info(
        "JOA: %s registros recibidos; %s cumplen los filtros.",
        len(ofertas),
        len(resultado),
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

    fecha = fecha_api(fecha_texto)

    if fecha is None:
        return str(fecha_texto)

    return fecha.astimezone(
        ZoneInfo("Europe/Madrid")
    ).strftime("%d/%m/%Y %H:%M")


def numero_espanol(valor):
    return f"{float(valor):,.0f}".replace(",", ".")


def formatear_salario(oferta):
    minimo_anual = oferta.get("salary_min_annual_eur")
    maximo = oferta.get("salary_max_annual_eur")
    origen = normalizar(oferta.get("salary_source", ""))

    try:
        if minimo_anual is not None:
            minimo_anual = float(minimo_anual)

            if maximo is not None:
                maximo = float(maximo)

                if maximo >= minimo_anual:
                    texto = (
                        f"{numero_espanol(minimo_anual)}–"
                        f"{numero_espanol(maximo)} € brutos/año"
                    )
                else:
                    texto = (
                        f"Desde {numero_espanol(minimo_anual)} "
                        "€ brutos/año"
                    )
            else:
                texto = (
                    f"Desde {numero_espanol(minimo_anual)} "
                    "€ brutos/año"
                )

            if origen == "parsed description":
                return "Detectado en el anuncio: " + texto

            return texto

    except (ValueError, TypeError):
        pass

    # Si no se puede normalizar el salario anual, mostramos
    # la cifra original solo con su periodo y moneda conocidos.
    minimo = oferta.get("salary_min")
    maximo_original = oferta.get("salary_max")
    moneda = str(oferta.get("salary_currency") or "").upper()
    periodo = normalizar(oferta.get("salary_period", ""))

    if minimo is not None and moneda:
        periodos = {
            "hour": "hora",
            "day": "día",
            "week": "semana",
            "month": "mes",
            "year": "año",
        }

        unidad = periodos.get(periodo)

        try:
            minimo_num = float(minimo)

            if maximo_original is not None:
                maximo_num = float(maximo_original)

                if maximo_num >= minimo_num:
                    cifra = (
                        f"{numero_espanol(minimo_num)}–"
                        f"{numero_espanol(maximo_num)}"
                    )
                else:
                    cifra = numero_espanol(minimo_num)
            else:
                cifra = numero_espanol(minimo_num)

            if unidad:
                return f"{cifra} {moneda}/{unidad}"

            return f"{cifra} {moneda}"

        except (ValueError, TypeError):
            pass

    return "Salario no publicado"


def identificador_oferta(oferta):
    identificador = oferta.get("id") or oferta.get("apply_url")

    if not identificador:
        return None

    return str(identificador)


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

    if es_zona_prioritaria(oferta):
        zona_texto = "📍 Zona prioritaria"
    else:
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
        if not solo_nuevas:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "🔎 No he encontrado ofertas que cumplan "
                    "los filtros en esta consulta.\n\n"
                    "📍 Zona prioritaria: San Javier, "
                    "San Pedro del Pinatar, Los Alcázares, "
                    "Torre-Pacheco y Cartagena.\n"
                    "📍 Murcia capital: salario anual "
                    "normalizado de al menos 24.000 €.\n\n"
                    "La API tiene una cobertura limitada y "
                    "el horario no siempre aparece en los anuncios."
                ),
            )
        return

    enviadas = 0
    pendientes = 0

    for oferta in ofertas:
        identificador = identificador_oferta(oferta)

        if not identificador:
            continue

        if oferta_ya_enviada(chat_id, identificador):
            continue

        pendientes += 1

        if enviadas >= MAX_OFERTAS_TELEGRAM:
            continue

        try:
            await enviar_una_oferta(bot, chat_id, oferta)

            marcar_oferta_enviada(chat_id, identificador)
            enviadas += 1

        except Exception:
            logging.exception(
                "No se ha podido enviar la oferta %s",
                identificador,
            )

    if enviadas == 0 and pendientes == 0 and not solo_nuevas:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "✅ Ya habías recibido todas las ofertas "
                "que cumplen los filtros de esta consulta."
            ),
        )

    elif enviadas > 0 and pendientes > enviadas and not solo_nuevas:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"📋 Te he enviado {enviadas} ofertas nuevas. "
                "Hay más resultados, pero he limitado este "
                "mensaje para no saturar el chat."
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
    guardar_suscriptor(chat_id)

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
        "🎯 Buscaré ofertas relacionadas con:\n"
        "🚚 Conductor y repartidor con carnet B\n"
        "📦 Carretillero y trabajos de almacén\n"
        "🏢 Conserje y control de accesos\n"
        "🔧 Operario, mantenimiento y otros puestos similares\n\n"
        "📍 PRIORIDAD:\n"
        "San Javier, San Pedro del Pinatar, Los Alcázares, "
        "Torre-Pacheco y Cartagena.\n\n"
        "Murcia capital solo se incluye si la API indica "
        "un salario anual de al menos 24.000 €.\n\n"
        "☀️ Intentaré descartar los turnos nocturnos cuando "
        "se indiquen claramente en el anuncio.\n\n"
        "Pulsa «Buscar ofertas reales» para empezar.\n\n"
        "Las alertas automáticas dependen de que Render "
        "mantenga el servicio activo.",
        reply_markup=teclado,
    )


async def comando_buscar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = update.effective_chat.id
    guardar_suscriptor(chat_id)

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
        "📍 San Javier, San Pedro del Pinatar, Los Alcázares, "
        "Torre-Pacheco y Cartagena.\n"
        "📍 Murcia capital: mínimo 24.000 € brutos anuales "
        "normalizados por la API.\n"
        "🚗 Carnet de conducir B.\n"
        "📦 Más de 10 años de experiencia con carretilla.\n"
        "🏢 Conserjería y control de accesos.\n"
        "🚚 Conducción, reparto y logística.\n"
        "☀️ Preferencia por trabajos sin turno de noche.\n"
        "📅 Disponibilidad los sábados.\n"
        "💶 Prioridad a mejores condiciones salariales.\n\n"
        "El salario y el horario pueden no estar indicados "
        "en el anuncio original."
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
                "📦 Carretillero y almacén",
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
        "🌐 BÚSQUEDAS COMPLEMENTARIAS\n\n"
        "Estos enlaces abren portales de empleo. Comprueba "
        "la ubicación, el salario, el horario y que la oferta "
        "siga vigente.",
        reply_markup=teclado,
    )


async def comando_ayuda(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "❓ AYUDA\n\n"
        "/start — Abrir el menú y suscribirte.\n"
        "/buscar — Consultar ofertas reales.\n"
        "/ofertas — Alias de buscar.\n"
        "/portales — Abrir portales de empleo.\n"
        "/preferencias — Ver tus preferencias.\n"
        "/ayuda — Mostrar esta ayuda.\n\n"
        "El bot hace una comprobación al arrancar y después "
        "intenta buscar una vez al día mientras Render "
        "mantenga el proceso activo.\n\n"
        "La búsqueda manual también consume registros "
        "de la cuota de JOA."
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
        guardar_suscriptor(chat_id)

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


# ============================================================
# ALERTAS AUTOMATICAS
# ============================================================

async def tarea_alertas(application):
    # Primera consulta breve después de arrancar.
    await asyncio.sleep(ESPERA_PRIMERA_ALERTA)

    while True:
        if suscriptores:
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
                            "Error enviando alerta al chat %s",
                            chat_id,
                        )

            except Exception:
                logging.exception(
                    "Error en la consulta automática de empleo"
                )

        else:
            logging.info(
                "No hay chats suscritos; se omite la consulta "
                "para no consumir cuota innecesariamente."
            )

        await asyncio.sleep(INTERVALO_ALERTAS)


async def al_iniciar(application):
    application.create_task(
        tarea_alertas(application)
    )

    logging.info(
        "Alertas automáticas preparadas: primera consulta "
        "en %s segundos.",
        ESPERA_PRIMERA_ALERTA,
    )


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

    inicializar_bd()

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
