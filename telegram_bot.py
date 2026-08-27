import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin
import time
import json
import os
import re
import logging
from difflib import SequenceMatcher


# ============================================================
# CONFIGURACIÓN
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

log = logging.getLogger("slrc_news_bot")

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

ARCHIVO_ENVIADAS = "noticias_enviadas.json"

# San Luis Río Colorado / Sonora = UTC-7
TZ = ZoneInfo("America/Hermosillo")

UMBRAL_SIMILITUD_TITULO = 0.80
MAX_HISTORIAL = 1000

# Máximo de noticias enviadas en cada ejecución
MAX_NOTICIAS_POR_CORRIDA = 10

# Como el bot corre una vez al día a las 4:00 AM,
# se consideran noticias publicadas hoy o ayer.
ACEPTAR_HOY_Y_AYER = True


# ============================================================
# FUENTES
# ============================================================

FUENTES = [
    {
        "nombre": "Tribuna de San Luis",
        "url": "https://oem.com.mx/tribunadesanluis/"
    },
    {
        "nombre": "Tribuna de San Luis - Local",
        "url": "https://oem.com.mx/tribunadesanluis/local/"
    },
    {
        "nombre": "Tribuna de San Luis - Policiaca",
        "url": "https://oem.com.mx/tribunadesanluis/policiaca/"
    },
    {
        "nombre": "Tribuna de San Luis - Valle",
        "url": "https://oem.com.mx/tribunadesanluis/tags/temas/valle"
    },
    {
        "nombre": "El Imparcial SLRC",
        "url": "https://www.elimparcial.com/sonora/sanluisriocolorado/"
    }
]


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.8"
}


# ============================================================
# UTILIDADES
# ============================================================

def limpiar_texto(texto):

    texto = str(texto or "").lower()

    texto = texto.replace("á", "a")
    texto = texto.replace("é", "e")
    texto = texto.replace("í", "i")
    texto = texto.replace("ó", "o")
    texto = texto.replace("ú", "u")
    texto = texto.replace("ü", "u")
    texto = texto.replace("ñ", "n")

    texto = re.sub(
        r"[^a-z0-9\s]",
        " ",
        texto
    )

    texto = re.sub(
        r"\s+",
        " ",
        texto
    ).strip()

    return texto


def escapar_html(texto):

    texto = str(texto or "")

    return (
        texto
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def titulo_parecido(t1, t2):

    a = limpiar_texto(t1)
    b = limpiar_texto(t2)

    if not a or not b:
        return False

    similitud = SequenceMatcher(
        None,
        a,
        b
    ).ratio()

    return similitud >= UMBRAL_SIMILITUD_TITULO


# ============================================================
# HISTORIAL
# ============================================================

def historial_vacio():

    return {
        "ultima_ejecucion": None,
        "ultimo_total_encontrado": 0,
        "ultimo_total_enviado": 0,
        "links": [],
        "titulos": []
    }


def cargar_enviadas():

    if not os.path.exists(ARCHIVO_ENVIADAS):
        return historial_vacio()

    try:

        with open(
            ARCHIVO_ENVIADAS,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if not isinstance(data, dict):
            return historial_vacio()

        base = historial_vacio()
        base.update(data)

        if not isinstance(base.get("links"), list):
            base["links"] = []

        if not isinstance(base.get("titulos"), list):
            base["titulos"] = []

        return base

    except Exception as error:

        log.error(
            f"Error leyendo historial: {error}"
        )

        try:

            respaldo = (
                f"{ARCHIVO_ENVIADAS}"
                f".bak_{int(time.time())}"
            )

            os.replace(
                ARCHIVO_ENVIADAS,
                respaldo
            )

            log.warning(
                f"Historial respaldado como {respaldo}"
            )

        except OSError:
            pass

        return historial_vacio()


def guardar_enviadas_en_disco(historial):

    historial["links"] = (
        historial
        .get("links", [])
        [-MAX_HISTORIAL:]
    )

    historial["titulos"] = (
        historial
        .get("titulos", [])
        [-MAX_HISTORIAL:]
    )

    temporal = ARCHIVO_ENVIADAS + ".tmp"

    with open(
        temporal,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            historial,
            f,
            ensure_ascii=False,
            indent=2
        )

        f.write("\n")

    os.replace(
        temporal,
        ARCHIVO_ENVIADAS
    )


class Historial:

    def __init__(self):

        data = cargar_enviadas()

        self.links = set(
            data["links"]
        )

        self.titulos = list(
            data["titulos"]
        )

        self.ultima_ejecucion = (
            data.get("ultima_ejecucion")
        )

        self.ultimo_total_encontrado = (
            data.get(
                "ultimo_total_encontrado",
                0
            )
        )

        self.ultimo_total_enviado = (
            data.get(
                "ultimo_total_enviado",
                0
            )
        )


    def ya_fue_enviada(self, noticia):

        if noticia["link"] in self.links:
            return True

        return any(

            titulo_parecido(
                noticia["titulo"],
                titulo_guardado
            )

            for titulo_guardado
            in self.titulos
        )


    def registrar(self, noticia):

        self.links.add(
            noticia["link"]
        )

        if noticia["titulo"] not in self.titulos:

            self.titulos.append(
                noticia["titulo"]
            )


    def guardar(
        self,
        encontrados=None,
        enviados=None
    ):

        if encontrados is not None:
            self.ultimo_total_encontrado = encontrados

        if enviados is not None:
            self.ultimo_total_enviado = enviados

        self.ultima_ejecucion = (
            datetime
            .now(TZ)
            .isoformat()
        )

        guardar_enviadas_en_disco({

            "ultima_ejecucion":
                self.ultima_ejecucion,

            "ultimo_total_encontrado":
                self.ultimo_total_encontrado,

            "ultimo_total_enviado":
                self.ultimo_total_enviado,

            "links":
                list(self.links),

            "titulos":
                self.titulos
        })

        log.info(
            f"Historial guardado: "
            f"{len(self.links)} links, "
            f"{len(self.titulos)} títulos"
        )


# ============================================================
# FILTRO GEOGRÁFICO
# ============================================================

def es_noticia_slrc(titulo, link):

    texto = limpiar_texto(
        f"{titulo} {link}"
    )

    claves_slrc = [
        "san luis rio colorado",
        "slrc",
        "san luis sonora",
        "san luis r c",
        "golfo de santa clara",
        "luis b sanchez",
        "valle de san luis",
        "san luis"
    ]

    ciudades_excluidas = [
        "hermosillo",
        "nogales",
        "guaymas",
        "ciudad obregon",
        "obregon",
        "caborca",
        "navojoa",
        "cananea",
        "agua prieta",
        "puerto penasco",
        "magdalena",
        "sonoyta",
        "tijuana",
        "ensenada"
    ]

    for ciudad in ciudades_excluidas:

        if ciudad in texto:

            if (
                "san luis rio colorado" not in texto
                and "slrc" not in texto
                and "golfo de santa clara" not in texto
                and "luis b sanchez" not in texto
            ):

                return False

    return any(
        clave in texto
        for clave in claves_slrc
    )


# ============================================================
# FECHAS
# ============================================================

def convertir_fecha(fecha_texto):

    if not fecha_texto:
        return None

    fecha_texto = str(
        fecha_texto
    ).strip()

    try:

        if fecha_texto.endswith("Z"):

            fecha_texto = (
                fecha_texto[:-1]
                + "+00:00"
            )

        fecha = datetime.fromisoformat(
            fecha_texto
        )

        if fecha.tzinfo is None:

            fecha = fecha.replace(
                tzinfo=TZ
            )

        return fecha.astimezone(TZ)

    except (ValueError, TypeError):
        pass

    try:

        fecha = datetime.strptime(
            fecha_texto,
            "%Y-%m-%d"
        )

        return fecha.replace(
            tzinfo=TZ
        )

    except (ValueError, TypeError):

        return None


def extraer_fecha_json_ld(soup):

    scripts = soup.find_all(
        "script",
        type="application/ld+json"
    )

    for script in scripts:

        texto = script.get_text(
            " ",
            strip=True
        )

        coincidencias = re.findall(
            r'"datePublished"\s*:\s*"([^"]+)"',
            texto
        )

        for fecha_texto in coincidencias:

            fecha = convertir_fecha(
                fecha_texto
            )

            if fecha:
                return fecha

    return None


def obtener_fecha_articulo(link):

    try:

        r = requests.get(
            link,
            headers=HEADERS,
            timeout=15,
            allow_redirects=True
        )

        r.raise_for_status()

        soup = BeautifulSoup(
            r.text,
            "html.parser"
        )

        metas = [
            {"property": "article:published_time"},
            {"property": "article:modified_time"},
            {"name": "date"},
            {"name": "pubdate"},
            {"name": "publishdate"},
            {"itemprop": "datePublished"}
        ]

        for meta_info in metas:

            meta = soup.find(
                "meta",
                attrs=meta_info
            )

            if (
                meta
                and meta.get("content")
            ):

                fecha = convertir_fecha(
                    meta.get("content")
                )

                if fecha:
                    return fecha

        fecha = extraer_fecha_json_ld(
            soup
        )

        if fecha:
            return fecha

        return None

    except requests.exceptions.RequestException as error:

        log.warning(
            f"No se pudo obtener fecha "
            f"de {link}: {error}"
        )

        return None


def es_fecha_aceptable(noticia):

    fecha = obtener_fecha_articulo(
        noticia["link"]
    )

    hoy = datetime.now(TZ).date()

    ayer = (
        hoy
        - timedelta(days=1)
    )

    if fecha:

        noticia["fecha"] = fecha

        if ACEPTAR_HOY_Y_AYER:

            return (
                fecha.date()
                in (hoy, ayer)
            )

        return (
            fecha.date()
            == hoy
        )

    log.info(
        "Sin fecha detectable, "
        f"se incluye: {noticia['titulo']}"
    )

    return True


# ============================================================
# IMAGEN DE LA NOTICIA
# ============================================================

def obtener_og_image(link):

    try:

        r = requests.get(
            link,
            headers=HEADERS,
            timeout=15,
            allow_redirects=True
        )

        r.raise_for_status()

        soup = BeautifulSoup(
            r.text,
            "html.parser"
        )

        # Open Graph
        meta = soup.find(
            "meta",
            attrs={
                "property": "og:image"
            }
        )

        if (
            meta
            and meta.get("content")
        ):

            return urljoin(
                link,
                meta["content"].strip()
            )

        # Twitter image como alternativa
        meta = soup.find(
            "meta",
            attrs={
                "name": "twitter:image"
            }
        )

        if (
            meta
            and meta.get("content")
        ):

            return urljoin(
                link,
                meta["content"].strip()
            )

    except requests.exceptions.RequestException as error:

        log.debug(
            "No se pudo obtener "
            f"imagen de {link}: {error}"
        )

    return None


# ============================================================
# DUPLICADOS
# ============================================================

def eliminar_duplicados(lista):

    unicas = []

    for noticia in lista:

        repetida = False

        for existente in unicas:

            if noticia["link"] == existente["link"]:

                repetida = True
                break

            if titulo_parecido(
                noticia["titulo"],
                existente["titulo"]
            ):

                repetida = True
                break

        if not repetida:

            unicas.append(
                noticia
            )

    return unicas


# ============================================================
# SCRAPING
# ============================================================

def construir_url_absoluta(
    base_url,
    href
):

    if not href:
        return None

    href = href.strip()

    if href.startswith("javascript:"):
        return None

    if href.startswith("#"):
        return None

    return urljoin(
        base_url,
        href
    )


def parece_articulo(link):

    if not link:
        return False

    url = limpiar_texto(link)

    excluir = [
        "facebook com",
        "twitter com",
        "instagram com",
        "youtube com",
        "whatsapp",
        "login",
        "suscripcion",
        "subscription",
        "privacy",
        "privacidad",
        "terminos",
        "contacto"
    ]

    return not any(
        palabra in url
        for palabra in excluir
    )


def obtener_noticias(historial):

    candidatas = []

    for fuente in FUENTES:

        try:

            log.info(
                f"Leyendo: "
                f"{fuente['nombre']}"
            )

            r = requests.get(
                fuente["url"],
                headers=HEADERS,
                timeout=15,
                allow_redirects=True
            )

            r.raise_for_status()

            soup = BeautifulSoup(
                r.text,
                "html.parser"
            )

            links = soup.find_all(
                "a",
                href=True
            )

            for posicion, item in enumerate(links):

                titulo = item.get_text(
                    " ",
                    strip=True
                )

                href = item.get(
                    "href",
                    ""
                ).strip()

                if (
                    not titulo
                    or len(titulo) < 25
                ):
                    continue

                link = construir_url_absoluta(
                    fuente["url"],
                    href
                )

                if not link:
                    continue

                if not parece_articulo(link):
                    continue

                if not es_noticia_slrc(
                    titulo,
                    link
                ):
                    continue

                noticia = {
                    "titulo": titulo,
                    "link": link,
                    "fuente": fuente["nombre"],
                    "posicion": posicion
                }

                if historial.ya_fue_enviada(
                    noticia
                ):

                    log.info(
                        "Repetida, se omite: "
                        f"{titulo}"
                    )

                    continue

                candidatas.append(
                    noticia
                )

        except requests.exceptions.RequestException as error:

            log.warning(
                "Error de red en "
                f"{fuente['nombre']}: "
                f"{error}"
            )

        except Exception as error:

            log.exception(
                "Error inesperado en "
                f"{fuente['nombre']}: "
                f"{error}"
            )

    # Eliminar duplicados entre fuentes y secciones
    candidatas = eliminar_duplicados(
        candidatas
    )

    noticias_finales = []

    for noticia in candidatas:

        if es_fecha_aceptable(
            noticia
        ):

            noticia["imagen"] = (
                obtener_og_image(
                    noticia["link"]
                )
            )

            noticias_finales.append(
                noticia
            )

        if (
            len(noticias_finales)
            >= MAX_NOTICIAS_POR_CORRIDA
        ):
            break

        time.sleep(0.3)

    return noticias_finales


# ============================================================
# TELEGRAM
# ============================================================

def validar_respuesta_telegram(
    response
):

    log.info(
        f"Telegram status: "
        f"{response.status_code}"
    )

    if response.status_code != 200:

        log.error(
            "Telegram respondió "
            f"con error: {response.text}"
        )

        return False

    try:

        payload = response.json()

    except ValueError:

        log.error(
            "Telegram devolvió "
            "respuesta inválida."
        )

        return False

    if not payload.get(
        "ok",
        False
    ):

        log.error(
            f"Telegram ok=false: "
            f"{payload}"
        )

        return False

    return True


def enviar_mensaje(
    texto,
    mostrar_preview=False
):

    url = (
        "https://api.telegram.org/"
        f"bot{TOKEN}/sendMessage"
    )

    try:

        datos = {
            "chat_id": CHAT_ID,
            "text": texto,
            "parse_mode": "HTML",

            # False = Telegram muestra la vista previa
            "disable_web_page_preview":
                not mostrar_preview
        }

        response = requests.post(
            url,
            data=datos,
            timeout=25
        )

        return validar_respuesta_telegram(
            response
        )

    except requests.exceptions.RequestException as error:

        log.error(
            "Excepción enviando "
            f"a Telegram: {error}"
        )

        return False


# ============================================================
# ENVIAR NOTICIA
# ============================================================

def enviar_noticia(noticia):

    titulo = escapar_html(
        noticia["titulo"]
    )

    fuente = escapar_html(
        noticia["fuente"]
    )

    # El URL directo permite a Telegram obtener:
    # og:image
    # og:title
    # og:description
    # y generar la vista previa.

    link = escapar_html(
        noticia["link"]
    )

    mensaje = (
        f"<b>{titulo}</b>\n"
        f"Fuente: {fuente}\n"
        f"Link: {link}"
    )

    enviado = enviar_mensaje(
        mensaje,
        mostrar_preview=True
    )

    if enviado:

        if noticia.get("imagen"):

            log.info(
                "Enviada con imagen detectada: "
                f"{noticia['imagen']}"
            )

        else:

            log.info(
                "Enviada. Telegram intentará "
                "generar la vista previa."
            )

    return enviado


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # Verificar TOKEN
    # --------------------------------------------------------

    if not TOKEN:

        log.error(
            "Falta configurar TOKEN."
        )

        return

    # --------------------------------------------------------
    # Verificar CHAT_ID
    # --------------------------------------------------------

    if not CHAT_ID:

        log.error(
            "Falta configurar CHAT_ID."
        )

        return

    log.info(
        "Buscando noticias nuevas "
        "de San Luis Río Colorado..."
    )

    historial = Historial()

    # --------------------------------------------------------
    # Registrar que el bot corrió
    # --------------------------------------------------------

    historial.guardar(
        encontrados=0,
        enviados=0
    )

    # --------------------------------------------------------
    # Buscar noticias
    # --------------------------------------------------------

    noticias_a_enviar = (
        obtener_noticias(
            historial
        )
    )

    historial.guardar(
        encontrados=len(
            noticias_a_enviar
        ),
        enviados=0
    )

    # --------------------------------------------------------
    # Si no hay noticias, terminar
    # --------------------------------------------------------

    if not noticias_a_enviar:

        log.info(
            "No hay noticias nuevas "
            "para publicar."
        )

        return

    # ========================================================
    # ENCABEZADO
    # ========================================================

    ahora = (
        datetime
        .now(TZ)
        .strftime("%d/%m/%Y")
    )

    encabezado = (
        "<b>SAN LUIS NOTICIAS</b>\n"
        f"<b>Fecha:</b> {ahora}"
    )

    # El encabezado no genera preview
    enviar_mensaje(
        encabezado,
        mostrar_preview=False
    )

    # Pausa antes de comenzar las noticias
    time.sleep(2)

    # ========================================================
    # ENVIAR NOTICIAS
    # ========================================================

    total_enviadas = 0
    total_fallidas = 0

    for noticia in noticias_a_enviar:

        enviado = enviar_noticia(
            noticia
        )

        if enviado:

            # Registrar la noticia
            historial.registrar(
                noticia
            )

            total_enviadas += 1

            # Guardar inmediatamente después
            # de cada envío exitoso.
            historial.guardar(
                encontrados=len(
                    noticias_a_enviar
                ),
                enviados=total_enviadas
            )

        else:

            total_fallidas += 1

            log.warning(
                "No se pudo enviar; "
                "se reintentará en próxima corrida: "
                f"{noticia['titulo']}"
            )

        # Evitar demasiadas solicitudes consecutivas
        time.sleep(1)

    # ========================================================
    # GUARDADO FINAL
    # ========================================================

    historial.guardar(
        encontrados=len(
            noticias_a_enviar
        ),
        enviados=total_enviadas
    )

    log.info(
        f"Total enviadas: "
        f"{total_enviadas} | "
        f"Total fallidas: "
        f"{total_fallidas}"
    )


# ============================================================
# EJECUCIÓN
# ============================================================

if __name__ == "__main__":
    main()
