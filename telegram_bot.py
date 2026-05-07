# ========================
# TELEGRAM
# ========================
def enviar_encabezado():
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    fecha = datetime.now().strftime("%d/%m/%Y")

    mensaje = f"""**SAN LUIS RIO COLORADO NOTICIAS**
**Fecha: {fecha}**
**Cobertura: últimas 24 horas**
"""

    response = requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": mensaje,
        "parse_mode": "Markdown"
    })

    print("HEADER STATUS:", response.status_code)
    print("HEADER RESPONSE:", response.text)


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

    # 🔥 ENVÍA ENCABEZADO PRIMERO
    enviar_encabezado()

    # 🔥 ENVÍA NOTICIAS
    for i, n in enumerate(noticias_a_enviar, 1):
        enviar_noticia(n, i)
        time.sleep(1)
