
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
import unicodedata

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

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36"
)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TELEGRAM_PHOTO_BYTES = 9 * 1024 * 1024
MAX_CAPTION_LENGTH = 1024

processed_update_ids = set()
update_lock = threading.Lock()

app = Flask(__name__)
application = None
event_loop = None


# =========================================================
# LOGS
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


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


def normalizar_texto(texto):

    texto = unicodedata.normalize("NFKD", str(texto).lower())

    texto = "".join(
        c for c in texto
        if not unicodedata.combining(c)
    )

    texto = re.sub(r"[^a-z0-9áéíóúñü]+", " ", texto)

    return texto.strip()


def obtener_palabras(texto):

    palabras = normalizar_texto(texto).split()

    detener = {
        "de", "la", "el", "en", "y", "a", "los", "las",
        "un", "una", "del", "por", "para", "con", "que",
        "se", "su", "sus", "al", "lo", "como", "sobre",
        "tras", "ante", "entre", "desde", "hasta",
        "the", "of", "and", "to", "in", "on", "for",
        "with", "after", "before", "new", "latest"
    }

    return {
        palabra for palabra in palabras
        if len(palabra) >= 3 and palabra not in detener
    }


# =========================================================
# PARSER DE IMÁGENES HTML
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

def descargar_url(url, timeout=15, referer=None, max_bytes=4 * 1024 * 1024):

    try:

        encabezados = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml,application/rss+xml,*/*;q=0.8"
            ),
            "Accept-Language": "es-419,es;q=0.9,en;q=0.7",
        }

        if referer:
            encabezados["Referer"] = referer

        solicitud = urllib.request.Request(
            url,
            headers=encabezados
        )

        with urllib.request.urlopen(solicitud, timeout=timeout) as respuesta:

            datos = respuesta.read(max_bytes + 1)

            if len(datos) > max_bytes:
                logger.warning("Contenido demasiado grande: %s", url[:150])
                return None

            return datos

    except urllib.error.HTTPError as e:

        logger.warning(
            "HTTP %s al descargar %s",
            e.code,
            url[:150]
        )

        return None

    except Exception as e:

        logger.warning(
            "No se pudo descargar contenido web: %s",
            e
        )

        return None


# =========================================================
# ABRIR PUBLICACIÓN ORIGINAL
# =========================================================

def descargar_pagina_original(url, timeout=18):

    try:

        solicitud = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*",
                "Accept-Language": "es-419,es;q=0.9,en;q=0.7",
                "Referer": "https://news.google.com/",
            }
        )

        with urllib.request.urlopen(solicitud, timeout=timeout) as respuesta:

            contenido = respuesta.read(4 * 1024 * 1024)
            url_final = respuesta.geturl()

            logger.info(
                "URL final de la noticia: %s",
                url_final[:200]
            )

            return contenido, url_final

    except urllib.error.HTTPError as e:

        logger.warning(
            "Google News / medio respondió HTTP %s",
            e.code
        )

        return None, url

    except Exception as e:

        logger.warning(
            "No se pudo resolver la publicación original: %s",
            e
        )

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
# FILTRAR IMÁGENES GENÉRICAS
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
            "blank",
            "transparent",
            "banner-ad",
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

        if etiqueta.endswith("enclosure"):

            imagen = elemento.attrib.get("url")
            tipo = elemento.attrib.get("type", "").lower()

            if imagen and "image" in tipo:

                candidatos.append({
                    "url": imagen,
                    "tipo": "rss-enclosure",
                    "alt": "",
                })

    descripcion = ""

    for elemento in item:

        if elemento.tag.lower().endswith("description"):

            descripcion = elemento.text or ""
            break

    parser = NewsImageParser()

    try:

        parser.feed(descripcion)
        candidatos.extend(parser.imagenes)

    except Exception:
        pass

    resultado = []
    revisadas = set()

    for candidato in candidatos:

        url = candidato.get("url", "").strip()

        if url and url not in revisadas:

            revisadas.add(url)
            resultado.append(candidato)

    return resultado


# =========================================================
# GOOGLE NEWS RSS
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

            for elemento in item:

                if elemento.tag.lower().endswith("source"):
                    fuente = elemento.text or "Google News"
                    break

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

    logger.info("RSS: %s noticias encontradas.", len(noticias))

    return noticias


# =========================================================
# EXTRAER IMÁGENES JSON-LD
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
# DESCARGAR IMAGEN
# =========================================================

def descargar_imagen(url, referer=None):

    try:

        encabezados = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "image/avif,image/webp,image/apng,"
                "image/jpeg,image/png,image/*,*/*;q=0.8"
            ),
            "Accept-Language": "es-419,es;q=0.9",
        }

        if referer:
            encabezados["Referer"] = referer

        solicitud = urllib.request.Request(
            url,
            headers=encabezados
        )

        with urllib.request.urlopen(solicitud, timeout=18) as respuesta:

            tipo = respuesta.headers.get("Content-Type", "").lower()

            if tipo and not tipo.startswith("image/"):

                logger.info(
                    "El servidor no entregó una imagen: %s",
                    tipo
                )

                return None

            datos = respuesta.read(MAX_IMAGE_BYTES + 1)

            if not datos or len(datos) > MAX_IMAGE_BYTES:
                return None

            return datos

    except urllib.error.HTTPError as e:

        logger.warning(
            "HTTP %s al descargar fotografía: %s",
            e.code,
            url[:150]
        )

        return None

    except Exception as e:

        logger.warning(
            "Error descargando fotografía: %s",
            e
        )

        return None


# =========================================================
# VALIDAR FOTOGRAFÍA
# =========================================================

def descargar_fotografia_valida(url, alt="", referer=None):

    if imagen_es_generica(url, alt):
        return None

    datos = descargar_imagen(
        url,
        referer=referer
    )

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
# PROBAR CANDIDATOS
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
            imagen[:160]
        )

        datos = descargar_fotografia_valida(
            imagen,
            alt,
            referer=url_base
        )

        if datos:
            return datos

    return None


# =========================================================
# BUSCAR EN WIKIMEDIA COMMONS
# =========================================================

def buscar_imagen_commons(titulo):

    palabras_titulo = obtener_palabras(titulo)

    if len(palabras_titulo) < 2:
        return None

    consulta = " ".join(list(palabras_titulo)[:8])

    parametros = {
        "action": "query",
        "generator": "search",
        "gsrsearch": consulta,
        "gsrnamespace": "6",
        "gsrlimit": "10",
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": "1200",
        "format": "json",
        "origin": "*",
    }

    url = (
        "https://commons.wikimedia.org/w/api.php?"
        + urllib.parse.urlencode(parametros)
    )

    contenido = descargar_url(
        url,
        timeout=18,
        referer="https://commons.wikimedia.org/"
    )

    if not contenido:
        return None

    try:

        resultado = json.loads(contenido.decode("utf-8"))

        paginas = resultado.get(
            "query", {}
        ).get("pages", {})

        candidatos = []

        for pagina in paginas.values():

            titulo_archivo = pagina.get("title", "")
            palabras_archivo = obtener_palabras(titulo_archivo)

            coincidencias = palabras_titulo.intersection(
                palabras_archivo
            )

            if not coincidencias:
                continue

            puntuacion = (
                len(coincidencias)
                / max(1, len(palabras_titulo))
            )

            # Filtro estricto para evitar fotografías sin relación.
            if len(coincidencias) < 2 or puntuacion < 0.35:
                continue

            imagenes = pagina.get("imageinfo", [])

            if not imagenes:
                continue

            info = imagenes[0]

            mime = info.get("mime", "")

            if mime not in [
                "image/jpeg",
                "image/png",
                "image/webp",
            ]:
                continue

            url_imagen = (
                info.get("thumburl")
                or info.get("url")
            )

            if not url_imagen:
                continue

            metadatos = info.get(
                "extmetadata", {}
            )

            autor = limpiar_html(
                metadatos.get("Artist", {}).get("value", "")
            )

            licencia = limpiar_html(
                metadatos.get("LicenseShortName", {}).get("value", "")
            )

            pagina_imagen = info.get(
                "descriptionurl",
                "https://commons.wikimedia.org/"
            )

            candidatos.append({
                "url": url_imagen,
                "puntuacion": puntuacion,
                "autor": autor[:100],
                "licencia": licencia[:60],
                "pagina": pagina_imagen,
            })

        candidatos.sort(
            key=lambda x: x["puntuacion"],
            reverse=True
        )

        for candidato in candidatos[:4]:

            datos = descargar_fotografia_valida(
                candidato["url"],
                referer="https://commons.wikimedia.org/"
            )

            if datos:

                autor = candidato["autor"] or "No indicado"
                licencia = candidato["licencia"] or "Consultar fuente"

                logger.info(
                    "Fotografía alternativa encontrada en Wikimedia Commons."
                )

                return {
                    "datos": datos,
                    "credito": (
                        "📷 Imagen: Wikimedia Commons\n"
                        f"Autor: {autor}\n"
                        f"Licencia: {licencia}\n"
                        f"Fuente: {candidato['pagina']}"
                    ),
                }

    except Exception as e:

        logger.exception(
            "Error consultando Wikimedia Commons: %s",
            e
        )

    return None


# =========================================================
# BUSCAR FOTOGRAFÍA EN LA NOTICIA ORIGINAL
# =========================================================

def buscar_imagen_original(noticia):

    candidatos_rss = noticia.get("imagenes", [])

    logger.info(
        "El RSS contiene %s candidatos de imagen.",
        len(candidatos_rss)
    )

    # PRIMER INTENTO: RSS.

    imagen_rss = probar_candidatos(
        candidatos_rss,
        noticia["enlace"]
    )

    if imagen_rss:

        logger.info("Fotografía recuperada desde el RSS.")

        return {
            "datos": imagen_rss,
            "credito": "📷 Fotografía de la publicación original.",
        }

    # SEGUNDO INTENTO: publicación original.

    contenido, url_final = descargar_pagina_original(
        noticia["enlace"]
    )

    if contenido:

        dominio_final = urllib.parse.urlparse(
            url_final
        ).netloc.lower()

        if dominio_final and not es_dominio_google(dominio_final):

            try:

                texto_html = contenido.decode(
                    "utf-8",
                    errors="replace"
                )

                parser = NewsImageParser()
                parser.feed(texto_html)

                candidatos = []

                # Open Graph primero.
                for imagen in parser.imagenes:

                    if imagen["tipo"] in [
                        "og:image",
                        "og:image:url",
                    ]:
                        candidatos.append(imagen)

                # Twitter después.
                for imagen in parser.imagenes:

                    if imagen["tipo"] in [
                        "twitter:image",
                        "twitter:image:src",
                    ]:
                        candidatos.append(imagen)

                # JSON-LD.
                candidatos.extend(
                    extraer_imagenes_jsonld(
                        parser.jsonld_scripts
                    )
                )

                # Imágenes HTML como último recurso.
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

                    return {
                        "datos": fotografia,
                        "credito": "📷 Fotografía de la publicación original.",
                    }

            except Exception as e:

                logger.exception(
                    "Error extrayendo fotografías del artículo: %s",
                    e
                )

        else:

            logger.info(
                "Google News no permitió llegar al medio original."
            )

    else:

        logger.info(
            "No se pudo abrir la página original."
        )

    # TERCER INTENTO: Wikimedia Commons.
    # Solo se utiliza si hay coincidencia suficiente en el nombre.

    logger.info(
        "Buscando fotografía alternativa con coincidencia de título."
    )

    alternativa = buscar_imagen_commons(
        noticia["titulo"]
    )

    if alternativa:
        return alternativa

    logger.info(
        "No se encontró una fotografía válida para esta noticia."
    )

    return None


def buscar_fotografia(noticia):

    return buscar_imagen_original(noticia)


# =========================================================
# PREPARAR IMAGEN PARA TELEGRAM
# =========================================================

def preparar_imagen_telegram(datos):

    try:

        imagen = Image.open(BytesIO(datos))

        imagen = ImageOps.exif_transpose(imagen)

        imagen.load()

        # Telegram acepta JPEG de forma confiable.
        if imagen.mode != "RGB":
            imagen = imagen.convert("RGB")

        # Reducir dimensiones para mejorar velocidad y tamaño.
        imagen.thumbnail(
            (1600, 1600),
            Image.Resampling.LANCZOS
        )

        salida = BytesIO()

        calidad = 88

        while calidad >= 55:

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

            calidad -= 8

        resultado = salida.getvalue()

        if not resultado:
            return None

        if len(resultado) > MAX_TELEGRAM_PHOTO_BYTES:
            return None

        return resultado

    except (UnidentifiedImageError, OSError, ValueError) as e:

        logger.warning(
            "No se pudo preparar la fotografía: %s",
            e
        )

        return None

    except Exception as e:

        logger.exception(
            "Error convirtiendo imagen para Telegram: %s",
            e
        )

        return None


# =========================================================
# RESUMEN CON GEMINI EN UNA SOLA SOLICITUD
# =========================================================

def generar_resumenes_lote(noticias):

    resumenes = [
        (
            noticia["descripcion"][:450]
            if noticia["descripcion"]
            else "No hay información suficiente para generar el resumen."
        )
        for noticia in noticias
    ]

    if not GEMINI_API_KEY:
        logger.warning("No está configurada GEMINI_API_KEY.")
        return resumenes

    entradas = []

    for i, noticia in enumerate(noticias):

        entradas.append({
            "id": i,
            "titular": noticia["titulo"],
            "descripcion": noticia["descripcion"][:1200],
            "fuente": noticia["fuente"],
        })

    instruccion = (
        "Eres un periodista que resume noticias en español. "
        "Resume cada noticia en 2 o 3 oraciones cortas, "
        "con lenguaje sencillo, claro y neutral. "
        "No inventes datos, fechas, cifras ni declaraciones. "
        "Si falta información, resume solamente lo disponible. "
        "No agregues opiniones. "
        "Devuelve exclusivamente un JSON válido con esta estructura: "
        '{"resumenes":[{"id":0,"resumen":"texto"}]}. '
        "Debes incluir un resumen por cada noticia y conservar su ID.\n\n"
        "NOTICIAS:\n"
        + json.dumps(entradas, ensure_ascii=False)
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
            "temperature": 0.2,
            "maxOutputTokens": 1200,
            "responseMimeType": "application/json",
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
            timeout=45
        ) as respuesta:

            resultado = json.loads(
                respuesta.read().decode("utf-8")
            )

        candidatos = resultado.get("candidates", [])

        if not candidatos:
            return resumenes

        partes = (
            candidatos[0]
            .get("content", {})
            .get("parts", [])
        )

        texto = " ".join(
            parte.get("text", "")
            for parte in partes
        ).strip()

        texto = re.sub(r"^```(?:json)?\s*", "", texto)
        texto = re.sub(r"\s*```$", "", texto)

        resultado_json = json.loads(texto)

        for elemento in resultado_json.get("resumenes", []):

            identificador = elemento.get("id")
            resumen = elemento.get("resumen", "").strip()

            if (
                isinstance(identificador, int)
                and 0 <= identificador < len(resumenes)
                and resumen
            ):

                resumenes[identificador] = resumen[:1200]

        logger.info(
            "Gemini generó resúmenes para %s noticias.",
            len(noticias)
        )

        return resumenes

    except urllib.error.HTTPError as e:

        try:
            detalle = e.read().decode(
                "utf-8",
                errors="replace"
            )
        except Exception:
            detalle = str(e)

        logger.error(
            "ERROR GEMINI HTTP %s: %s",
            e.code,
            detalle[:1500]
        )

        logger.warning(
            "Se utilizarán los textos originales del RSS."
        )

        return resumenes

    except Exception as e:

        logger.exception(
            "Error generando resúmenes por lote: %s",
            e
        )

        return resumenes


# =========================================================
# CREAR MENSAJE
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

async def enviar_fotografia_y_texto(
    chat,
    fotografia,
    mensaje
):

    imagen_jpg = await asyncio.to_thread(
        preparar_imagen_telegram,
        fotografia["datos"]
    )

    if not imagen_jpg:

        logger.warning(
            "No se pudo preparar la imagen. Se enviará solo texto."
        )

        await chat.send_message(
            mensaje,
            parse_mode="HTML",
            disable_web_page_preview=False
        )

        return False

    try:

        if len(mensaje) <= MAX_CAPTION_LENGTH:

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

        else:

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

        logger.info("Fotografía enviada correctamente.")
        return True

    except Exception as e:

        logger.exception(
            "FALLO AL ENVIAR FOTOGRAFÍA A TELEGRAM: %s",
            e
        )

        try:

            await chat.send_message(
                mensaje,
                parse_mode="HTML",
                disable_web_page_preview=False
            )

        except Exception as segundo_error:

            logger.exception(
                "También falló el envío del texto: %s",
                segundo_error
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
            "Preparando resúmenes y fotografías..."
        )

        # Una sola llamada a Gemini para las cinco noticias.
        resumenes = await asyncio.to_thread(
            generar_resumenes_lote,
            noticias
        )

        for numero, noticia in enumerate(noticias, start=1):

            try:

                logger.info(
                    "Procesando noticia %s de %s: %s",
                    numero,
                    len(noticias),
                    noticia["titulo"]
                )

                resumen = resumenes[numero - 1]

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

                try:

                    await chat.send_message(
                        "⚠️ No pude procesar una noticia. "
                        "Continuaré con las demás."
                    )

                except Exception:
                    pass

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
