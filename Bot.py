
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

# Fuentes complementarias gratuitas: páginas públicas de ofertas.
URLS_EXTERNAS = [
    "https://activa.sanjavier.es/empleo",
    "https://www.interempleo.es/es/ofertas-de-empleo/carretillerosas-zona-san-javier",
    # Indeed devuelve HTTP 403 desde el servidor; se conserva fuera de la consulta
    # automática para evitar perder tiempo en cada búsqueda.
    "https://jobtoday.com/es/trabajos-transporte-y-reparto/san-javier",
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
    "jefe de produccion", "jefa de produccion",
    "tecnico de produccion", "tecnica de produccion",
    "técnico de producción", "técnica de producción",
    "director de produccion", "directora de produccion",
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
        conexion.execute("""
            CREATE TABLE IF NOT EXISTS ofertas_guardadas (
                id TEXT PRIMARY KEY,
                titulo TEXT NOT NULL,
                zona TEXT,
                url TEXT NOT NULL,
                inicio TEXT,
                fin TEXT,
                texto TEXT,
                actualizada TEXT NOT NULL
            )
        """)
        # Limpia de la base las páginas de categoría que versiones anteriores
        # guardaron por error como si fueran ofertas individuales.
        conexion.execute("""
            DELETE FROM ofertas_guardadas
            WHERE lower(url) LIKE '%jobtoday.com/es/trabajos-%'
               OR lower(url) LIKE '%indeed.%/q-%'
               OR lower(url) LIKE '%indeed.%/ofertas-en-%'
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


def guardar_ofertas(ofertas):
    ahora = datetime.now().isoformat()
    filas = []
    for oferta in ofertas:
        inicio = oferta["inicio"].isoformat() if oferta.get("inicio") else None
        fin = oferta["fin"].isoformat() if oferta.get("fin") else None
        filas.append((
            oferta["id"], oferta["titulo"], oferta.get("zona", ""),
            oferta["url"], inicio, fin, oferta.get("texto", ""), ahora,
        ))
    if filas:
        with db() as conexion:
            conexion.executemany("""
                INSERT INTO ofertas_guardadas
                (id, titulo, zona, url, inicio, fin, texto, actualizada)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    titulo=excluded.titulo,
                    zona=excluded.zona,
                    url=excluded.url,
                    inicio=excluded.inicio,
                    fin=excluded.fin,
                    texto=excluded.texto,
                    actualizada=excluded.actualizada
            """, filas)


def obtener_ultimas_ofertas(limite=10):
    with db() as conexion:
        return conexion.execute("""
            SELECT titulo, zona, url, inicio, fin, texto, actualizada
            FROM ofertas_guardadas
            ORDER BY actualizada DESC
            LIMIT ?
        """, (limite,)).fetchall()


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


def es_enlace_listado_generico(url):
    """Evita guardar páginas de búsqueda/categoría como si fueran vacantes."""
    parsed = urlparse(url)
    ruta = normalizar(parsed.path).rstrip("/")
    dominio = parsed.netloc.lower().replace("www.", "")

    # En JobToday, /trabajos-... son categorías/listados, no fichas individuales.
    if "jobtoday.com" in dominio and (
        "/trabajos-" in ruta or ruta.endswith("/trabajos")
    ):
        return True

    # Páginas de resultados de Indeed.
    if "indeed." in dominio and (
        "/q-" in ruta or "/ofertas-en-" in ruta or "/jobs" == ruta
    ):
        return True

    # Páginas iniciales/categorías de los portales que usamos como fuente.
    if ruta in ("/empleo", "/es/ofertas-de-empleo/carretillerosas-zona-san-javier"):
        return True

    return False


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
    t = normalizar(titulo).strip()

    if not t or any(normalizar(p) in t for p in TITULOS_EXCLUIDOS):
        return False

    # Algunos portales escriben el género con barra: "mozo/a de almacén".
    if "mozo/a de almacen" in t or "operario/a de almacen" in t:
        return True

    # Excluir cargos de responsabilidad o perfiles técnicos especializados.
    excluidos_especializados = (
        "jefe de produccion", "jefa de produccion",
        "tecnico de produccion", "tecnica de produccion",
        "director de produccion", "directora de produccion",
        "responsable de produccion", "encargado de produccion",
        "encargada de produccion", "supervisor de produccion",
        "supervisora de produccion", "ingeniero de produccion",
        "ingeniera de produccion",
    )
    if any(puesto in t for puesto in excluidos_especializados):
        return False

    if "administrativ" in t:
        return False

    # No usar palabras genéricas aisladas como "producción" o "operario"
    # para aceptar títulos de puestos que no son realmente compatibles.
    palabras_demasiado_genericas = {
        "produccion", "operario", "operaria", "almacen", "reparto",
        "logistica", "fabrica", "supermercado",
    }
    puestos_especificos = [
        normalizar(p) for p in PUESTOS
        if normalizar(p) not in palabras_demasiado_genericas
    ]
    if any(puesto in t for puesto in puestos_especificos):
        return True

    # Algunos títulos sencillos sí son válidos por sí solos.
    return t in {
        "operario", "operaria", "mozo de almacen", "moza de almacen",
        "auxiliar de almacen", "operario de almacen", "operaria de almacen",
        "reparto", "logistica", "almacen",
    }


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
            html = await asyncio.to_thread(descargar_pagina, url_pagina)
        except Exception:
            logger.warning("No se pudo consultar listado oficial: %s", url_pagina)
            continue

        lector = LectorHTML()
        lector.feed(html)

        for href, titulo in lector.enlaces:
            url = urljoin(url_pagina, href)
            if "detalleOferta.do" in href:
                fichas[url] = {"titulo": titulo, "fuente": "SEF"}
            elif es_pagina_resultados(href, url_pagina):
                if url not in visitadas and url not in pendientes:
                    pendientes.append(url)

        logger.info("Listado oficial %s/%s; fichas: %s",
                    len(visitadas), MAX_PAGINAS, len(fichas))

    # Añadir enlaces de portales gratuitos; cada página se procesa por separado.
    for url_pagina in URLS_EXTERNAS:
        try:
            html = await asyncio.to_thread(descargar_pagina, url_pagina)
            lector = LectorHTML()
            lector.feed(html)
            encontrados = 0
            for href, titulo in lector.enlaces:
                titulo_limpio = " ".join((titulo or "").split())
                url = urljoin(url_pagina, href)
                dominio = urlparse(url).netloc.lower()
                if not titulo_limpio or not titulo_interesante(titulo_limpio):
                    continue
                if dominio not in urlparse(url_pagina).netloc.lower() and "indeed." not in dominio:
                    continue
                # No tratar una categoría o página de resultados como una vacante.
                if es_enlace_listado_generico(url):
                    continue
                if url == url_pagina or url in fichas:
                    continue
                if any(x in url.lower() for x in ("login", "signin", "register", "politica", "privacidad")):
                    continue
                if len(titulo_limpio) > 120:
                    continue
                fichas[url] = {"titulo": titulo_limpio, "fuente": "externa"}
                encontrados += 1
            logger.info("Fuente externa %s: %s enlaces compatibles", url_pagina, encontrados)
        except Exception as exc:
            logger.warning("Fuente externa no disponible %s: %s", url_pagina, exc)

    return [(url, datos["titulo"], datos["fuente"]) for url, datos in fichas.items()]


def extraer_detalle_externo(url, titulo_listado):
    if es_enlace_listado_generico(url):
        logger.info("Descartada página genérica de búsqueda: %s", url)
        return None

    html = descargar_pagina(url)
    lector = LectorHTML()
    lector.feed(html)
    texto = lector.texto_completo()
    combinado = normalizar(titulo_listado + " " + texto)

    if not titulo_interesante(titulo_listado):
        return None

    # Localidad: no se aceptan ofertas si no aparece claramente una zona permitida.
    zonas = [
        ("san javier", "San Javier"),
        ("san pedro del pinatar", "San Pedro del Pinatar"),
        ("los alcazares", "Los Alcázares"),
        ("torre-pacheco", "Torre-Pacheco"),
        ("torre pacheco", "Torre-Pacheco"),
        ("cartagena", "Cartagena"),
    ]
    zona = next((nombre for clave, nombre in zonas if clave in combinado), None)
    if not zona:
        return None

    if any(normalizar(frase) in combinado for frase in FRASES_NOCHE):
        return None
    if "tip en vigor" in combinado:
        return None
    # El usuario tiene permiso B: no enviar puestos que exigen permisos profesionales superiores.
    if re.search(r"carnet(?: de conducir)?\s*(?:c\+e|c1|c|d|adr)|permiso de conducir\s*(?:c\+e|c1|c|d)|carnet c\s*\+\s*cap|cap en vigor", combinado):
        return None
    if re.search(r"desemplead[oa]s? de larga duracion|parad[oa]s? de larga duracion|"
                 r"estar desemplead[oa]s?|inscrit[oa]s? como demandante de empleo.{0,100}"
                 r"(?:obligatori|imprescindible|requisito)", combinado):
        return None

    inicio = None
    coincidencia = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", texto)
    if coincidencia:
        try:
            inicio = date(int(coincidencia.group(3)), int(coincidencia.group(2)),
                          int(coincidencia.group(1)))
        except ValueError:
            inicio = None

    # El enlace se mantiene para comprobar que la oferta sigue activa.
    identificador = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return {
        "id": identificador,
        "titulo": titulo_listado,
        "zona": zona,
        "url": url,
        "inicio": inicio,
        "fin": None,
        "texto": texto,
    }


def extraer_ofertas_activa():
    """
    Activa San Javier publica las vacantes dentro del texto de una sola página,
    no como enlaces individuales con el título de cada puesto. Se extraen esas
    fichas de texto para que no se queden fuera del bot.
    """
    url = "https://activa.sanjavier.es/empleo"
    html = descargar_pagina(url)
    lector = LectorHTML()
    lector.feed(html)
    texto = lector.texto_completo()
    partes = re.split(r"\s+DESCRIPCION DEL PUESTO:\s*", texto, flags=re.IGNORECASE)
    ofertas = []

    for indice in range(1, len(partes)):
        prefijo = partes[indice - 1]
        titulo = prefijo.rsplit("SOLICITAR INSCRIPCIÓN", 1)[-1]
        # El título suele ser el último encabezado antes de la descripción.
        titulo = " ".join(titulo.split()).strip(" :-")
        if indice == 1:
            coincidencia_titulo = re.search(
                r"([A-ZÁÉÍÓÚÜÑ0-9 /().-]{4,90})$",
                titulo,
            )
            if coincidencia_titulo:
                titulo = coincidencia_titulo.group(1).strip(" :-")

        bloque = partes[indice]
        # El bloque de una oferta termina en su fecha de actualización.
        bloque_actual = re.split(
            r"Actualización de la oferta de empleo:",
            bloque,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        fecha_match = re.search(
            r"Actualización de la oferta de empleo:\s*(\d{1,2} de [a-záéíóú]+ de \d{4}|\d{1,2}/\d{1,2}/\d{4})",
            bloque,
            re.IGNORECASE,
        )
        if not titulo_interesante(titulo):
            continue

        combinado = normalizar(titulo + " " + bloque_actual)
        zonas = [
            ("san javier", "San Javier"),
            ("san pedro del pinatar", "San Pedro del Pinatar"),
            ("los alcazares", "Los Alcázares"),
            ("torre-pacheco", "Torre-Pacheco"),
            ("torre pacheco", "Torre-Pacheco"),
            ("cartagena", "Cartagena"),
        ]
        zona = next((nombre for clave, nombre in zonas if clave in combinado), None)
        if not zona:
            continue
        if any(normalizar(frase) in combinado for frase in FRASES_NOCHE):
            continue
        if re.search(
            r"carnet(?: de conducir)?\s*(?:c\+e|c1|c|d|adr)|"
            r"permiso de conducir\s*(?:c\+e|c1|c|d)|cap en vigor",
            combinado,
        ):
            continue

        inicio = None
        if fecha_match:
            fecha_txt = fecha_match.group(1)
            try:
                if "/" in fecha_txt:
                    inicio = fecha_es(fecha_txt)
                else:
                    inicio = datetime.strptime(fecha_txt.lower(), "%d de %B de %Y").date()
            except ValueError:
                inicio = None

        identificador = hashlib.sha256(
            (titulo + "|" + zona + "|" + (fecha_match.group(1) if fecha_match else "")).encode("utf-8")
        ).hexdigest()
        ofertas.append({
            "id": identificador,
            "titulo": titulo,
            "zona": zona,
            "url": url,
            "inicio": inicio,
            "fin": None,
            "texto": bloque_actual,
        })

    return ofertas


async def consultar_ofertas():
    logger.info("Iniciando consulta de ofertas multifuente.")
    try:
        ofertas_activa = await asyncio.to_thread(extraer_ofertas_activa)
        logger.info("Activa San Javier: %s ofertas compatibles extraídas del texto.", len(ofertas_activa))
    except Exception:
        logger.exception("No se pudieron extraer las ofertas de Activa San Javier.")
        ofertas_activa = []
    enlaces = await recoger_enlaces_listado()
    if not enlaces and not ofertas_activa:
        logger.warning("Ningún portal ha devuelto ofertas compatibles.")
        return []

    enlaces.sort(key=lambda elemento: 0 if titulo_interesante(elemento[1]) else 1)
    resultados = list(ofertas_activa)
    conteo_fuente = {}
    fallos_fuente = {}

    for url, titulo, fuente in enlaces[:MAX_CANDIDATOS]:
        nombre_fuente = "Sistema Nacional de Empleo" if fuente == "SEF" else urlparse(url).netloc.replace("www.", "")
        conteo_fuente.setdefault(nombre_fuente, {"candidatos": 0, "validas": 0, "descartadas": 0, "errores": 0})
        conteo_fuente[nombre_fuente]["candidatos"] += 1
        try:
            if fuente == "SEF":
                oferta = await asyncio.to_thread(extraer_detalle, url, titulo)
            else:
                oferta = await asyncio.to_thread(extraer_detalle_externo, url, titulo)
            if oferta:
                resultados.append(oferta)
                conteo_fuente[nombre_fuente]["validas"] += 1
            else:
                conteo_fuente[nombre_fuente]["descartadas"] += 1
        except Exception as exc:
            conteo_fuente[nombre_fuente]["errores"] += 1
            fallos_fuente.setdefault(nombre_fuente, []).append(type(exc).__name__ + ": " + str(exc)[:100])
            logger.warning("No se pudo leer la oferta %s", url, exc_info=True)
        await asyncio.sleep(0.15)

    for nombre, cuenta in conteo_fuente.items():
        logger.info("RESULTADO FUENTE | %s | candidatos=%s válidas=%s descartadas=%s errores=%s",
                    nombre, cuenta["candidatos"], cuenta["validas"], cuenta["descartadas"], cuenta["errores"])
        for fallo in fallos_fuente.get(nombre, [])[:2]:
            logger.info("ERROR FUENTE | %s | %s", nombre, fallo)

    unicas = {oferta["id"]: oferta for oferta in resultados}
    ofertas = list(unicas.values())
    ofertas.sort(key=lambda oferta: oferta["inicio"] or date.min, reverse=True)
    logger.info("Consulta multifuente terminada: enlaces=%s; ofertas válidas=%s.",
                min(len(enlaces), MAX_CANDIDATOS), len(ofertas))
    guardar_ofertas(ofertas)
    return ofertas



async def comando_diagnostico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comprueba qué devuelve cada página sin aplicar todavía los filtros finales."""
    await update.message.reply_text(
        "🧪 Comprobando las páginas de empleo. Puede tardar hasta un minuto..."
    )
    lineas = ["🧪 DIAGNÓSTICO DE FUENTES"]
    fuentes = [("Sistema Nacional de Empleo", URL_LISTADO)] + [
        (urlparse(url).netloc.replace("www.", ""), url)
        for url in URLS_EXTERNAS
    ]

    for nombre, url in fuentes:
        try:
            html = await asyncio.to_thread(descargar_pagina, url)
            lector = LectorHTML()
            lector.feed(html)
            enlaces = [
                (href, titulo.strip())
                for href, titulo in lector.enlaces
                if href and titulo.strip()
            ]
            candidatos = [
                (href, titulo) for href, titulo in enlaces
                if titulo_interesante(titulo)
                and not es_enlace_listado_generico(urljoin(url, href))
            ]
            texto = lector.texto_completo().lower()
            bloqueos = any(p in texto for p in (
                "captcha", "verify you are human", "enable javascript",
                "robot check", "access denied", "unusual traffic"
            ))
            estado = "posible bloqueo/JavaScript" if bloqueos else "página leída"
            lineas.append(
                f"\n• {nombre}: {len(enlaces)} enlaces con texto; "
                f"{len(candidatos)} títulos compatibles; {estado}"
            )
        except Exception as exc:
            lineas.append(
                f"\n• {nombre}: ERROR {type(exc).__name__}: "
                f"{str(exc)[:100]}"
            )

    lineas.append(
        "\nSi una fuente muestra 0 enlaces o 0 títulos compatibles, "
        "el bot no está obteniendo ofertas de esa página."
    )
    respuesta = "\n".join(lineas)
    for inicio in range(0, len(respuesta), 3800):
        await update.message.reply_text(respuesta[inicio:inicio + 3800])

# ============================================================
# MENSAJES
# ============================================================

def crear_mensaje(oferta):
    inicio = (
        oferta["inicio"].strftime("%d/%m/%Y")
        if oferta["inicio"] else "No indicada"
    )
    fin = oferta["fin"].strftime("%d/%m/%Y") if oferta.get("fin") else "Consultar vigencia en el portal"

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
        "🌙 Sin ofertas que indiquen expresamente turno nocturno.\n"
        "🌐 Búsqueda ampliada en varios portales gratuitos.\n\n"
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


async def comando_ultimas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ofertas = obtener_ultimas_ofertas(10)
    if not ofertas:
        await update.message.reply_text(
            "Todavía no tengo ofertas guardadas. Usa /buscar para hacer una búsqueda."
        )
        return

    lineas = ["📋 ÚLTIMAS OFERTAS ENCONTRADAS"]
    for i, (titulo, zona, url, inicio, fin, texto, actualizada) in enumerate(ofertas, 1):
        fecha = ""
        if inicio:
            try:
                fecha = datetime.fromisoformat(inicio).strftime("%d/%m/%Y")
            except ValueError:
                fecha = inicio
        salario = extraer_salario_anual(texto or "")
        salario_txt = f"{salario:,} € brutos/año".replace(",", ".") if salario else "sueldo no publicado"
        lineas.append(
            f"\n{i}. {titulo}\n📍 {zona or 'Zona no indicada'}"
            + (f" · 📅 {fecha}" if fecha else "")
            + f"\n💶 {salario_txt}\n🔗 {url}"
        )

    respuesta = "\n".join(lineas)
    for inicio_bloque in range(0, len(respuesta), 3800):
        await update.message.reply_text(respuesta[inicio_bloque:inicio_bloque + 3800],
                                        disable_web_page_preview=True)


async def comando_portales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Fuentes gratuitas: Sistema Nacional de Empleo, Indeed, Job Today, Activa San Javier e Interempleo.\n\n"
        "Cada oferta incluye un enlace a su página original para comprobar si sigue activa.\n\n"
        "Esta consulta no utiliza créditos de JOA."
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
        "/ultimas - Ver las últimas ofertas encontradas\n"
        "/portales - Ver fuente\n"        "/diagnostico - Comprobar qué lee cada portal\n"
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
    aplicacion.add_handler(CommandHandler("ultimas", comando_ultimas))
    aplicacion.add_handler(CommandHandler("portales", comando_portales))
    aplicacion.add_handler(CommandHandler("diagnostico", comando_diagnostico))
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
