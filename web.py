
import os
import re
import json
import logging
import asyncio
import threading
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

from html.parser import HTMLParser
from flask import Flask, request
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes
)

# --------------------------------------------------
# CONFIGURACIÓN
# --------------------------------------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

GEMINI_MODEL = os.environ.get(
    "GEMINI_MODEL",
    "gemini-3.8-flash"
)

URL = "https://notimundialexpress-bot.onrender.com"
WEBHOOK_URL = f"{URL}/webhook"

PORT = int(os.environ.get("PORT", 10000))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

app = Flask(__name__)

application = None
loop = None


# --------------------------------------------------
# OBTENER IMÁGENES DE LOS ARTÍCULOS
# --------------------------------------------------

class ImageParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.image = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return

        data = dict(attrs)

        property_name = (
            data.get("property", "") or
            data.get("name", "")
        ).lower()

        if property_name in (
            "og:image",
            "og:image:url",
            "twitter:image",
            "twitter:image:src"
        ):
            image_url = data.get("content")

            if image_url and image_url.startswith("http"):
                self.image = image_url


def obtener_imagen(url):

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
        )

        with urllib.request.urlopen(req, timeout=8) as response:
            html = response.read(500000).decode(
                "utf-8",
                errors="ignore"
            )

        parser = ImageParser()
        parser.feed(html)

        return parser.image

    except Exception as e:
        logger.warning("No se pudo obtener imagen: %s", e)
        return None


# --------------------------------------------------
# CONSULTAR GOOGLE NEWS RSS
# --------------------------------------------------

def consultar_rss(tema, cantidad=5):

    query = urllib.parse.quote(tema)

    rss_url = (
        "https://news.google.com/rss/search?"
        f"q={query}&hl=es-419&gl=CR&ceid=CR:es-419"
    )

    noticias = []

    try:
        req = urllib.request.Request(
            rss_url,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
        )

        with urllib.request.urlopen(req, timeout=15) as response:
            contenido = response.read()

        root = ET.fromstring(contenido)

        for item in root.findall(".//item")[:cantidad]:

            titulo = item.findtext("title", default="Sin título")
            enlace = item.findtext("link", default="")
            descripcion = item.findtext("description", default="")

            descripcion = re.sub(
                r"<[^>]+>",
                " ",
                descripcion
            )

            descripcion = re.sub(
                r"\s+",
                " ",
                descripcion
            ).strip()

            imagen = None

            # Buscar imagen incluida en el RSS
            for elemento in item.iter():

                if elemento.tag.lower().endswith(
                    ("thumbnail", "content")
                ):
                    imagen = elemento.attrib.get("url")

                    if imagen:
                        break

            # Si RSS no trae imagen, revisar el artículo
            if not imagen and enlace:
                imagen = obtener_imagen(enlace)

            noticias.append({
                "titulo": titulo,
                "enlace": enlace,
                "descripcion": descripcion,
                "imagen": imagen
            })

    except Exception as e:
        logger.exception("Error consultando RSS: %s", e)

    return noticias


# --------------------------------------------------
# RESUMEN CON GEMINI GRATIS
# --------------------------------------------------

def generar_resumen(noticia):

    if not GEMINI_API_KEY:
        return "Resumen automático no configurado."

    contenido = (
        f"Título: {noticia['titulo']}\n"
        f"Descripción: {noticia['descripcion']}"
    )

    instruccion = """
    Eres el redactor de Noticiero Mundial Express.

    Resume la noticia en español utilizando únicamente
    la información proporcionada.

    Reglas:
    - Utiliza lenguaje sencillo y profesional.
    - Escribe entre 35 y 55 palabras.
    - Explica qué ocurrió y por qué es importante.
    - No inventes datos.
    - No agregues opiniones personales.
    - Entrega solamente el resumen.
    """

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{GEMINI_MODEL}:generateContent"
        f"?key={urllib.parse.quote(GEMINI_API_KEY)}"
    )

    payload = {
        "systemInstruction": {
            "parts": [
                {"text": instruccion}
            ]
        },
        "contents": [
            {
                "parts": [
                    {"text": contenido}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 150
        }
    }

    try:
        data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json"
            },
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=25) as response:
            resultado = json.loads(response.read().decode("utf-8"))

        texto = (
            resultado["candidates"][0]
            ["content"]["parts"][0]["text"]
        )

        return texto.strip()

    except Exception as e:
        logger.warning("Error de Gemini: %s", e)

        return (
            "No fue posible generar el resumen en este momento. "
            "Puedes consultar la noticia en el enlace."
        )


# --------------------------------------------------
# ENVIAR NOTICIAS A TELEGRAM
# --------------------------------------------------

async def enviar_noticias(update, tema, seccion):

    mensaje = await update.message.reply_text(
        f"🌎 Buscando las últimas noticias de {seccion}..."
    )

    noticias = await asyncio.to_thread(
        consultar_rss,
        tema,
        5
    )

    if not noticias:
        await mensaje.edit_text(
            "No pude encontrar noticias en este momento. "
            "Inténtalo nuevamente más tarde."
        )
        return

    await mensaje.delete()

    for noticia in noticias:

        resumen = await asyncio.to_thread(
            generar_resumen,
            noticia
        )

        texto = (
            f"📰 <b>{noticia['titulo']}</b>\n\n"
            f"📝 <b>Resumen:</b>\n{resumen}\n\n"
            f"🔗 <a href=\"{noticia['enlace']}\">Leer noticia completa</a>\n\n"
            f"🌎 <i>Noticiero Mundial Express</i>"
        )

        try:

            if noticia["imagen"]:
                await update.message.reply_photo(
                    photo=noticia["imagen"],
                    caption=texto[:1024],
                    parse_mode="HTML"
                )

                if len(texto) > 1024:
                    await update.message.reply_text(
                        texto[1024:4000],
                        parse_mode="HTML"
                    )

            else:
                await update.message.reply_text(
                    texto,
                    parse_mode="HTML",
                    disable_web_page_preview=False
                )

        except Exception as e:

            logger.warning(
                "No se pudo enviar noticia con imagen: %s",
                e
            )

            # Si falla la imagen, enviar solamente texto
            await update.message.reply_text(
                texto,
                parse_mode="HTML",
                disable_web_page_preview=False
            )


# --------------------------------------------------
# COMANDOS DEL BOT
# --------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    texto = """
🌎 <b>¡Bienvenido a Noticiero Mundial Express!</b>

Tu espacio para mantenerte informado.

Selecciona una sección:

📰 /noticias - Noticias internacionales
🎬 /cine - Cine y entretenimiento
📈 /mercados - Economía y mercados
⚽ /deportes - Noticias deportivas

ℹ️ /ayuda - Ver comandos disponibles

<i>Información clara, rápida y mundial.</i>
"""

    await update.message.reply_text(
        texto,
        parse_mode="HTML"
    )


async def noticias(update, context):
    await enviar_noticias(
        update,
        "noticias internacionales mundo",
        "Noticias internacionales"
    )


async def cine(update, context):
    await enviar_noticias(
        update,
        "cine películas series entretenimiento",
        "Cine y entretenimiento"
    )


async def mercados(update, context):
    await enviar_noticias(
        update,
        "economía mercados financieros bolsa",
        "Economía y mercados"
    )


async def deportes(update, context):
    await enviar_noticias(
        update,
        "deportes fútbol deportes internacionales",
        "Deportes"
    )


async def ayuda(update, context):

    texto = """
📌 <b>Comandos disponibles</b>

/start - Menú principal
/noticias - Noticias internacionales
/cine - Cine y entretenimiento
/mercados - Economía y mercados
/deportes - Noticias deportivas
/ayuda - Ayuda

🌎 <i>Noticiero Mundial Express</i>
"""

    await update.message.reply_text(
        texto,
        parse_mode="HTML"
    )


# --------------------------------------------------
# INICIAR TELEGRAM
# --------------------------------------------------

async def initialize_bot():

    await application.initialize()
    await application.start()

    await application.bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET
    )

    logger.info("Bot conectado correctamente a Telegram.")


# --------------------------------------------------
# WEBHOOK DE FLASK
# --------------------------------------------------

@app.route("/", methods=["GET"])
def home():
    return "Noticiero Mundial Express está funcionando."


@app.route("/webhook", methods=["POST"])
def webhook():

    if request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    ) != WEBHOOK_SECRET:
        return "Unauthorized", 403

    try:
        datos = request.get_json(force=True)
        update = Update.de_json(datos, application.bot)

        asyncio.run_coroutine_threadsafe(
            application.process_update(update),
            loop
        )

        return "OK", 200

    except Exception as e:
        logger.exception("Error en webhook: %s", e)
        return "Error", 500


# --------------------------------------------------
# EJECUCIÓN PRINCIPAL
# --------------------------------------------------

def iniciar_loop():

    global loop

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_forever()


def main():

    global application

    if not TELEGRAM_TOKEN:
        raise RuntimeError("Falta TELEGRAM_TOKEN")

    if not WEBHOOK_SECRET:
        raise RuntimeError("Falta WEBHOOK_SECRET")

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
        raise RuntimeError("WEBHOOK_SECRET tiene caracteres no permitidos")

    if not GEMINI_API_KEY:
        logger.warning(
            "GEMINI_API_KEY no configurada. "
            "El bot funcionará sin resúmenes de IA."
        )

    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("noticias", noticias))
    application.add_handler(CommandHandler("cine", cine))
    application.add_handler(CommandHandler("mercados", mercados))
    application.add_handler(CommandHandler("deportes", deportes))
    application.add_handler(CommandHandler("ayuda", ayuda))

    hilo = threading.Thread(
        target=iniciar_loop,
        daemon=True
    )

    hilo.start()

    import time
    time.sleep(1)

    future = asyncio.run_coroutine_threadsafe(
        initialize_bot(),
        loop
    )

    future.result(timeout=30)

    logger.info("Iniciando servidor Flask...")

    app.run(
        host="0.0.0.0",
        port=PORT
    )


if __name__ == "__main__":
    main()
