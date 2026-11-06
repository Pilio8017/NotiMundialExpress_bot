
import asyncio
import logging
import os
import threading
import hmac

from flask import Flask, request
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# CONFIGURACIÓN
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


# COMANDOS DEL BOT

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Bienvenido a Noticiero Mundial Express! 📰\n\n"
        "Selecciona una sección:\n\n"
        "/noticias - Noticias globales\n"
        "/cine - Cine y entretenimiento\n"
        "/mercados - Mercados y tecnología\n"
        "/deportes - Deportes\n"
        "/ayuda - Ayuda"
    )


async def noticias_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌍 Noticias Globales\n\n"
        "Estamos preparando la consulta de noticias actualizadas."
    )


async def cine_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 Cine y Entretenimiento\n\n"
        "Estamos preparando la consulta de estrenos y cartelera."
    )


async def mercados_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📈 Mercados y Tecnología\n\n"
        "Estamos preparando las actualizaciones económicas y tecnológicas."
    )


async def deportes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ Deportes\n\n"
        "Estamos preparando la consulta de resultados y noticias deportivas."
    )


async def ayuda_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 AYUDA\n\n"
        "Utiliza estos comandos:\n"
        "/start\n"
        "/noticias\n"
        "/cine\n"
        "/mercados\n"
        "/deportes\n"
        "/ayuda"
    )


async def error_handler(update, context):
    logger.error(
        "Error procesando actualización: %s",
        context.error,
        exc_info=context.error,
    )


# INICIALIZACIÓN DEL BOT

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
    logger.info("Bot iniciado y listo para recibir mensajes.")


# RECEPCIÓN DE MENSAJES

@app.route("/")
def home():
    return "Noticiero Mundial Express está activo.", 200


@app.route("/webhook", methods=["POST"])
def webhook():

    if not WEBHOOK_SECRET:
        return "Webhook secret no configurado", 503

    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )

    if not hmac.compare_digest(received_secret, WEBHOOK_SECRET):
        return "Unauthorized", 403

    if application is None or loop is None or not loop.is_running():
        return "Bot no disponible", 503

    json_data = request.get_json(silent=True)

    if not json_data:
        return "JSON inválido", 400

    try:
        update = Update.de_json(json_data, application.bot)

        future = asyncio.run_coroutine_threadsafe(
            application.process_update(update),
            loop,
        )

        future.result(timeout=15)

        return "ok", 200

    except Exception:
        logger.exception("Error procesando webhook")
        return "Error interno", 500


# INICIO DEL SERVIDOR

def run_event_loop():
    asyncio.set_event_loop(loop)
    loop.run_forever()


def main():

    global application, loop

    if not TOKEN:
        raise RuntimeError("Falta configurar TELEGRAM_TOKEN.")

    if not WEBHOOK_SECRET:
        raise RuntimeError("Falta configurar WEBHOOK_SECRET.")

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("noticias", noticias_command))
    application.add_handler(CommandHandler("cine", cine_command))
    application.add_handler(CommandHandler("mercados", mercados_command))
    application.add_handler(CommandHandler("deportes", deportes_command))
    application.add_handler(CommandHandler("ayuda", ayuda_command))

    application.add_error_handler(error_handler)

    loop = asyncio.new_event_loop()

    thread = threading.Thread(
        target=run_event_loop,
        daemon=True,
    )

    thread.start()

    future = asyncio.run_coroutine_threadsafe(
        initialize_bot(),
        loop,
    )

    future.result(timeout=30)

    logger.info("Servidor web iniciado.")

    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
