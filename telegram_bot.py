import requests
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urlparse
import time
import json
import os
import re
import logging
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

# ========================
# CONFIG
# ========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("slrc_news_bot")

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

ARCHIVO_ENVIADAS = "noticias_enviadas.json"
TZ = ZoneInfo("America/Hermosillo")

UMBRAL_SIMILITUD_TITULO = 0.80
MAX_HISTORIAL = 300
MAX_NOTICIAS_POR_CORRIDA = 10

FUENTES = [
    {"nombre": "Tribuna Inicio", "url": "https://oem.com.mx/tribunadesanluis/"},
    {"nombre": "Tribuna Local", "url": "https://oem.com.mx/tribunadesanluis/local/"},
    {"nombre": "Tribuna Policiaca", "url": "https://oem.com.mx/tribunadesanluis/policiaca/"},
    {"nombre": "Tribuna Valle", "url": "https://oem.com.mx/tribunadesanluis/tags/temas/valle"},
    {"nombre": "El Imparcial SLRC", "url": "https://www.elimparcial.com/sonora/sanluisriocolorado/"},
    {"nombre": "El Imparcial Sonora", "url": "https://www.elimparcial.com/sonora/"}
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# ========================
# UTILIDADES
# ========================

def ahora_slrc():
    return datetime.now(TZ)


def escapar_markdown(texto):
    """Escapa caracteres reservados de MarkdownV2. IMPORTANTE: el backslash
    debe escaparse PRIMERO, o se duplicará el escape de los caracteres
    procesados después (bug del script original: el backslash nunca se
    escapaba, lo que provocaba mensajes rechazados por Telegram con error
    400 ante títulos que lo contenían)."""

    texto = str(texto)
    texto = texto.replace("\\", "\\\\")

    caracteres = r"_*[]()~`>#+-=|{}.!"

    for c in caracteres:
        texto = texto.replace(c, f"\\{c}")

    return texto


def limpiar_texto(texto):
    texto = texto.lower()
    texto = texto.replace("á", "a")
    texto = texto.replace("é", "e")
    texto = texto.replace("í", "i")
    texto = texto.replace("ó", "o")
    texto = texto.replace("ú", "u")
    texto = texto.replace("ñ", "n")
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def titulo_parecido(t1, t2):
    return SequenceMatcher(None, limpiar_texto(t1), limpiar_texto(t2)).ratio() >= UMBRAL_SIMILITUD_TITULO


# ========================
# FILTRO SLRC
# ========================

def es_noticia_slrc(titulo, link):
    texto = limpiar_texto(titulo + " " + link)

    claves_slrc = [
        "san luis rio colorado",
        "slrc",
        "san luis sonora",
        "san luis rc",
        "san luis r c"
    ]

    claves_locales = [
        "ayuntamiento",
        "cabildo",
        "policia municipal",
        "bomberos",
        "garita",
        "aduana",
        "valle de san luis",
        "golfo de santa clara",
        "luis b sanchez",
        "riito",
        "ejido",
        "mexicali san luis",
        "san luis"
    ]

    ciudades_excluidas = [
        "hermosillo",
        "nogales",
        "guaymas",
        "obregon",
        "caborca",
        "navojoa",
        "cananea",
        "agua prieta",
        "puerto penasco",
        "magdalena",
        "sonoyta",
        "sinaloa",
        "chihuahua",
        "tijuana"
    ]

    for ciudad in ciudades_excluidas:
        if ciudad in texto:
            if "san luis rio colorado" not in texto and "slrc" not in texto:
                return False

    if any(c in texto for c in claves_slrc):
        return True

    coincidencias = sum(1 for palabra in claves_locales if palabra in texto)

    return coincidencias >= 2


# ========================
# HISTORIAL (cargado UNA sola vez en memoria por corrida)
# ========================

def cargar_enviadas():
    if not os.path.exists(ARCHIVO_ENVIADAS):
        return {"links": [], "titulos": []}

    try:
        with open(ARCHIVO_ENVIADAS, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"links": [], "titulos": []}
            data.setdefault("links", [])
            data.setdefault("titulos", [])
            return data

    except Exception as error:
        log.error(f"Error leyendo historial, se respalda y reinicia: {error}")
        try:
            os.replace(ARCHIVO_ENVIADAS, f"{ARCHIVO_ENVIADAS}.bak_{int(time.time())}")
        except OSError:
            pass
        return {"links": [], "titulos": []}


def guardar_enviadas_en_disco(historial):
    with open(ARCHIVO_ENVIADAS, "w", encoding="utf-8") as f:
        json.dump(
            {
                "links": historial["links"][-MAX_HISTORIAL:],
                "titulos": historial["titulos"][-MAX_HISTORIAL:]
            },
            f,
            ensure_ascii=False,
            indent=2
        )


class Historial:
    """Envoltorio en memoria del historial. El script original recargaba
    el JSON del disco en CADA verificación (ya_fue_enviada) y en cada
    guardado (guardar_enviada), y además repetía el mismo chequeo dos
    veces (una vez dentro de obtener_noticias y otra en main). Aquí se
    carga una sola vez y se reutiliza durante toda la corrida."""

    def __init__(self):
        data = cargar_enviadas()
        self.links = set(data["links"])
        self.titulos = list(data["titulos"])
        self._hay_cambios = False

    def ya_fue_enviada(self, noticia):
        if noticia["link"] in self.links:
            return True

        return any(
            titulo_parecido(noticia["titulo"], titulo_guardado)
            for titulo_guardado in self.titulos
        )

    def registrar(self, noticia):
        if noticia["link"] not in self.links:
            self.links.add(noticia["link"])
            self._hay_cambios = True

        if noticia["titulo"] not in self.titulos:
            self.titulos.append(noticia["titulo"])
            self._hay_cambios = True

    def persistir_si_hay_cambios(self):
        if not self._hay_cambios:
            return

        guardar_enviadas_en_disco({"links": list(self.links), "titulos": self.titulos})
        self._hay_cambios = False
        log.info(f"Historial guardado: {len(self.links)} links, {len(self.titulos)} títulos")


# ========================
# FECHA
# ========================

def parsear_fecha_desde_texto(texto):
    meses = {
        "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
        "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
        "septiembre": 9, "setiembre": 9, "octubre": 10,
        "noviembre": 11, "diciembre": 12
    }

    texto = texto.lower()

    patrones = [
        r"(\d{4}-\d{2}-\d{2})",
        r"(\d{1,2}/\d{1,2}/\d{4})",
        r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+de\s+(\d{4})"
    ]

    for patron in patrones:
        match = re.search(patron, texto)

        if not match:
            continue

        try:
            if patron == r"(\d{4}-\d{2}-\d{2})":
                return datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=TZ)

            if patron == r"(\d{1,2}/\d{1,2}/\d{4})":
                return datetime.strptime(match.group(1), "%d/%m/%Y").replace(tzinfo=TZ)

            dia = int(match.group(1))
            mes_nombre = limpiar_texto(match.group(2))
            anio = int(match.group(3))
            mes = meses.get(mes_nombre)

            if mes:
                return datetime(anio, mes, dia, tzinfo=TZ)

        except (ValueError, TypeError) as error:
            log.debug(f"No se pudo parsear fecha con patrón {patron}: {error}")

    return None


def obtener_fecha_noticia(link):
    try:
        r = requests.get(link, headers=HEADERS, timeout=10)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")

        metas = [
            {"property": "article:published_time"},
            {"property": "article:modified_time"},
            {"name": "pubdate"},
            {"name": "publish-date"},
            {"itemprop": "datePublished"}
        ]

        for meta in metas:
            tag = soup.find("meta", attrs=meta)

            if tag and tag.get("content"):
                fecha_raw = tag["content"]

                try:
                    dt = datetime.fromisoformat(fecha_raw.replace("Z", "+00:00"))
                    dt = dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)
                    log.debug(f"Fecha detectada (meta): {dt}")
                    return dt

                except (ValueError, TypeError) as error:
                    log.debug(f"Error parseando meta de fecha: {error}")

                    fecha_parseada = parsear_fecha_desde_texto(fecha_raw)
                    if fecha_parseada:
                        log.debug(f"Fecha detectada (meta como texto): {fecha_parseada}")
                        return fecha_parseada

        texto = soup.get_text(" ", strip=True)
        fecha_parseada = parsear_fecha_desde_texto(texto)

        if fecha_parseada:
            log.debug(f"Fecha detectada (texto): {fecha_parseada}")
            return fecha_parseada

        log.debug(f"Sin fecha detectable: {link}")
        return None

    except requests.exceptions.RequestException as error:
        log.warning(f"Error obteniendo fecha de {link}: {error}")
        return None


def es_fecha_valida(fecha_noticia):
    if fecha_noticia is None:
        return False

    hoy = ahora_slrc().date()

    if fecha_noticia.tzinfo:
        fecha_local = fecha_noticia.astimezone(TZ).date()
    else:
        fecha_local = fecha_noticia.replace(tzinfo=TZ).date()

    log.debug(f"Validando fecha: {fecha_local} vs hoy={hoy}")

    return fecha_local == hoy


# ========================
# SCRAPING
# ========================

def construir_url_absoluta(base_url, href):
    if href.startswith("http"):
        return href

    if href.startswith("/"):
        partes = urlparse(base_url)
        return f"{partes.scheme}://{partes.netloc}{href}"

    return None


def obtener_noticias(historial):
    noticias = []

    for fuente in FUENTES:
        try:
            log.info(f"Leyendo: {fuente['nombre']}")

            r = requests.get(fuente["url"], headers=HEADERS, timeout=10)
            r.raise_for_status()

            soup = BeautifulSoup(r.text, "html.parser")
            links = soup.find_all("a", href=True)

            for item in links:
                titulo = item.get_text(" ", strip=True)
                href = item["href"]

                if not titulo or len(titulo) < 30:
                    continue

                href = construir_url_absoluta(fuente["url"], href)
                if not href:
                    continue

                if not es_noticia_slrc(titulo, href):
                    continue

                noticia_candidata = {"titulo": titulo, "link": href, "fuente": fuente["nombre"]}

                if historial.ya_fue_enviada(noticia_candidata):
                    log.info(f"Repetida, se omite: {titulo}")
                    continue

                fecha_noticia = obtener_fecha_noticia(href)

                if not es_fecha_valida(fecha_noticia):
                    log.info(f"Ignorada por fecha: {titulo}")
                    continue

                noticias.append(noticia_candidata)

        except requests.exceptions.RequestException as e:
            log.warning(f"Error de red en fuente {fuente['nombre']}: {e}")
        except Exception as e:
            log.error(f"Error inesperado en fuente {fuente['nombre']}: {e}")

    return eliminar_duplicados(noticias)


def eliminar_duplicados(lista):
    unicas = []

    for noticia in lista:
        repetida = False

        for existente in unicas:
            if noticia["link"] == existente["link"]:
                repetida = True
                break

            if titulo_parecido(noticia["titulo"], existente["titulo"]):
                repetida = True
                break

        if not repetida:
            unicas.append(noticia)

    return unicas


# ========================
# TELEGRAM
# ========================

def _validar_respuesta_telegram(response):
    """Retorna True solo si Telegram confirma la entrega
    (HTTP 200 + ok:true en el payload JSON)."""
    if response.status_code != 200:
        log.error(f"Telegram respondió con error: {response.text}")
        return False

    try:
        payload = response.json()
    except ValueError:
        log.error(f"Respuesta de Telegram no es JSON válido: {response.text}")
        return False

    if not payload.get("ok", False):
        log.error(f"Telegram ok=false: {payload}")
        return False

    return True


def enviar_encabezado():
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    ahora = ahora_slrc()
    fecha = escapar_markdown(ahora.strftime("%d/%m/%Y"))

    mensaje = (
        "*SAN LUIS RIO COLORADO NOTICIAS*\n"
        f"*Fecha:* {fecha}"
    )

    try:
        response = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": mensaje,
                "parse_mode": "MarkdownV2"
            },
            timeout=20
        )
        log.info(f"Encabezado status: {response.status_code}")
        _validar_respuesta_telegram(response)

    except requests.exceptions.RequestException as error:
        log.error(f"Excepción enviando encabezado: {error}")


def enviar_noticia(noticia):
    """Envía una noticia a Telegram. Retorna True solo si Telegram
    confirma la entrega."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    titulo = escapar_markdown(noticia["titulo"])
    fuente = escapar_markdown(noticia["fuente"])
    link = escapar_markdown(noticia["link"])

    mensaje = (
        f"*{titulo}*\n"
        f"Fuente: {fuente}\n"
        f"Link: {link}"
    )

    try:
        response = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": mensaje,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": False
            },
            timeout=20
        )

        if _validar_respuesta_telegram(response):
            log.info(f"Enviada: {noticia['titulo']}")
            return True

        log.warning(f"No se pudo enviar (se reintentará en próxima corrida): {noticia['titulo']}")
        return False

    except requests.exceptions.RequestException as error:
        log.error(f"Excepción enviando noticia: {error}")
        return False


# ========================
# MAIN
# ========================

def main():
    if not TOKEN:
        log.error("Falta configurar TOKEN.")
        return

    if not CHAT_ID:
        log.error("Falta configurar CHAT_ID.")
        return

    log.info("Buscando noticias de HOY...")

    historial = Historial()

    # obtener_noticias() ya filtra contra el historial durante el scraping;
    # no se repite la verificación en main() (era trabajo redundante que
    # además releía el archivo del disco por cada noticia).
    noticias_a_enviar = obtener_noticias(historial)[:MAX_NOTICIAS_POR_CORRIDA]

    if not noticias_a_enviar:
        log.info("No hay noticias nuevas de HOY. No se publicará nada.")
        return

    enviar_encabezado()
    time.sleep(3)

    total_enviadas = 0
    total_fallidas = 0

    for noticia in noticias_a_enviar:
        exito = enviar_noticia(noticia)

        if exito:
            historial.registrar(noticia)
            total_enviadas += 1
        else:
            total_fallidas += 1

        time.sleep(1)

    historial.persistir_si_hay_cambios()

    log.info(f"Total enviadas: {total_enviadas} | Total fallidas: {total_fallidas}")


if __name__ == "__main__":
    main()
