import requests
from bs4 import BeautifulSoup
from datetime import datetime
import time
import json
import os
import re
from difflib import SequenceMatcher

# ========================
# CONFIG (desde GitHub Secrets)
# ========================
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

ARCHIVO_ENVIADAS = "noticias_enviadas.json"

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
# DEBUG (CLAVE)
# ========================
def debug_env():
    print("TOKEN existe:", TOKEN is not None)
    print("CHAT_ID existe:", CHAT_ID is not None)

    if TOKEN:
        print("TOKEN inicio:", TOKEN[:10])
    else:
        print("NO TOKEN")

    if CHAT_ID:
        print("CHAT_ID:", CHAT_ID)
    else:
        print("NO CHAT_ID")


# ========================
# UTILIDADES
# ========================
def limpiar_texto(texto):
    texto = texto.lower()
    texto = texto.replace("á","a").replace("é","e").replace("í","i")
    texto = texto.replace("ó","o").replace("ú","u").replace("ñ","n")
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def es_noticia_slrc(titulo, link):
    texto = limpiar_texto(titulo + " " + link)

    claves = [
        "san luis rio colorado","slrc","san luis sonora",
        "san luis rc","san luis r c",
        "garita","aduana","frontera",
        "valle de san luis","riito","sonoyta",
        "golfo de santa clara","luis b sanchez",
        "colonia","ejido","ayuntamiento","policia"
    ]

    return any(c in texto for c in claves)


def titulo_parecido(t1, t2):
    return SequenceMatcher(None, limpiar_texto(t1), limpiar_texto(t2)).ratio() > 0.82


def cargar_enviadas():
    if not os.path.exists(ARCHIVO_ENVIADAS):
        return {"links": [], "titulos": []}
    with open(ARCHIVO_ENVIADAS, "r", encoding="utf-8") as f:
        return json.load(f)


def guardar_enviada(noticia):
    data = cargar_enviadas()
    data["links"].append(noticia["link"])
    data["titulos"].append(noticia["titulo"])

    data["links"] = data["links"][-300:]
    data["titulos"] = data["titulos"][-300:]

    with open(ARCHIVO_ENVIADAS, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def ya_fue_enviada(noticia):
    data = cargar_enviadas()

    if noticia["link"] in data["links"]:
        return True

    for t in data["titulos"]:
        if titulo_parecido(noticia["titulo"], t):
            return True

    return False


# ========================
# SCRAPING
# ========================
def obtener_noticias():
    noticias = []

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

                noticias.append({
                    "titulo": titulo,
                    "link": href,
                    "fuente": fuente["nombre"]
                })

        except Exception as e:
            print(f"Error en {fuente['nombre']}: {e}")

    return eliminar_duplicados(noticias)


def eliminar_duplicados(lista):
    unicas = []

    for n in lista:
        repetida = False

        for u in unicas:
            if n["link"] == u["link"] or titulo_parecido(n["titulo"], u["titulo"]):
                repetida = True
                break

        if not repetida:
            unicas.append(n)

    return unicas


# ========================
# TELEGRAM
# ========================
def enviar_noticia(noticia, i):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    fecha = datetime.now().strftime("%d/%m/%Y")

    mensaje = f"""{i}. {noticia['titulo']}
Fecha: {fecha}
Fuente: {noticia['fuente']}
Link: {noticia['link']}
"""

    response = requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": mensaje
    })

    print("STATUS:", response.status_code)
    print("RESPONSE:", response.text)

    if response.status_code == 200:
        guardar_enviada(noticia)


# ========================
# MAIN
# ========================
def main():
    debug_env()

    print("Buscando noticias...")
    noticias = obtener_noticias()

    nuevas = [n for n in noticias if not ya_fue_enviada(n)]

    noticias_a_enviar = nuevas[:10]

    if len(noticias_a_enviar) < 10:
        for n in noticias:
            if n not in noticias_a_enviar:
                noticias_a_enviar.append(n)
            if len(noticias_a_enviar) == 10:
                break

    print(f"Enviando {len(noticias_a_enviar)} noticias...")

    for i, n in enumerate(noticias_a_enviar, 1):
        enviar_noticia(n, i)
        time.sleep(1)


if __name__ == "__main__":
    main()
