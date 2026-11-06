import os
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

app = Flask(__name__)
TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 10000))
# Tu URL pública de Render
URL = "https://notimundialexpress-bot.onrender.com"

# Construir la aplicación de Telegram sin polling
application = None


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


def init_bot():
  global application
  if not TOKEN:
    return None
  app_builder = ApplicationBuilder().token(TOKEN).build()
  app_builder.add_handler(CommandHandler("start", start))
  app_builder.add_handler(CommandHandler("noticias", noticias_command))
  app_builder.add_handler(CommandHandler("cine", cine_command))
  app_builder.add_handler(CommandHandler("mercados", mercados_command))
  app_builder.add_handler(CommandHandler("deportes", deportes_command))
  app_builder.add_handler(CommandHandler("ayuda", ayuda_command))
  return app_builder


@app.route("/")
def home():
  return "¡El Noticiero Mundial Express Bot está activo con Webhooks!"


@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
  """Recibe los mensajes que envía Telegram y los procesa"""
  global application
  if application:
    update = Update.de_json(request.get_json(force=True), application.bot)
    # Ejecutamos el procesamiento de la actualización de forma asíncrona o directa
    import asyncio

    asyncio.run(application.process_update(update))
  return "ok", 200


if __name__ == "__main__":
  application = init_bot()
  if application:
    # Configuramos el Webhook en Telegram automáticamente al arrancar
    import asyncio

    asyncio.run(application.initialize())
    asyncio.run(application.bot.set_webhook(url=f"{URL}/{TOKEN}"))
    print("Webhook configurado exitosamente en Telegram.")

  app.run(host="0.0.0.0", port=PORT)
