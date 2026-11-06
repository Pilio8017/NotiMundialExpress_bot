import logging
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TOKEN = os.environ.get("TELEGRAM_TOKEN")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
  await update.message.reply_text(
      "¡Bienvenido a tu Noticiero Inteligente! 📰\nEscribe /noticias, /cine,"
      " /mercados o /deportes para empezar."
  )


async def noticias_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  await update.message.reply_text(
      "🌍 *Noticias Globales:*\n\nTitulares más importantes del día.",
      parse_mode="Markdown",
  )


async def cine_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  await update.message.reply_text(
      "🎬 *Cine y Entretenimiento:*\n\nPróximos estrenos y cartelera.",
      parse_mode="Markdown",
  )


async def mercados_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  await update.message.reply_text(
      "📈 *Mercados y Tecnología:*\n\nActualizaciones económicas y tecnológicas.",
      parse_mode="Markdown",
  )


async def deportes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  await update.message.reply_text(
      "⚽ *Deportes:*\n\nResultados y resúmenes deportivos.", parse_mode="Markdown"
  )


async def ayuda_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  await update.message.reply_text(
      "🤖 *Ayuda:*\nUsa los comandos /noticias, /cine, /mercados o /deportes.",
      parse_mode="Markdown",
  )


def main():
  if not TOKEN:
    print("Error: TELEGRAM_TOKEN no está configurado.")
    return

  application = ApplicationBuilder().token(TOKEN).build()

  application.add_handler(CommandHandler("start", start))
  application.add_handler(CommandHandler("noticias", noticias_command))
  application.add_handler(CommandHandler("cine", cine_command))
  application.add_handler(CommandHandler("mercados", mercados_command))
  application.add_handler(CommandHandler("deportes", deportes_command))
  application.add_handler(CommandHandler("ayuda", ayuda_command))

  print("El bot de Telegram está corriendo...")
  application.run_polling()


if __name__ == "__main__":
  main()
