import os
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# 1. Configuración de Flask para Render (mantiene el servicio en verde)
app = Flask(__name__)
TOKEN = os.environ.get("TELEGRAM_TOKEN")


@app.route("/")
def home():
  return "¡El Noticiero Mundial Express Bot está activo y funcionando!"


# 2. Lógica y comandos del Bot de Telegram
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


def run_telegram_bot():
  """Arranca el bot de Telegram en segundo plano de manera persistente"""
  if not TOKEN:
    print("Error: TELEGRAM_TOKEN no está configurado.")
    return

  application = ApplicationBuilder().token(TOKEN).build()

  # Registro de comandos
  application.add_handler(CommandHandler("start", start))
  application.add_handler(CommandHandler("noticias", noticias_command))
  application.add_handler(CommandHandler("cine", cine_command))
  application.add_handler(CommandHandler("mercados", mercados_command))
  application.add_handler(CommandHandler("deportes", deportes_command))
  application.add_handler(CommandHandler("ayuda", ayuda_command))

  print("Iniciando sondeo de Telegram en hilo secundario...")
  application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
  # Lanzamos el bot de Telegram en un hilo independiente para que no bloquee el puerto web
  bot_thread = threading.Thread(target=run_telegram_bot, daemon=True)
  bot_thread.start()

  # Iniciamos Flask en el puerto que exige Render
  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)
