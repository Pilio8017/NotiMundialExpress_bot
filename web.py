
import asyncio
import html
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from html.parser import HTMLParser
from io import BytesIO

from flask import Flask, request
from telegram import Update, InputFile
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# =========================================================
# CONFIGURACIÓN
# =========================================================

TOKEN = os.environ.get("TELEGRAM_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

URL = "https://notimundialexpress-bot.onrender.com"
WEBHOOK_URL = f"{URL}/webhook"
PORT = int(os.environ.get("PORT", 10000))

if not TOKEN:
    raise RuntimeError("Falta configurar TELEGRAM_TOKEN.")

if not WEBHOOK_SECRET:
    raise RuntimeError("Falta configurar WEBHOOK_SECRET.")

# =========================================================
# LOGS
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# Evita que los logs HTTP muestren URLs que puedan contener
# información sensible, como el token de Telegram.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

app = Flask(__name__)
application = None
event_loop = None

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
)

# =========================================================
# LIMPIEZA DE TEXTO HTML
# =========================================================

def limpiar_html(texto):
    if not texto:
        return ""

    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = html.unescape(texto)
    texto = re.sub(r"\s+", " ", texto)

    return texto.strip()


# =========================================================
# BUSCADOR DE IMÁGENES EN HTML
# =========================================================

class ImageParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.imagenes = []

    def handle_starttag(self, tag, attrs):

        atributos = dict(attrs)

        if tag.lower() == "meta":

            propiedad = (
                atributos.get("property", "")
                or atributos.get("name", "")
            ).lower()

            if propiedad in [
                "og:image",
                "og:image:url",
                "twitter:image",
                "twitter:image:src",
            ]:
                imagen = atributos.get("content")

                if imagen:
                    self.imagenes.append(imagen)

        if tag.lower() == "img":

            imagen = (
                atributos.get("src")
                or atributos.get("data-src")
                or atributos.get("data-original")
            )

            if imagen:
                self.imagenes.append(imagen)

            srcset = atributos.get("srcset")

            if srcset:
                partes = srcset.split(",")

                for parte in partes:
                    url = parte.strip().split(" ")[0]

                    if url:
                        self.imagenes.append(url)


# =========================================================
# DESCARGAR CONTENIDO WEB
# =========================================================

def descargar_url(url, timeout=12):

    try:

        solicitud = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
            },
        )

        with urllib.request.urlopen(
            solicitud,
            timeout=timeout
        ) as respuesta:

            return respuesta.read()

    except Exception as e:

        logger.warning("No se pudo descargar contenido: %s", e)
        return None


# =========================================================
# OBTENER FOTOGRAFÍA DE UNA NOTICIA
# =========================================================

def buscar_fotografia(item, enlace):

    candidatos = []

    # 1. Revisar imágenes incluidas directamente en el RSS.

    for elemento in item.iter():

        etiqueta = elemento.tag.lower()

        if etiqueta.endswith("}content") or etiqueta.endswith("}thumbnail"):

            imagen = elemento.attrib.get("url")

            if imagen:
                candidatos.append(imagen)

        if etiqueta.endswith("enclosure"):

            imagen = elemento.attrib.get("url")
            tipo = elemento.attrib.get("type", "")

            if imagen and "image" in tipo.lower():
                candidatos.append(imagen)

    # 2. Revisar imágenes dentro de la descripción RSS.

    descripcion = ""

    elemento_descripcion = item.find("description")

    if elemento_descripcion is not None:
        descripcion = elemento_descripcion.text or ""

    parser_rss = ImageParser()

    try:
        parser_rss.feed(descripcion)
        candidatos.extend(parser_rss.imagenes)
    except Exception:
        pass

    # 3. Revisar metadatos de la página original.

    contenido = descargar_url(enlace)

    if contenido:

        try:

            texto_html = contenido.decode(
                "utf-8",
                errors="replace"
            )

            parser_web = ImageParser()
            parser_web.feed(texto_html)

            candidatos.extend(parser_web.imagenes)

        except Exception as e:

            logger.warning(
                "No se pudieron analizar imágenes de la página: %s",
                e
            )

    # 4. Intentar descargar las imágenes encontradas.

    imagenes_revisadas = set()

    for imagen in candidatos:

        if not imagen:
            continue

        imagen = html.unescape(imagen.strip())

        imagen = urllib.parse.urljoin(enlace, imagen)

        if imagen in imagenes_revisadas:
            continue

        imagenes_revisadas.add(imagen)

        if not imagen.startswith(("http://", "https://")):
            continue

        try:

            solicitud = urllib.request.Request(
                imagen,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "image/*",
                },
            )

            with urllib.request.urlopen(
                solicitud,
                timeout=10
            ) as respuesta:

                tipo = respuesta.headers.get(
                    "Content-Type",
                    ""
                )

                if "image" not in tipo.lower():
                    continue

                datos = respuesta.read(5 * 1024 * 1024 + 1)

                if len(datos) > 5 * 1024 * 1024:
                    continue

                if not datos:
                    continue

                return datos

        except Exception as e:

            logger.warning(
                "No se pudo obtener una imagen: %s",
                e
            )

    return None


# =========================================================
# CONSULTAR NOTICIAS DE GOOGLE NEWS
# =========================================================

def consultar_rss(consulta, limite=5):

    noticias = []

    consulta_codificada = urllib.parse.quote(consulta)

    url = (
        "https://news.google.com/rss/search?q="
        f"{consulta_codificada}"
        "&hl=es-419&gl=CR&ceid=CR:es-419"
    )

    contenido = descargar_url(url, timeout=15)

    if not contenido:
        return noticias

    try:

        raiz = ET.fromstring(contenido)

        canal = raiz.find("channel")

        if canal is None:
            return noticias

        for item in canal.findall("item")[:limite]:

            titulo = item.findtext("title", "")
            enlace = item.findtext("link", "")
            descripcion = item.findtext("description", "")
            fecha = item.findtext("pubDate", "")

            fuente = "Google News"

            elemento_fuente = item.find("source")

            if elemento_fuente is not None:
                fuente = elemento_fuente.text or "Google News"

            titulo = limpiar_html(titulo)
            descripcion = limpiar_html(descripcion)

            if not titulo or not enlace:
                continue

            noticias.append({
                "titulo": titulo,
                "enlace": enlace,
                "descripcion": descripcion,
                "fuente": fuente,
                "fecha": fecha,
                "imagen": None,
            })

    except Exception as e:

        logger.exception(
            "Error procesando Google News RSS: %s",
            e
        )

    return noticias


# =========================================================
# GENERAR RESUMEN CON GEMINI
# =========================================================

def generar_resumen(titulo, descripcion, fuente):

    if not GEMINI_API_KEY:

        logger.warning(
            "No está configurada GEMINI_API_KEY."
        )

        return descripcion[:450] if descripcion else (
            "No hay información suficiente para generar el resumen."
        )

    contexto = (
        f"Titular: {titulo}\n"
        f"Descripción disponible: {descripcion}\n"
        f"Fuente: {fuente}"
    )

    instruccion = (
        "Eres un periodista que resume noticias en español. "
        "Resume la noticia en un lenguaje sencillo, claro y neutral. "
        "Escribe entre 2 y 4 oraciones cortas. "
        "No inventes datos, fechas, cifras ni declaraciones. "
        "Si la información es insuficiente, indícalo claramente. "
        "No agregues opiniones personales.\n\n"
        f"{contexto}"
    )

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{GEMINI_MODEL}:generateContent"
    )

    datos = {
        "contents": [
            {
                "parts": [
                    {
                        "text": instruccion
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 180
        }
    }

    cuerpo = json.dumps(datos).encode("utf-8")

    solicitud = urllib.request.Request(
        url,
        data=cuerpo,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        },
        method="POST",
    )

    try:

        with urllib.request.urlopen(
            solicitud,
            timeout=25
        ) as respuesta:

            resultado = json.loads(
                respuesta.read().decode("utf-8")
            )

        candidatos = resultado.get("candidates", [])

        if not candidatos:
            logger.warning(
                "Gemini respondió sin candidatos."
            )

            return descripcion[:450] if descripcion else (
                "No fue posible generar el resumen."
            )

        partes = (
            candidatos[0]
            .get("content", {})
            .get("parts", [])
        )

        resumen = " ".join(
            parte.get("text", "")
            for parte in partes
        ).strip()

        if resumen:
            return resumen

        return descripcion[:450] if descripcion else (
            "No fue posible generar el resumen."
        )

    except urllib.error.HTTPError as e:

        try:
            detalle = e.read().decode(
                "utf-8",
                errors="replace"
            )
        except Exception:
            detalle = str(e)

        # No registrar la clave API ni la URL completa.
        logger.error(
            "ERROR DETALLADO DE GEMINI - HTTP %s: %s",
            e.code,
            detalle
        )

        return descripcion[:450] if descripcion else (
            "No fue posible generar el resumen en este momento."
        )

    except Exception as e:

        logger.exception(
            "ERROR AL GENERAR RESUMEN CON GEMINI: %s",
            e
        )

        return descripcion[:450] if descripcion else (
            "No fue posible generar el resumen en este momento."
        )


# =========================================================
# CREAR MENSAJE DE NOTICIA
# =========================================================

def crear_mensaje(noticia, resumen):

    titulo = noticia["titulo"]
    fuente = noticia["fuente"]
    enlace = noticia["enlace"]

    mensaje = (
        f"🌎 <b>{html.escape(titulo)}</b>\n\n"
        f"📝 <b>Resumen:</b>\n"
        f"{html.escape(resumen)}\n\n"
        f"📰 <b>Fuente:</b> {html.escape(fuente)}\n\n"
        f'🔗 <a href="{html.escape(enlace, quote=True)}">'
        "Leer noticia completa</a>"
    )

    return mensaje


# =========================================================
# ENVIAR NOTICIAS AL USUARIO
# =========================================================

async def enviar_noticias(update, titulo, consulta):

    chat = update.effective_chat

    mensaje_estado = await chat.send_message(
        f"🔎 Buscando {titulo.lower()}..."
    )

    try:

        noticias = consultar_rss(
            consulta,
            limite=5
        )

        if not noticias:

            await mensaje_estado.edit_text(
                "No encontré noticias disponibles en este momento. "
                "Inténtalo nuevamente más tarde."
            )

            return

        await mensaje_estado.edit_text(
            f"📰 Encontré {len(noticias)} noticias. "
            "Preparando fotografías y resúmenes..."
        )

        for numero, noticia in enumerate(noticias, start=1):

            try:

                logger.info(
                    "Procesando noticia %s de %s",
                    numero,
                    len(noticias)
                )

                resumen = generar_resumen(
                    noticia["titulo"],
                    noticia["descripcion"],
                    noticia["fuente"]
                )

                mensaje = crear_mensaje(
                    noticia,
                    resumen
                )

                # Buscar fotografía de la noticia.
                imagen = buscar_fotografia(
                    noticia,
                    noticia["enlace"]
                )

                if imagen:

                    try:

                        await chat.send_photo(
                            photo=InputFile(
                                BytesIO(imagen),
                                filename="noticia.jpg"
                            ),
                            caption=mensaje,
                            parse_mode="HTML"
                        )

                    except Exception as e:

                        logger.warning(
                            "No se pudo enviar la fotografía: %s",
                            e
                        )

                        await chat.send_message(
                            mensaje,
                            parse_mode="HTML",
                            disable_web_page_preview=False
                        )

                else:

                    await chat.send_message(
                        mensaje,
                        parse_mode="HTML",
                        disable_web_page_preview=False
                    )

            except Exception as e:

                logger.exception(
                    "Error procesando una noticia: %s",
                    e
                )

                await chat.send_message(
                    "⚠️ No pude procesar una noticia. "
                    "Continuaré con las demás."
                )

        try:

            await mensaje_estado.delete()

        except Exception:
            pass

        await chat.send_message(
            "✅ <b>Noticias actualizadas.</b>\n"
            "Gracias por utilizar Noticiero Mundial Express.",
            parse_mode="HTML"
        )

    except Exception as e:

        logger.exception(
            "Error general enviando noticias: %s",
            e
        )

        try:

            await mensaje_estado.edit_text(
                "Ocurrió un problema al consultar las noticias. "
                "Inténtalo nuevamente."
            )

        except Exception:
            pass


# =========================================================
# COMANDOS DEL BOT
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    mensaje = (
        "🌎 <b>¡Bienvenido a Noticiero Mundial Express!</b>\n\n"
        "Tu espacio para mantenerte informado de manera "
        "rápida y sencilla.\n\n"
        "Selecciona una categoría:\n\n"
        "📰 /noticias - Noticias internacionales\n"
        "🎬 /cine - Cine y entretenimiento\n"
        "📈 /mercados - Economía y mercados financieros\n"
        "⚽ /deportes - Noticias deportivas\n"
        "ℹ️ /ayuda - Información del bot\n\n"
        "Cada noticia incluye, cuando está disponible, "
        "una fotografía, un resumen y el enlace a su fuente."
    )

    await update.message.reply_text(
        mensaje,
        parse_mode="HTML"
    )


async def noticias(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await enviar_noticias(
        update,
        "Noticias internacionales",
        "noticias internacionales mundo"
    )


async def cine(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await enviar_noticias(
        update,
        "Cine y entretenimiento",
        "cine películas series entretenimiento"
    )


async def mercados(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await enviar_noticias(
        update,
        "Economía y mercados financieros",
        "economía mercados financieros bolsa"
    )


async def deportes(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await enviar_noticias(
        update,
        "Deportes",
        "deportes fútbol deportes internacionales"
    )


async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):

    mensaje = (
        "ℹ️ <b>AYUDA - NOTICIERO MUNDIAL EXPRESS</b>\n\n"
        "Estos son los comandos disponibles:\n\n"
        "/start - Iniciar el bot\n"
        "/noticias - Noticias internacionales\n"
        "/cine - Cine y entretenimiento\n"
        "/mercados - Economía y mercados\n"
        "/deportes - Noticias deportivas\n"
        "/ayuda - Mostrar esta información\n\n"
        "Las noticias provienen de Google News. "
        "Los resúmenes se generan con inteligencia artificial "
        "cuando el servicio está disponible."
    )

    await update.message.reply_text(
        mensaje,
        parse_mode="HTML"
    )


# =========================================================
# MANEJO DE ERRORES DE TELEGRAM
# =========================================================

async def error_handler(update, context):

    logger.error(
        "Error en el bot de Telegram: %s",
        context.error
    )


# =========================================================
# CONFIGURAR BOT Y WEBHOOK
# =========================================================

async def inicializar_bot():

    await application.initialize()

    await application.start()

    await application.bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message"]
    )

    logger.info("Webhook configurado correctamente.")


# =========================================================
# RECIBIR ACTUALIZACIONES DE TELEGRAM
# =========================================================

@app.route("/webhook", methods=["POST"])
def webhook():

    if not hmac_comparar(
        request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            ""
        ),
        WEBHOOK_SECRET
    ):

        logger.warning(
            "Se recibió una solicitud con secreto incorrecto."
        )

        return "Unauthorized", 403

    try:

        datos = request.get_json(silent=True)

        if not datos:
            return "No content", 400

        update = Update.de_json(
            datos,
            application.bot
        )

        future = asyncio.run_coroutine_threadsafe(
            application.process_update(update),
            event_loop
        )

        # Esperar a que Telegram reciba confirmación
        # de que la actualización fue procesada.
        future.result(timeout=15)

        return "OK", 200

    except Exception as e:

        logger.exception(
            "Error procesando webhook: %s",
            e
        )

        return "Internal Server Error", 500


def hmac_comparar(valor1, valor2):

    import hmac

    return hmac.compare_digest(
        str(valor1),
        str(valor2)
    )


# =========================================================
# INICIAR SERVIDOR
# =========================================================

def ejecutar_event_loop(loop):

    asyncio.set_event_loop(loop)
    loop.run_forever()


def main():

    global application
    global event_loop

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    # Registrar comandos.
    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("noticias", noticias)
    )

    application.add_handler(
        CommandHandler("cine", cine)
    )

    application.add_handler(
        CommandHandler("mercados", mercados)
    )

    application.add_handler(
        CommandHandler("deportes", deportes)
    )

    application.add_handler(
        CommandHandler("ayuda", ayuda)
    )

    application.add_error_handler(error_handler)

    # Crear y arrancar el ciclo de eventos.
    event_loop = asyncio.new_event_loop()

    hilo = threading.Thread(
        target=ejecutar_event_loop,
        args=(event_loop,),
        daemon=True
    )

    hilo.start()

    # Inicializar Telegram y configurar webhook.
    futuro = asyncio.run_coroutine_threadsafe(
        inicializar_bot(),
        event_loop
    )

    futuro.result(timeout=60)

    logger.info(
        "Noticiero Mundial Express iniciado correctamente."
    )

    # Iniciar Flask.
    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True,
        use_reloader=False
    )


if __name__ == "__main__":
    main()
