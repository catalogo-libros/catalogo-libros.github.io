"""
Catálogo de Libros Británicos
-----------------------------
Panel de administración local + generador del sitio público.

Instalación:   pip install flask instaloader pillow
Uso:           python catalogo.py        (se abre el navegador solo)

Imágenes:      se guardan en alta calidad (hasta MAX_SIDE px, WebP calidad 90 o el archivo
               original si ya es liviano) y el sitio genera miniaturas aparte (imagenes/t/)
               para que la grilla cargue rápido y la ampliación se vea nítida.

Resultado:     carpeta "sitio/" con index.html + imagenes/  -> eso es lo que subís a la web.
"""
import functools
import io
import json
import re
import os
import html as html_lib
import hashlib
import shutil
import sys
import threading
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

# ======================= CONFIGURACIÓN =======================
WHATSAPP = "5492215050333"      # código de país + área + número, sin espacios ni signos
IG_PERFIL = "librosbritanicos"  # perfil de Instagram del que se importan publicaciones
IG_USER = os.getenv("INSTAGRAM_USER", "").strip()  # opcional; también se detecta instagram_session_user.txt
PORT = 5000
# =============================================================

BASE = Path(__file__).resolve().parent
SCRIPT_NAME = Path(__file__).name
DATA_FILE = BASE / "books.json"
IMG_DIR = BASE / "imagenes"
SITE_DIR = BASE / "sitio"
THUMB_CACHE = BASE / ".thumbs"   # miniaturas del panel (agregar a .gitignore)
ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
IMG_DIR.mkdir(exist_ok=True)

MAX_SIDE = 2400            # lado mayor máximo de la imagen guardada (nunca se agranda)
WEBP_QUALITY = 90          # calidad al recomprimir
KEEP_ORIGINAL_BYTES = 2_500_000  # si el archivo ya entra en MAX_SIDE y pesa menos, se guarda tal cual
THUMB_SIDE = 640           # miniaturas para la grilla del catálogo
DATA_LOCK = threading.RLock()


def locked(fn):
    """Serializa las operaciones que leen y reescriben books.json (evita pisar cambios)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with DATA_LOCK:
            return fn(*args, **kwargs)
    return wrapper


def _lanczos():
    from PIL import Image
    return getattr(Image, "Resampling", Image).LANCZOS


def save_image(fileobj, dest):
    """Guarda la foto con la mayor fidelidad posible y devuelve la ruta realmente guardada.

    - Si ya es JPEG/PNG/WebP/GIF, entra en MAX_SIDE y es liviana: se conserva el archivo
      ORIGINAL sin recomprimir (cero pérdida).
    - Si no: se reduce solo si supera MAX_SIDE (jamás se agranda) y se guarda en WebP q90.
    - Sin Pillow o si falla, se conservan los bytes originales.
    """
    fileobj.seek(0)
    raw = fileobj.read()
    try:
        from PIL import Image, ImageOps
    except ImportError:
        dest.write_bytes(raw)
        return dest

    try:
        with Image.open(io.BytesIO(raw)) as source:
            fmt = (source.format or "").upper()
            width, height = source.size
            ext_by_format = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}
            if fmt in ext_by_format and max(width, height) <= MAX_SIDE and len(raw) <= KEEP_ORIGINAL_BYTES:
                target = dest.with_suffix(ext_by_format[fmt])
                target.write_bytes(raw)
                if target != dest:
                    dest.unlink(missing_ok=True)
                return target

            img = ImageOps.exif_transpose(source)
            img.thumbnail((MAX_SIDE, MAX_SIDE), _lanczos())  # solo reduce
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA" if "transparency" in img.info else "RGB")
            output = dest.with_suffix(".webp")
            img.save(output, format="WEBP", quality=WEBP_QUALITY, method=6)
        if output != dest:
            dest.unlink(missing_ok=True)
        return output
    except Exception:
        dest.with_suffix(".webp").unlink(missing_ok=True)
        dest.write_bytes(raw)
        return dest


def make_thumb(src, dst, side=THUMB_SIDE):
    """Genera (o reutiliza) una miniatura WebP. Devuelve la ruta o None si no se pudo."""
    try:
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            return dst
        from PIL import Image, ImageOps
        dst.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as source:
            img = ImageOps.exif_transpose(source)
            img.thumbnail((side, side), _lanczos())
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA" if "transparency" in img.info else "RGB")
            img.save(dst, format="WEBP", quality=82, method=4)
        return dst
    except Exception:
        dst.unlink(missing_ok=True)
        return None


def safe_image_rel(path):
    """Devuelve 'imagenes/x.ext' si la ruta apunta a un archivo real dentro de IMG_DIR; si no, None."""
    if not isinstance(path, str) or not path.startswith("imagenes/"):
        return None
    target = (BASE / path).resolve()
    try:
        rel = target.relative_to(IMG_DIR.resolve())
    except ValueError:
        return None
    return "imagenes/" + rel.as_posix() if target.is_file() else None


def delete_image(path):
    """Borra una foto y su miniatura del panel (solo dentro de IMG_DIR)."""
    rel = safe_image_rel(path)
    if not rel:
        return
    (BASE / rel).unlink(missing_ok=True)
    (THUMB_CACHE / Path(rel).relative_to("imagenes")).with_suffix(".webp").unlink(missing_ok=True)


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024


@app.before_request
def local_only_guard():
    """El panel es local: rechaza otros hosts (DNS rebinding) y escrituras desde webs ajenas (CSRF)."""
    if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
        abort(403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        own = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
        if origin and origin not in own and not origin.startswith("chrome-extension://"):
            abort(403)


# ----------------------- datos -----------------------
def load_books():
    books = []
    if DATA_FILE.exists():
        books = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    for b in books:  # migración del formato viejo (una sola "image")
        if "images" not in b:
            b["images"] = [b["image"]] if b.get("image") else []
        b.pop("image", None)
        # Pages CMS escribe rutas como "/imagenes/x.jpg"; las pasamos a relativas
        # para que funcionen dentro de https://usuario.github.io/catalogo-libros/
        b["images"] = [p[1:] if p.startswith("/imagenes/") else p for p in (b["images"] or [])]
        b["title"] = b.get("title") or "Sin título"
        for k in ("author", "publisher", "description"):
            b[k] = b.get(k) or ""
        b["category"] = b.get("category") or "General"
        b["year"] = num(b.get("year"))
        b["price"] = num(b.get("price"))
        b["language"] = b.get("language") or ""
        b["edition"] = b.get("edition") or ""
        b["binding"] = b.get("binding") or ""
        b["pages"] = num(b.get("pages"))
        b["isbn"] = b.get("isbn") or ""
        b["condition"] = b.get("condition") or ""
        for k in ("series", "translator", "illustrator", "format", "printer", "printing_place"):
            b[k] = b.get(k) or ""
        sold_value = b.get("sold_out", False)
        if isinstance(sold_value, str):
            b["sold_out"] = sold_value.strip().lower() in {"1", "true", "yes", "si", "sí", "on", "agotado"}
        else:
            b["sold_out"] = bool(sold_value)
        for k in ("identification_notes", "uncertain_fields"):
            if not isinstance(b.get(k), list):
                b[k] = [str(b[k])] if b.get(k) else []
    for b in books:  # los libros cargados desde Pages CMS no traen id
        if not b.get("id"):
            b["id"] = next_id(books)
    return books


def save_books(books):
    """Escritura atómica + copia de seguridad de la versión anterior (books.json.bak)."""
    tmp = DATA_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(books, ensure_ascii=False, indent=2), encoding="utf-8")
    if DATA_FILE.exists():
        shutil.copy2(DATA_FILE, DATA_FILE.with_suffix(".json.bak"))
    os.replace(tmp, DATA_FILE)


def next_id(books):
    nums = [int(m.group(1)) for b in books if (m := re.fullmatch(r"LIB-(\d+)", b.get("id", "")))]
    return f"LIB-{max(nums, default=0) + 1:03d}"


def num(value, default=0):
    """Entero tolerante: acepta '25.000', '25,000', '$ 25000', 1942.0, etc."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return default
    text = re.sub(r"[^\d.,\-]", "", str(value or ""))
    if not text:
        return default
    if re.fullmatch(r"-?\d{1,3}([.,]\d{3})+", text):  # separador de miles
        text = re.sub(r"[.,]", "", text)
    else:
        text = text.replace(",", ".")
    try:
        return int(float(text))
    except (ValueError, OverflowError):
        return default


def normalize_lines(value):
    """Convierte un array JSON o texto multilínea en una lista limpia."""
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if value is None:
        return []
    return [line.strip() for line in str(value).splitlines() if line.strip()]


def norm_identity(value):
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def gemini_fingerprint(book):
    identity = {
        "title": norm_identity(book.get("title")),
        "author": norm_identity(book.get("author")),
        "year": num(book.get("year")),
        "publisher": norm_identity(book.get("publisher")),
        "edition": norm_identity(book.get("edition")),
        "isbn": re.sub(r"[^0-9xX]", "", str(book.get("isbn") or "")).upper(),
    }
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def same_catalog_identity(a, b):
    """Deduplicación conservadora: ISBN exacto o identidad bibliográfica suficientemente completa."""
    isbn_a = re.sub(r"[^0-9xX]", "", str(a.get("isbn") or "")).upper()
    isbn_b = re.sub(r"[^0-9xX]", "", str(b.get("isbn") or "")).upper()
    if isbn_a and isbn_b and isbn_a == isbn_b:
        return True
    title_a, title_b = norm_identity(a.get("title")), norm_identity(b.get("title"))
    author_a, author_b = norm_identity(a.get("author")), norm_identity(b.get("author"))
    if not title_a or title_a != title_b or not author_a or author_a != author_b:
        return False
    # Exigimos al menos un discriminador visible para no confundir ediciones distintas.
    discriminators = ("year", "publisher", "edition")
    supplied = [k for k in discriminators if b.get(k)]
    if not supplied:
        return False
    return all(norm_identity(a.get(k)) == norm_identity(b.get(k)) for k in supplied)


def make_book(d, books):
    return {
        "id": next_id(books),
        "title": (d.get("title") or "Sin título").strip(),
        "author": (d.get("author") or "").strip(),
        "year": num(d.get("year")),
        "publisher": (d.get("publisher") or "").strip(),
        "price": num(d.get("price")),
        "sold_out": False,
        "category": (d.get("category") or "General").strip(),
        "description": (d.get("description") or "").strip(),
        "language": (d.get("language") or "").strip(),
        "edition": (d.get("edition") or "").strip(),
        "binding": (d.get("binding") or "").strip(),
        "pages": num(d.get("pages")),
        "isbn": (d.get("isbn") or "").strip(),
        "condition": (d.get("condition") or "").strip(),
        "series": (d.get("series") or "").strip(),
        "translator": (d.get("translator") or "").strip(),
        "illustrator": (d.get("illustrator") or "").strip(),
        "format": (d.get("format") or "").strip(),
        "printer": (d.get("printer") or "").strip(),
        "printing_place": (d.get("printing_place") or "").strip(),
        "identification_notes": normalize_lines(d.get("identification_notes")),
        "uncertain_fields": normalize_lines(d.get("uncertain_fields")),
        "images": d.get("images", []),
        "ig_shortcode": d.get("ig_shortcode"),
        "date_added": datetime.now().strftime("%Y-%m-%d"),
    }


# ----------------------- Instagram -----------------------
def parse_caption(caption):
    """Intenta sacar título, autor, año, editorial y precio del texto de una publicación."""
    caption = (caption or "").strip()
    lines = [l.strip() for l in caption.splitlines() if l.strip()]

    def find(*keys):
        for k in keys:
            m = re.search(rf"(?im)^\W*{k}\s*[:\-–]\s*(.+)$", caption)
            if m:
                return m.group(1).strip()
        return ""

    title = find("t[ií]tulo", "title") or (lines[0] if lines else "")
    title = re.sub(r"^[\W_]+", "", title)[:90]

    price, text_wo_price = 0, caption
    m = re.search(r"\$\s*(\d[\d\.,]*)", caption)
    if m:
        price = num(re.sub(r"\D", "", m.group(1)))
        text_wo_price = caption.replace(m.group(0), " ")
    y = re.search(r"\b(1[5-9]\d\d|20[0-2]\d)\b", text_wo_price)

    return {
        "title": title,
        "author": find("autor", "autora", "author", "by", "por"),
        "year": int(y.group(1)) if y else 0,
        "publisher": find("editorial", "ed", "publisher", "edici[oó]n"),
        "price": price,
        "description": caption,
        "category": find("genero", "genre")
    }


def instagram_session_user():
    """Devuelve el usuario de Instagram configurado o guardado localmente."""
    if IG_USER:
        return IG_USER
    user_file = BASE / "instagram_session_user.txt"
    if user_file.exists():
        return user_file.read_text(encoding="utf-8").strip()
    return ""


def ig_loader():
    import instaloader
    L = instaloader.Instaloader(
        download_pictures=False, download_videos=False, download_comments=False,
        save_metadata=False, quiet=True,
        max_connection_attempts=1,
    )
    username = instagram_session_user()
    if username:
        try:
            L.load_session_from_file(username)
        except Exception as exc:
            raise RuntimeError(
                f"No se pudo cargar la sesión guardada de Instagram para '{username}'. "
                f"Creá/renová la sesión ejecutando: python {SCRIPT_NAME} --login-instagram"
            ) from exc
    return instaloader, L


def create_instagram_session(username=""):
    """Inicio de sesión interactivo de una sola vez; no guarda la contraseña en este archivo."""
    import instaloader
    username = (username or input("Usuario de Instagram: ")).strip()
    if not username:
        raise RuntimeError("Tenés que indicar tu usuario de Instagram.")
    loader = instaloader.Instaloader(quiet=False, max_connection_attempts=1)
    print("Instagram te va a pedir la contraseña en la consola. No la pegues en el código.")
    loader.interactive_login(username)
    loader.save_session_to_file()
    (BASE / "instagram_session_user.txt").write_text(username, encoding="utf-8")
    print("Sesión guardada localmente para", username)
    print("Ya podés cerrar esta consola y abrir el catálogo normalmente.")


def ig_post_data(post):
    """Descarga las fotos de una publicación y parsea el texto."""
    import requests
    if post.typename == "GraphSidecar":
        urls = [n.display_url for n in post.get_sidecar_nodes()]
    else:
        urls = [post.url]
    imgs = []
    for i, u in enumerate(urls, 1):
        r = requests.get(u, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        name = f"ig_{post.shortcode}_{i}.jpg"
        saved = save_image(io.BytesIO(r.content), IMG_DIR / name)
        imgs.append(f"imagenes/{saved.name}")
    data = parse_caption(post.caption or "")
    data["images"] = imgs
    data["ig_shortcode"] = post.shortcode
    return data


def ig_public_metadata_fallback(shortcode):
    """Intenta recuperar al menos la descripción y portada desde metadatos públicos OG."""
    import requests

    url = f"https://www.instagram.com/p/{shortcode}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    }
    response = requests.get(url, headers=headers, timeout=(6, 18), allow_redirects=True)
    response.raise_for_status()
    if "accounts/login" in response.url or "challenge" in response.url:
        raise RuntimeError("Instagram redirigió a inicio de sesión o verificación.")

    metadata = {}
    for tag in re.findall(r"<meta\b[^>]*>", response.text, flags=re.I):
        attrs = {}
        for match in re.finditer(r"([\w:-]+)\s*=\s*(['\"])(.*?)\2", tag, flags=re.S):
            attrs[match.group(1).lower()] = html_lib.unescape(match.group(3))
        key = attrs.get("property") or attrs.get("name") or ""
        if key in {"og:description", "og:image", "og:title", "description"}:
            metadata[key] = attrs.get("content", "")

    description = metadata.get("og:description") or metadata.get("description", "")
    # Instagram suele anteponer likes/fecha/usuario. Extraemos el texto si viene entre comillas.
    caption = ""
    caption_match = re.search(r":\s*[\"“](.*?)[\"”]\s*$", description, flags=re.S)
    if caption_match:
        caption = caption_match.group(1).strip()
    elif description and not re.search(r"\b(likes?|me gusta|comments?|comentarios)\b", description, flags=re.I):
        caption = description.strip()

    images = []
    image_url = metadata.get("og:image", "")
    if image_url.startswith("https://") or image_url.startswith("http://"):
        image_response = requests.get(image_url, headers=headers, timeout=(6, 18))
        image_response.raise_for_status()
        content_type = image_response.headers.get("Content-Type", "")
        if content_type.startswith("image/") and image_response.content:
            saved = save_image(io.BytesIO(image_response.content), IMG_DIR / f"ig_{shortcode}_1.jpg")
            images.append(f"imagenes/{saved.name}")

    if not caption and not images:
        raise RuntimeError("La página no entregó metadatos públicos aprovechables.")
    data = parse_caption(caption)
    data["images"] = images
    data["ig_shortcode"] = shortcode
    return data


# ----------------------- API -----------------------
def apply_form(book):
    f = request.form
    book["title"] = f.get("title", "").strip() or "Sin título"
    book["author"] = f.get("author", "").strip()
    book["year"] = num(f.get("year"))
    book["publisher"] = f.get("publisher", "").strip()
    book["price"] = num(f.get("price"))
    book["sold_out"] = f.get("sold_out", "").strip().lower() in {"on", "1", "true", "yes"}
    book["category"] = f.get("category", "").strip() or "General"
    book["description"] = f.get("description", "").strip()
    book["language"] = f.get("language", "").strip()
    book["edition"] = f.get("edition", "").strip()
    book["binding"] = f.get("binding", "").strip()
    book["pages"] = num(f.get("pages"))
    book["isbn"] = f.get("isbn", "").strip()
    book["condition"] = f.get("condition", "").strip()
    for k in ("series", "translator", "illustrator", "format", "printer", "printing_place"):
        book[k] = f.get(k, "").strip()
    book["identification_notes"] = normalize_lines(f.get("identification_notes", ""))
    book["uncertain_fields"] = normalize_lines(f.get("uncertain_fields", ""))

    files = request.files.getlist("images")
    try:
        order = json.loads(f.get("image_order") or "null")
    except ValueError:
        order = None
    if not isinstance(order, list):  # compatibilidad con el formato anterior
        try:
            legacy = json.loads(f.get("existing_images", "[]"))
        except ValueError:
            legacy = []
        order = [f"old:{p}" for p in legacy] + [f"new:{i}" for i in range(len(files))]

    imgs, used_new = [], set()
    for token in order:
        kind, _, value = str(token).partition(":")
        if kind == "old":
            rel = safe_image_rel(value)
            if rel and rel not in imgs:
                imgs.append(rel)
            elif value.startswith(("http://", "https://")) and value not in imgs:
                imgs.append(value)
        elif kind == "new" and value.isdigit() and int(value) < len(files) and int(value) not in used_new:
            file = files[int(value)]
            used_new.add(int(value))
            ext = Path(file.filename or "").suffix.lower()
            if file and ext in ALLOWED:
                saved = save_image(file, IMG_DIR / f"{uuid.uuid4().hex[:10]}{ext}")
                imgs.append(f"imagenes/{saved.name}")
    for old in set(book.get("images", [])) - set(imgs):  # borrar fotos quitadas
        delete_image(old)
    book["images"] = imgs  # el orden define la portada: la primera es la tapa
    return book


@app.get("/")
def admin():
    return ADMIN_HTML.replace("__SCRIPT__", SCRIPT_NAME)


@app.get("/imagenes/<path:name>")
def serve_img(name):
    return send_from_directory(IMG_DIR, name)


@app.get("/thumb/<path:name>")
def serve_thumb(name):
    """Miniatura para la grilla del panel (se cachea en .thumbs/)."""
    rel = safe_image_rel("imagenes/" + name)
    if not rel:
        abort(404)
    thumb = make_thumb(BASE / rel, (THUMB_CACHE / name).with_suffix(".webp"))
    if thumb:
        return send_from_directory(thumb.parent, thumb.name)
    return send_from_directory(IMG_DIR, name)


@app.get("/api/books")
def api_list():
    return jsonify(load_books())


@app.get("/api/health")
def api_health():
    return jsonify(ok=True, service="catalogo", gemini_import=True)


@app.post("/api/gemini_import")
@locked
def api_gemini_import():
    """Importa fichas desde JSON o multipart/form-data y guarda sus fotos."""
    body = None

    # La extensión envía FormData para transportar los archivos de imagen.
    # También mantenemos compatibilidad con clientes que envían JSON puro.
    if request.mimetype == "multipart/form-data":
        raw_books = request.form.get("books") or request.form.get("data")
        if not raw_books:
            return jsonify(
                ok=False,
                error="La extensión envió un formulario, pero no incluyó el campo 'books'.",
            ), 400
        try:
            body = json.loads(raw_books)
        except (TypeError, json.JSONDecodeError) as exc:
            return jsonify(
                ok=False,
                error=f"El campo 'books' no contiene JSON válido: {exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)}",
            ), 400
    elif request.is_json:
        body = request.get_json(silent=True)
        if body is None:
            return jsonify(ok=False, error="El cuerpo de la petición contiene JSON vacío o inválido."), 400
    else:
        return jsonify(
            ok=False,
            error=(
                "Formato no compatible. La extensión debe enviar JSON o multipart/form-data "
                "con el campo 'books' y, opcionalmente, archivos 'images_0', 'images_1', etc."
            ),
        ), 415

    if isinstance(body, dict) and "books" in body:
        items = body.get("books")
    elif isinstance(body, dict) and "data" in body:
        items = body.get("data")
    else:
        items = body

    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        return jsonify(ok=False, error="No encontré una ficha JSON o una lista de fichas."), 400

    books = load_books()
    added_ids, duplicates, errors = [], [], []
    image_errors = []
    images_saved = 0
    books_changed = False
    fingerprint_index = {
        b["gemini_fingerprint"]: b for b in books if b.get("gemini_fingerprint")
    }

    mime_extensions = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }

    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            errors.append({"item": index, "error": "La ficha no es un objeto JSON."})
            continue

        data = dict(item)
        title = str(data.get("title") or "").strip()
        if not title:
            errors.append({"item": index, "error": "Falta el título; no se importó."})
            continue

        data["title"] = title
        # Las rutas que vengan desde Gemini no se consideran archivos locales.
        # Las fotos reales llegan como archivos multipart asociados al índice.
        data["images"] = []
        data["ig_shortcode"] = None
        data["price"] = num(data.get("price"))
        data["year"] = num(data.get("year"))
        data["pages"] = num(data.get("pages"))
        fingerprint = gemini_fingerprint(data)

        # Detectar duplicado antes de guardar, pero conservar sus nuevas fotos:
        # una ficha ya existente puede no tener imágenes.
        duplicate_book = fingerprint_index.get(fingerprint)
        if duplicate_book is None:
            duplicate_book = next(
                (b for b in books if same_catalog_identity(b, data)), None
            )

        target_images = duplicate_book.setdefault("images", []) if duplicate_book else []
        if not isinstance(target_images, list):
            target_images = []
            if duplicate_book:
                duplicate_book["images"] = target_images

        uploaded_paths = []
        uploaded_digests = set()
        incoming_hashes = []
        existing_digests = set()
        if duplicate_book:
            # Evita volver a adjuntar la misma foto al reimportar: se compara tanto el archivo
            # guardado como el hash de la foto original que llegó la vez anterior.
            existing_digests.update(duplicate_book.get("source_hashes") or [])
            for old_path in target_images:
                rel = safe_image_rel(old_path)
                if rel:
                    try:
                        existing_digests.add(hashlib.sha256((BASE / rel).read_bytes()).hexdigest())
                    except OSError:
                        pass

        for image_number, file in enumerate(request.files.getlist(f"images_{index - 1}"), 1):
            if not file or not file.filename:
                continue

            ext = Path(file.filename).suffix.lower()
            mime_type = (file.mimetype or "").split(";")[0].strip().lower()
            if ext not in ALLOWED:
                ext = mime_extensions.get(mime_type, "")
            if ext not in ALLOWED:
                image_errors.append({
                    "item": index,
                    "book": title,
                    "image": image_number,
                    "error": f"Formato de imagen no admitido ({mime_type or 'desconocido'}).",
                })
                continue

            filename = f"gemini_{uuid.uuid4().hex[:12]}{ext}"
            destination = IMG_DIR / filename
            try:
                raw_digest = hashlib.sha256(file.read()).hexdigest()
                file.seek(0)
                saved = save_image(file, destination)
                if not saved.is_file() or saved.stat().st_size == 0:
                    raise RuntimeError("El archivo guardado quedó vacío.")
                relative_path = "imagenes/" + saved.name
                digest = hashlib.sha256(saved.read_bytes()).hexdigest()

                seen = existing_digests | uploaded_digests
                if raw_digest in seen or digest in seen:
                    saved.unlink(missing_ok=True)
                    continue

                uploaded_paths.append(relative_path)
                uploaded_digests.update((digest, raw_digest))
                incoming_hashes.append(raw_digest)
            except Exception as exc:
                destination.unlink(missing_ok=True)
                destination.with_suffix(".webp").unlink(missing_ok=True)
                image_errors.append({
                    "item": index,
                    "book": title,
                    "image": image_number,
                    "error": str(exc)[:220],
                })

        if duplicate_book:
            duplicates.append(title)
            if uploaded_paths:
                target_images.extend(uploaded_paths)
                duplicate_book["source_hashes"] = (duplicate_book.get("source_hashes") or []) + incoming_hashes
                images_saved += len(uploaded_paths)
                books_changed = True
            continue

        book = make_book(data, books)
        book["images"] = uploaded_paths
        book["source"] = "gemini_extension"
        book["gemini_fingerprint"] = fingerprint
        book["source_hashes"] = incoming_hashes
        books.append(book)
        fingerprint_index[fingerprint] = book
        added_ids.append(book["id"])
        images_saved += len(uploaded_paths)
        books_changed = True

    if not added_ids and errors and not duplicates:
        return jsonify(
            ok=False,
            error="No se importó ninguna ficha válida.",
            details=errors,
            images_saved=images_saved,
            images_failed=len(image_errors),
            image_errors=image_errors,
        ), 400

    if books_changed:
        save_books(books)

    return jsonify(
        ok=True,
        added=len(added_ids),
        ids=added_ids,
        duplicates=duplicates,
        errors=errors,
        images_saved=images_saved,
        images_failed=len(image_errors),
        image_errors=image_errors,
        message=(
            f"Agregados: {len(added_ids)}; duplicados omitidos: {len(duplicates)}; "
            f"fotos guardadas: {images_saved}; errores: {len(errors) + len(image_errors)}."
        ),
    )

@app.post("/api/books")
@locked
def api_add():
    books = load_books()
    book = make_book({}, books)
    books.append(apply_form(book))
    save_books(books)
    return jsonify(ok=True, id=book["id"])


@app.put("/api/books/<book_id>")
@locked
def api_edit(book_id):
    books = load_books()
    for b in books:
        if b["id"] == book_id:
            apply_form(b)
            save_books(books)
            return jsonify(ok=True)
    return jsonify(ok=False, error="No existe"), 404


@app.delete("/api/books/<book_id>")
@locked
def api_delete(book_id):
    books = load_books()
    for b in books:
        if b["id"] == book_id:
            for p in b.get("images", []):
                delete_image(p)
    save_books([b for b in books if b["id"] != book_id])
    return jsonify(ok=True)


@app.post("/api/instagram")
def api_instagram():
    """Importa una publicación por URL; usa metadatos públicos si Instaloader falla."""
    body = request.get_json(silent=True) or {}
    url, caption = str(body.get("url") or "").strip(), str(body.get("caption") or "").strip()
    result, warning = {}, ""
    shortcode = ""
    if url:
        match = re.search(r"instagram\.com/(?:[^/?#]+/)?(?:p|reel|reels|tv)/([\w-]+)", url, flags=re.I)
        if not match:
            return jsonify(ok=False, error="El enlace no parece una publicación válida. Pegá un enlace /p/ o /reel/ de Instagram."), 400
        shortcode = match.group(1)
        try:
            ig, loader = ig_loader()
            post = ig.Post.from_shortcode(loader.context, shortcode)
            result = ig_post_data(post)
        except Exception as exc:
            warning = (
                "Instagram bloqueó la lectura automática con la sesión actual. "
                "Se intentará recuperar la vista previa pública; si no alcanza, pegá la descripción y subí las fotos manualmente."
            )
            try:
                result = ig_public_metadata_fallback(shortcode)
                warning = (
                    "Instagram no entregó todos los datos. Se recuperó solo la vista previa pública "
                    "(como máximo la primera foto); revisá el formulario y completá lo que falte."
                )
            except Exception:
                # No exponemos trazas enormes ni secretos de sesión al panel.
                detail = str(exc).replace("\n", " ")[:180]
                warning += f" Detalle: {detail or type(exc).__name__}."

    if caption:
        parsed = parse_caption(caption)
        result = {**result, **{k: v for k, v in parsed.items() if v}}
    if shortcode and result:
        result.setdefault("ig_shortcode", shortcode)
    if not result:
        return jsonify(
            ok=False,
            error=(warning + " Pegá la descripción de la publicación en el cuadro de texto y volvé a importar; las fotos se pueden seleccionar abajo.")
                  if url else "Pegá un enlace de publicación o su descripción.",
        ), 502
    return jsonify(ok=True, data=result, warning=warning)


@app.post("/api/instagram_perfil")
@locked
def api_instagram_perfil():
    """Importa las últimas N publicaciones del perfil, salteando las ya importadas."""
    n = num((request.get_json(silent=True) or {}).get("cantidad"), 12)
    try:
        ig, L = ig_loader()
        profile = ig.Profile.from_username(L.context, IG_PERFIL)
        books = load_books()
        known = {b.get("ig_shortcode") for b in books}
        added = 0
        try:
            for i, post in enumerate(profile.get_posts()):
                if i >= n:
                    break
                if post.shortcode in known:
                    continue
                books.append(make_book(ig_post_data(post), books))
                added += 1
        finally:
            if added:
                save_books(books)
        return jsonify(ok=True, agregados=added)
    except Exception as e:
        detail = str(e).replace("\n", " ")[:240]
        return jsonify(ok=False, error=(detail + f"\n\nSi Instagram bloquea la consulta, ejecutá `python {SCRIPT_NAME} --login-instagram` en CMD para crear una sesión local, y volvé a intentarlo.")), 502


@app.post("/api/publicar")
@locked
def api_publish():
    publish_site()
    return jsonify(ok=True, carpeta=str(SITE_DIR))


# ----------------------- sitio público -----------------------
def sync_published_images(books):
    """Copia a sitio/imagenes/ solo lo necesario: foto completa + miniatura (imagenes/t/)."""
    published = SITE_DIR / "imagenes"
    published.mkdir(parents=True, exist_ok=True)
    keep = set()
    for book in books:
        for image_path in book.get("images", []):
            rel = safe_image_rel(image_path)
            if not rel:
                continue
            source = BASE / rel
            relative = Path(rel).relative_to("imagenes")
            target = published / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or target.stat().st_size != source.stat().st_size \
                    or int(target.stat().st_mtime) != int(source.stat().st_mtime):
                shutil.copy2(source, target)
            keep.add(target)
            thumb = make_thumb(source, (published / "t" / relative).with_suffix(".webp"))
            if thumb:
                keep.add(thumb)
    for path in published.rglob("*"):  # limpia lo que ya no se usa
        if path.is_file() and path not in keep:
            path.unlink()


def publish_site():
    """Genera sitio/index.html + imágenes (completas y miniaturas) sin recopiar lo que no cambió."""
    books = load_books()
    SITE_DIR.mkdir(exist_ok=True)
    sync_published_images(books)

    private_fields = {"source", "gemini_fingerprint", "source_hashes", "uncertain_fields", "date_added", "ig_shortcode"}
    public_books = [{k: v for k, v in book.items() if k not in private_fields} for book in books]
    books_json = json.dumps(public_books, ensure_ascii=False).replace("</", "<\\/")
    html = PUBLIC_HTML.replace("__BOOKS__", books_json).replace("__PHONE__", WHATSAPP)
    (SITE_DIR / "index.html").write_text(html, encoding="utf-8")


# ======================= PLANTILLAS =======================
ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Administrar catálogo</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-stone-100 text-stone-800">
<header class="bg-stone-900 text-stone-100 py-4 shadow">
  <div class="container mx-auto px-4 flex flex-wrap gap-3 justify-between items-center">
    <h1 class="text-xl font-serif">Panel · Libros Británicos <span id="count" class="text-stone-400 text-sm"></span></h1>
    <div class="flex flex-wrap gap-2">
      <button onclick="importProfile()" class="bg-stone-700 hover:bg-stone-600 px-3 py-2 rounded text-sm">📷 Traer últimas de Instagram</button>
      <button onclick="publish()" class="bg-green-700 hover:bg-green-600 px-3 py-2 rounded text-sm">🌐 Publicar sitio</button>
      <button onclick="openModal()" class="bg-amber-700 hover:bg-amber-600 px-4 py-2 rounded text-sm font-medium">+ Agregar libro</button>
    </div>
  </div>
</header>

<main class="container mx-auto px-4 py-6">
  <input id="q" oninput="render()" placeholder="Buscar..." class="w-full max-w-md mb-6 px-4 py-2 rounded border border-stone-300">
  <div id="grid" class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-4"></div>
</main>

<div id="modal" class="fixed inset-0 bg-black/50 hidden items-start justify-center overflow-y-auto z-50 p-4">
  <div class="bg-white rounded-lg w-full max-w-2xl p-6 my-8">
    <div class="flex justify-between items-center mb-4">
      <h2 id="modal-title" class="text-xl font-serif font-bold">Agregar libro</h2>
      <button onclick="closeModal()" class="text-2xl text-stone-500">&times;</button>
    </div>

    <div id="ig-box" class="mb-5 p-3 rounded bg-amber-50 border border-amber-200">
      <p class="text-sm font-semibold mb-2">📷 Importar desde una publicación de Instagram</p>
      <input id="ig-url" placeholder="https://www.instagram.com/p/XXXXXXXX/" class="w-full px-3 py-2 border rounded text-sm mb-2">
      <textarea id="ig-caption" rows="2" placeholder="Pegá acá el texto de la publicación si Instagram no deja leerla automáticamente" class="w-full px-3 py-2 border rounded text-sm mb-2"></textarea>
      <button onclick="importPost()" class="bg-amber-700 hover:bg-amber-600 text-white px-3 py-1.5 rounded text-sm">Importar y completar formulario</button>
      <p class="text-xs text-stone-600 mt-2">Instagram a veces bloquea la importación automática. Si no trae las fotos, cargalas manualmente en Imágenes. Para probar con tu sesión, ejecutá en CMD: <code>python __SCRIPT__ --login-instagram</code>.</p>
      <p id="ig-msg" class="text-sm mt-2"></p>
    </div>

    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
      <label class="sm:col-span-2 text-sm">Título *<input id="f-title" class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">Autor<input id="f-author" class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">Año<input id="f-year" type="number" class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">Editorial<input id="f-publisher" class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">Precio (ARS)<input id="f-price" type="number" min="0" class="w-full px-3 py-2 border rounded"></label>
      <label class="sm:col-span-2 flex items-start gap-2 rounded border border-red-200 bg-red-50 p-3 text-sm">
        <input id="f-sold_out" type="checkbox" class="mt-0.5 h-4 w-4 accent-red-700">
        <span><strong>Agotado / vendido</strong><span class="block text-xs text-stone-600">La ficha seguirá visible en el catálogo, pero sin opción de agregar al carrito.</span></span>
      </label>
      <label class="sm:col-span-2 text-sm">Categoría<input id="f-category" list="cats" placeholder="Clásicos, Infantil, Historia..." class="w-full px-3 py-2 border rounded"></label>
      <datalist id="cats"></datalist>
      <label class="sm:col-span-2 text-sm">Descripción de la obra<textarea id="f-description" rows="5" placeholder="Resumen claro de la obra y su contenido. Evitá repetir año, editorial, precio o estado: esos datos tienen campos propios." class="w-full px-3 py-2 border rounded"></textarea></label>
      <label class="text-sm">Idioma<input id="f-language" class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">Edición<input id="f-edition" class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">Encuadernación<input id="f-binding" class="w-full px-3 py-2 border rounded" placeholder="Tapa dura, rústica..."></label>
      <label class="text-sm">Páginas<input id="f-pages" type="number" min="0" class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">ISBN<input id="f-isbn" class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">Estado de conservación<input id="f-condition" placeholder="Bueno; foxing en páginas preliminares..." class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">Colección / serie<input id="f-series" class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">Traductor<input id="f-translator" class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">Ilustrador<input id="f-illustrator" class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">Formato físico<input id="f-format" placeholder="Folio, 8vo, gran formato..." class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">Impresor / imprenta<input id="f-printer" class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">Lugar de impresión<input id="f-printing_place" class="w-full px-3 py-2 border rounded"></label>
      <label class="sm:col-span-2 text-sm">Detalles bibliográficos y del ejemplar (uno por línea)<textarea id="f-identification_notes" rows="3" placeholder="Mapas, láminas, dedicatorias, sellos de biblioteca, procedencia, sobrecubierta..." class="w-full px-3 py-2 border rounded"></textarea></label>
      <label class="sm:col-span-2 text-sm">Datos que faltan verificar (uno por línea)<textarea id="f-uncertain_fields" rows="2" class="w-full px-3 py-2 border rounded"></textarea></label>
    </div>

    <div class="mt-4">
      <p class="text-sm mb-1">Imágenes <span class="text-stone-500">(arrastrá para ordenar · ★ elige la portada)</span></p>
      <label class="block border-2 border-dashed border-stone-300 rounded p-4 text-center text-sm text-stone-500 cursor-pointer hover:bg-stone-50">
        Hacé clic para elegir fotos (podés elegir varias)
        <input id="f-files" type="file" accept="image/*" multiple class="hidden" onchange="addFiles(this)">
      </label>
      <div id="thumbs" class="flex flex-wrap gap-2 mt-3"></div>
      <p class="text-xs text-stone-500 mt-1">La primera imagen es la portada. Las fotos se guardan en alta calidad (hasta 2400 px; si ya son livianas se conserva el original) y el sitio genera miniaturas aparte.</p>
    </div>

    <div class="flex justify-end gap-2 mt-6">
      <button onclick="closeModal()" class="px-4 py-2 rounded border">Cancelar</button>
      <button id="btn-save" onclick="save()" class="px-5 py-2 rounded bg-stone-900 hover:bg-amber-800 text-white">Guardar</button>
    </div>
  </div>
</div>

<script>
let books = [], editingId = null, items = [], dragIndex = null;  // items: {kind:'old', path} | {kind:'new', file, url}
const thumbUrl = p => (typeof p === 'string' && p.startsWith('imagenes/')) ? '/thumb/' + p.slice(9) : p;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const norm = s => String(s ?? '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase('es');
const FIELDS = ['title','author','year','publisher','price','category','description','language','edition','binding','pages','isbn','condition','series','translator','illustrator','format','printer','printing_place','identification_notes','uncertain_fields'];
const ARRAY_FIELDS = ['identification_notes','uncertain_fields'];

async function load() {
  books = await (await fetch('/api/books')).json();
  $('cats').innerHTML = [...new Set(books.map(b => b.category))].sort().map(c => `<option value="${esc(c)}">`).join('');
  render();
}

function render() {
  const q = norm($('q').value);
  const list = books.filter(b => norm([b.title,b.author,b.publisher,b.category,b.year].join(' ')).includes(q));
  $('count').textContent = `(${books.length} libros)`;
  $('grid').innerHTML = list.map(b => `
    <div class="bg-white rounded shadow-sm border overflow-hidden flex flex-col">
      <div class="h-48 bg-stone-100 relative">${b.images[0] ? `<img src="${esc(thumbUrl(b.images[0]))}" loading="lazy" class="w-full h-full object-contain">` : '<div class="h-full flex items-center justify-center text-xs text-stone-400">Sin foto</div>'}${b.images.length > 1 ? `<span class="absolute bottom-1 right-1 rounded bg-black/60 px-1.5 text-[10px] text-white">${b.images.length} fotos</span>` : ''}</div>
      <div class="p-3 flex-1 flex flex-col justify-between">
        <div><p class="font-serif font-bold text-sm leading-tight">${esc(b.title)}</p>
        <p class="text-xs text-stone-500">${esc(b.author)} ${b.year ? '· ' + b.year : ''}</p>
        <p class="text-sm font-semibold mt-1">${b.sold_out ? '<span class="text-red-700 font-bold">AGOTADO</span>' : (b.price ? '$' + b.price.toLocaleString('es-AR') : '<span class="text-red-600">Sin precio</span>')}</p></div>
        <div class="flex gap-2 mt-2 text-xs">
          <button onclick="openModal('${b.id}')" class="flex-1 border rounded py-1 hover:bg-stone-100">Editar</button>
          <button onclick="del('${b.id}')" class="border rounded px-2 py-1 text-red-600 hover:bg-red-50">🗑️</button>
        </div>
      </div>
    </div>`).join('') || '<p class="col-span-full text-stone-500">No hay libros todavía. ¡Agregá el primero!</p>';
}

function openModal(id = null) {
  editingId = id; releaseItems();
  const b = id ? books.find(x => x.id === id) : {};
  items = (b.images || []).map(path => ({kind: 'old', path}));
  FIELDS.forEach(f => $('f-' + f).value = ARRAY_FIELDS.includes(f) ? (Array.isArray(b[f]) ? b[f].join('\n') : (b[f] || '')) : (b[f] || ''));
  $('f-sold_out').checked = Boolean(b.sold_out);
  $('modal-title').textContent = id ? 'Editar libro' : 'Agregar libro';
  $('ig-box').classList.toggle('hidden', !!id);
  $('ig-url').value = ''; $('ig-caption').value = ''; $('ig-msg').textContent = '';
  thumbs(); $('modal').classList.replace('hidden', 'flex');
}
function closeModal() { releaseItems(); $('modal').classList.replace('flex', 'hidden'); }
function releaseItems() { items.forEach(it => it.url && URL.revokeObjectURL(it.url)); items = []; }

function addFiles(input) {
  Array.from(input.files).forEach(file => items.push({kind: 'new', file, url: URL.createObjectURL(file)}));
  input.value = ''; thumbs();
}
function thumbs() {
  $('thumbs').innerHTML = items.map((it, i) => `
    <div draggable="true" ondragstart="dragStart(event, ${i})" ondragover="event.preventDefault()" ondrop="dropOn(event, ${i})"
         class="relative w-28 rounded border bg-white p-1 cursor-grab ${i === 0 ? 'ring-2 ring-amber-600' : ''}">
      <img src="${esc(it.kind === 'old' ? thumbUrl(it.path) : it.url)}" class="w-full h-24 object-contain bg-stone-100 rounded pointer-events-none">
      ${i === 0 ? '<span class="absolute left-1 top-1 rounded bg-amber-600 px-1.5 text-[10px] font-bold text-white">PORTADA</span>' : ''}
      <div class="mt-1 flex items-center justify-between text-xs">
        <button type="button" onclick="moveItem(${i}, -1)" class="px-1.5 py-0.5 rounded border hover:bg-stone-100" title="Mover a la izquierda">◀</button>
        ${i === 0 ? '<span class="text-amber-700">★</span>' : `<button type="button" onclick="makeCover(${i})" class="px-1.5 py-0.5 rounded border hover:bg-amber-50" title="Usar como portada">★ Portada</button>`}
        <button type="button" onclick="moveItem(${i}, 1)" class="px-1.5 py-0.5 rounded border hover:bg-stone-100" title="Mover a la derecha">▶</button>
      </div>
      <button type="button" onclick="rmItem(${i})" class="absolute -top-2 -right-2 bg-red-600 text-white rounded-full w-5 h-5 text-xs" title="Quitar foto">×</button>
    </div>`).join('');
}
function moveItem(i, delta) {
  const j = i + delta;
  if (j < 0 || j >= items.length) return;
  [items[i], items[j]] = [items[j], items[i]]; thumbs();
}
function makeCover(i) { items.unshift(...items.splice(i, 1)); thumbs(); }
function rmItem(i) { const [it] = items.splice(i, 1); if (it.url) URL.revokeObjectURL(it.url); thumbs(); }
function dragStart(event, i) { dragIndex = i; event.dataTransfer.effectAllowed = 'move'; }
function dropOn(event, i) {
  event.preventDefault();
  if (dragIndex === null || dragIndex === i) return;
  items.splice(i, 0, ...items.splice(dragIndex, 1)); dragIndex = null; thumbs();
}

async function save() {
  if (!$('f-title').value.trim()) { alert('Poné al menos el título.'); return; }
  const fd = new FormData();
  FIELDS.forEach(f => fd.append(f, $('f-' + f).value));
  fd.append('sold_out', $('f-sold_out').checked ? 'on' : '');
  const order = []; let nextNew = 0;
  items.forEach(it => {
    if (it.kind === 'old') order.push('old:' + it.path);
    else { order.push('new:' + nextNew++); fd.append('images', it.file); }
  });
  fd.append('image_order', JSON.stringify(order));  // el orden define la portada (la primera)
  $('btn-save').disabled = true;
  let r;
  try { r = await fetch(editingId ? '/api/books/' + editingId : '/api/books', { method: editingId ? 'PUT' : 'POST', body: fd }); }
  catch (err) { r = { ok: false }; }
  $('btn-save').disabled = false;
  if (!r.ok) { alert('No se pudo guardar.'); return; }
  closeModal(); load();
}

async function del(id) {
  if (!confirm('¿Eliminar este libro?')) return;
  await fetch('/api/books/' + id, { method: 'DELETE' }); load();
}

async function importPost() {
  const msg = $('ig-msg'); msg.className = 'text-sm mt-2 text-stone-600'; msg.textContent = 'Importando...';
  try {
    const r = await fetch('/api/instagram', { method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ url: $('ig-url').value, caption: $('ig-caption').value }) });
    const j = await r.json();
    if (!r.ok || !j.ok) { msg.className = 'text-sm mt-2 text-red-600'; msg.textContent = j.error || 'Instagram no permitió la importación.'; return; }
    FIELDS.forEach(f => { if (j.data[f]) $('f-' + f).value = j.data[f]; });
    (j.data.images || []).forEach(path => items.push({kind: 'old', path})); thumbs();
    msg.className = 'text-sm mt-2 ' + (j.warning ? 'text-amber-700' : 'text-green-700');
    msg.textContent = j.warning || '¡Listo! Revisá los datos y guardá.';
  } catch (err) {
    msg.className = 'text-sm mt-2 text-red-600';
    msg.textContent = 'No pude conectar con el panel. Confirmá que __SCRIPT__ esté abierto y que la página use http://127.0.0.1:5000.';
  }
}

async function importProfile() {
  if (!confirm('Se van a traer las últimas publicaciones de Instagram como libros nuevos (revisalos y completá precios). ¿Seguir?')) return;
  try {
    const r = await fetch('/api/instagram_perfil', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}' });
    const j = await r.json();
    alert(j.ok ? `Se agregaron ${j.agregados} publicaciones nuevas.` : 'Instagram no respondió: ' + j.error +
      '\n\nCreá una sesión local con el comando indicado o importá la publicación desde "+ Agregar libro".');
  } catch (err) {
    alert('No pude conectar con el panel. Confirmá que __SCRIPT__ siga abierto en http://127.0.0.1:5000.');
  }
  load();
}

async function publish() {
  const j = await (await fetch('/api/publicar', { method: 'POST' })).json();
  alert('Sitio generado en:\n' + j.carpeta + '\n\nSubí esa carpeta completa (index.html + imagenes) a tu hosting.');
}
load();
</script>
</body></html>
"""

PUBLIC_HTML = r"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="theme-color" content="#292524">
<title>Catálogo de Libros - Libros Británicos</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  * { -webkit-tap-highlight-color: transparent; }
  body { padding-bottom: env(safe-area-inset-bottom); }
  .book-title { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; line-height: 1.25; min-height: 2.5em; }
  .book-author { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; min-height: 2.5em; }
  .category-strip { scrollbar-width: none; }
  .category-strip::-webkit-scrollbar { display: none; }
  .safe-bottom { padding-bottom: max(1rem, env(safe-area-inset-bottom)); }
  button, select, input { touch-action: manipulation; }
  .gallery-arrow { display:flex; align-items:center; justify-content:center; width:2.6rem; height:2.6rem; border-radius:9999px; background:rgba(28,25,23,.72); color:#fff; font-size:1.1rem; box-shadow:0 2px 12px rgba(0,0,0,.22); transition:background .15s, transform .15s; }
  .gallery-arrow:hover { background:rgba(120,53,15,.95); }
  .gallery-arrow:focus-visible { outline:3px solid #fbbf24; outline-offset:2px; }
  @media (max-width: 639px) {
    .catalog-card { border-radius: .65rem; }
    .catalog-card .cover-area { aspect-ratio: 3 / 3.5; }
  }
</style>
</head>
<body class="bg-stone-100 text-stone-800 font-sans antialiased">
<header class="sticky top-0 z-30 bg-stone-900 text-stone-100 shadow-md">
  <div class="mx-auto flex max-w-7xl items-center justify-between gap-2 px-3 py-2.5 sm:px-4 sm:py-4">
    <div class="min-w-0">
      <h1 class="truncate font-serif text-base font-bold tracking-wide sm:text-xl md:text-2xl">Libros Británicos &amp; Americanos</h1>
      <p class="hidden text-xs text-stone-300 sm:block">Catálogo bibliográfico · Ejemplares antiguos y de colección</p>
    </div>
    <button onclick="toggleCart()" class="shrink-0 rounded-lg bg-amber-700 px-3 py-2.5 text-sm font-semibold hover:bg-amber-600 sm:px-4" aria-label="Abrir carrito">
      <span aria-hidden="true">🛒</span> <span class="hidden sm:inline">Carrito</span> <span class="rounded-full bg-stone-900/30 px-1.5 py-0.5"><span id="cart-count">0</span></span>
    </button>
  </div>
</header>

<main class="mx-auto max-w-7xl px-3 pb-24 pt-4 sm:px-4 sm:py-7">
  <section aria-label="Presentación del catálogo" class="mb-4 border-b border-stone-200 pb-4 sm:mb-6 sm:pb-5">
    <h2 class="font-serif text-xl font-semibold text-stone-900 sm:text-2xl">Biblioteca y libros de colección</h2>
    <p class="mt-1 max-w-3xl text-sm leading-relaxed text-stone-600">Explorá las obras disponibles, consultá sus datos bibliográficos y revisá las fotografías de cada ejemplar.</p>
  </section>
  <section aria-label="Buscar y filtrar libros" class="mb-4 sm:mb-6">
    <label for="search" class="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-stone-500 sm:text-sm">Encontrá tu próximo libro</label>
    <div class="relative">
      <span class="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-stone-400" aria-hidden="true">⌕</span>
      <input id="search" oninput="filterBooks()" autocomplete="off" placeholder="Título, autor, año o temática..."
        class="w-full rounded-xl border border-stone-300 bg-white py-3 pl-10 pr-10 text-base shadow-sm outline-none focus:border-amber-700 focus:ring-2 focus:ring-amber-700/20">
      <button id="clear-search" onclick="clearSearch()" class="absolute right-2 top-1/2 hidden -translate-y-1/2 rounded-lg px-3 py-2 text-stone-500 hover:bg-stone-100" aria-label="Borrar búsqueda">×</button>
    </div>
    <div id="category-strip" class="category-strip -mx-3 mt-3 flex gap-2 overflow-x-auto px-3 pb-1 sm:mx-0 sm:flex-wrap sm:overflow-visible sm:px-0"></div>
    <div class="mt-3 flex items-center justify-between gap-3">
      <p id="results-count" class="text-xs text-stone-500 sm:text-sm" aria-live="polite"></p>
      <label class="flex shrink-0 items-center gap-2 text-xs text-stone-600 sm:text-sm">
        <span>Ordenar</span>
        <select id="sort" onchange="filterBooks()" class="max-w-[155px] rounded-lg border border-stone-300 bg-white px-2 py-2 text-xs outline-none focus:border-amber-700 sm:max-w-none sm:text-sm">
          <option value="default">Orden original</option>
          <option value="title">Título A–Z</option>
          <option value="price-asc">Menor precio</option>
          <option value="price-desc">Mayor precio</option>
          <option value="year-desc">Más recientes</option>
          <option value="year-asc">Más antiguos</option>
        </select>
      </label>
    </div>
  </section>

  <div id="grid" class="grid grid-cols-2 gap-3 sm:grid-cols-2 sm:gap-4 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5"></div>
</main>

<!-- Detalle: panel inferior en celular, modal centrado en pantallas grandes -->
<div id="detail" class="fixed inset-0 z-40 hidden items-end justify-center bg-black/60 p-0 sm:items-center sm:p-4" onclick="if(event.target===this)closeDetail()">
  <div class="max-h-[94dvh] w-full max-w-3xl overflow-y-auto rounded-t-2xl bg-white p-4 shadow-xl sm:max-h-[90vh] sm:rounded-xl sm:p-5" id="detail-body" style="padding-bottom:max(1rem, env(safe-area-inset-bottom))"></div>
</div>

<!-- Visor de imagen a pantalla completa -->
<div id="image-lightbox" class="fixed inset-0 z-[80] hidden items-center justify-center bg-black/95 p-3 sm:p-6"
     role="dialog" aria-modal="true" aria-label="Imagen ampliada" onclick="if(event.target===this)closeLightbox()">
  <button type="button" onclick="closeLightbox()" aria-label="Cerrar imagen ampliada"
          class="absolute right-3 top-2 z-10 rounded-full bg-white/10 px-3 py-1 text-4xl leading-none text-white sm:right-6 sm:top-5">&times;</button>
  <div class="relative flex h-full w-full flex-col items-center justify-center gap-3" ontouchstart="galleryTouchStart(event)" ontouchend="galleryTouchEnd(event)">
    <img id="lightbox-img" src="" alt="" class="max-h-[84dvh] max-w-full object-contain sm:max-h-[88vh]">
    <p id="lightbox-caption" class="max-w-full break-words text-center text-sm text-white"></p>
    <p id="lightbox-counter" class="rounded-full bg-white/10 px-3 py-1 text-xs text-white"></p>
    <button id="lightbox-prev" type="button" onclick="moveGallery(-1)" aria-label="Foto anterior" class="gallery-arrow absolute left-1 top-1/2 -translate-y-1/2 sm:left-4">&#10094;</button>
    <button id="lightbox-next" type="button" onclick="moveGallery(1)" aria-label="Foto siguiente" class="gallery-arrow absolute right-1 top-1/2 -translate-y-1/2 sm:right-4">&#10095;</button>
  </div>
</div>

<!-- Carrito: panel inferior en celulares y lateral en escritorio -->
<div id="cart-modal" class="fixed inset-0 z-50 hidden items-end justify-end bg-black/50 sm:items-stretch">
  <div class="safe-bottom flex h-[88dvh] w-full max-w-md flex-col rounded-t-2xl bg-white p-4 shadow-xl sm:h-full sm:rounded-none sm:p-6">
    <div class="flex items-center justify-between border-b pb-3 sm:pb-4">
      <h2 class="font-serif text-xl font-bold">Tu pedido</h2>
      <button onclick="toggleCart()" class="rounded-lg px-3 py-1 text-3xl leading-none text-stone-500" aria-label="Cerrar carrito">&times;</button>
    </div>
    <div id="cart-items" class="flex-1 space-y-3 overflow-y-auto py-4"></div>
    <div class="border-t pt-4">
      <div class="mb-4 flex justify-between gap-3 text-lg font-bold"><span>Total estimado:</span><span id="cart-total">$0 ARS</span></div>
      <button onclick="checkout()" class="w-full rounded-xl bg-green-600 py-3.5 font-semibold text-white shadow hover:bg-green-700">Enviar pedido por WhatsApp 📱</button>
    </div>
  </div>
</div>

<a href="https://wa.me/__PHONE__" target="_blank" rel="noopener noreferrer" aria-label="Contactar por WhatsApp"
   class="fixed bottom-4 right-3 z-[60] flex h-12 w-12 items-center justify-center rounded-full bg-green-600 text-2xl text-white shadow-xl hover:bg-green-700 sm:bottom-5 sm:right-5 sm:h-auto sm:w-auto sm:gap-2 sm:px-5 sm:py-3 sm:text-base">
  <span aria-hidden="true">💬</span><span class="hidden sm:inline">WhatsApp</span>
</a>
<div id="toast" role="status" aria-live="polite"
     class="fixed bottom-20 left-3 right-3 z-[70] hidden rounded-xl bg-stone-900 px-4 py-3 text-sm text-white shadow-xl sm:bottom-24 sm:left-auto sm:right-5 sm:max-w-xs"></div>

<script>
const books = __BOOKS__;
const PHONE = "__PHONE__";
let cart = [];
let selectedCategory = 'Todos';
let activeGallery = { bookId: null, images: [], index: 0, title: '' };
let galleryTouchX = null;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const norm = s => String(s ?? '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase('es').trim();
const money = n => '$' + Number(n || 0).toLocaleString('es-AR');
// Miniatura generada por el publicador (imagenes/t/<nombre>.webp); si falta, se usa la foto completa.
const thumb = src => /^imagenes\//.test(src) ? src.replace(/^imagenes\/(.+?)\.[^./]+$/, 'imagenes/t/$1.webp') : src;

// Cinco grupos amplios para filtrar sin tener que editar las categorías guardadas.
const CATEGORY_GROUPS = ['Literatura', 'Historia y sociedad', 'Arte y cultura', 'Idiomas y consulta', 'Otros temas'];
function categoryGroup(book) { return book._group || (book._group = computeCategoryGroup(book)); }
function computeCategoryGroup(book) {
  const originalCategory = String(book.category || 'General').trim();
  const categoryText = norm(originalCategory);
  const generic = !categoryText || ['general', 'otros', 'otro', 'sin categoria', 'varios', 'miscellaneous'].includes(categoryText);
  // En fichas sin categoría útil, el título y la descripción ayudan a clasificarlas automáticamente.
  const source = generic ? norm([book.title, book.description, book.author].join(' ')) : categoryText;

  if (/literatura|literary|ficcion|fiction|novela|novel|narrativa|poesia|poetry|poema|cuento|short stor|teatro|drama|clasico|classic|romance|thriller|misterio|detective|comic|humor|infantil|juvenil|ensayo literario/.test(source)) return 'Literatura';
  if (/historia|history|biograf|autobiograf|politic|sociedad|society|sociolog|econom|religion|mitolog|mytholog|filosof|philosoph|antropolog|arqueolog|geograf|geograph|guerra|militar|derecho|law|educacion|education|psicolog|psycholog/.test(source)) return 'Historia y sociedad';
  if (/arte|artes|art |painting|pintura|fotograf|photograph|musica|music|cine|cinema|film|cultura|culture|arquitect|architecture|diseno|design|ilustracion|illustration|moda|fashion|artesania|craft|teatro|decoracion/.test(source)) return 'Arte y cultura';
  if (/idioma|idiomas|language|lengua|linguist|diccionario|dictionary|gramatic|grammar|latin|ingles|english|espanol|spanish|frances|french|aleman|german|italiano|italian|vocabulario|vocabulary|atlas|enciclopedia|encyclopedia|referencia|reference|manual|handbook|guia|guide|textbook|libro de texto|consulta/.test(source)) return 'Idiomas y consulta';
  return 'Otros temas';
}
function renderCategories() {
  const categories = ['Todos', ...CATEGORY_GROUPS];
  if (!categories.some(c => norm(c) === norm(selectedCategory))) selectedCategory = 'Todos';
  $('category-strip').innerHTML = categories.map(c => {
    const active = norm(c) === norm(selectedCategory);
    return `<button type="button" data-category="${esc(c)}" class="shrink-0 rounded-full border px-3.5 py-2 text-xs font-semibold transition sm:text-sm ${active ? 'border-stone-900 bg-stone-900 text-white' : 'border-stone-300 bg-white text-stone-700 hover:border-stone-500'}">${esc(c)}</button>`;
  }).join('');
  $('category-strip').querySelectorAll('[data-category]').forEach(button => {
    button.addEventListener('click', () => selectCategory(button.dataset.category));
  });
}
function selectCategory(category) { selectedCategory = category; renderCategories(); filterBooks(); }
function clearSearch() { $('search').value = ''; filterBooks(); $('search').focus(); }

function filterBooks() {
  const q = norm($('search').value);
  $('clear-search').classList.toggle('hidden', !$('search').value);
  let list = books.filter(b => {
    const matchesText = norm([b.title, b.author, b.publisher, b.year, b.category, b.description, b.isbn].join(' ')).includes(q);
    const matchesCategory = norm(selectedCategory) === 'todos' || norm(categoryGroup(b)) === norm(selectedCategory);
    return matchesText && matchesCategory;
  });
  const sort = $('sort').value;
  if (sort === 'title') list.sort((a, b) => String(a.title || '').localeCompare(String(b.title || ''), 'es', { sensitivity: 'base' }));
  if (sort === 'price-asc') list.sort((a, b) => (Number(a.price) > 0 ? Number(a.price) : Number.MAX_SAFE_INTEGER) - (Number(b.price) > 0 ? Number(b.price) : Number.MAX_SAFE_INTEGER));
  if (sort === 'price-desc') list.sort((a, b) => (Number(b.price) || 0) - (Number(a.price) || 0));
  if (sort === 'year-desc') list.sort((a, b) => (Number(b.year) || 0) - (Number(a.year) || 0));
  if (sort === 'year-asc') list.sort((a, b) => (Number(a.year) || Number.MAX_SAFE_INTEGER) - (Number(b.year) || Number.MAX_SAFE_INTEGER));
  $('results-count').textContent = `${list.length} ${list.length === 1 ? 'libro' : 'libros'} encontrados`;
  renderBooks(list);
}

function renderBooks(list) {
  $('grid').innerHTML = list.map(b => {
    const images = Array.isArray(b.images) ? b.images : [];
    const inCart = cart.some(i => i.id === b.id);
    const soldOut = Boolean(b.sold_out);
    return `
    <article class="catalog-card flex min-w-0 flex-col overflow-hidden rounded-xl border border-stone-200 bg-white shadow-sm">
      <button type="button" class="cover-area aspect-[3/4] relative block w-full overflow-hidden bg-stone-200 text-left" onclick="openDetail('${esc(b.id)}')" aria-label="Ver detalles de ${esc(b.title)}">
        ${images[0] ? `<img src="${esc(thumb(images[0]))}" onerror="this.onerror=null;this.src='${esc(images[0])}'" alt="${esc(b.title)}" loading="lazy" decoding="async" class="h-full w-full object-contain">` : `<span class="flex h-full w-full flex-col items-center justify-center bg-stone-100 text-stone-500"><span class="text-3xl sm:text-4xl" aria-hidden="true">📚</span><span class="mt-1 text-xs font-semibold sm:text-sm">Sin foto</span></span>`}
        ${soldOut ? `<span class="pointer-events-none absolute left-1.5 top-1.5 rounded-md bg-red-700 px-2 py-1 text-[10px] font-bold tracking-wide text-white sm:left-2 sm:top-2 sm:text-xs">AGOTADO</span>` : ''}
        ${b.year ? `<span class="absolute right-1.5 top-1.5 rounded-md bg-stone-900/90 px-1.5 py-1 text-[10px] font-medium text-white sm:right-2 sm:top-2 sm:px-2 sm:text-xs">${esc(b.year)}</span>` : ''}
      </button>
      <div class="flex flex-1 flex-col p-2.5 sm:p-3.5">
        <div class="mb-1 text-[9px] font-bold uppercase tracking-wide text-amber-800 sm:text-[11px]">${esc(categoryGroup(b))}</div>
        <button type="button" onclick="openDetail('${esc(b.id)}')" class="text-left font-serif text-sm font-bold text-stone-900 hover:text-amber-800 sm:text-base"><span class="book-title">${esc(b.title)}</span></button>
        <p class="book-author mt-1 text-[11px] leading-snug text-stone-500 sm:text-sm">${esc(b.author || 'Autor no indicado')}</p>
        <div class="mt-auto pt-2.5 sm:pt-3">
          <div class="flex min-h-8 flex-wrap items-center justify-between gap-1.5 border-t border-stone-100 pt-2">
            <span class="text-sm font-bold sm:text-base ${soldOut ? 'text-red-700' : ''}">${soldOut ? 'Agotado' : (Number(b.price) > 0 ? money(b.price) : 'Consultar')}</span>
            ${soldOut ? `<button disabled class="cursor-not-allowed rounded-lg bg-red-100 px-2.5 py-2 text-[11px] font-semibold text-red-700 sm:text-sm">Agotado</button>` : (inCart ? `<button disabled class="cursor-not-allowed rounded-lg bg-stone-200 px-2.5 py-2 text-[11px] font-semibold text-stone-500 sm:text-sm">En carrito</button>` : `<button onclick="addToCart('${esc(b.id)}')" class="rounded-lg bg-stone-900 px-2.5 py-2 text-[11px] font-semibold text-white hover:bg-amber-800 sm:px-3 sm:text-sm">Agregar +</button>`)}
          </div>
        </div>
      </div>
    </article>`;
  }).join('') || '<p class="col-span-full rounded-xl border border-dashed border-stone-300 bg-white px-4 py-12 text-center text-sm text-stone-500">No encontramos libros con esos criterios. Probá otra palabra o categoría.</p>';
}

function openDetail(id) {
  const b = books.find(x => x.id === id);
  if (!b) return;
  const images = Array.isArray(b.images) ? b.images : [];
  activeGallery = { bookId: b.id, images: images, index: 0, title: b.title || 'Libro' };
  $('detail-body').innerHTML = `
    <div class="grid grid-cols-1 gap-4 md:grid-cols-2 md:gap-5">
      <div class="min-w-0">
        ${images.length
          ? `<div class="relative overflow-hidden rounded-lg bg-stone-100" ontouchstart="galleryTouchStart(event)" ontouchend="galleryTouchEnd(event)">
              <button type="button" class="block w-full cursor-zoom-in" onclick="openLightbox()" aria-label="Ampliar foto del libro"><img id="main-img" src="${esc(images[0])}" alt="${esc(b.title)}" class="max-h-[42vh] min-h-56 w-full rounded-lg object-contain sm:max-h-96"></button>
              ${images.length > 1 ? `<button type="button" onclick="moveGallery(-1)" aria-label="Foto anterior" class="gallery-arrow absolute left-2 top-1/2 -translate-y-1/2">&#10094;</button><button type="button" onclick="moveGallery(1)" aria-label="Foto siguiente" class="gallery-arrow absolute right-2 top-1/2 -translate-y-1/2">&#10095;</button>` : ''}
              <span id="gallery-counter" class="absolute bottom-2 right-2 rounded-full bg-black/65 px-2.5 py-1 text-xs font-medium text-white">1 / ${images.length}</span>
            </div>
            <div id="gallery-thumbs" class="mt-2 flex flex-wrap gap-2">${images.map((s, i) => `<button type="button" data-thumb-index="${i}" aria-label="Ver foto ${i + 1}" onclick="setGalleryImage(${i})" class="h-14 w-14 overflow-hidden rounded-md border-2 ${i === 0 ? 'border-amber-700' : 'border-stone-200'} bg-stone-50 sm:h-16 sm:w-16"><img src="${esc(thumb(s))}" onerror="this.onerror=null;this.src='${esc(s)}'" alt="${esc(b.title)} · foto ${i + 1}" loading="lazy" class="h-full w-full object-contain"></button>`).join('')}</div>
            <p class="mt-2 text-xs text-stone-500">Usá las flechas o las miniaturas para recorrer las fotos. Tocá la imagen para ampliarla.</p>`
          : `<div class="flex min-h-48 w-full flex-col items-center justify-center rounded-lg bg-stone-100 text-stone-500 sm:min-h-64"><span class="text-5xl" aria-hidden="true">📚</span><span class="mt-2 font-medium">Sin fotografías disponibles</span></div>`}
      </div>
      <div class="flex min-w-0 flex-col">
        <button onclick="closeDetail()" class="-mr-1 -mt-2 self-end rounded-lg px-3 py-1 text-3xl leading-none text-stone-500" aria-label="Cerrar detalle">&times;</button>
        <span class="text-[11px] font-bold uppercase tracking-wider text-amber-800">${esc(categoryGroup(b))}</span>
        ${b.sold_out ? `<span class="mt-2 self-start rounded-md bg-red-700 px-2.5 py-1 text-xs font-bold tracking-wide text-white">AGOTADO · EJEMPLAR VENDIDO</span>` : ''}
        <h2 class="mt-1 break-words font-serif text-xl font-bold sm:text-2xl">${esc(b.title)}</h2>
        <p class="mt-1 text-sm text-stone-600">${esc(b.author || 'Autor no indicado')}</p>
        <p class="mb-4 mt-1 text-xs text-stone-500 sm:text-sm">${[b.publisher, b.year || ''].filter(Boolean).map(esc).join(' · ')}</p>
        ${(() => { const rows = [['Edición', b.edition], ['Idioma', b.language], ['Encuadernación', b.binding], ['Formato', b.format], ['Páginas', b.pages ? String(b.pages) : ''], ['ISBN / SBN', b.isbn], ['Colección / serie', b.series], ['Traductor', b.translator], ['Ilustrador', b.illustrator], ['Impresor', b.printer], ['Lugar de impresión', b.printing_place]] .filter(x => x[1]); return rows.length ? `<section class="mb-4"><h3 class="mb-2 text-xs font-bold uppercase tracking-wider text-stone-500">Datos de la edición</h3><dl class="grid grid-cols-1 gap-x-4 gap-y-2 rounded-lg border border-stone-200 bg-stone-50 p-3 sm:grid-cols-2">${rows.map(([k,v]) => `<div class="min-w-0"><dt class="text-[11px] font-semibold uppercase tracking-wide text-stone-500">${esc(k)}</dt><dd class="break-words text-sm text-stone-800">${esc(v)}</dd></div>`).join('')}</dl></section>` : ''; })()}
        ${b.description ? `<section class="mt-2"><h3 class="mb-2 text-xs font-bold uppercase tracking-wider text-stone-500">Sobre esta obra</h3><div class="space-y-2 text-sm leading-relaxed text-stone-700">${esc(b.description).split(/\n\s*\n/).filter(Boolean).map(p => `<p>${p.replace(/\n/g, ' ')}</p>`).join('')}</div></section>` : ''}
        ${b.condition ? `<section class="mt-4 rounded-lg border border-stone-200 p-3"><h3 class="mb-1 text-xs font-bold uppercase tracking-wider text-stone-500">Estado de conservación</h3><p class="text-sm leading-relaxed text-stone-700">${esc(b.condition)}</p></section>` : ''}
        ${(b.identification_notes || []).length ? `<section class="mt-4 text-sm"><h3 class="mb-2 text-xs font-bold uppercase tracking-wider text-stone-500">Particularidades del ejemplar</h3><ul class="ml-5 list-disc space-y-1 text-stone-700">${b.identification_notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul></section>` : ''}
        <div class="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-stone-100 pt-4">
          <span class="text-xl font-bold ${b.sold_out ? 'text-red-700' : ''}">${b.sold_out ? 'Agotado' : (Number(b.price) > 0 ? money(b.price) : 'Consultar')}</span>
          ${b.sold_out ? `<span class="rounded-xl bg-red-100 px-4 py-3 font-semibold text-red-700">No disponible</span>` : (cart.some(i => i.id === b.id) ? `<button disabled class="cursor-not-allowed rounded-xl bg-stone-200 px-4 py-3 font-semibold text-stone-500">En carrito</button>` : `<button onclick="addToCart('${esc(b.id)}');closeDetail()" class="rounded-xl bg-stone-900 px-5 py-3 font-semibold text-white hover:bg-amber-800">Agregar al carrito</button>`)}
        </div>
        <button onclick="shareBook('${esc(b.id)}')" class="mt-3 rounded-lg py-2 text-left text-sm font-semibold text-amber-800 hover:underline">🔗 Copiar enlace individual</button>
      </div>
    </div>`;
  $('detail').classList.replace('hidden', 'flex');
  document.body.style.overflow = 'hidden';
}
function closeDetail() {
  $('detail').classList.replace('flex', 'hidden');
  if ($('image-lightbox').classList.contains('hidden') && $('cart-modal').classList.contains('hidden')) document.body.style.overflow = '';
}
function setGalleryImage(index) {
  if (!activeGallery.images.length) return;
  const total = activeGallery.images.length;
  activeGallery.index = (index + total) % total;
  const src = activeGallery.images[activeGallery.index];
  const main = $('main-img');
  if (main) main.src = src;
  const counter = $('gallery-counter');
  if (counter) counter.textContent = `${activeGallery.index + 1} / ${total}`;
  const thumbs = $('gallery-thumbs');
  if (thumbs) thumbs.querySelectorAll('[data-thumb-index]').forEach(button => {
    const active = Number(button.dataset.thumbIndex) === activeGallery.index;
    button.classList.toggle('border-amber-700', active);
    button.classList.toggle('border-stone-200', !active);
  });
  const lightbox = $('image-lightbox');
  if (lightbox && !lightbox.classList.contains('hidden')) {
    $('lightbox-img').src = src;
    $('lightbox-img').alt = `${activeGallery.title} · foto ${activeGallery.index + 1}`;
    $('lightbox-caption').textContent = activeGallery.title;
    $('lightbox-counter').textContent = `${activeGallery.index + 1} / ${total}`;
  }
}
function moveGallery(delta) { setGalleryImage(activeGallery.index + delta); }
function galleryTouchStart(event) {
  if (event.touches && event.touches.length) galleryTouchX = event.touches[0].clientX;
}
function galleryTouchEnd(event) {
  if (galleryTouchX === null || !event.changedTouches || !event.changedTouches.length) return;
  const dx = event.changedTouches[0].clientX - galleryTouchX;
  galleryTouchX = null;
  if (Math.abs(dx) > 45 && activeGallery.images.length > 1) moveGallery(dx < 0 ? 1 : -1);
}
function openLightbox() {
  if (!activeGallery.images.length) return;
  $('image-lightbox').classList.replace('hidden', 'flex');
  const hasMultiple = activeGallery.images.length > 1;
  $('lightbox-prev').classList.toggle('hidden', !hasMultiple);
  $('lightbox-next').classList.toggle('hidden', !hasMultiple);
  setGalleryImage(activeGallery.index);
  document.body.style.overflow = 'hidden';
}
function closeLightbox() {
  $('image-lightbox').classList.replace('flex', 'hidden');
  $('lightbox-img').src = '';
  if ($('detail').classList.contains('hidden') && $('cart-modal').classList.contains('hidden')) document.body.style.overflow = '';
}
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    closeLightbox();
    closeDetail();
    if (!$('cart-modal').classList.contains('hidden')) toggleCart();
    return;
  }
  if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
    const detailOpen = !$('detail').classList.contains('hidden');
    const lightboxOpen = !$('image-lightbox').classList.contains('hidden');
    if ((detailOpen || lightboxOpen) && activeGallery.images.length > 1) {
      event.preventDefault();
      moveGallery(event.key === 'ArrowRight' ? 1 : -1);
    }
  }
});

function toggleCart() {
  const m = $('cart-modal');
  const opening = m.classList.contains('hidden');
  m.classList.toggle('hidden'); m.classList.toggle('flex');
  document.body.style.overflow = opening ? 'hidden' : '';
}
function addToCart(id) {
  const b = books.find(x => x.id === id);
  if (!b) return;
  if (b.sold_out) { showToast('Este libro está agotado y ya no se puede pedir.'); return; }
  if (cart.some(i => i.id === id)) { showToast('Ya agregaste este libro. Solo hay un ejemplar disponible.'); return; }
  cart.push({ ...b, quantity: 1 });
  updateCart();
  filterBooks();
  showToast('Agregado al carrito: ' + b.title);
}
function removeFromCart(id) { cart = cart.filter(i => i.id !== id); updateCart(); filterBooks(); }
function updateCart() {
  $('cart-count').innerText = cart.length;
  let total = 0;
  $('cart-items').innerHTML = cart.map(i => {
    total += (Number(i.price) || 0) * i.quantity;
    return `<div class="flex items-center justify-between gap-3 rounded-xl bg-stone-50 p-3">
      <div class="min-w-0"><h4 class="break-words text-sm font-bold">${esc(i.title)}</h4><p class="mt-1 text-xs text-stone-500">${Number(i.price) > 0 ? money(i.price) : 'Precio a consultar'}</p></div>
      <button onclick="removeFromCart('${esc(i.id)}')" class="shrink-0 rounded-lg px-3 py-2 text-lg text-red-600 hover:bg-red-50" aria-label="Quitar ${esc(i.title)} del carrito">🗑️</button></div>`;
  }).join('') || '<p class="py-8 text-center text-sm text-stone-500">Todavía no agregaste libros.</p>';
  $('cart-total').innerText = money(total) + ' ARS';
}
let toastTimer = null;
function showToast(message) {
  const toast = $('toast'); toast.textContent = message; toast.classList.remove('hidden');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => toast.classList.add('hidden'), 3000);
}
async function shareBook(id) {
  const url = new URL(window.location.href); url.searchParams.set('libro', id);
  try { await navigator.clipboard.writeText(url.toString()); showToast('Enlace individual copiado.'); }
  catch (err) { window.prompt('Copiá este enlace individual:', url.toString()); }
}
function checkout() {
  if (!cart.length) { showToast('Tu carrito está vacío.'); return; }
  if (cart.some(i => books.some(b => b.id === i.id && b.sold_out))) {
    cart = cart.filter(i => !books.some(b => b.id === i.id && b.sold_out));
    updateCart(); filterBooks();
    showToast('Quité del pedido los libros que figuran agotados.');
    return;
  }
  let msg = '¡Hola! Quiero consultar/encargar estos libros de tu catálogo:\n\n', total = 0;
  cart.forEach(i => {
    msg += `• ${i.title}${i.year ? ' (' + i.year + ')' : ''} - ${Number(i.price) > 0 ? money(i.price) : 'precio a consultar'}\n`;
    total += (Number(i.price) || 0) * i.quantity;
  });
  if (total > 0) msg += `\n*Total estimado:* ${money(total)} ARS`;
  window.open(`https://wa.me/${PHONE}?text=${encodeURIComponent(msg)}`, '_blank', 'noopener');
}

renderCategories();
filterBooks();
updateCart();
const requestedBookId = new URLSearchParams(window.location.search).get('libro');
if (requestedBookId && books.some(b => b.id === requestedBookId)) openDetail(requestedBookId);
</script>
</body></html>

"""

if __name__ == "__main__":
    if "--login-instagram" in sys.argv:
        try:
            arg_index = sys.argv.index("--login-instagram")
            login_user = sys.argv[arg_index + 1] if arg_index + 1 < len(sys.argv) and not sys.argv[arg_index + 1].startswith("--") else ""
            create_instagram_session(login_user)
        except Exception as exc:
            print("No se pudo guardar la sesión de Instagram:", str(exc))
            sys.exit(1)
        sys.exit(0)
    if "--build" in sys.argv:  # modo para GitHub Actions: solo genera sitio/ y termina
        publish_site()
        print("Sitio generado en", SITE_DIR)
        sys.exit(0)
    publish_site()  # deja el sitio actualizado al abrir
    threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    app.run(host="127.0.0.1", port=PORT, debug=False)
