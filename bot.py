import logging
import os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TOKEN = os.environ.get("TELEGRAM_TOKEN")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
  keyboard = [
      [InlineKeyboardButton("🌍 Noticias Globales", callback_data="noticias")],
      [InlineKeyboardButton("🎬 Cine y Entretenimiento", callback_data="cine")],
      [InlineKeyboardButton("📈 Mercados y Tech", callback_data="mercados")],
      [InlineKeyboardButton("⚽ Deportes", callback_data="deportes")],
  ]
  reply_markup = InlineKeyboardMarkup(keyboard)
  await update.message.reply_text(
      "¡Bienvenido a tu Noticiero Inteligente! 📰\nSelecciona una categoría"
      " para empezar o usa el menú de comandos:",
      reply_markup=reply_markup,
  )


async def noticias_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  await update.message.reply_text(
      "🌍 *Noticias Globales:*\n\nAquí la IA te resumirá los titulares más"
      " importantes del día.",
      parse_mode="Markdown",
  )


async def cine_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  await update.message.reply_text(
      "🎬 *Cine y Entretenimiento:*\n\nAquí la IA te mostrará los próximos"
      " estrenos y cartelera.",
      parse_mode="Markdown",
  )


async def mercados_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  await update.message.reply_text(
      "📈 *Mercados y Tecnología:*\n\nAquí verás actualizaciones económicas y"
      " tecnológicas.",
      parse_mode="Markdown",
  )


async def deportes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  await update.message.reply_text(
      "⚽ *Deportes:*\n\nAquí tendrás los resultados y resúmenes deportivos.",
      parse_mode="Markdown",
  )


async def ayuda_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  await update.message.reply_text(
      "🤖 *Ayuda del Bot:*\n\nPuedes navegar usando los botones interactivos o"
      " escribiendo directamente los comandos del menú (/noticias, /cine,"
      " /mercados, /deportes).",
      parse_mode="Markdown",
  )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
  query = update.callback_query
  await query.answer()

  if query.data == "cine":
    texto = (
        "🎬 *Cine y Entretenimiento:*\n\nAquí la IA te mostrará los próximos"
        " estrenos y carteleras."
    )
  elif query.data == "noticias":
    texto = (
        "🌍 *Noticias Globales:*\n\nAquí la IA te resumirá los titulares más"
        " importantes del día."
    )
  elif query.data == "mercados":
    texto = (
        "📈 *Mercados y Tecnología:*\n\nAquí verás actualizaciones económicas y"
        " tecnológicas."
    )
  elif query.data == "deportes":
    texto = (
        "⚽ *Deportes:*\n\nAquí tendrás los resultados y resúmenes deportivos."
    )
  else:
    texto = "Opción no válida."

  await query.edit_message_text(text=texto, parse_mode="Markdown")


def main():
  app = ApplicationBuilder().token(TOKEN).build()

  # Comandos del bot
  app.add_handler(CommandHandler("start", start))
  app.add_handler(CommandHandler("noticias", noticias_command))
  app.add_handler(CommandHandler("cine", cine_command))
  app.add_handler(CommandHandler("mercados", mercados_command))
  app.add_handler(CommandHandler("deportes", deportes_command))
  app.add_handler(CommandHandler("ayuda", ayuda_command))

  # Manejador de botones inline
  app.add_handler(CallbackQueryHandler(button_handler))

  print("El bot está corriendo...")
  app.run_polling()


if __name__ == "__main__":
  main()
