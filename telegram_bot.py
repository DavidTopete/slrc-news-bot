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
def ahora_local():
    return datetime.now(TZ)


def limpiar_texto(texto):
    texto = texto.lower()
    texto = texto.replace("á", "a").replace("é", "e").replace("í", "i")
    texto = texto.replace("ó", "o").replace("ú", "u").replace("ñ", "n")
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def es_noticia_slrc(titulo, link):
    texto = limpiar_texto(titulo + " " + link)

    claves = [
        "san luis rio colorado", "slrc", "san luis sonora",
        "san luis rc", "san luis r c",
        "garita", "aduana", "frontera",
        "valle de san luis", "riito", "sonoyta",
        "golfo de santa clara", "luis b sanchez",
        "colonia", "ejido", "ayuntamiento", "policia"
    ]

    return any(c in texto for c in claves)


def titulo_parecido(t1, t2):
    return SequenceMatcher(None, limpiar_texto(t1), limpiar_texto(t2)).ratio() >= 0.80


def cargar_enviadas():
    if not os.path.exists(ARCHIVO_ENVIADAS):
        return {"links": [], "titulos": []}

    try:
        with open(ARCHIVO_ENVIADAS, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"links": [], "titulos": []}


def guardar_enviada(noticia):
    data = cargar_enviadas()

    if noticia["link"] not in data["links"]:
        data["links"].append(noticia["link"])

    if noticia["titulo"] not in data["titulos"]:
        data["titulos"].append(noticia["titulo"])

    data["links"] = data["links"][-300:]
    data["titulos"] = data["titulos"][-300:]

    with open(ARCHIVO_ENVIADAS, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def ya_fue_enviada(noticia):
    data = cargar_enviadas()

    if noticia["link"] in data["links"]:
        return True

    for titulo_guardado in data["titulos"]:
        if titulo_parecido(noticia["titulo"], titulo_guardado):
            return True

    return False


# ========================
# FECHA / HORA DE NOTICIA
# ========================
def convertir_fecha_local(fecha_raw):
    try:
        dt = datetime.fromisoformat(fecha_raw.replace("Z", "+00:00"))

        if dt.tzinfo:
            dt = dt.astimezone(TZ)

        return dt
    except:
        return None


def obtener_fecha_hora_noticia(link):
    try:
        r = requests.get(link, headers=HEADERS, timeout=10)
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
                dt = convertir_fecha_local(tag["content"])

                if dt:
                    return dt

        texto = soup.get_text(" ", strip=True)

        patrones = [
            r"(\d{1,2}/\d{1,2}/\d{4})",
            r"(\d{4}-\d{2}-\d{2})"
        ]

        for patron in patrones:
            match = re.search(patron, texto)

            if match:
                fecha_txt = match.group(1)

                for formato in ("%d/%m/%Y", "%Y-%m-%d"):
                    try:
                        dt = datetime.strptime(fecha_txt, formato)
                        return dt.replace(tzinfo=TZ)
                    except:
                        pass

    except Exception as e:
        print(f"Error obteniendo fecha/hora: {e}")

    return None


def es_de_hoy(fecha_dt):
    if not fecha_dt:
        return False

    hoy = ahora_local().date()
    return fecha_dt.date() == hoy


# ========================
# SCRAPING
# ========================
def obtener_noticias():
    noticias = []
    data_enviadas = cargar_enviadas()

    for fuente in FUENTES:
        try:
            print(f"Leyendo: {fuente['nombre']}")

            r = requests.get(fuente["url"], headers=HEADERS, timeout=10)
            soup = BeautifulSoup(r.text, "html.parser")

            links = soup.find_all("a", href=True)

            for item in links:
                titulo = item.get_text(" ", strip=True)
                href = item["href"]

                if not titulo or len(titulo) < 30:
                    continue

                if href.startswith("/"):
                    base = fuente["url"].split("/")[0] + "//" + fuente["url"].split("/")[2]
                    href = base + href

                if not href.startswith("http"):
                    continue

                if not es_noticia_slrc(titulo, href):
                    continue

                # Evitar repetidos por link
                if href in data_enviadas["links"]:
                    print(f"REPETIDA POR LINK: {titulo}")
                    continue

                # Evitar repetidos por título parecido
                repetida = False
                for titulo_guardado in data_enviadas["titulos"]:
                    if titulo_parecido(titulo, titulo_guardado):
                        repetida = True
                        break

                if repetida:
                    print(f"REPETIDA POR TITULO: {titulo}")
                    continue

                fecha_dt = obtener_fecha_hora_noticia(href)

                # No mostrar noticias sin fecha o de días anteriores
                if not es_de_hoy(fecha_dt):
                    print(f"IGNORADA POR FECHA: {titulo}")
                    continue

                noticia = {
                    "titulo": titulo,
                    "link": href,
                    "fuente": fuente["nombre"],
                    "fecha_hora": fecha_dt.strftime("%d/%m/%Y %H:%M")
                }

                noticias.append(noticia)

        except Exception as e:
            print(f"Error en {fuente['nombre']}: {e}")

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
def enviar_encabezado():
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    ahora = ahora_local()

    mensaje = f"""*SAN LUIS RIO COLORADO NOTICIAS*
*Fecha: {ahora.strftime("%d/%m/%Y")}*
*Hora: {ahora.strftime("%H:%M")}*
*Cobertura: noticias publicadas hoy*
"""

    requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": mensaje,
        "parse_mode": "Markdown"
    })


def enviar_noticia(noticia, i):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    mensaje = f"""{i}. {noticia['titulo']}
Fecha/Hora de la noticia: {noticia['fecha_hora']}
Fuente: {noticia['fuente']}
Link: {noticia['link']}
"""

    response = requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": mensaje
    })

    if response.status_code == 200:
        guardar_enviada(noticia)
        print(f"Enviada: {noticia['titulo']}")
    else:
        print("Error enviando:", response.text)


# ========================
# MAIN
# ========================
def main():
    print("Buscando noticias de hoy...")

    noticias = obtener_noticias()

    noticias_a_enviar = []

    for noticia in noticias:
        if not ya_fue_enviada(noticia):
            noticias_a_enviar.append(noticia)

    noticias_a_enviar = noticias_a_enviar[:10]

    if not noticias_a_enviar:
        print("No hay noticias nuevas de hoy. No se enviará nada.")
        return

    enviar_encabezado()
    time.sleep(3)

    for i, noticia in enumerate(noticias_a_enviar, 1):
        enviar_noticia(noticia, i)
        time.sleep(1)


if __name__ == "__main__":
    main()
