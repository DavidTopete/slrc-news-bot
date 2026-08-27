#!/usr/bin/env python3
"""
SLRC News Bot - noticias de San Luis Río Colorado hacia Telegram.

Correcciones respecto a la versión anterior del repo:

  1. HISTORIAL ORDENADO. `self.links` era un set; `list(set)` no tiene orden
     estable (PYTHONHASHSEED), por lo que el truncado [-MAX_HISTORIAL:]
     conservaba 300 links AL AZAR en cada corrida: los links descartados se
     re-enviaban y cada escritura generaba un diff completo en git.
     Ahora se usa lista ordenada + set auxiliar solo para búsqueda O(1).

  2. FILTRO SLRC vs SLUG. limpiar_texto() convierte '/' en espacio, pero el
     slug queda como un token único ('sanluisriocolorado', 'tribunadesanluis'),
     así que la clave "san luis rio colorado" NUNCA coincidía con la URL.
     Ahora se compara también contra el texto compactado sin espacios.

  3. VENTANA TEMPORAL. es_fecha_valida() exigía fecha == hoy. Con el cron de
     GitHub Actions en UTC y America/Hermosillo en UTC-7 fijo (sin DST), la
     ventana quedaba desalineada. Ahora es una ventana móvil en horas.

  4. DETECCIÓN DE FECHA. Se agregó JSON-LD (schema.org datePublished), que es
     lo que emiten Arc XP (El Imparcial) y OEM, y <time datetime="...">.
     El fallback sobre el texto completo de la página quedó como último
     recurso porque captura cualquier fecha del pie de página.

  5. OBSERVABILIDAD. Contadores de embudo por fuente: indican exactamente en
     qué etapa se descartan las noticias en lugar de un "no hay noticias".

  6. VERIFICACIÓN DE DESTINO. getMe + getChat al arranque: confirma token y
     que CHAT_ID apunta al chat esperado (causa típica de "el bot corre pero
     no veo nada": CHAT_ID de canal sin el prefijo -100).

  7. Manejo de HTTP 429 (retry_after), reintentos con backoff y sesión
     reutilizada con keep-alive.

Variables de entorno:
  TOKEN, CHAT_ID           credenciales (requeridas)
  DRY_RUN=1                no envía nada, solo reporta (default 0)
  VENTANA_HORAS=30         antigüedad máxima aceptada (default 30)
  MIN_LARGO_TITULO=30      longitud mínima del ancla (default 30)
  UMBRAL_SIMILITUD=0.90    umbral de deduplicación por título (default 0.90)
  HEARTBEAT=1              avisa a Telegram aunque no haya noticias (default 0)
  LOG_LEVEL=DEBUG          verbosidad (default INFO)
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ========================
# CONFIG
# ========================

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("slrc_news_bot")

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
HEARTBEAT = os.getenv("HEARTBEAT", "0") == "1"
VENTANA_HORAS = int(os.getenv("VENTANA_HORAS", "30"))
MIN_LARGO_TITULO = int(os.getenv("MIN_LARGO_TITULO", "30"))
UMBRAL_SIMILITUD_TITULO = float(os.getenv("UMBRAL_SIMILITUD", "0.90"))

ARCHIVO_ENVIADAS = "noticias_enviadas.json"
TZ = ZoneInfo("America/Hermosillo")

MAX_HISTORIAL = 300
MAX_NOTICIAS_POR_CORRIDA = 10
PAUSA_ENTRE_ENVIOS = 1.2          # s, margen contra el rate limit de Telegram
TIMEOUT_SCRAPE = 15               # s
TIMEOUT_TELEGRAM = 20             # s

FUENTES = [
    {"nombre": "Tribuna Inicio",      "url": "https://oem.com.mx/tribunadesanluis/"},
    {"nombre": "Tribuna Local",       "url": "https://oem.com.mx/tribunadesanluis/local/"},
    {"nombre": "Tribuna Policiaca",   "url": "https://oem.com.mx/tribunadesanluis/policiaca/"},
    {"nombre": "Tribuna Valle",       "url": "https://oem.com.mx/tribunadesanluis/tags/temas/valle"},
    {"nombre": "El Imparcial SLRC",   "url": "https://www.elimparcial.com/sonora/sanluisriocolorado/"},
    {"nombre": "El Imparcial Sonora", "url": "https://www.elimparcial.com/sonora/"},
]

# Fuentes cuya sección ya es local por definición: no se les exige el filtro
# de keywords, solo la exclusión de otras ciudades.
FUENTES_YA_LOCALES = {"Tribuna Inicio", "Tribuna Local", "Tribuna Policiaca",
                      "Tribuna Valle", "El Imparcial SLRC"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
}

ETAPAS = [
    "anclas_totales",
    "titulo_corto",
    "url_invalida",
    "no_es_articulo",
    "filtro_slrc",
    "ya_enviada",
    "sin_fecha",
    "fuera_de_ventana",
    "candidatas",
]


def crear_sesion():
    """Sesión con keep-alive y reintentos automáticos ante 5xx/429."""
    sesion = requests.Session()
    sesion.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    sesion.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=10))
    return sesion


SESION = crear_sesion()


# ========================
# UTILIDADES
# ========================

def ahora_slrc():
    return datetime.now(TZ)


def nuevo_contador():
    return {etapa: 0 for etapa in ETAPAS}


def imprimir_embudo(nombre, stats):
    resumen = " | ".join(f"{k}={v}" for k, v in stats.items() if v)
    log.info(f"Embudo [{nombre}]: {resumen or 'sin datos'}")


def escapar_markdown(texto):
    """Escapa caracteres reservados de MarkdownV2.
    El backslash debe escaparse PRIMERO o se duplica el escape posterior."""
    texto = str(texto).replace("\\", "\\\\")
    for c in r"_*[]()~`>#+-=|{}.!":
        texto = texto.replace(c, f"\\{c}")
    return texto


def limpiar_texto(texto):
    texto = texto.lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"),
                 ("ú", "u"), ("ü", "u"), ("ñ", "n")):
        texto = texto.replace(a, b)
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def titulo_parecido(t1, t2):
    return SequenceMatcher(None, limpiar_texto(t1),
                           limpiar_texto(t2)).ratio() >= UMBRAL_SIMILITUD_TITULO


# ========================
# FILTRO SLRC
# ========================

CLAVES_SLRC = [
    "san luis rio colorado",
    "slrc",
    "san luis sonora",
    "san luis rc",
]

CLAVES_LOCALES = [
    "ayuntamiento", "cabildo", "policia municipal", "bomberos",
    "garita", "aduana", "valle de san luis", "golfo de santa clara",
    "luis b sanchez", "riito", "ejido", "mexicali san luis", "san luis",
]

CIUDADES_EXCLUIDAS = [
    "hermosillo", "nogales", "guaymas", "obregon", "caborca", "navojoa",
    "cananea", "agua prieta", "puerto penasco", "magdalena", "sonoyta",
    "sinaloa", "chihuahua", "tijuana",
]

# Rutas que no son artículos (evita gastar un GET por cada una para leer fecha)
PATRONES_NO_ARTICULO = re.compile(
    r"/(tags?|autor|author|seccion|categoria|suscri|newsletter|clasificados|"
    r"aviso-de-privacidad|contacto|login|registro|video|galeria)s?(/|$)"
)


def _contexto_filtro(titulo, link):
    """Devuelve (texto_espaciado, texto_compactado).

    El compactado permite que 'sanluisriocolorado' en la URL coincida con la
    clave multipalabra 'san luis rio colorado'. Sin esto el link no aportaba
    ninguna señal al filtro."""
    base = limpiar_texto(f"{titulo} {link}")
    return base, base.replace(" ", "")


def _contiene(clave, base, compacto):
    return clave in base or clave.replace(" ", "") in compacto


def es_noticia_slrc(titulo, link, fuente_ya_local=False, explicar=False):
    base, compacto = _contexto_filtro(titulo, link)

    es_slrc = any(_contiene(c, base, compacto) for c in CLAVES_SLRC)

    if not es_slrc:
        for ciudad in CIUDADES_EXCLUIDAS:
            if _contiene(ciudad, base, compacto):
                if explicar:
                    log.debug(f"  rechazo (ciudad '{ciudad}'): {titulo[:70]}")
                return False

    if es_slrc or fuente_ya_local:
        return True

    coincidencias = [p for p in CLAVES_LOCALES if _contiene(p, base, compacto)]
    if len(coincidencias) >= 2:
        return True

    if explicar:
        log.debug(f"  rechazo ({len(coincidencias)} claves {coincidencias}): {titulo[:70]}")
    return False


def parece_articulo(url):
    ruta = urlparse(url).path
    if PATRONES_NO_ARTICULO.search(ruta):
        return False
    # Un artículo real tiene un slug con varias palabras
    return len(ruta.strip("/").split("/")) >= 2 and len(ruta) > 25


# ========================
# HISTORIAL
# ========================

def cargar_enviadas():
    if not os.path.exists(ARCHIVO_ENVIADAS):
        log.warning(f"{ARCHIVO_ENVIADAS} no existe: historial vacío.")
        return {"links": [], "titulos": []}
    try:
        with open(ARCHIVO_ENVIADAS, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("estructura inesperada")
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


class Historial:
    """Historial en memoria, cargado una sola vez por corrida.

    `_links_lista` mantiene el ORDEN de inserción (para un truncado estable y
    diffs de git limpios); `_links_set` da búsqueda O(1)."""

    def __init__(self):
        data = cargar_enviadas()
        self._links_lista = list(dict.fromkeys(data["links"]))   # dedup preservando orden
        self._links_set = set(self._links_lista)
        self.titulos = list(dict.fromkeys(data["titulos"]))
        self._hay_cambios = False
        log.info(f"Historial: {len(self._links_lista)} links, {len(self.titulos)} títulos")

    def ya_fue_enviada(self, noticia):
        if noticia["link"] in self._links_set:
            return True
        for guardado in self.titulos:
            if titulo_parecido(noticia["titulo"], guardado):
                log.debug(f"  dedup contra: {guardado[:70]}")
                return True
        return False

    def registrar(self, noticia):
        if noticia["link"] not in self._links_set:
            self._links_set.add(noticia["link"])
            self._links_lista.append(noticia["link"])
            self._hay_cambios = True
        if noticia["titulo"] not in self.titulos:
            self.titulos.append(noticia["titulo"])
            self._hay_cambios = True

    def persistir_si_hay_cambios(self):
        if not self._hay_cambios:
            log.info("Sin cambios en el historial: no se reescribe el archivo.")
            return
        with open(ARCHIVO_ENVIADAS, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "links": self._links_lista[-MAX_HISTORIAL:],
                    "titulos": self.titulos[-MAX_HISTORIAL:],
                },
                f, ensure_ascii=False, indent=2,
            )
        self._hay_cambios = False
        log.info(f"Historial guardado: {len(self._links_lista)} links, "
                 f"{len(self.titulos)} títulos")


# ========================
# FECHA
# ========================

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

_cache_fechas = {}


def _a_datetime(valor):
    """Convierte una cadena ISO-8601 a datetime con tz local de SLRC."""
    if not valor:
        return None
    try:
        dt = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)


def parsear_fecha_desde_texto(texto):
    texto = texto.lower()

    m = re.search(r"(\d{4}-\d{2}-\d{2})", texto)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=TZ)
        except ValueError:
            pass

    m = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", texto)
    if m:
        try:
            return datetime.strptime(m.group(1), "%d/%m/%Y").replace(tzinfo=TZ)
        except ValueError:
            pass

    m = re.search(r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+de\s+(\d{4})", texto)
    if m:
        mes = MESES.get(limpiar_texto(m.group(2)))
        if mes:
            try:
                return datetime(int(m.group(3)), mes, int(m.group(1)), tzinfo=TZ)
            except ValueError:
                pass

    return None


def _fecha_desde_jsonld(soup):
    """Arc XP (El Imparcial) y OEM publican schema.org NewsArticle en JSON-LD.
    Es la fuente de fecha más confiable de las tres."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (ValueError, TypeError):
            continue

        pendientes = data if isinstance(data, list) else [data]
        while pendientes:
            obj = pendientes.pop()
            if not isinstance(obj, dict):
                continue
            if "@graph" in obj and isinstance(obj["@graph"], list):
                pendientes.extend(obj["@graph"])
            fecha = _a_datetime(obj.get("datePublished") or obj.get("dateModified"))
            if fecha:
                return fecha
    return None


def obtener_fecha_noticia(link):
    if link in _cache_fechas:
        return _cache_fechas[link]

    fecha = None
    try:
        r = SESION.get(link, timeout=TIMEOUT_SCRAPE)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # 1) meta tags
        for meta in (
            {"property": "article:published_time"},
            {"property": "article:modified_time"},
            {"name": "pubdate"},
            {"name": "publish-date"},
            {"itemprop": "datePublished"},
        ):
            tag = soup.find("meta", attrs=meta)
            if tag and tag.get("content"):
                fecha = _a_datetime(tag["content"]) or parsear_fecha_desde_texto(tag["content"])
                if fecha:
                    log.debug(f"  fecha via meta {meta}: {fecha.isoformat()}")
                    break

        # 2) JSON-LD
        if fecha is None:
            fecha = _fecha_desde_jsonld(soup)
            if fecha:
                log.debug(f"  fecha via JSON-LD: {fecha.isoformat()}")

        # 3) <time datetime="...">
        if fecha is None:
            tag = soup.find("time")
            if tag and tag.get("datetime"):
                fecha = _a_datetime(tag["datetime"])
                if fecha:
                    log.debug(f"  fecha via <time>: {fecha.isoformat()}")

        # 4) último recurso: texto de la página (poco confiable)
        if fecha is None:
            fecha = parsear_fecha_desde_texto(soup.get_text(" ", strip=True))
            if fecha:
                log.debug(f"  fecha via texto (baja confianza): {fecha.isoformat()}")

    except requests.exceptions.RequestException as error:
        log.warning(f"Error obteniendo fecha de {link}: {error}")

    _cache_fechas[link] = fecha
    return fecha


def dentro_de_ventana(fecha_noticia):
    """Ventana móvil en horas en lugar de 'fecha == hoy'.

    El cron de GitHub Actions corre en UTC y se retrasa entre 5 y 30 min bajo
    carga; SLRC es UTC-7 fijo. Comparar días calendario deja huecos donde el
    bot descarta absolutamente todo."""
    if fecha_noticia is None:
        return False
    if fecha_noticia.tzinfo is None:
        fecha_noticia = fecha_noticia.replace(tzinfo=TZ)

    delta = ahora_slrc() - fecha_noticia.astimezone(TZ)
    # 2 h de tolerancia hacia el futuro por relojes/metadatos inconsistentes
    return timedelta(hours=-2) <= delta <= timedelta(hours=VENTANA_HORAS)


# ========================
# SCRAPING
# ========================

def construir_url_absoluta(base_url, href):
    href = href.strip()
    if href.startswith("http"):
        return href
    partes = urlparse(base_url)
    if href.startswith("//"):
        return f"{partes.scheme}:{href}"
    if href.startswith("/"):
        return f"{partes.scheme}://{partes.netloc}{href}"
    return None


def obtener_noticias(historial):
    noticias = []
    total = nuevo_contador()
    debug = log.isEnabledFor(logging.DEBUG)

    for fuente in FUENTES:
        stats = nuevo_contador()
        ya_local = fuente["nombre"] in FUENTES_YA_LOCALES

        try:
            r = SESION.get(fuente["url"], timeout=TIMEOUT_SCRAPE)
            log.info(f"[{fuente['nombre']}] HTTP {r.status_code} · {len(r.text)} bytes")
            r.raise_for_status()

            soup = BeautifulSoup(r.text, "html.parser")
            anclas = soup.find_all("a", href=True)
            stats["anclas_totales"] = len(anclas)

            if len(anclas) < 20:
                log.warning(f"[{fuente['nombre']}] solo {len(anclas)} anclas: "
                            "posible render por JavaScript o bloqueo WAF.")

            vistos = set()
            for item in anclas:
                titulo = item.get_text(" ", strip=True)
                if not titulo or len(titulo) < MIN_LARGO_TITULO:
                    stats["titulo_corto"] += 1
                    continue

                href = construir_url_absoluta(fuente["url"], item["href"])
                if not href:
                    stats["url_invalida"] += 1
                    continue

                if href in vistos:
                    continue
                vistos.add(href)

                if not parece_articulo(href):
                    stats["no_es_articulo"] += 1
                    continue

                if not es_noticia_slrc(titulo, href, ya_local, explicar=debug):
                    stats["filtro_slrc"] += 1
                    continue

                candidata = {"titulo": titulo, "link": href, "fuente": fuente["nombre"]}

                if historial.ya_fue_enviada(candidata):
                    stats["ya_enviada"] += 1
                    continue

                fecha = obtener_fecha_noticia(href)
                if fecha is None:
                    stats["sin_fecha"] += 1
                    log.info(f"  sin fecha detectable: {titulo[:70]}")
                    continue

                if not dentro_de_ventana(fecha):
                    stats["fuera_de_ventana"] += 1
                    log.info(f"  {fecha.strftime('%Y-%m-%d %H:%M')} fuera de ventana: {titulo[:70]}")
                    continue

                candidata["fecha"] = fecha
                stats["candidatas"] += 1
                noticias.append(candidata)

        except requests.exceptions.RequestException as e:
            log.warning(f"Error de red en {fuente['nombre']}: {e}")
        except Exception as e:
            log.error(f"Error inesperado en {fuente['nombre']}: {e}", exc_info=True)

        imprimir_embudo(fuente["nombre"], stats)
        for k in ETAPAS:
            total[k] += stats[k]

    imprimir_embudo("TOTAL", total)
    return eliminar_duplicados(noticias)


def eliminar_duplicados(lista):
    unicas = []
    for noticia in lista:
        if any(noticia["link"] == e["link"] or titulo_parecido(noticia["titulo"], e["titulo"])
               for e in unicas):
            continue
        unicas.append(noticia)
    if len(unicas) != len(lista):
        log.info(f"Deduplicación: {len(unicas)} de {len(lista)}")
    return unicas


# ========================
# TELEGRAM
# ========================

API = "https://api.telegram.org/bot{token}/{metodo}"


def _validar_respuesta(response):
    if response.status_code != 200:
        log.error(f"Telegram HTTP {response.status_code}: {response.text[:300]}")
        return False, response
    try:
        payload = response.json()
    except ValueError:
        log.error(f"Respuesta no-JSON de Telegram: {response.text[:300]}")
        return False, response
    if not payload.get("ok", False):
        log.error(f"Telegram ok=false: {payload}")
        return False, response
    return True, response


def verificar_destino():
    """Confirma token y, sobre todo, A QUÉ CHAT se está publicando.

    Un CHAT_ID incorrecto no produce error: Telegram entrega el mensaje a otro
    destino con ok:true. Esta verificación imprime el nombre real del chat."""
    if not TOKEN or not CHAT_ID:
        log.error(f"Credenciales ausentes -> TOKEN={'OK' if TOKEN else 'FALTA'} "
                  f"CHAT_ID={'OK' if CHAT_ID else 'FALTA'}. "
                  "Revisa el bloque env: del step en el workflow.")
        return False

    try:
        ok, r = _validar_respuesta(
            SESION.get(API.format(token=TOKEN, metodo="getMe"), timeout=TIMEOUT_TELEGRAM))
        if not ok:
            return False
        log.info(f"Bot autenticado: @{r.json()['result'].get('username')}")

        ok, r = _validar_respuesta(SESION.get(
            API.format(token=TOKEN, metodo="getChat"),
            params={"chat_id": CHAT_ID}, timeout=TIMEOUT_TELEGRAM))
        if not ok:
            log.error(f"CHAT_ID={CHAT_ID} no es alcanzable. Si es canal o "
                      "supergrupo debe incluir el prefijo -100 y el bot debe "
                      "ser administrador.")
            return False

        chat = r.json()["result"]
        log.info(f"Destino: id={chat.get('id')} tipo={chat.get('type')} "
                 f"titulo={chat.get('title') or chat.get('username')}")
        return True

    except requests.exceptions.RequestException as e:
        log.error(f"Sin conectividad con la API de Telegram: {e}")
        return False


def enviar_mensaje(texto):
    if DRY_RUN:
        log.info(f"[DRY-RUN] no enviado:\n{texto}")
        return True

    for intento in range(3):
        try:
            r = SESION.post(
                API.format(token=TOKEN, metodo="sendMessage"),
                data={
                    "chat_id": CHAT_ID,
                    "text": texto,
                    "parse_mode": "MarkdownV2",
                    "disable_web_page_preview": False,
                },
                timeout=TIMEOUT_TELEGRAM,
            )
            if r.status_code == 429:
                espera = r.json().get("parameters", {}).get("retry_after", 5)
                log.warning(f"Rate limit de Telegram, esperando {espera}s")
                time.sleep(espera + 1)
                continue

            ok, _ = _validar_respuesta(r)
            return ok

        except requests.exceptions.RequestException as error:
            log.error(f"Excepción enviando mensaje (intento {intento + 1}/3): {error}")
            time.sleep(2 * (intento + 1))

    return False


def enviar_encabezado():
    fecha = escapar_markdown(ahora_slrc().strftime("%d/%m/%Y"))
    return enviar_mensaje(f"*SAN LUIS RIO COLORADO NOTICIAS*\n*Fecha:* {fecha}")


def enviar_noticia(noticia):
    mensaje = (
        f"*{escapar_markdown(noticia['titulo'])}*\n"
        f"Fuente: {escapar_markdown(noticia['fuente'])}\n"
        f"Link: {escapar_markdown(noticia['link'])}"
    )
    if enviar_mensaje(mensaje):
        log.info(f"Enviada: {noticia['titulo']}")
        return True
    log.warning(f"No se pudo enviar (reintento en próxima corrida): {noticia['titulo']}")
    return False


# ========================
# MAIN
# ========================

def main():
    inicio = time.monotonic()
    log.info(f"SLRC local: {ahora_slrc().strftime('%Y-%m-%d %H:%M:%S %Z')} · "
             f"UTC: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"Config: DRY_RUN={DRY_RUN} VENTANA_HORAS={VENTANA_HORAS} "
             f"MIN_LARGO_TITULO={MIN_LARGO_TITULO} UMBRAL={UMBRAL_SIMILITUD_TITULO}")

    if not DRY_RUN and not verificar_destino():
        raise SystemExit(1)

    historial = Historial()
    noticias = obtener_noticias(historial)[:MAX_NOTICIAS_POR_CORRIDA]

    if not noticias:
        log.warning("Cero noticias candidatas. Revisa el embudo TOTAL para "
                    "identificar la etapa que las descarta.")
        if HEARTBEAT and not DRY_RUN:
            enviar_mensaje(escapar_markdown(
                f"[bot] Corrida {ahora_slrc().strftime('%d/%m %H:%M')}: sin noticias nuevas."))
        return

    log.info(f"{len(noticias)} noticias por publicar")

    if DRY_RUN:
        for n in noticias:
            log.info(f"  [{n['fuente']}] {n['fecha'].strftime('%Y-%m-%d %H:%M')} · {n['titulo']}")
            log.info(f"    {n['link']}")
        return

    enviar_encabezado()
    time.sleep(3)

    enviadas = fallidas = 0
    for noticia in noticias:
        if enviar_noticia(noticia):
            historial.registrar(noticia)
            enviadas += 1
        else:
            fallidas += 1
        time.sleep(PAUSA_ENTRE_ENVIOS)

    historial.persistir_si_hay_cambios()
    log.info(f"Enviadas: {enviadas} | Fallidas: {fallidas} | "
             f"Duración: {time.monotonic() - inicio:.1f}s")


if __name__ == "__main__":
    main()
