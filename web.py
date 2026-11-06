import asyncio
import os
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

app = Flask(__name__)
TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 10000))
URL = "https://notimundialexpress-bot.onrender.com"

# Creamos la aplicación global de Telegram
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


async def setup_bot():
  """Configura e inicializa el bot y su webhook usando un único bucle de eventos"""
  global application
  if not TOKEN:
    print("Error: TELEGRAM_TOKEN no está configurado.")
    return None

  app_builder = ApplicationBuilder().token(TOKEN).build()
  app_builder.add_handler(CommandHandler("start", start))
  app_builder.add_handler(CommandHandler("noticias", noticias_command))
  app_builder.add_handler(CommandHandler("cine", cine_command))
  app_builder.add_handler(CommandHandler("mercados", mercados_command))
  app_builder.add_handler(CommandHandler("deportes", deportes_command))
  app_builder.add_handler(CommandHandler("ayuda", ayuda_command))

  # Inicializar y configurar webhook en un solo ciclo continuo
  await app_builder.initialize()
  await app_builder.bot.set_webhook(url=f"{URL}/{TOKEN}")
  print("Webhook configurado exitosamente en Telegram.")
  return app_builder


@app.route("/")
def home():
  return "¡El Noticiero Mundial Express Bot está activo con Webhooks!"


@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
  """Recibe las peticiones POST enviadas por Telegram"""
  global application
  if application:
    try:
      json_data = request.get_json(force=True)
      update = Update.de_json(json_data, application.bot)
      # Procesamos la actualización dentro del bucle activo
      asyncio.run_coroutine_threadsafe(
          application.process_update(update), loop
      )
    except Exception as e:
      print(f"Error procesando update: {e}")
  return "ok", 200


if __name__ == "__main__":
  if TOKEN:
    # Capturamos el bucle de eventos principal para inicializar todo correctamente
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    application = loop.run_until_complete(setup_bot())

  # Arrancamos Flask para mantener el servicio en vivo en Render
  app.run(host="0.0.0.0", port=PORT)
