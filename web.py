import os
import threading
from flask import Flask
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# 1. Configuración de Flask para cumplir con Render
app = Flask(__name__)
TOKEN = os.environ.get("TELEGRAM_TOKEN")


@app.route("/")
def home():
  return "¡El Noticiero Mundial Express Bot está activo y funcionando!"


# 2. Lógica del Bot de Telegram
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
  await update.message.reply_text(
      "¡Bienvenido a tu Noticiero Inteligente! 📰\nEscribe /noticias, /cine,"
      " /mercados o /deportes para empezar."
  )


async def noticias_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  await update.message.reply_text(
      "🌍 *Noticias Globales:*\n\nAquí la IA te resumirá los titulares más"
      " importantes del día.",
      parse_mode="Markdown",
  )


async def cine_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  await update.message.reply_text(
      "🎬 *Cine y Entretenimiento:*\n\nPróximos estrenos y cartelera al"
      " instante.",
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
      "🤖 *Ayuda:*\nUsa los comandos del menú o escribe directamente /noticias,"
      " /cine, /mercados o /deportes.",
      parse_mode="Markdown",
  )


def run_telegram_bot():
  """Inicia el bot de Telegram en segundo plano"""
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

  print("Iniciando sondeo de Telegram...")
  application.run_polling()


if __name__ == "__main__":
  # Lanzamos el bot de Telegram en un hilo separado para que no bloquee a Flask
  bot_thread = threading.Thread(target=run_telegram_bot, daemon=True)
  bot_thread.start()

  # Puerto dinámico que exige Render
  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)
