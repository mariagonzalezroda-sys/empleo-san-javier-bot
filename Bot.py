
import os
import re
import asyncio
import sqlite3
import logging
import threading
import hashlib
import urllib.request
import xml.etree.ElementTree as ET

from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin
from datetime import datetime, date
from email.utils import parsedate_to_datetime

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
MAX_CANDIDATOS = 100
TIMEOUT_WEB = 25

URL_LISTADO = (
    "https://www.sistemanacionalempleo.es/"
    "OfertaDifusionWEB/busquedaOfertas.do?CA=14&modo=inicio"
)

# Canales RSS oficiales del Portal Único de Empleo.
BASE_RSS = (
    "https://bnde.sistemanacionalempleo.es/"
    "es/portaltrabaja/resources/rss/"
)

CANALES_RSS = [
    "Conductores_Transporte.xml",
    "Almacenes_Reponedores.xml",
    "Fabricas_Industria.xml",
    "Agricultura_Jardineria_Alimentacion.xml",
    "Metal_Mecanica.xml",
    "Dependientes_Informacion.xml",
    "Limpieza_CuidadoDePersonas.xml",
    "Vigilancia_Servicios.xml",
    "Construccion.xml",
]

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
    "auxiliar de almacen", "operario de almacen",
    "peon", "peona", "peon de fabrica", "peon industrial",
    "conductor de camion", "conductor de furgoneta",
    "conductor repartidor", "reparto", "bulldozer",
    "maquinista", "operador de planta", "operador industrial",
    "auxiliar de mantenimiento", "mantenimiento industrial",
    "carpintero", "carpintera", "soldador", "soldadora",
    "fontanero", "fontanera", "mecanico industrial",
    "electromecanico", "electromecanica", "jardinero",
    "jardinera", "lavacoches", "limpieza industrial",
    "auxiliar de limpieza", "control de entradas",
    "control de instalaciones", "conserjeria",
]

FRASES_NOCHE = [
    "turno de noche",
    "turnos de noche",
    "turno nocturno",
    "turno fijo de noche",
    "horario nocturno",
    "jornada nocturna",
    "solo noches",
    "trabajo nocturno",
    "turno de noches",
    "noche fija",
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
# UTILIDADES
# ============================================================

def normalizar(texto):
    texto = unescape(str(texto or "")).lower()
    tabla = str.maketrans(
        "áéíóúüñ",
        "aeiouun",
    )
    texto = texto.translate(tabla)
    return re.sub(r"\s+", " ", texto).strip()


def fecha_es(texto):
    if not texto:
        return None

    for formato in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(texto.strip(), formato).date()
        except ValueError:
            pass

    return None


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
                "application/xml,application/rss+xml,*/*;q=0.8"
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


def titulo_interesante(titulo):
    titulo_n = normalizar(titulo)
    return any(normalizar(p) in titulo_n for p in PUESTOS)


def fecha_publicacion(texto):
    coincidencia = re.search(
        r"(?:fecha de publicacion|fecha de publicación|"
        r"publicada el|publicado el)\s*:?\s*(\d{2}/\d{2}/\d{4})",
        texto,
        re.IGNORECASE,
    )

    if coincidencia:
        return fecha_es(coincidencia.group(1))

    return None


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

    return (
        fecha_es(inicio.group(1)) if inicio else None,
        fecha_es(fin.group(1)) if fin else None,
    )


# ============================================================
# LOCALIDAD Y SALARIO
# ============================================================

def extraer_localidad(texto):
    patrones = [
        r"Localidad de Ubicación del Puesto\s*:?\s*(.*?)"
        r"(?=\s+(?:Datos adicionales|INSCRIPCION|INSCRIPCIÓN|"
        r"Requerimientos|Requisitos|Fecha de inicio|Fecha de fin|"
        r"Datos de la oferta|Condiciones|Provincia)\b|$)",

        r"Localidad\s*:?\s*(.*?)"
        r"(?=\s+(?:Datos adicionales|INSCRIPCION|INSCRIPCIÓN|"
        r"Requerimientos|Requisitos|Fecha de inicio|Fecha de fin|"
        r"Provincia|Condiciones)\b|$)",
    ]

    for patron in patrones:
        coincidencia = re.search(
            patron,
            texto,
            re.IGNORECASE,
        )
        if coincidencia:
            valor = coincidencia.group(1).strip(" .,-")
            if valor and len(valor) < 100:
                return valor

    return ""


def extraer_salario_anual(texto):
    texto_n = normalizar(texto)

    patrones = [
        r"(\d{2,3}(?:[.\s]\d{3})+|\d{5,6})\s*euros?"
        r".{0,60}(?:anuales|al ano|anual|brutos al ano)",

        r"(\d{2,3}(?:[.\s]\d{3})+|\d{5,6})\s*€"
        r".{0,60}(?:anuales|al ano|anual|brutos al ano)",

        r"(?:salario|sueldo|retribucion)[^.;]{0,60}"
        r"(\d{2,3}(?:[.\s]\d{3})+|\d{5,6})\s*€?",
    ]

    cantidades = []

    for patron in patrones:
        for cantidad in re.findall(patron, texto_n):
            try:
                valor = int(re.sub(r"[.\s]", "", cantidad))
                if 10000 <= valor <= 150000:
                    cantidades.append(valor)
            except ValueError:
                continue

    return min(cantidades) if cantidades else None


def detectar_zona(texto):
    texto_n = normalizar(texto)

    # Se comprueban primero los nombres más largos.
    for clave, nombre in sorted(
        ZONAS.items(),
        key=lambda elemento: len(elemento[0]),
        reverse=True,
    ):
        clave_n = normalizar(clave)

        if clave_n in texto_n:
            return nombre

    return None


def comprobar_localidad(localidad, texto_completo):
    zona = detectar_zona(localidad)

    if zona:
        return zona

    # En ocasiones el municipio aparece en la descripción y no
    # en el campo específico de localidad.
    zona = detectar_zona(texto_completo)

    if zona:
        return zona

    lugar = normalizar(localidad)

    if re.fullmatch(r"murcia(?:\s*\(murcia\))?", lugar):
        salario = extraer_salario_anual(texto_completo)

        if salario is not None and salario >= 24000:
            return "Murcia capital"

    return None


def motivo_descartada(titulo, texto, fin=None):
    texto_n = normalizar(texto)
    titulo_n = normalizar(titulo)

    if fin and fin < date.today():
        return "la fecha límite ya ha pasado"

    if "vigilante de seguridad" in titulo_n:
        return "puesto de vigilante de seguridad"

    if "tip en vigor" in texto_n:
        return "exige TIP en vigor"

    if any(normalizar(frase) in texto_n for frase in FRASES_NOCHE):
        return "indica expresamente turno nocturno"

    localidad = extraer_localidad(texto)
    zona = comprobar_localidad(localidad, texto)

    if not zona:
        if localidad:
            return "localidad fuera de las zonas configuradas: " + localidad
        return "no se ha podido identificar el municipio"

    if not titulo_interesante(titulo):
        return "título fuera de los puestos buscados"

    return None


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
            "h1", "h2", "h3", "section",
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
            enlace = self.href_actual.strip()
            titulo = " ".join(self.texto_enlace).strip()

            if enlace:
                self.enlaces.append((enlace, titulo))

            self.href_actual = None
            self.texto_enlace = []

        if tag in (
            "br", "p", "div", "tr", "td", "li",
            "h1", "h2", "h3", "section",
        ):
            self.textos.append(" ")

    def texto_completo(self):
        return re.sub(
            r"\s+",
            " ",
            unescape(" ".join(self.textos)),
        ).strip()


# ============================================================
# CONSTRUIR Y FILTRAR OFERTAS
# ============================================================

def crear_oferta(identificador, titulo, url, texto, publicado=None):
    titulo = unescape((titulo or "").strip())
    texto = unescape((texto or "").strip())

    if not titulo or not url:
        return None

    inicio, fin = extraer_fechas(texto)

    motivo = motivo_descartada(titulo, texto, fin)

    if motivo:
        logger.info(
            "Oferta descartada | %s | Motivo: %s",
            titulo[:100],
            motivo,
        )
        return None

    zona = comprobar_localidad(extraer_localidad(texto), texto)

    if not zona:
        logger.info(
            "Oferta descartada | %s | Motivo: municipio no identificado",
            titulo[:100],
        )
        return None

    if not identificador:
        identificador = hashlib.sha256(
            (url + titulo).encode("utf-8")
        ).hexdigest()

    return {
        "id": str(identificador),
        "titulo": titulo,
        "zona": zona,
        "url": url,
        "inicio": inicio or publicado,
        "fin": fin,
        "texto": texto,
    }


def extraer_detalle(url, titulo_listado):
    html = descargar_pagina(url)
    lector = LectorHTML()
    lector.feed(html)
    texto = lector.texto_completo()

    titulo = titulo_listado.strip()

    if not titulo or normalizar(titulo) in (
        "ver oferta", "mas informacion", "mas info",
    ):
        coincidencia = re.search(
            r"(?:Puesto de trabajo|Ocupación|Ocupacion|Puesto)\s*:?\s*"
            r"(.{3,100}?)(?=\s+(?:Localidad|Provincia|Fecha de inicio)\b|$)",
            texto,
            re.IGNORECASE,
        )
        if coincidencia:
            titulo = coincidencia.group(1).strip()

    identificador = re.search(
        r"(?:Datos de la oferta número|Oferta)\s*:?\s*(\d{8,12})",
        texto,
        re.IGNORECASE,
    )

    id_oferta = identificador.group(1) if identificador else ""

    return crear_oferta(
        id_oferta,
        titulo,
        url,
        texto,
        fecha_publicacion(texto),
    )


# ============================================================
# FUENTE 1: SISTEMA NACIONAL DE EMPLEO
# ============================================================

async def consultar_listado_regional():
    resultados = []

    try:
        html = await asyncio.to_thread(
            descargar_pagina,
            URL_LISTADO,
        )

        lector = LectorHTML()
        lector.feed(html)

        enlaces = []
        vistos = set()

        for href, titulo in lector.enlaces:
            if "detalleOferta.do" not in href:
                continue

            url = urljoin(URL_LISTADO, href)

            if url not in vistos:
                vistos.add(url)
                enlaces.append((url, titulo))

        logger.info(
            "Listado regional: %s enlaces, %s fichas.",
            len(lector.enlaces),
            len(enlaces),
        )

        # Revisar títulos interesantes primero.
        enlaces.sort(
            key=lambda x: 0 if titulo_interesante(x[1]) else 1
        )

        for url, titulo in enlaces[:MAX_CANDIDATOS]:
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
                    "Error leyendo ficha regional %s: %s",
                    titulo[:80],
                    type(error).__name__,
                )

            await asyncio.sleep(0.15)

    except Exception as error:
        logger.warning(
            "Error en listado regional: %s: %s",
            type(error).__name__,
            str(error)[:180],
        )

    return resultados


# ============================================================
# FUENTE 2: CANALES RSS OFICIALES
# ============================================================

def texto_xml(elemento, nombre):
    nodo = elemento.find(nombre)

    if nodo is None:
        nodo = elemento.find(
            "{http://www.w3.org/2005/Atom}" + nombre
        )

    if nodo is None:
        return ""

    return " ".join(nodo.itertext()).strip()


def fecha_rss(valor):
    if not valor:
        return None

    try:
        dt = parsedate_to_datetime(valor)
        return dt.date()
    except Exception:
        return None


async def consultar_rss(canal):
    url_rss = urljoin(BASE_RSS, canal)
    resultados = []

    try:
        contenido = await asyncio.to_thread(
            descargar_pagina,
            url_rss,
        )
        raiz = ET.fromstring(contenido)

        elementos = raiz.findall(".//item")

        if not elementos:
            elementos = raiz.findall(
                ".//{http://www.w3.org/2005/Atom}entry"
            )

        logger.info(
            "RSS %s: %s entradas recibidas.",
            canal,
            len(elementos),
        )

        for item in elementos:
            titulo = texto_xml(item, "title")
            enlace = texto_xml(item, "link")

            # Atom suele utilizar href en lugar de texto.
            if not enlace:
                nodo_enlace = item.find(
                    "{http://www.w3.org/2005/Atom}link"
                )
                if nodo_enlace is not None:
                    enlace = nodo_enlace.attrib.get("href", "")

            descripcion = (
                texto_xml(item, "description")
                or texto_xml(item, "summary")
                or texto_xml(item, "content")
            )

            publicado = fecha_rss(
                texto_xml(item, "pubDate")
                or texto_xml(item, "published")
                or texto_xml(item, "updated")
            )

            if not titulo or not enlace:
                continue

            enlace = urljoin(url_rss, enlace)

            # La descripción del RSS puede contener etiquetas HTML.
            lector = LectorHTML()
            lector.feed(descripcion)
            descripcion_limpia = lector.texto_completo()

            # Si la entrada es de interés, intentamos leer su ficha
            # para recuperar localidad, requisitos y fecha límite.
            if titulo_interesante(titulo):
                try:
                    oferta_detalle = await asyncio.to_thread(
                        extraer_detalle,
                        enlace,
                        titulo,
                    )
                    if oferta_detalle:
                        resultados.append(oferta_detalle)
                        continue
                except Exception as error:
                    logger.info(
                        "No se pudo abrir detalle RSS (%s): %s",
                        titulo[:70],
                        type(error).__name__,
                    )

            texto_completo = titulo + " " + descripcion_limpia

            if not titulo_interesante(titulo):
                continue

            motivo = motivo_descartada(
                titulo,
                texto_completo,
                None,
            )

            if motivo:
                logger.info(
                    "RSS descartado | %s | Motivo: %s",
                    titulo[:100],
                    motivo,
                )
                continue

            zona = comprobar_localidad(
                extraer_localidad(texto_completo),
                texto_completo,
            )

            if not zona:
                logger.info(
                    "RSS descartado | %s | Motivo: municipio no identificado",
                    titulo[:100],
                )
                continue

            identificador = hashlib.sha256(
                (enlace + titulo).encode("utf-8")
            ).hexdigest()

            resultados.append({
                "id": identificador,
                "titulo": titulo,
                "zona": zona,
                "url": enlace,
                "inicio": publicado,
                "fin": None,
                "texto": texto_completo,
            })

    except Exception as error:
        logger.warning(
            "No se pudo consultar RSS %s: %s: %s",
            canal,
            type(error).__name__,
            str(error)[:150],
        )

    return resultados


# ============================================================
# BUSQUEDA CONJUNTA
# ============================================================

async def consultar_ofertas():
    logger.info("Iniciando búsqueda de empleo en fuentes gratuitas.")
    tareas = [
        consultar_listado_regional(),
    ]

    for canal in CANALES_RSS:
        tareas.append(consultar_rss(canal))

    respuestas = await asyncio.gather(
        *tareas,
        return_exceptions=True,
    )

    resultados = []
    for respuesta in respuestas:
        if isinstance(respuesta, Exception):
            logger.warning(
                "Una fuente falló: %s",
                type(respuesta).__name__,
            )
            continue

        resultados.extend(respuesta)

    unicas = {}
    for oferta in resultados:
        unicas[oferta["id"]] = oferta

    resultados = list(unicas.values())
    resultados.sort(
        key=lambda oferta: oferta["inicio"] or date.min,
        reverse=True,
    )

    logger.info(
        "Resumen conjunto: %s ofertas válidas únicas.",
        len(resultados),
    )

    return resultados


# ============================================================
# MENSAJES TELEGRAM
# ============================================================

def crear_mensaje(oferta):
    inicio = (
        oferta["inicio"].strftime("%d/%m/%Y")
        if oferta.get("inicio")
        else "No indicada"
    )

    fin = (
        oferta["fin"].strftime("%d/%m/%Y")
        if oferta.get("fin")
        else "Consultar en el anuncio"
    )

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
        "📅 Publicada: " + inicio,
        "⏳ Fecha límite: " + fin,
        "💶 Salario: " + salario_texto,
    ]

    if requisitos_texto:
        partes.extend(["", "📋 Requisitos:", requisitos_texto])

    partes.extend([
        "",
        "🔗 Inscripción y detalles:",
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
        "Buscaré ofertas en varias fuentes gratuitas.\n\n"
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
    global BUSQUEDA_EN_CURSO

    if BUSQUEDA_EN_CURSO:
        await update.message.reply_text(
            "⏳ Ya hay una búsqueda en marcha. Espera a que termine."
        )
        return

    BUSQUEDA_EN_CURSO = True
    mensaje = await update.message.reply_text(
        "🔎 Buscando ofertas en varias fuentes gratuitas. "
        "Puede tardar un poco..."
    )

    try:
        ofertas = await consultar_ofertas()

        if not ofertas:
            await mensaje.edit_text(
                "No he encontrado ofertas que cumplan los filtros.\n\n"
                "Se han consultado el listado regional y los canales RSS "
                "gratuitos. Puede que las fuentes no publiquen ahora "
                "ofertas compatibles con tu zona y tus puestos."
            )
            return

        await mensaje.edit_text(
            f"✅ He encontrado {len(ofertas)} ofertas que pasan los filtros. "
            f"Te envío hasta {min(len(ofertas), MAX_OFERTAS_ENVIO)}."
        )

        for oferta in ofertas[:MAX_OFERTAS_ENVIO]:
            await enviar_oferta(
                context.bot,
                update.effective_chat.id,
                oferta,
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
        "📚 FUENTES CONSULTADAS\n\n"
        "1. Sistema Nacional de Empleo:\n"
        + URL_LISTADO
        + "\n\n2. Canales RSS oficiales del Portal Único de Empleo:\n"
        + BASE_RSS
        + "\n\nNo se utilizan créditos de JOA ni APIs de pago."
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
        "mantenimiento, limpieza, industria y puestos similares.\n\n"
        "Se descartan los anuncios que mencionen expresamente turnos "
        "nocturnos y los puestos de vigilante de seguridad."
    )


async def comando_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 COMANDOS\n\n"
        "/start - Activar avisos\n"
        "/buscar - Consultar ofertas ahora\n"
        "/scan - Consultar ofertas ahora\n"
        "/ofertas - Consultar ofertas ahora\n"
        "/portales - Ver fuentes\n"
        "/preferencias - Ver filtros\n"
        "/parar - Desactivar avisos\n"
        "/ayuda - Mostrar ayuda"
    )


# ============================================================
# AVISOS AUTOMATICOS
# ============================================================

async def revisar_nuevas(application):
    chats = suscriptores()

    if not chats:
        logger.info("No hay suscriptores activos. Envía /start en Telegram.")
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
                    "No se pudo enviar una oferta al chat %s.",
                    chat_id,
                )

        if enviada_a_alguien:
            enviadas_correctamente.append(oferta["id"])

        await asyncio.sleep(0.4)

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
        "fuentes": [
            "Sistema Nacional de Empleo",
            "Canales RSS oficiales",
        ],
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

    logger.info("Bot iniciado. Fuentes gratuitas de empleo.")
    logger.info("No se utilizan créditos de JOA.")

    aplicacion.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
