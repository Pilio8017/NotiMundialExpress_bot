import logging
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

# Tu Token real de BotFather integrado correctamente
TOKEN = "9035646731:AAF2hfQN9stP3_enhLRuM0Y9DXhcP4lvD8c"


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
      " para empezar:",
      reply_markup=reply_markup,
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
  app.add_handler(CommandHandler("start", start))
  app.add_handler(CallbackQueryHandler(button_handler))

  print("El bot está corriendo...")
  app.run_polling()


if __name__ == "__main__":
  main()
