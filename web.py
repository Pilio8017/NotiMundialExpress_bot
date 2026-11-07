
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
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes


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

logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

app = Flask(__name__)

application = None
event_loop = None

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TELEGRAM_PHOTO_BYTES = 9 * 1024 * 1024
MAX_CAPTION_LENGTH = 1024

processed_update_ids = set()
update_lock = threading.Lock()


# =========================================================
# LIMPIAR TEXTO
# =========================================================

def limpiar_html(texto):

    if not texto:
        return ""

    texto = re.sub(r"<[^>]+>", " ", str(texto))
    texto = html.unescape(texto)
    texto = re.sub(r"\s+", " ", texto)

    return texto.strip()


# =========================================================
# EXTRAER IMÁGENES HTML Y METADATOS
# =========================================================

class NewsImageParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.imagenes = []
        self.jsonld_scripts = []
        self.capturando_jsonld = False
        self.jsonld_actual = []

    def handle_starttag(self, tag, attrs):

        atributos = dict(attrs)
        tag = tag.lower()

        if tag == "meta":

            propiedad = (
                atributos.get("property", "")
                or atributos.get("name", "")
                or atributos.get("itemprop", "")
            ).lower()

            imagen = atributos.get("content")

            if imagen and propiedad in [
                "og:image",
                "og:image:url",
                "twitter:image",
                "twitter:image:src",
                "image",
            ]:

                self.imagenes.append({
                    "url": imagen,
                    "tipo": propiedad,
                    "alt": atributos.get("alt", ""),
                })

        elif tag == "script":

            tipo = atributos.get("type", "").lower()

            if "ld+json" in tipo:
                self.capturando_jsonld = True
                self.jsonld_actual = []

        elif tag == "img":

            imagen = (
                atributos.get("data-src")
                or atributos.get("data-original")
                or atributos.get("data-lazy-src")
                or atributos.get("src")
            )

            if imagen:
                self.imagenes.append({
                    "url": imagen,
                    "tipo": "html-img",
                    "alt": atributos.get("alt", ""),
                })

            srcset = atributos.get("srcset", "")

            if srcset:
                for parte in srcset.split(","):
                    url = parte.strip().split(" ")[0]

                    if url:
                        self.imagenes.append({
                            "url": url,
                            "tipo": "html-srcset",
                            "alt": atributos.get("alt", ""),
                        })

    def handle_data(self, data):

        if self.capturando_jsonld:
            self.jsonld_actual.append(data)

    def handle_endtag(self, tag):

        if tag.lower() == "script" and self.capturando_jsonld:

            contenido = "".join(self.jsonld_actual).strip()

            if contenido:
                self.jsonld_scripts.append(contenido)

            self.capturando_jsonld = False
            self.jsonld_actual = []


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

        with urllib.request.urlopen(solicitud, timeout=timeout) as respuesta:
            return respuesta.read()

    except Exception as e:

        logger.warning("No se pudo descargar contenido web: %s", e)
        return None


# =========================================================
# ABRIR Y RESOLVER LA PUBLICACIÓN ORIGINAL
# =========================================================

def descargar_pagina_original(url, timeout=15):

    try:

        solicitud = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*",
            },
        )

        with urllib.request.urlopen(solicitud, timeout=timeout) as respuesta:

            contenido = respuesta.read(4 * 1024 * 1024)
            url_final = respuesta.geturl()

            return contenido, url_final

    except Exception as e:

        logger.warning("No se pudo abrir la publicación: %s", e)
        return None, url


# =========================================================
# IDENTIFICAR DOMINIOS
# =========================================================

def es_dominio_google(dominio):

    dominio = dominio.lower().split(":")[0]

    bloqueados = [
        "news.google.com",
        "google.com",
    ]

    return any(
        dominio == d or dominio.endswith("." + d)
        for d in bloqueados
    )


# =========================================================
# FILTRO DE IMÁGENES GENÉRICAS
# =========================================================

def imagen_es_generica(url, alt=""):

    try:

        partes = urllib.parse.urlparse(url)
        dominio = partes.netloc.lower()
        ruta = urllib.parse.unquote(partes.path).lower()

        if not dominio:
            return True

        if dominio == "news.google.com":
            return True

        texto = ruta + " " + str(alt).lower()

        bloqueadas = [
            "favicon",
            "placeholder",
            "no-image",
            "noimage",
            "default-avatar",
            "social-icon",
            "site-logo",
            "logo",
            "avatar",
            "sprite",
            "tracking",
            "pixel",
            "advertisement",
            "loading",
            "default-image",
        ]

        if any(p in texto for p in bloqueadas):
            return True

        if ruta.endswith((".svg", ".ico")):
            return True

        return False

    except Exception:
        return True


# =========================================================
# EXTRAER IMÁGENES DEL RSS
# =========================================================

def extraer_imagenes_rss(item):

    candidatos = []

    for elemento in item.iter():

        etiqueta = elemento.tag.lower()

        # media:content y media:thumbnail
        if (
            etiqueta.endswith("}content")
            or etiqueta.endswith("}thumbnail")
        ):

            imagen = elemento.attrib.get("url")

            if imagen:
                candidatos.append({
                    "url": imagen,
                    "tipo": "rss-media",
                    "alt": "",
                })

        # enclosure de tipo imagen
        if etiqueta.endswith("enclosure"):

            imagen = elemento.attrib.get("url")
            tipo = elemento.attrib.get("type", "").lower()

            if imagen and "image" in tipo:
                candidatos.append({
                    "url": imagen,
                    "tipo": "rss-enclosure",
                    "alt": "",
                })

    # Imágenes incluidas en la descripción del RSS.
    descripcion = ""

    elemento_descripcion = item.find("description")

    if elemento_descripcion is not None:
        descripcion = elemento_descripcion.text or ""

    parser = NewsImageParser()

    try:
        parser.feed(descripcion)
        candidatos.extend(parser.imagenes)
    except Exception:
        pass

    # Eliminar duplicados.
    resultado = []
    revisadas = set()

    for candidato in candidatos:

        url = candidato.get("url", "").strip()

        if url and url not in revisadas:
            revisadas.add(url)
            resultado.append(candidato)

    return resultado


# =========================================================
# CONSULTAR GOOGLE NEWS RSS
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
                "imagenes": extraer_imagenes_rss(item),
            })

    except Exception as e:

        logger.exception("Error procesando Google News RSS: %s", e)

    return noticias


# =========================================================
# EXTRAER IMÁGENES DE JSON-LD
# =========================================================

def extraer_imagenes_jsonld(scripts):

    imagenes = []

    def recorrer(objeto):

        if isinstance(objeto, dict):

            for clave, valor in objeto.items():

                if clave.lower() == "image":

                    if isinstance(valor, str):

                        imagenes.append({
                            "url": valor,
                            "tipo": "jsonld",
                            "alt": "",
                        })

                    elif isinstance(valor, list):

                        for elemento in valor:

                            if isinstance(elemento, str):

                                imagenes.append({
                                    "url": elemento,
                                    "tipo": "jsonld",
                                    "alt": "",
                                })

                            elif isinstance(elemento, dict):

                                url = (
                                    elemento.get("url")
                                    or elemento.get("contentUrl")
                                )

                                if url:
                                    imagenes.append({
                                        "url": url,
                                        "tipo": "jsonld",
                                        "alt": "",
                                    })

                    elif isinstance(valor, dict):

                        url = (
                            valor.get("url")
                            or valor.get("contentUrl")
                        )

                        if url:
                            imagenes.append({
                                "url": url,
                                "tipo": "jsonld",
                                "alt": "",
                            })

                else:
                    recorrer(valor)

        elif isinstance(objeto, list):

            for elemento in objeto:
                recorrer(elemento)

    for script in scripts:

        try:
            recorrer(json.loads(script))
        except Exception:
            continue

    return imagenes


# =========================================================
# DESCARGAR UNA IMAGEN
# =========================================================

def descargar_imagen(url):

    try:

        solicitud = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": (
                    "image/avif,image/webp,image/apng,"
                    "image/*,*/*;q=0.8"
                ),
            },
        )

        with urllib.request.urlopen(solicitud, timeout=15) as respuesta:

            tipo = respuesta.headers.get("Content-Type", "").lower()

            if tipo and not tipo.startswith("image/"):
                logger.info("El recurso no es una imagen: %s", tipo)
                return None

            datos = respuesta.read(MAX_IMAGE_BYTES + 1)

            if not datos or len(datos) > MAX_IMAGE_BYTES:
                return None

            return datos

    except Exception as e:

        logger.warning("No se pudo descargar imagen: %s", e)
        return None


# =========================================================
# VALIDAR FOTOGRAFÍA
# =========================================================

def descargar_fotografia_valida(url, alt=""):

    if imagen_es_generica(url, alt):
        return None

    datos = descargar_imagen(url)

    if not datos:
        return None

    try:

        imagen = Image.open(BytesIO(datos))

        ancho, alto = imagen.size

        if ancho < 180 or alto < 120:
            return None

        proporcion = ancho / alto

        if proporcion < 0.45 or proporcion > 3.5:
            return None

        imagen.verify()

        logger.info(
            "Fotografía válida: %s x %s",
            ancho,
            alto
        )

        return datos

    except Exception as e:

        logger.info("Imagen descartada: %s", e)
        return None


# =========================================================
# PROBAR CANDIDATOS DE IMÁGENES
# =========================================================

def probar_candidatos(candidatos, url_base):

    revisadas = set()

    for candidato in candidatos:

        if isinstance(candidato, str):
            candidato = {
                "url": candidato,
                "tipo": "desconocido",
                "alt": "",
            }

        imagen = candidato.get("url", "")
        alt = candidato.get("alt", "")

        if not imagen:
            continue

        imagen = html.unescape(imagen.strip())

        imagen = urllib.parse.urljoin(
            url_base,
            imagen
        )

        if imagen in revisadas:
            continue

        revisadas.add(imagen)

        if not imagen.startswith(("http://", "https://")):
            continue

        if imagen_es_generica(imagen, alt):
            continue

        logger.info(
            "Probando imagen (%s): %s",
            candidato.get("tipo", ""),
            imagen[:180]
        )

        datos = descargar_fotografia_valida(
            imagen,
            alt
        )

        if datos:
            return datos

    return None


# =========================================================
# BUSCAR FOTOGRAFÍA EN LA NOTICIA ORIGINAL Y RSS
# =========================================================

def buscar_imagen_original(noticia):

    # PRIMER INTENTO: imágenes del RSS.
    # Se prueban incluso si Google News no redirige al medio.

    candidatos_rss = noticia.get("imagenes", [])

    logger.info(
        "El RSS contiene %s candidatos de imagen.",
        len(candidatos_rss)
    )

    imagen_rss = probar_candidatos(
        candidatos_rss,
        noticia["enlace"]
    )

    if imagen_rss:
        logger.info("Fotografía recuperada desde el RSS.")
        return imagen_rss

    # SEGUNDO INTENTO: abrir el enlace de la noticia.

    contenido, url_final = descargar_pagina_original(
        noticia["enlace"]
    )

    if not contenido:
        logger.info("No se pudo abrir la página de la noticia.")
        return None

    dominio_final = urllib.parse.urlparse(
        url_final
    ).netloc.lower()

    if not dominio_final or es_dominio_google(dominio_final):

        logger.info(
            "Google News no permitió llegar al medio original."
        )

        return None

    # TERCER INTENTO: metadatos del medio original.

    try:

        texto_html = contenido.decode(
            "utf-8",
            errors="replace"
        )

        parser = NewsImageParser()
        parser.feed(texto_html)

        candidatos = []

        # Primero Open Graph.
        for imagen in parser.imagenes:

            if imagen["tipo"] in [
                "og:image",
                "og:image:url",
            ]:
                candidatos.append(imagen)

        # Después Twitter.
        for imagen in parser.imagenes:

            if imagen["tipo"] in [
                "twitter:image",
                "twitter:image:src",
            ]:
                candidatos.append(imagen)

        # Después JSON-LD.
        candidatos.extend(
            extraer_imagenes_jsonld(
                parser.jsonld_scripts
            )
        )

        # Finalmente imágenes HTML como último recurso.
        # Se mantienen los filtros de logos y tamaños.

        for imagen in parser.imagenes:

            if imagen["tipo"] in [
                "html-img",
                "html-srcset",
            ]:
                candidatos.append(imagen)

        fotografia = probar_candidatos(
            candidatos,
            url_final
        )

        if fotografia:

            logger.info(
                "Fotografía encontrada en el medio original."
            )

            return fotografia

    except Exception as e:

        logger.exception(
            "Error extrayendo fotografías del artículo: %s",
            e
        )

    logger.info("No se encontró fotografía válida.")
    return None


# =========================================================
# SISTEMA DE BÚSQUEDA DE FOTOGRAFÍAS
# =========================================================

def buscar_fotografia(noticia):

    imagen = buscar_imagen_original(noticia)

    if imagen:

        return {
            "datos": imagen,
            "credito": "📷 Fotografía de la noticia.",
        }

    logger.info(
        "No se encontró una imagen disponible para esta noticia."
    )

    return None


# =========================================================
# RESUMEN CON GEMINI
# =========================================================

def generar_resumen(titulo, descripcion, fuente):

    if not GEMINI_API_KEY:

        logger.warning("No está configurada GEMINI_API_KEY.")

        return (
            descripcion[:450]
            if descripcion
            else "No hay información suficiente para generar el resumen."
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

        with urllib.request.urlopen(solicitud, timeout=25) as respuesta:

            resultado = json.loads(
                respuesta.read().decode("utf-8")
            )

        candidatos = resultado.get("candidates", [])

        if not candidatos:

            return (
                descripcion[:450]
                if descripcion
                else "No fue posible generar el resumen."
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

        return (
            descripcion[:450]
            if descripcion
            else "No fue posible generar el resumen."
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
            "ERROR DE GEMINI - HTTP %s: %s",
            e.code,
            detalle
        )

        return (
            descripcion[:450]
            if descripcion
            else "No fue posible generar el resumen."
        )

    except Exception as e:

        logger.exception(
            "ERROR AL GENERAR RESUMEN CON GEMINI: %s",
            e
        )

        return (
            descripcion[:450]
            if descripcion
            else "No fue posible generar el resumen."
        )


# =========================================================
# CREAR MENSAJE DE NOTICIA
# =========================================================

def crear_mensaje(noticia, resumen, credito=None):

    titulo = html.escape(noticia["titulo"])
    fuente = html.escape(noticia["fuente"])

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
        mensaje += f"{html.escape(credito)}\n\n"

    mensaje += (
        f'<a href="{enlace}">'
        "🔗 Leer noticia completa</a>"
    )

    return mensaje


# =========================================================
# ENVIAR FOTOGRAFÍA Y TEXTO
# =========================================================

async def enviar_fotografia_y_texto(chat, fotografia, mensaje):

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

            logger.info("Fotografía enviada correctamente.")
            return True

        except Exception as e:

            logger.exception(
                "FALLO AL ENVIAR FOTOGRAFÍA CON CAPTION: %s",
                e
            )

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

                await chat.send_message(
                    mensaje,
                    parse_mode="HTML",
                    disable_web_page_preview=False
                )

                return True

            except Exception as segundo_error:

                logger.exception(
                    "FALLO AL ENVIAR FOTOGRAFÍA: %s",
                    segundo_error
                )

                await chat.send_message(
                    mensaje,
                    parse_mode="HTML",
                    disable_web_page_preview=False
                )

                return False

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

        for numero, noticia in enumerate(noticias, start=1):

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

                credito = (
                    fotografia["credito"]
                    if fotografia
                    else None
                )

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

    logger.info("Webhook configurado correctamente.")


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

        logger.warning("Solicitud con secreto incorrecto.")
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

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("noticias", noticias))
    application.add_handler(CommandHandler("cine", cine))
    application.add_handler(CommandHandler("mercados", mercados))
    application.add_handler(CommandHandler("deportes", deportes))
    application.add_handler(CommandHandler("ayuda", ayuda))

    application.add_error_handler(error_handler)

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
