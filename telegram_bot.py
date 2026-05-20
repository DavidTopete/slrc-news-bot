import requests
from bs4 import BeautifulSoup
from datetime import datetime
import time
import json
import os
import re
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

# ========================
# CONFIG
# ========================
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

ARCHIVO_ENVIADAS = "noticias_enviadas.json"
TZ = ZoneInfo("America/Hermosillo")

FUENTES = [
    {"nombre": "Tribuna Inicio", "url": "https://oem.com.mx/tribunadesanluis/"},
    {"nombre": "Tribuna Local", "url": "https://oem.com.mx/tribunadesanluis/local/"},
    {"nombre": "Tribuna Policiaca", "url": "https://oem.com.mx/tribunadesanluis/policiaca/"},
    {"nombre": "Tribuna Valle", "url": "https://oem.com.mx/tribunadesanluis/tags/temas/valle"},
    {"nombre": "El Imparcial SLRC", "url": "https://www.elimparcial.com/sonora/sanluisriocolorado/"},
    {"nombre": "El Imparcial Sonora", "url": "https://www.elimparcial.com/sonora/"}
]

HEADERS = {"User-Agent": "Mozilla/5.0"}

# ========================
# UTILIDADES
# ========================
def ahora_slrc():
    return datetime.now(TZ)


def escapar_markdown(texto):
    texto = str(texto)

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

            if (
                "san luis rio colorado" not in texto
                and "slrc" not in texto
            ):

                return False

    if any(c in texto for c in claves_slrc):
        return True

    coincidencias = 0

    for palabra in claves_locales:

        if palabra in texto:
            coincidencias += 1

    return coincidencias >= 2


def titulo_parecido(t1, t2):

    return SequenceMatcher(
        None,
        limpiar_texto(t1),
        limpiar_texto(t2)
    ).ratio() >= 0.80


# ========================
# HISTORIAL
# ========================
def cargar_enviadas():

    if not os.path.exists(ARCHIVO_ENVIADAS):

        return {
            "links": [],
            "titulos": []
        }

    try:

        with open(
            ARCHIVO_ENVIADAS,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except:

        return {
            "links": [],
            "titulos": []
        }


def guardar_enviada(noticia):

    data = cargar_enviadas()

    if noticia["link"] not in data["links"]:
        data["links"].append(noticia["link"])

    if noticia["titulo"] not in data["titulos"]:
        data["titulos"].append(noticia["titulo"])

    data["links"] = data["links"][-300:]
    data["titulos"] = data["titulos"][-300:]

    with open(
        ARCHIVO_ENVIADAS,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )


def ya_fue_enviada(noticia):

    data = cargar_enviadas()

    if noticia["link"] in data["links"]:
        return True

    for titulo_guardado in data["titulos"]:

        if titulo_parecido(
            noticia["titulo"],
            titulo_guardado
        ):

            return True

    return False


# ========================
# FECHA DE NOTICIA
# ========================
def parsear_fecha_desde_texto(texto):

    meses = {
        "enero": 1,
        "febrero": 2,
        "marzo": 3,
        "abril": 4,
        "mayo": 5,
        "junio": 6,
        "julio": 7,
        "agosto": 8,
        "septiembre": 9,
        "setiembre": 9,
        "octubre": 10,
        "noviembre": 11,
        "diciembre": 12
    }

    texto = texto.lower()

    patrones = [
        r"(\d{4}-\d{2}-\d{2})",
        r"(\d{1,2}/\d{1,2}/\d{4})",
        r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+de\s+(\d{4})"
    ]

    for patron in patrones:

        match = re.search(
            patron,
            texto
        )

        if not match:
            continue

        try:

            if patron == r"(\d{4}-\d{2}-\d{2})":

                return datetime.strptime(
                    match.group(1),
                    "%Y-%m-%d"
                ).replace(tzinfo=TZ)

            if patron == r"(\d{1,2}/\d{1,2}/\d{4})":

                return datetime.strptime(
                    match.group(1),
                    "%d/%m/%Y"
                ).replace(tzinfo=TZ)

            dia = int(match.group(1))

            mes_nombre = limpiar_texto(
                match.group(2)
            )

            anio = int(match.group(3))

            mes = meses.get(mes_nombre)

            if mes:

                return datetime(
                    anio,
                    mes,
                    dia,
                    tzinfo=TZ
                )

        except:
            pass

    return None


def obtener_fecha_noticia(link):

    try:

        r = requests.get(
            link,
            headers=HEADERS,
            timeout=10
        )

        soup = BeautifulSoup(
            r.text,
            "html.parser"
        )

        metas = [
            {"property": "article:published_time"},
            {"property": "article:modified_time"},
            {"name": "pubdate"},
            {"name": "publish-date"},
            {"itemprop": "datePublished"}
        ]

        for meta in metas:

            tag = soup.find(
                "meta",
                attrs=meta
            )

            if tag and tag.get("content"):

                fecha_raw = tag["content"]

                try:

                    dt = datetime.fromisoformat(
                        fecha_raw.replace(
                            "Z",
                            "+00:00"
                        )
                    )

                    if dt.tzinfo:
                        dt = dt.astimezone(TZ)
                    else:
                        dt = dt.replace(tzinfo=TZ)

                    print(f"FECHA DETECTADA META: {dt}")

                    return dt

                except Exception as e:

                    print(f"ERROR PARSEANDO META: {e}")

                    fecha_parseada = parsear_fecha_desde_texto(
                        fecha_raw
                    )

                    if fecha_parseada:

                        print(
                            f"FECHA DETECTADA META TEXTO: {fecha_parseada}"
                        )

                        return fecha_parseada

        texto = soup.get_text(
            " ",
            strip=True
        )

        fecha_parseada = parsear_fecha_desde_texto(texto)

        if fecha_parseada:

            print(
                f"FECHA DETECTADA TEXTO: {fecha_parseada}"
            )

            return fecha_parseada

        print(f"SIN FECHA DETECTABLE: {link}")

        return None

    except Exception as e:

        print(f"ERROR OBTENIENDO FECHA: {e}")

        return None


def es_fecha_valida(fecha_noticia):

    if fecha_noticia is None:
        return False

    hoy = ahora_slrc().date()

    if fecha_noticia.tzinfo:

        fecha_local = fecha_noticia.astimezone(
            TZ
        ).date()

    else:

        fecha_local = fecha_noticia.replace(
            tzinfo=TZ
        ).date()

    print(
        f"VALIDANDO FECHA: {fecha_local} vs {hoy}"
    )

    return fecha_local == hoy


# ========================
# SCRAPING
# ========================
def obtener_noticias():

    noticias = []

    data_enviadas = cargar_enviadas()

    for fuente in FUENTES:

        try:

            print(f"Leyendo: {fuente['nombre']}")

            r = requests.get(
                fuente["url"],
                headers=HEADERS,
                timeout=10
            )

            soup = BeautifulSoup(
                r.text,
                "html.parser"
            )

            links = soup.find_all(
                "a",
                href=True
            )

            for item in links:

                titulo = item.get_text(
                    " ",
                    strip=True
                )

                href = item["href"]

                if not titulo or len(titulo) < 30:
                    continue

                if href.startswith("/"):

                    base = (
                        fuente["url"].split("/")[0]
                        + "//"
                        + fuente["url"].split("/")[2]
                    )

                    href = base + href

                if not href.startswith("http"):
                    continue

                if not es_noticia_slrc(
                    titulo,
                    href
                ):
                    continue

                if href in data_enviadas["links"]:

                    print(
                        f"REPETIDA LINK: {titulo}"
                    )

                    continue

                repetida = False

                for titulo_guardado in data_enviadas["titulos"]:

                    if titulo_parecido(
                        titulo,
                        titulo_guardado
                    ):

                        repetida = True

                        break

                if repetida:

                    print(
                        f"REPETIDA TITULO: {titulo}"
                    )

                    continue

                fecha_noticia = obtener_fecha_noticia(
                    href
                )

                if not es_fecha_valida(
                    fecha_noticia
                ):

                    print(
                        f"IGNORADA POR FECHA: {titulo}"
                    )

                    continue

                noticia = {
                    "titulo": titulo,
                    "link": href,
                    "fuente": fuente["nombre"]
                }

                noticias.append(noticia)

        except Exception as e:

            print(
                f"ERROR EN FUENTE {fuente['nombre']}: {e}"
            )

    return eliminar_duplicados(noticias)


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
            unicas.append(noticia)

    return unicas


# ========================
# TELEGRAM
# ========================
def enviar_encabezado():

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    ahora = ahora_slrc()

    fecha = escapar_markdown(
        ahora.strftime("%d/%m/%Y")
    )

    hora = escapar_markdown(
        ahora.strftime("%I:%M %p")
    )

    mensaje = (
        "*SAN LUIS RIO COLORADO NOTICIAS*\n"
        f"*Fecha:* {fecha}\n"
        f"*Hora SLRC:* {hora}"
    )

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": mensaje,
            "parse_mode": "MarkdownV2"
        }
    )

    print(
        "HEADER STATUS:",
        response.status_code
    )

    print(
        "HEADER RESPONSE:",
        response.text
    )


def enviar_noticia(noticia):

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    titulo = escapar_markdown(
        noticia["titulo"]
    )

    fuente = escapar_markdown(
        noticia["fuente"]
    )

    link = escapar_markdown(
        noticia["link"]
    )

    mensaje = (
        f"*{titulo}*\n"
        f"Fuente: {fuente}\n"
        f"Link: {link}"
    )

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": mensaje,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": False
        }
    )

    if response.status_code == 200:

        guardar_enviada(noticia)

        print(
            f"ENVIADA: {noticia['titulo']}"
        )

    else:

        print("ERROR TELEGRAM:")

        print(response.text)


# ========================
# MAIN
# ========================
def main():

    print("Buscando noticias de HOY...")

    noticias = obtener_noticias()

    noticias_nuevas = []

    for noticia in noticias:

        if not ya_fue_enviada(noticia):

            noticias_nuevas.append(noticia)

    noticias_a_enviar = noticias_nuevas[:10]

    if not noticias_a_enviar:

        print(
            "No hay noticias nuevas de HOY. No se publicará nada."
        )

        return

    enviar_encabezado()

    time.sleep(3)

    for noticia in noticias_a_enviar:

        enviar_noticia(noticia)

        time.sleep(1)


if __name__ == "__main__":
    main()
