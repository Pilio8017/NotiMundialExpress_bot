
import os
import re
import json
import html
import logging
import asyncio
import threading
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

from io import BytesIO
from html.parser import HTMLParser
from urllib.parse import urljoin

from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


# ==================================================
# CONFIGURACIÓN
# ==================================================

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

MAX_IMAGE_SIZE = 8 * 1024 * 1024

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

app = Flask(__name__)

application = None
loop = None


# ==================================================
# LIMPIEZA DE TEXTO
# ==================================================

def limpiar_html(texto):

    if not texto:
        return ""

    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = html.unescape(texto)
    texto = re.sub(r"\s+", " ", texto)

    return texto.strip()


# ==================================================
# DETECTAR IMÁGENES EN RSS Y PÁGINAS WEB
# ==================================================

class ImageParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):

        data = dict(attrs)

        if tag.lower() == "meta":

            nombre = (
                data.get("property", "") or
                data.get("name", "")
            ).lower()

            if nombre in (
                "og:image",
                "og:image:url",
                "twitter:image",
                "twitter:image:src"
            ):

                imagen = data.get("content")

                if imagen:
                    self.images.append(imagen)

        if tag.lower() == "img":

            imagen = (
                data.get("src") or
                data.get("data-src") or
                data.get("data-original")
            )

            if imagen:
                self.images.append(imagen)

            srcset = data.get("srcset", "")

            if srcset:
                primera = srcset.split(",")[0].strip().split(" ")[0]

                if primera:
                    self.images.append(primera)


def obtener_imagenes_rss(item):

    imagenes = []

    for elemento in item.iter():

        tag = elemento.tag.lower()

        if tag.endswith(("thumbnail", "content")):
            url_imagen = elemento.attrib.get("url")

            if url_imagen:
                imagenes.append(url_imagen)

        if tag.endswith("enclosure"):

            url_imagen = elemento.attrib.get("url")
            tipo = elemento.attrib.get("type", "")

            if url_imagen and tipo.startswith("image/"):
                imagenes.append(url_imagen)

    # Buscar imágenes dentro de la descripción
    for nombre in ("description", "{http://purl.org/rss/1.0/modules/content/}encoded"):

        elemento = item.find(nombre)

        if elemento is not None and elemento.text:

            parser = ImageParser()

            try:
                parser.feed(elemento.text)
                imagenes.extend(parser.images)
            except Exception:
                pass

    return imagenes


def obtener_imagenes_pagina(url):

    try:

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"
            }
        )

        with urllib.request.urlopen(req, timeout=10) as response:

            contenido = response.read(700000).decode(
                "utf-8",
                errors="ignore"
            )

            pagina_final = response.geturl()

        parser = ImageParser()
        parser.feed(contenido)

        imagenes = [
            urljoin(pagina_final, imagen)
            for imagen in parser.images
        ]

        return imagenes

    except Exception as e:

        logger.warning(
            "No se pudo revisar la página del artículo: %s",
            e
        )

        return []


# ==================================================
# DESCARGAR Y VALIDAR FOTOGRAFÍAS
# ==================================================

def descargar_imagen(url):

    if not url or not url.startswith(("http://", "https://")):
        return None

    try:

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "image/*"
            }
        )

        with urllib.request.urlopen(req, timeout=12) as response:

            tipo = response.headers.get("Content-Type", "").lower()

            if not tipo.startswith("image/"):
                return None

            contenido = response.read(MAX_IMAGE_SIZE + 1)

            if len(contenido) > MAX_IMAGE_SIZE:
                return None

            if not contenido:
                return None

        archivo = BytesIO(contenido)
        archivo.name = "noticia.jpg"

        return archivo

    except Exception as e:

        logger.warning(
            "No se pudo descargar imagen: %s",
            e
        )

        return None


def buscar_fotografia(noticia):

    candidatos = noticia.get("imagenes", [])

    # Primero probar las imágenes proporcionadas por el RSS
    for imagen in candidatos:

        archivo = descargar_imagen(imagen)

        if archivo:
            return archivo

    # Si no hay imágenes válidas, revisar la página original
    enlace = noticia.get("enlace")

    if enlace:

        imagenes_pagina = obtener_imagenes_pagina(enlace)

        for imagen in imagenes_pagina:

            archivo = descargar_imagen(imagen)

            if archivo:
                return archivo

    return None


# ==================================================
# CONSULTAR GOOGLE NEWS RSS
# ==================================================

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

        with urllib.request.urlopen(req, timeout=20) as response:
            contenido = response.read()

        root = ET.fromstring(contenido)

        for item in root.findall(".//item")[:cantidad]:

            titulo = limpiar_html(
                item.findtext("title", default="Sin título")
            )

            enlace = item.findtext("link", default="")

            descripcion = limpiar_html(
                item.findtext("description", default="")
            )

            fuente_elemento = item.find("source")

            fuente = (
                fuente_elemento.text
                if fuente_elemento is not None and fuente_elemento.text
                else "Fuente informativa"
            )

            imagenes = obtener_imagenes_rss(item)

            noticias.append({
                "titulo": titulo,
                "enlace": enlace,
                "descripcion": descripcion,
                "fuente": fuente,
                "imagenes": imagenes
            })

    except Exception as e:

        logger.exception(
            "Error consultando Google News: %s",
            e
        )

    return noticias


# ==================================================
# RESUMEN CON GEMINI
# ==================================================

def generar_resumen(noticia):

    titulo = noticia["titulo"]
    descripcion = noticia["descripcion"]

    if not GEMINI_API_KEY:

        return (
            descripcion[:400]
            if descripcion
            else "Consulta el enlace para conocer los detalles."
        )

    contenido = (
        f"Titular: {titulo}\n"
        f"Descripción disponible: {descripcion}"
    )

    instruccion = """
    Eres el redactor de Noticiero Mundial Express.

    Resume la noticia en español.

    Reglas:
    - Escribe entre 35 y 55 palabras.
    - Utiliza lenguaje sencillo y profesional.
    - Explica qué ocurrió y por qué es importante.
    - No inventes información.
    - No agregues opiniones.
    - Si la información es insuficiente, indícalo.
    - Entrega solamente el resumen.
    """

    api_url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{GEMINI_MODEL}:generateContent"
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

        datos = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            api_url,
            data=datos,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": GEMINI_API_KEY
            },
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=30) as response:

            resultado = json.loads(
                response.read().decode("utf-8")
            )

        resumen = (
            resultado["candidates"][0]
            ["content"]["parts"][0]["text"]
        )

        return resumen.strip()

    except Exception as e:

        logger.warning(
            "Gemini no pudo generar el resumen: %s",
            e
        )

        return (
            descripcion[:400]
            if descripcion
            else "No fue posible generar el resumen. "
                 "Consulta la noticia original."
        )


# ==================================================
# FORMATO PROFESIONAL DE NOTICIAS
# ==================================================

def crear_mensaje(noticia, resumen):

    titulo = html.escape(noticia["titulo"])
    fuente = html.escape(noticia["fuente"])
    resumen = html.escape(resumen)

    enlace = html.escape(
        noticia["enlace"],
        quote=True
    )

    texto = (
        f"🌎 <b>NOTICIERO MUNDIAL EXPRESS</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📰 <b>{titulo}</b>\n\n"
        f"📝 <b>RESUMEN</b>\n"
        f"{resumen}\n\n"
        f"📡 <b>Fuente:</b> {fuente}\n\n"
        f"🔗 <a href=\"{enlace}\">LEER NOTICIA ORIGINAL</a>\n\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    return texto


# ==================================================
# PUBLICAR NOTICIAS EN TELEGRAM
# ==================================================

async def enviar_noticias(update, tema, seccion):

    mensaje = await update.message.reply_text(
        f"🌎 Buscando noticias de {seccion}..."
    )

    noticias = await asyncio.to_thread(
        consultar_rss,
        tema,
        5
    )

    if not noticias:

        await mensaje.edit_text(
            "No encontré noticias en este momento. "
            "Inténtalo nuevamente."
        )

        return

    await mensaje.delete()

    for noticia in noticias:

        # Generar resumen
        resumen = await asyncio.to_thread(
            generar_resumen,
            noticia
        )

        texto = crear_mensaje(
            noticia,
            resumen
        )

        # Buscar y descargar fotografía
        fotografia = await asyncio.to_thread(
            buscar_fotografia,
            noticia
        )

        try:

            if fotografia:

                fotografia.seek(0)

                await update.message.reply_photo(
                    photo=fotografia,
                    caption=texto,
                    parse_mode="HTML"
                )

            else:

                # Si no existe fotografía, enviar noticia
                # con vista previa del enlace cuando Telegram
                # pueda obtenerla de la publicación original.

                await update.message.reply_text(
                    texto,
                    parse_mode="HTML",
                    disable_web_page_preview=False
                )

        except Exception as e:

            logger.warning(
                "Error enviando noticia: %s",
                e
            )

            await update.message.reply_text(
                texto,
                parse_mode="HTML",
                disable_web_page_preview=False
            )


# ==================================================
# COMANDOS DEL BOT
# ==================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    texto = """
🌎 <b>¡Bienvenido a Noticiero Mundial Express!</b>

Información mundial en un solo lugar.

📰 /noticias - Noticias internacionales
🎬 /cine - Cine y entretenimiento
📈 /mercados - Economía y mercados
⚽ /deportes - Noticias deportivas

ℹ️ /ayuda - Ver comandos

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
📌 <b>COMANDOS DISPONIBLES</b>

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


# ==================================================
# INICIALIZAR TELEGRAM
# ==================================================

async def initialize_bot():

    await application.initialize()
    await application.start()

    await application.bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET
    )

    logger.info(
        "Webhook de Telegram configurado correctamente."
    )


# ==================================================
# WEBHOOK FLASK
# ==================================================

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

        update = Update.de_json(
            datos,
            application.bot
        )

        asyncio.run_coroutine_threadsafe(
            application.process_update(update),
            loop
        )

        return "OK", 200

    except Exception as e:

        logger.exception(
            "Error en webhook: %s",
            e
        )

        return "Error", 500


# ==================================================
# LOOP ASÍNCRONO
# ==================================================

def iniciar_loop():

    global loop

    loop = asyncio.new_event_loop()

    asyncio.set_event_loop(loop)

    loop.run_forever()


# ==================================================
# EJECUCIÓN PRINCIPAL
# ==================================================

def main():

    global application

    if not TELEGRAM_TOKEN:
        raise RuntimeError("Falta TELEGRAM_TOKEN")

    if not WEBHOOK_SECRET:
        raise RuntimeError("Falta WEBHOOK_SECRET")

    if not re.fullmatch(
        r"[A-Za-z0-9_-]{1,256}",
        WEBHOOK_SECRET
    ):
        raise RuntimeError(
            "WEBHOOK_SECRET tiene caracteres no permitidos."
        )

    if not GEMINI_API_KEY:

        logger.warning(
            "GEMINI_API_KEY no configurada. "
            "Se utilizarán descripciones del RSS."
        )

    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("noticias", noticias)
    )

    application.add_handler(
        CommandHandler("cine", cine)
    )

    application.add_handler(
        CommandHandler("mercados", mercados)
    )

    application.add_handler(
        CommandHandler("deportes", deportes)
    )

    application.add_handler(
        CommandHandler("ayuda", ayuda)
    )

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

    logger.info(
        "Iniciando Noticiero Mundial Express..."
    )

    app.run(
        host="0.0.0.0",
        port=PORT
    )


if __name__ == "__main__":
    main()
