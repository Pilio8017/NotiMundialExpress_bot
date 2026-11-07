
import logging
import os

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Bienvenido a Noticiero Mundial Express! 📰\n\n"
        "/noticias - Noticias globales\n"
        "/cine - Cine y entretenimiento\n"
        "/mercados - Mercados y tecnología\n"
        "/deportes - Deportes\n"
        "/ayuda - Ayuda"
    )


async def noticias_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌍 Noticias Globales\n\n"
        "Estamos preparando las noticias actualizadas."
    )


async def cine_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 Cine y Entretenimiento\n\n"
        "Estamos preparando los estrenos y la cartelera."
    )


async def mercados_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📈 Mercados y Tecnología\n\n"
        "Estamos preparando las actualizaciones económicas."
    )


async def deportes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ Deportes\n\n"
        "Estamos preparando los resultados deportivos."
    )


async def ayuda_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 AYUDA\n\n"
        "Comandos disponibles:\n"
        "/start\n"
        "/noticias\n"
        "/cine\n"
        "/mercados\n"
        "/deportes\n"
        "/ayuda"
    )


async def error_handler(update, context):
    logger.error("Error del bot: %s", context.error)


def main():

    if not TOKEN:
        raise RuntimeError("Falta configurar TELEGRAM_TOKEN.")

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("noticias", noticias_command))
    application.add_handler(CommandHandler("cine", cine_command))
    application.add_handler(CommandHandler("mercados", mercados_command))
    application.add_handler(CommandHandler("deportes", deportes_command))
    application.add_handler(CommandHandler("ayuda", ayuda_command))

    application.add_error_handler(error_handler)

    logger.info("Iniciando Noticiero Mundial Express...")

    application.run_polling()


if __name__ == "__main__":
    main()
