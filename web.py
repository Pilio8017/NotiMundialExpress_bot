
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
import hmac

from html.parser import HTMLParser
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

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
GEMINI_MODEL = os.environ.get(
    "GEMINI_MODEL",
    "gemini-3.8-flash"
)

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

logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

app = Flask(__name__)

application = None
event_loop = None

USER_AGENT = (
    "Mozilla/5.0 (compatible; "
    "NoticieroMundialExpress/1.0)"
)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TELEGRAM_PHOTO_BYTES = 9 * 1024 * 1024
MAX_CAPTION_LENGTH = 1024

# Evitar actualizaciones duplicadas.
processed_update_ids = set()
update_lock = threading.Lock()


# =========================================================
# LIMPIAR TEXTO
# =========================================================

def limpiar_html(texto):

    if not texto:
        return ""

    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = html.unescape(texto)
    texto = re.sub(r"\s+", " ", texto)

    return texto.strip()


# =========================================================
# BUSCADOR DE IMÁGENES HTML
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

                for parte in srcset.split(","):

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

        logger.warning(
            "No se pudo descargar contenido web: %s",
            e
        )

        return None


# =========================================================
# CONSULTAR GOOGLE NEWS RSS
# =========================================================

def extraer_imagenes_rss(item):

    candidatos = []

    for elemento in item.iter():

        etiqueta = elemento.tag.lower()

        if (
            etiqueta.endswith("}content")
            or etiqueta.endswith("}thumbnail")
        ):

            imagen = elemento.attrib.get("url")

            if imagen:
                candidatos.append(imagen)

        if etiqueta.endswith("enclosure"):

            imagen = elemento.attrib.get("url")
            tipo = elemento.attrib.get("type", "")

            if imagen and "image" in tipo.lower():
                candidatos.append(imagen)

    descripcion = ""

    elemento_descripcion = item.find("description")

    if elemento_descripcion is not None:
        descripcion = elemento_descripcion.text or ""

    parser = ImageParser()

    try:
        parser.feed(descripcion)
        candidatos.extend(parser.imagenes)
    except Exception:
        pass

    return candidatos


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
                "imagenes": extraer_imagenes_rss(item),
            })

    except Exception as e:

        logger.exception(
            "Error procesando Google News RSS: %s",
            e
        )

    return noticias


# =========================================================
# DESCARGAR UNA IMAGEN
# =========================================================

def descargar_imagen(url):

    try:

        solicitud = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "image/*",
            },
        )

        with urllib.request.urlopen(
            solicitud,
            timeout=12
        ) as respuesta:

            tipo = respuesta.headers.get(
                "Content-Type",
                ""
            ).lower()

            if "image" not in tipo:
                return None

            datos = respuesta.read(
                MAX_IMAGE_BYTES + 1
            )

            if not datos or len(datos) > MAX_IMAGE_BYTES:
                logger.warning(
                    "Imagen descartada por superar el límite de descarga."
                )
                return None

            return datos

    except Exception as e:

        logger.warning(
            "No se pudo descargar imagen: %s",
            e
        )

        return None


# =========================================================
# CONVERTIR Y OPTIMIZAR IMAGEN PARA TELEGRAM
# =========================================================

def preparar_imagen_telegram(datos):

    if not datos:
        return None

    try:

        imagen_original = Image.open(
            BytesIO(datos)
        )

        # Corregir orientación según los metadatos EXIF.
        imagen = ImageOps.exif_transpose(
            imagen_original
        )

        # Evitar imágenes excesivamente grandes.
        imagen.thumbnail(
            (1600, 1600),
            Image.Resampling.LANCZOS
        )

        # Convertir transparencias a fondo blanco.
        if imagen.mode in ("RGBA", "LA"):

            imagen = imagen.convert("RGBA")

            fondo = Image.new(
                "RGB",
                imagen.size,
                (255, 255, 255)
            )

            fondo.paste(
                imagen,
                mask=imagen.getchannel("A")
            )

            imagen = fondo

        elif imagen.mode == "P":

            imagen = imagen.convert("RGBA")

            fondo = Image.new(
                "RGB",
                imagen.size,
                (255, 255, 255)
            )

            fondo.paste(
                imagen,
                mask=imagen.getchannel("A")
            )

            imagen = fondo

        else:

            imagen = imagen.convert("RGB")

        # Generar JPG optimizado.
        salida = BytesIO()

        calidad = 88

        while calidad >= 45:

            salida.seek(0)
            salida.truncate(0)

            imagen.save(
                salida,
                format="JPEG",
                quality=calidad,
                optimize=True
            )

            if salida.tell() <= MAX_TELEGRAM_PHOTO_BYTES:
                break

            calidad -= 10

        resultado = salida.getvalue()

        if not resultado:
            return None

        if len(resultado) > MAX_TELEGRAM_PHOTO_BYTES:

            logger.warning(
                "Imagen todavía supera el tamaño permitido."
            )

            return None

        logger.info(
            "Imagen optimizada para Telegram: %s bytes.",
            len(resultado)
        )

        return resultado

    except (
        UnidentifiedImageError,
        OSError,
        ValueError
    ) as e:

        logger.warning(
            "No se pudo convertir la imagen: %s",
            e
        )

        return None

    except Exception as e:

        logger.exception(
            "Error inesperado optimizando imagen: %s",
            e
        )

        return None


# =========================================================
# BUSCAR FOTOGRAFÍA EN LA NOTICIA ORIGINAL
# =========================================================

def buscar_imagen_original(noticia):

    candidatos = list(
        noticia.get("imagenes", [])
    )

    enlace = noticia["enlace"]

    contenido = descargar_url(
        enlace,
        timeout=12
    )

    if contenido:

        try:

            texto_html = contenido.decode(
                "utf-8",
                errors="replace"
            )

            parser = ImageParser()
            parser.feed(texto_html)

            candidatos.extend(parser.imagenes)

        except Exception as e:

            logger.warning(
                "Error leyendo imágenes de la noticia: %s",
                e
            )

    revisadas = set()

    for imagen in candidatos:

        if not imagen:
            continue

        imagen = html.unescape(
            imagen.strip()
        )

        imagen = urllib.parse.urljoin(
            enlace,
            imagen
        )

        if imagen in revisadas:
            continue

        revisadas.add(imagen)

        if not imagen.startswith(("http://", "https://")):
            continue

        datos = descargar_imagen(imagen)

        if datos:

            logger.info(
                "Fotografía original encontrada."
            )

            return datos

    return None


# =========================================================
# BUSCAR IMÁGENES EN WIKIMEDIA COMMONS
# =========================================================

def buscar_imagen_commons(titulo):

    api = "https://commons.wikimedia.org/w/api.php"

    consulta = titulo

    if " - " in consulta:
        consulta = consulta.rsplit(" - ", 1)[0]

    consulta = consulta.strip()[:180]

    parametros = {
        "action": "query",
        "generator": "search",
        "gsrsearch": consulta,
        "gsrnamespace": "6",
        "gsrlimit": "10",
        "prop": "imageinfo",
        "iiprop": "url|extmetadata",
        "iiurlwidth": "1200",
        "format": "json",
    }

    url = (
        api
        + "?"
        + urllib.parse.urlencode(parametros)
    )

    try:

        solicitud = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "NoticieroMundialExpress/1.0 "
                    "(news image search)"
                ),
                "Accept": "application/json",
            },
        )

        with urllib.request.urlopen(
            solicitud,
            timeout=15
        ) as respuesta:

            resultado = json.loads(
                respuesta.read().decode("utf-8")
            )

        paginas = (
            resultado
            .get("query", {})
            .get("pages", {})
        )

        if not paginas:

            logger.info(
                "Commons no encontró imágenes para: %s",
                consulta
            )

            return None

        for pagina in paginas.values():

            informacion = pagina.get(
                "imageinfo",
                []
            )

            if not informacion:
                continue

            info = informacion[0]

            mime = info.get("mime", "").lower()

            if mime not in [
                "image/jpeg",
                "image/png",
                "image/webp",
            ]:
                continue

            metadatos = info.get(
                "extmetadata",
                {}
            )

            licencia = limpiar_html(
                metadatos.get(
                    "LicenseShortName",
                    {}
                ).get("value", "Licencia Wikimedia")
            )

            autor = limpiar_html(
                metadatos.get(
                    "Artist",
                    {}
                ).get("value", "Autor no identificado")
            )

            titulo_archivo = pagina.get(
                "title",
                ""
            )

            pagina_archivo = (
                "https://commons.wikimedia.org/wiki/"
                + urllib.parse.quote(
                    titulo_archivo.replace(" ", "_"),
                    safe=":"
                )
            )

            url_imagen = (
                info.get("thumburl")
                or info.get("url")
            )

            if not url_imagen:
                continue

            datos = descargar_imagen(url_imagen)

            if not datos:
                continue

            credito = (
                f"📷 Imagen: {autor} | {licencia}\n"
                f"🔗 {pagina_archivo}"
            )

            logger.info(
                "Imagen alternativa encontrada en Wikimedia Commons."
            )

            return {
                "datos": datos,
                "credito": credito,
            }

    except Exception as e:

        logger.warning(
            "Error buscando imágenes en Wikimedia Commons: %s",
            e
        )

    return None


# =========================================================
# SISTEMA DE BÚSQUEDA DE FOTOGRAFÍAS
# =========================================================

def buscar_fotografia(noticia):

    imagen_original = buscar_imagen_original(
        noticia
    )

    if imagen_original:

        return {
            "datos": imagen_original,
            "credito": (
                "📷 Fotografía de la publicación original."
            ),
        }

    logger.info(
        "No hay fotografía original. Buscando alternativa..."
    )

    imagen_alternativa = buscar_imagen_commons(
        noticia["titulo"]
    )

    if imagen_alternativa:
        return imagen_alternativa

    logger.info(
        "No se encontró una fotografía adecuada."
    )

    return None


# =========================================================
# RESUMEN CON GEMINI
# =========================================================

def generar_resumen(titulo, descripcion, fuente):

    if not GEMINI_API_KEY:

        logger.warning(
            "No está configurada GEMINI_API_KEY."
        )

        return descripcion[:450] if descripcion else (
            "No hay información suficiente para generar el resumen."
        )

    instruccion = (
        "Eres un periodista que resume noticias en español. "
        "Resume la noticia en lenguaje sencillo, claro y neutral. "
        "Escribe entre 2 y 4 oraciones cortas. "
        "No inventes datos, fechas, cifras ni declaraciones. "
        "Si la información es insuficiente, indícalo claramente. "
        "No agregues opiniones personales.\n\n"
        f"Titular: {titulo}\n"
        f"Descripción disponible: {descripcion}\n"
        f"Fuente: {fuente}"
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
            "maxOutputTokens": 180,
        },
    }

    solicitud = urllib.request.Request(
        url,
        data=json.dumps(datos).encode("utf-8"),
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

        candidatos = resultado.get(
            "candidates",
            []
        )

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

        logger.error(
            "ERROR DETALLADO DE GEMINI - HTTP %s: %s",
            e.code,
            detalle
        )

        return descripcion[:450] if descripcion else (
            "No fue posible generar el resumen."
        )

    except Exception as e:

        logger.exception(
            "ERROR AL GENERAR RESUMEN CON GEMINI: %s",
            e
        )

        return descripcion[:450] if descripcion else (
            "No fue posible generar el resumen."
        )


# =========================================================
# CREAR MENSAJE DE NOTICIA
# =========================================================

def crear_mensaje(noticia, resumen, credito=None):

    titulo = html.escape(
        noticia["titulo"]
    )

    fuente = html.escape(
        noticia["fuente"]
    )

    enlace = html.escape(
        noticia["enlace"],
        quote=True
    )

    mensaje = (
        f"🌎 <b>{titulo}</b>\n\n"
        f"📝 <b>Resumen:</b>\n"
        f"{html.escape(resumen)}\n\n"
        f"📰 <b>Fuente:</b> {fuente}\n\n"
    )

    if credito:

        mensaje += (
            f"{html.escape(credito)}\n\n"
        )

    mensaje += (
        f'<a href="{enlace}">'
        "🔗 Leer noticia completa</a>"
    )

    return mensaje


# =========================================================
# ENVIAR FOTOGRAFÍA Y TEXTO
# =========================================================

async def enviar_fotografia_y_texto(chat, fotografia, mensaje):

    # Primero convertir la imagen a JPG compatible.
    imagen_jpg = await asyncio.to_thread(
        preparar_imagen_telegram,
        fotografia["datos"]
    )

    if not imagen_jpg:

        logger.warning(
            "La imagen no pudo convertirse. Se enviará solo el texto."
        )

        await chat.send_message(
            mensaje,
            parse_mode="HTML",
            disable_web_page_preview=False
        )

        return False

    # Si el texto completo cabe en el caption, enviarlo junto con la foto.
    if len(mensaje) <= MAX_CAPTION_LENGTH:

        try:

            await chat.send_photo(
                photo=InputFile(
                    BytesIO(imagen_jpg),
                    filename="noticia.jpg"
                ),
                caption=mensaje,
                parse_mode="HTML",
                read_timeout=40,
                write_timeout=40,
                connect_timeout=20,
                pool_timeout=20
            )

            logger.info(
                "Fotografía enviada correctamente con su descripción."
            )

            return True

        except Exception as e:

            logger.exception(
                "FALLO REAL AL ENVIAR FOTOGRAFÍA CON CAPTION: %s",
                e
            )

            # Intentar nuevamente sin caption.
            try:

                await chat.send_photo(
                    photo=InputFile(
                        BytesIO(imagen_jpg),
                        filename="noticia.jpg"
                    ),
                    read_timeout=40,
                    write_timeout=40,
                    connect_timeout=20,
                    pool_timeout=20
                )

                logger.info(
                    "Fotografía enviada sin caption. Enviando texto separado."
                )

                await chat.send_message(
                    mensaje,
                    parse_mode="HTML",
                    disable_web_page_preview=False
                )

                return True

            except Exception as segundo_error:

                logger.exception(
                    "FALLO AL ENVIAR FOTOGRAFÍA SIN CAPTION: %s",
                    segundo_error
                )

                await chat.send_message(
                    mensaje,
                    parse_mode="HTML",
                    disable_web_page_preview=False
                )

                return False

    # Si el mensaje supera 1024 caracteres, enviar foto y texto separados.
    try:

        await chat.send_photo(
            photo=InputFile(
                BytesIO(imagen_jpg),
                filename="noticia.jpg"
            ),
            caption="🌎 <b>Fotografía de la noticia</b>",
            parse_mode="HTML",
            read_timeout=40,
            write_timeout=40,
            connect_timeout=20,
            pool_timeout=20
        )

        logger.info(
            "Fotografía enviada. El texto se enviará por separado."
        )

        await chat.send_message(
            mensaje,
            parse_mode="HTML",
            disable_web_page_preview=False
        )

        return True

    except Exception as e:

        logger.exception(
            "FALLO AL ENVIAR FOTOGRAFÍA: %s",
            e
        )

        await chat.send_message(
            mensaje,
            parse_mode="HTML",
            disable_web_page_preview=False
        )

        return False


# =========================================================
# ENVIAR NOTICIAS
# =========================================================

async def enviar_noticias(update, titulo, consulta):

    chat = update.effective_chat

    mensaje_estado = await chat.send_message(
        f"🔎 Buscando {titulo.lower()}..."
    )

    try:

        noticias = await asyncio.to_thread(
            consultar_rss,
            consulta,
            5
        )

        if not noticias:

            await mensaje_estado.edit_text(
                "No encontré noticias disponibles. "
                "Inténtalo nuevamente más tarde."
            )

            return

        await mensaje_estado.edit_text(
            f"📰 Encontré {len(noticias)} noticias. "
            "Buscando fotografías y preparando resúmenes..."
        )

        for numero, noticia in enumerate(
            noticias,
            start=1
        ):

            try:

                logger.info(
                    "Procesando noticia %s de %s: %s",
                    numero,
                    len(noticias),
                    noticia["titulo"]
                )

                resumen = await asyncio.to_thread(
                    generar_resumen,
                    noticia["titulo"],
                    noticia["descripcion"],
                    noticia["fuente"]
                )

                fotografia = await asyncio.to_thread(
                    buscar_fotografia,
                    noticia
                )

                credito = None

                if fotografia:
                    credito = fotografia["credito"]

                mensaje = crear_mensaje(
                    noticia,
                    resumen,
                    credito
                )

                if fotografia:

                    foto_enviada = await enviar_fotografia_y_texto(
                        chat,
                        fotografia,
                        mensaje
                    )

                    if foto_enviada:

                        logger.info(
                            "Noticia %s enviada con fotografía.",
                            numero
                        )

                    else:

                        logger.warning(
                            "Noticia %s enviada sin fotografía.",
                            numero
                        )

                else:

                    await chat.send_message(
                        mensaje,
                        parse_mode="HTML",
                        disable_web_page_preview=False
                    )

                    logger.info(
                        "Noticia %s enviada sin fotografía disponible.",
                        numero
                    )

            except Exception as e:

                logger.exception(
                    "Error procesando noticia %s: %s",
                    numero,
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
# COMANDOS
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
# ERRORES
# =========================================================

async def error_handler(update, context):

    logger.error(
        "Error en el bot de Telegram: %s",
        context.error
    )


# =========================================================
# INICIALIZAR TELEGRAM
# =========================================================

async def inicializar_bot():

    await application.initialize()

    await application.start()

    await application.bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message"]
    )

    logger.info(
        "Webhook configurado correctamente."
    )


# =========================================================
# VALIDAR SECRETO
# =========================================================

def hmac_comparar(valor1, valor2):

    return hmac.compare_digest(
        str(valor1),
        str(valor2)
    )


# =========================================================
# WEBHOOK
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
            "Solicitud con secreto incorrecto."
        )

        return "Unauthorized", 403

    try:

        datos = request.get_json(silent=True)

        if not datos:
            return "No content", 400

        update_id = datos.get("update_id")

        if update_id is not None:

            with update_lock:

                if update_id in processed_update_ids:

                    logger.info(
                        "Actualización duplicada ignorada: %s",
                        update_id
                    )

                    return "OK", 200

                processed_update_ids.add(update_id)

                if len(processed_update_ids) > 2000:

                    processed_update_ids.clear()
                    processed_update_ids.add(update_id)

        update = Update.de_json(
            datos,
            application.bot
        )

        asyncio.run_coroutine_threadsafe(
            application.process_update(update),
            event_loop
        )

        return "OK", 200

    except Exception as e:

        logger.exception(
            "Error recibiendo actualización: %s",
            e
        )

        return "Internal Server Error", 500


# =========================================================
# ESTADO DEL SERVICIO
# =========================================================

@app.route("/", methods=["GET"])
def inicio():

    return (
        "Noticiero Mundial Express está funcionando correctamente.",
        200
    )


@app.route("/health", methods=["GET"])
def health():

    return {
        "status": "online",
        "bot": "Noticiero Mundial Express"
    }, 200


# =========================================================
# CICLO DE EVENTOS
# =========================================================

def ejecutar_event_loop(loop):

    asyncio.set_event_loop(loop)
    loop.run_forever()


# =========================================================
# INICIAR SERVIDOR
# =========================================================

def main():

    global application
    global event_loop

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

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

    application.add_error_handler(
        error_handler
    )

    event_loop = asyncio.new_event_loop()

    hilo = threading.Thread(
        target=ejecutar_event_loop,
        args=(event_loop,),
        daemon=True
    )

    hilo.start()

    futuro = asyncio.run_coroutine_threadsafe(
        inicializar_bot(),
        event_loop
    )

    futuro.result(timeout=60)

    logger.info(
        "Noticiero Mundial Express iniciado correctamente."
    )

    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True,
        use_reloader=False
    )


if __name__ == "__main__":
    main()
