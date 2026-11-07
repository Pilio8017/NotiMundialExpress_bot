
import asyncio
import logging
import os
import threading
import hmac
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import html
import re

from flask import Flask, request
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ==========================================
# CONFIGURACION
# ==========================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
PORT = int(os.environ.get("PORT", 10000))

URL = "https://notimundialexpress-bot.onrender.com"

app = Flask(__name__)

application = None
loop = None


# ==========================================
# MOTOR DE NOTICIAS - GOOGLE NEWS RSS
# ==========================================

def consultar_rss(consulta, limite=5):

    parametros = urllib.parse.urlencode({
        "q": consulta,
        "hl": "es-419",
        "gl": "CR",
        "ceid": "CR:es-419"
    })

    url = "https://news.google.com/rss/search?" + parametros

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0"
        }
    )

    with urllib.request.urlopen(req, timeout=20) as response:
        contenido = response.read()

    root = ET.fromstring(contenido)

    noticias = []

    for item in root.findall(".//item")[:limite]:

        titulo = item.findtext("title", "Sin título")
        enlace = item.findtext("link", "")
        fecha = item.findtext("pubDate", "")

        titulo = html.unescape(
            re.sub(r"<[^>]+>", "", titulo)
        )

        if enlace:
            noticias.append({
                "titulo": titulo,
                "enlace": enlace,
                "fecha": fecha
            })

    return noticias


# ==========================================
# ENVIO DE NOTICIAS A TELEGRAM
# ==========================================

async def enviar_noticias(update, titulo, consulta):

    mensaje_inicial = await update.message.reply_text(
        f"{titulo}\n\n"
        "🔎 Buscando noticias actualizadas..."
    )

    try:

        resultados = await asyncio.to_thread(
            consultar_rss,
            consulta,
            5
        )

        if not resultados:

            await mensaje_inicial.edit_text(
                f"{titulo}\n\n"
                "No encontramos noticias disponibles. "
                "Intenta nuevamente más tarde."
            )
            return

        mensaje = f"{titulo}\n\n"

        for i, noticia in enumerate(resultados, 1):

            mensaje += (
                f"📰 {i}. {noticia['titulo']}\n\n"
                f"🔗 {noticia['enlace']}\n\n"
            )

        await mensaje_inicial.edit_text(
            mensaje[:4000],
            disable_web_page_preview=True
        )

        logger.info(
            "Consulta exitosa: %s noticias encontradas.",
            len(resultados)
        )

    except Exception:

        logger.exception(
            "Error consultando noticias RSS."
        )

        await mensaje_inicial.edit_text(
            "⚠️ No pudimos consultar las noticias "
            "en este momento.\n\n"
            "Intenta nuevamente más tarde."
        )


# ==========================================
# COMANDOS DEL BOT
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "¡Bienvenido a Noticiero Mundial Express! 📰\n\n"
        "Tu noticiero inteligente y rápido.\n\n"
        "Selecciona una sección:\n\n"
        "/noticias - Noticias globales\n"
        "/cine - Cine y entretenimiento\n"
        "/mercados - Mercados y tecnología\n"
        "/deportes - Deportes\n"
        "/ayuda - Ayuda"
    )


async def noticias_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await enviar_noticias(
        update,
        "🌍 NOTICIAS GLOBALES",
        "noticias internacionales mundo última hora"
    )


async def cine_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await enviar_noticias(
        update,
        "🎬 CINE Y ENTRETENIMIENTO",
        "cine películas estrenos entretenimiento"
    )


async def mercados_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await enviar_noticias(
        update,
        "📈 MERCADOS Y TECNOLOGÍA",
        "economía mercados financieros tecnología inteligencia artificial"
    )


async def deportes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await enviar_noticias(
        update,
        "⚽ DEPORTES",
        "deportes fútbol noticias deportivas internacionales"
    )


async def ayuda_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🤖 AYUDA - NOTICIERO MUNDIAL EXPRESS\n\n"
        "Utiliza estos comandos:\n\n"
        "/start - Menú principal\n"
        "/noticias - Noticias globales\n"
        "/cine - Cine y entretenimiento\n"
        "/mercados - Mercados y tecnología\n"
        "/deportes - Noticias deportivas\n"
        "/ayuda - Mostrar ayuda"
    )


# ==========================================
# MANEJO DE ERRORES
# ==========================================

async def error_handler(update, context):

    logger.error(
        "Error procesando actualización: %s",
        context.error,
        exc_info=context.error,
    )


# ==========================================
# INICIALIZACION DEL BOT
# ==========================================

async def initialize_bot():

    global application

    await application.initialize()

    await application.start()

    await application.bot.set_webhook(
        url=f"{URL}/webhook",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message"],
    )

    logger.info("Webhook configurado correctamente.")

    logger.info("Noticiero Mundial Express iniciado.")


# ==========================================
# SERVIDOR WEB
# ==========================================

@app.route("/")
def home():

    return "Noticiero Mundial Express está activo.", 200


@app.route("/webhook", methods=["POST"])
def webhook():

    if not WEBHOOK_SECRET:

        return "Webhook secret no configurado", 503

    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        ""
    )

    if not hmac.compare_digest(
        received_secret,
        WEBHOOK_SECRET
    ):

        return "Unauthorized", 403

    if application is None or loop is None or not loop.is_running():

        return "Bot no disponible", 503

    json_data = request.get_json(silent=True)

    if not json_data:

        return "JSON inválido", 400

    try:

        update = Update.de_json(
            json_data,
            application.bot
        )

        future = asyncio.run_coroutine_threadsafe(
            application.process_update(update),
            loop
        )

        future.result(timeout=30)

        return "ok", 200

    except Exception:

        logger.exception(
            "Error procesando webhook"
        )

        return "Error interno", 500


# ==========================================
# INICIO DEL SERVIDOR
# ==========================================

def run_event_loop():

    asyncio.set_event_loop(loop)

    loop.run_forever()


def main():

    global application, loop

    if not TOKEN:

        raise RuntimeError(
            "Falta configurar TELEGRAM_TOKEN."
        )

    if not WEBHOOK_SECRET:

        raise RuntimeError(
            "Falta configurar WEBHOOK_SECRET."
        )

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    # REGISTRO DE COMANDOS

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("noticias", noticias_command)
    )

    application.add_handler(
        CommandHandler("cine", cine_command)
    )

    application.add_handler(
        CommandHandler("mercados", mercados_command)
    )

    application.add_handler(
        CommandHandler("deportes", deportes_command)
    )

    application.add_handler(
        CommandHandler("ayuda", ayuda_command)
    )

    application.add_error_handler(error_handler)

    # CREAR EVENT LOOP

    loop = asyncio.new_event_loop()

    thread = threading.Thread(
        target=run_event_loop,
        daemon=True
    )

    thread.start()

    future = asyncio.run_coroutine_threadsafe(
        initialize_bot(),
        loop
    )

    future.result(timeout=30)

    logger.info(
        "Servidor web iniciado correctamente."
    )

    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True,
        use_reloader=False
    )


if __name__ == "__main__":

    main()
