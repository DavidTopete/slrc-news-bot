import os
import re
import json
import time
import html
import logging
import unicodedata
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from difflib import SequenceMatcher
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURACION
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

log = logging.getLogger("slrc_news_bot")

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

ARCHIVO_ENVIADAS = "noticias_enviadas.json"

# San Luis Rio Colorado / Sonora no usa horario de verano.
TZ = ZoneInfo("America/Hermosillo")

# El bot corre una vez al dia a las 04:00.
# Se revisan 30 horas para cubrir todo el dia anterior y dejar
# un pequeno traslape. El historial evita volver a enviar noticias.
VENTANA_HORAS = 30

MAX_NOTICIAS_POR_CORRIDA = 15
MAX_HISTORIAL = 500
UMBRAL_SIMILITUD_TITULO = 0.84

# Google News RSS funciona mejor desde GitHub Actions que hacer scraping
# directo de sitios que pueden bloquear robots/runners.
CONSULTAS_GOOGLE_NEWS = [
    '"San Luis Río Colorado" when:2d',
    '"San Luis Rio Colorado" when:2d',
    '"SLRC" Sonora when:2d',
    '"Golfo de Santa Clara" when:2d',
    '"Luis B. Sánchez" Sonora when:2d',
    '"Luis B Sanchez" Sonora when:2d',
]

# Mantiene el enfoque original del bot.
# Si mas adelante quieres aceptar otras fuentes, agrega nombres aqui.
FUENTES_PERMITIDAS = (
    "tribuna de san luis",
    "el imparcial",
)

PALABRAS_LOCALES = (
    "san luis rio colorado",
    "san luis río colorado",
    "slrc",
    "san luis sonora",
    "san luis r c",
    "golfo de santa clara",
    "luis b sanchez",
    "luis b. sanchez",
    "luis b sánchez",
    "luis b. sánchez",
    "valle de san luis",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
}


# ============================================================
# HTTP
# ============================================================

def crear_sesion():
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)

    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


SESSION = crear_sesion()


# ============================================================
# UTILIDADES
# ============================================================

def ahora_slrc():
    return datetime.now(TZ)


def normalizar_texto(texto):
    texto = str(texto or "").strip().lower()

    # Quita acentos para comparar sin problemas.
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))

    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()

    return texto


def titulo_parecido(titulo_1, titulo_2):
    a = normalizar_texto(titulo_1)
    b = normalizar_texto(titulo_2)

    if not a or not b:
        return False

    return SequenceMatcher(None, a, b).ratio() >= UMBRAL_SIMILITUD_TITULO


def quitar_fuente_del_titulo(titulo, fuente):
    """
    Google News suele devolver:
        Titulo de la noticia - Tribuna de San Luis

    Aqui se elimina el sufijo para guardar/comparar un titulo limpio.
    """
    titulo = (titulo or "").strip()
    fuente = (fuente or "").strip()

    if fuente:
        sufijo = f" - {fuente}"
        if titulo.lower().endswith(sufijo.lower()):
            titulo = titulo[:-len(sufijo)].strip()

    return titulo


def fuente_permitida(fuente):
    fuente_normalizada = normalizar_texto(fuente)
    return any(
        normalizar_texto(nombre) in fuente_normalizada
        for nombre in FUENTES_PERMITIDAS
    )


def es_noticia_local(titulo):
    texto = normalizar_texto(titulo)

    return any(
        normalizar_texto(clave) in texto
        for clave in PALABRAS_LOCALES
    )


# ============================================================
# HISTORIAL JSON
# ============================================================

def historial_vacio():
    return {
        "ultima_ejecucion": None,
        "ultimo_total_encontrado": 0,
        "ultimo_total_enviado": 0,
        "links": [],
        "titulos": [],
    }


def cargar_historial():
    if not os.path.exists(ARCHIVO_ENVIADAS):
        log.info("No existe historial. Se creara uno nuevo.")
        return historial_vacio()

    try:
        with open(ARCHIVO_ENVIADAS, "r", encoding="utf-8") as archivo:
            data = json.load(archivo)

        if not isinstance(data, dict):
            raise ValueError("El JSON no contiene un objeto valido.")

        base = historial_vacio()
        base.update(data)

        if not isinstance(base.get("links"), list):
            base["links"] = []

        if not isinstance(base.get("titulos"), list):
            base["titulos"] = []

        return base

    except Exception as error:
        log.error(f"No se pudo leer {ARCHIVO_ENVIADAS}: {error}")

        # No elimina el archivo defectuoso; lo respalda.
        if os.path.exists(ARCHIVO_ENVIADAS):
            respaldo = f"{ARCHIVO_ENVIADAS}.bak_{int(time.time())}"
            try:
                os.replace(ARCHIVO_ENVIADAS, respaldo)
                log.warning(f"Historial defectuoso respaldado como: {respaldo}")
            except OSError as error_respaldo:
                log.error(f"No se pudo crear respaldo: {error_respaldo}")

        return historial_vacio()


def guardar_historial(historial):
    """
    Escritura atomica:
    primero escribe un archivo temporal y luego lo reemplaza.
    Asi se evita dejar un JSON incompleto si el proceso se interrumpe.
    """
    historial["links"] = historial.get("links", [])[-MAX_HISTORIAL:]
    historial["titulos"] = historial.get("titulos", [])[-MAX_HISTORIAL:]

    temporal = f"{ARCHIVO_ENVIADAS}.tmp"

    with open(temporal, "w", encoding="utf-8") as archivo:
        json.dump(
            historial,
            archivo,
            ensure_ascii=False,
            indent=2,
        )
        archivo.write("\n")

    os.replace(temporal, ARCHIVO_ENVIADAS)

    log.info(
        "Historial guardado: %s links / %s titulos",
        len(historial["links"]),
        len(historial["titulos"]),
    )


def ya_fue_enviada(noticia, historial):
    if noticia["link"] in historial["links"]:
        return True

    for titulo_guardado in historial["titulos"]:
        if titulo_parecido(noticia["titulo"], titulo_guardado):
            return True

    return False


def registrar_noticia(noticia, historial):
    if noticia["link"] not in historial["links"]:
        historial["links"].append(noticia["link"])

    if noticia["titulo"] not in historial["titulos"]:
        historial["titulos"].append(noticia["titulo"])


# ============================================================
# GOOGLE NEWS RSS
# ============================================================

def construir_url_google_news(consulta):
    # hl = idioma
    # gl = pais
    # ceid = region/idioma
    return (
        "https://news.google.com/rss/search"
        f"?q={quote_plus(consulta)}"
        "&hl=es-419"
        "&gl=MX"
        "&ceid=MX:es-419"
    )


def parsear_fecha_rss(fecha_raw):
    if not fecha_raw:
        return None

    try:
        fecha = parsedate_to_datetime(fecha_raw)

        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=TZ)

        return fecha.astimezone(TZ)

    except Exception as error:
        log.debug(f"No se pudo interpretar fecha RSS '{fecha_raw}': {error}")
        return None


def fecha_en_ventana(fecha):
    if fecha is None:
        return False

    limite = ahora_slrc() - timedelta(hours=VENTANA_HORAS)

    return limite <= fecha <= ahora_slrc() + timedelta(minutes=10)


def leer_rss_google_news(consulta):
    url = construir_url_google_news(consulta)

    log.info(f"Consultando Google News: {consulta}")

    try:
        response = SESSION.get(url, timeout=25)

        if response.status_code != 200:
            log.warning(
                "Google News respondio HTTP %s para consulta: %s",
                response.status_code,
                consulta,
            )
            return []

        root = ET.fromstring(response.content)

        noticias = []

        for item in root.findall(".//item"):
            titulo_raw = item.findtext("title", default="").strip()
            link = item.findtext("link", default="").strip()
            pub_date = item.findtext("pubDate", default="").strip()

            source_tag = item.find("source")
            fuente = ""

            if source_tag is not None and source_tag.text:
                fuente = source_tag.text.strip()

            titulo = quitar_fuente_del_titulo(titulo_raw, fuente)
            fecha = parsear_fecha_rss(pub_date)

            if not titulo or not link:
                continue

            # Mantener solamente las fuentes originales del bot.
            if not fuente_permitida(fuente):
                continue

            # La consulta ya es local, pero se hace un segundo filtro.
            if not es_noticia_local(titulo):
                continue

            if not fecha_en_ventana(fecha):
                continue

            noticias.append(
                {
                    "titulo": titulo,
                    "link": link,
                    "fuente": fuente or "Fuente no identificada",
                    "fecha": fecha,
                }
            )

        log.info(
            "Consulta '%s': %s noticias locales recientes",
            consulta,
            len(noticias),
        )

        return noticias

    except ET.ParseError as error:
        log.error(f"RSS XML invalido para '{consulta}': {error}")
        return []

    except requests.RequestException as error:
        log.error(f"Error de red consultando '{consulta}': {error}")
        return []

    except Exception as error:
        log.exception(f"Error inesperado consultando '{consulta}': {error}")
        return []


def eliminar_duplicados(noticias):
    unicas = []

    # Mas recientes primero.
    noticias_ordenadas = sorted(
        noticias,
        key=lambda n: n.get("fecha") or datetime.min.replace(tzinfo=TZ),
        reverse=True,
    )

    for noticia in noticias_ordenadas:
        duplicada = False

        for existente in unicas:
            if noticia["link"] == existente["link"]:
                duplicada = True
                break

            if titulo_parecido(noticia["titulo"], existente["titulo"]):
                duplicada = True
                break

        if not duplicada:
            unicas.append(noticia)

    return unicas


def obtener_noticias(historial):
    candidatas = []

    for consulta in CONSULTAS_GOOGLE_NEWS:
        candidatas.extend(leer_rss_google_news(consulta))
        time.sleep(0.5)

    candidatas = eliminar_duplicados(candidatas)

    nuevas = []

    for noticia in candidatas:
        if ya_fue_enviada(noticia, historial):
            log.info(f"Ya enviada, se omite: {noticia['titulo']}")
            continue

        nuevas.append(noticia)

    log.info(
        "Resultado final: %s candidatas unicas / %s nuevas",
        len(candidatas),
        len(nuevas),
    )

    return nuevas


# ============================================================
# TELEGRAM
# ============================================================

def validar_telegram(response):
    try:
        data = response.json()
    except ValueError:
        log.error(
            "Telegram devolvio una respuesta no JSON. HTTP %s: %s",
            response.status_code,
            response.text[:500],
        )
        return False

    if response.status_code != 200 or not data.get("ok"):
        log.error(
            "Telegram rechazo el mensaje. HTTP %s: %s",
            response.status_code,
            data,
        )
        return False

    return True


def enviar_mensaje_telegram(texto, preview=True):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    try:
        response = SESSION.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": not preview,
            },
            timeout=25,
        )

        return validar_telegram(response)

    except requests.RequestException as error:
        log.error(f"Error de red enviando Telegram: {error}")
        return False


def enviar_encabezado(total):
    ahora = ahora_slrc()

    mensaje = (
        "<b>SAN LUIS RÍO COLORADO NOTICIAS</b>\n"
        f"<b>Fecha:</b> {html.escape(ahora.strftime('%d/%m/%Y'))}\n"
        f"<b>Noticias nuevas:</b> {total}"
    )

    return enviar_mensaje_telegram(mensaje, preview=False)


def enviar_noticia(noticia):
    titulo = html.escape(noticia["titulo"])
    fuente = html.escape(noticia["fuente"])
    link = html.escape(noticia["link"], quote=True)

    fecha = noticia.get("fecha")
    if fecha:
        fecha_texto = fecha.astimezone(TZ).strftime("%d/%m/%Y %H:%M")
    else:
        fecha_texto = "Sin fecha"

    mensaje = (
        f"<b>{titulo}</b>\n"
        f"<b>Fuente:</b> {fuente}\n"
        f"<b>Publicada:</b> {html.escape(fecha_texto)}\n"
        f'<a href="{link}">Abrir noticia</a>'
    )

    exito = enviar_mensaje_telegram(mensaje, preview=True)

    if exito:
        log.info(f"Enviada correctamente: {noticia['titulo']}")
    else:
        log.warning(f"NO enviada: {noticia['titulo']}")

    return exito


# ============================================================
# MAIN
# ============================================================

def main():
    if not TOKEN:
        raise RuntimeError(
            "Falta el secret TOKEN. Configuralo en GitHub > Settings > "
            "Secrets and variables > Actions."
        )

    if not CHAT_ID:
        raise RuntimeError(
            "Falta el secret CHAT_ID. Configuralo en GitHub > Settings > "
            "Secrets and variables > Actions."
        )

    log.info("=" * 60)
    log.info("INICIO SLRC NEWS BOT")
    log.info("Hora SLRC: %s", ahora_slrc().isoformat())
    log.info("Ventana de busqueda: ultimas %s horas", VENTANA_HORAS)
    log.info("=" * 60)

    historial = cargar_historial()

    # IMPORTANTE:
    # Se escribe el JSON aunque no haya noticias.
    # Esto permite comprobar que el bot SI corrio.
    historial["ultima_ejecucion"] = ahora_slrc().isoformat()
    historial["ultimo_total_encontrado"] = 0
    historial["ultimo_total_enviado"] = 0
    guardar_historial(historial)

    noticias = obtener_noticias(historial)
    noticias = noticias[:MAX_NOTICIAS_POR_CORRIDA]

    historial["ultimo_total_encontrado"] = len(noticias)
    guardar_historial(historial)

    if not noticias:
        log.info(
            "No hay noticias nuevas dentro de las ultimas %s horas.",
            VENTANA_HORAS,
        )
        return

    # El encabezado NO determina si se guarda una noticia.
    if not enviar_encabezado(len(noticias)):
        log.warning("No se pudo enviar el encabezado; se continuara con las noticias.")

    time.sleep(1)

    enviadas = 0
    fallidas = 0

    for noticia in noticias:
        if enviar_noticia(noticia):
            registrar_noticia(noticia, historial)
            enviadas += 1

            # Guardar INMEDIATAMENTE despues de cada envio exitoso.
            # Si el workflow se corta a mitad, no se pierde el historial.
            historial["ultimo_total_enviado"] = enviadas
            guardar_historial(historial)
        else:
            fallidas += 1

        time.sleep(1)

    historial["ultima_ejecucion"] = ahora_slrc().isoformat()
    historial["ultimo_total_encontrado"] = len(noticias)
    historial["ultimo_total_enviado"] = enviadas
    guardar_historial(historial)

    log.info("=" * 60)
    log.info("FIN")
    log.info("Encontradas: %s", len(noticias))
    log.info("Enviadas: %s", enviadas)
    log.info("Fallidas: %s", fallidas)
    log.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("ERROR FATAL DEL BOT")
        raise
