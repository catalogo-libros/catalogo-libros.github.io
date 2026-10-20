"""
Catálogo de Libros Británicos
-----------------------------
Panel de administración local + generador del sitio público.

Instalación:   pip install flask instaloader
Uso:           python catalogo.py        (se abre el navegador solo)

Resultado:     carpeta "sitio/" con index.html + imagenes/  -> eso es lo que subís a la web.
"""
import json
import re
import shutil
import sys
import threading
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

# ======================= CONFIGURACIÓN =======================
WHATSAPP = "5492211234567"      # código de país + área + número, sin espacios ni signos
IG_PERFIL = "librosbritanicos"  # perfil de Instagram del que se importan publicaciones
IG_USER = ""                    # (opcional) usuario con sesión guardada de instaloader, ver explicación
PORT = 5000
# =============================================================

BASE = Path(__file__).resolve().parent
DATA_FILE = BASE / "books.json"
IMG_DIR = BASE / "imagenes"
SITE_DIR = BASE / "sitio"
ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
IMG_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024 * 1024


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
    for b in books:  # los libros cargados desde Pages CMS no traen id
        if not b.get("id"):
            b["id"] = next_id(books)
    return books


def save_books(books):
    DATA_FILE.write_text(json.dumps(books, ensure_ascii=False, indent=2), encoding="utf-8")


def next_id(books):
    nums = [int(m.group(1)) for b in books if (m := re.fullmatch(r"LIB-(\d+)", b.get("id", "")))]
    return f"LIB-{max(nums, default=0) + 1:03d}"


def num(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def make_book(d, books):
    return {
        "id": next_id(books),
        "title": (d.get("title") or "Sin título").strip(),
        "author": (d.get("author") or "").strip(),
        "year": num(d.get("year")),
        "publisher": (d.get("publisher") or "").strip(),
        "price": num(d.get("price")),
        "category": (d.get("category") or "General").strip(),
        "description": (d.get("description") or "").strip(),
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
    }


def ig_loader():
    import instaloader
    L = instaloader.Instaloader(
        download_pictures=False, download_videos=False, download_comments=False,
        save_metadata=False, quiet=True,
        max_connection_attempts=1,  # sin reintentos infinitos: falla rápido y avisa
    )
    if IG_USER:
        L.load_session_from_file(IG_USER)
    return instaloader, L


def ig_post_data(post):
    """Descarga las fotos de la publicación a /imagenes (las URLs de Instagram vencen) y parsea el texto."""
    import requests
    if post.typename == "GraphSidecar":
        urls = [n.display_url for n in post.get_sidecar_nodes()]
    else:
        urls = [post.url]
    imgs = []
    for i, u in enumerate(urls, 1):
        r = requests.get(u, timeout=30)
        r.raise_for_status()
        name = f"ig_{post.shortcode}_{i}.jpg"
        (IMG_DIR / name).write_bytes(r.content)
        imgs.append(f"imagenes/{name}")
    data = parse_caption(post.caption or "")
    data["images"] = imgs
    data["ig_shortcode"] = post.shortcode
    return data


# ----------------------- API -----------------------
def apply_form(book):
    f = request.form
    book["title"] = f.get("title", "").strip() or "Sin título"
    book["author"] = f.get("author", "").strip()
    book["year"] = num(f.get("year"))
    book["publisher"] = f.get("publisher", "").strip()
    book["price"] = num(f.get("price"))
    book["category"] = f.get("category", "").strip() or "General"
    book["description"] = f.get("description", "").strip()

    keep = [p for p in json.loads(f.get("existing_images", "[]"))
            if p.startswith(("imagenes/", "http"))]
    for old in set(book.get("images", [])) - set(keep):  # borrar fotos quitadas
        if old.startswith("imagenes/"):
            (BASE / old).unlink(missing_ok=True)
    imgs = list(keep)
    for file in request.files.getlist("images"):
        ext = Path(file.filename or "").suffix.lower()
        if file and ext in ALLOWED:
            name = f"{uuid.uuid4().hex[:10]}{ext}"
            file.save(IMG_DIR / name)
            imgs.append(f"imagenes/{name}")
    book["images"] = imgs
    return book


@app.get("/")
def admin():
    return ADMIN_HTML


@app.get("/imagenes/<path:name>")
def serve_img(name):
    return send_from_directory(IMG_DIR, name)


@app.get("/api/books")
def api_list():
    return jsonify(load_books())


@app.post("/api/books")
def api_add():
    books = load_books()
    book = make_book({}, books)
    books.append(apply_form(book))
    save_books(books)
    return jsonify(ok=True, id=book["id"])


@app.put("/api/books/<book_id>")
def api_edit(book_id):
    books = load_books()
    for b in books:
        if b["id"] == book_id:
            apply_form(b)
            save_books(books)
            return jsonify(ok=True)
    return jsonify(ok=False, error="No existe"), 404


@app.delete("/api/books/<book_id>")
def api_delete(book_id):
    books = load_books()
    for b in books:
        if b["id"] == book_id:
            for p in b.get("images", []):
                if p.startswith("imagenes/"):
                    (BASE / p).unlink(missing_ok=True)
    save_books([b for b in books if b["id"] != book_id])
    return jsonify(ok=True)


@app.post("/api/instagram")
def api_instagram():
    """Importa UNA publicación (por URL) y devuelve los datos para revisar en el formulario."""
    body = request.get_json(silent=True) or {}
    url, caption = body.get("url", "").strip(), body.get("caption", "").strip()
    result, warning = {}, ""
    if url:
        m = re.search(r"instagram\.com/(?:[^/]+/)?(?:p|reel|tv)/([\w-]+)", url)
        if not m:
            return jsonify(ok=False, error="El link no parece de una publicación de Instagram."), 400
        try:
            ig, L = ig_loader()
            post = ig.Post.from_shortcode(L.context, m.group(1))
            result = ig_post_data(post)
        except Exception as e:
            warning = (f"Instagram bloqueó la consulta ({e}). "
                       "Pegá la descripción abajo y subí las fotos a mano, o probá más tarde / con sesión iniciada.")
    if caption:
        parsed = parse_caption(caption)
        result = {**result, **{k: v for k, v in parsed.items() if v}}
    if not result:
        return jsonify(ok=False, error=warning or "Pegá un link o una descripción."), 502
    return jsonify(ok=True, data=result, warning=warning)


@app.post("/api/instagram_perfil")
def api_instagram_perfil():
    """Importa las últimas N publicaciones del perfil, salteando las ya importadas."""
    n = num((request.get_json(silent=True) or {}).get("cantidad"), 12)
    try:
        ig, L = ig_loader()
        profile = ig.Profile.from_username(L.context, IG_PERFIL)
        books = load_books()
        known = {b.get("ig_shortcode") for b in books}
        added = 0
        for i, post in enumerate(profile.get_posts()):
            if i >= n:
                break
            if post.shortcode in known:
                continue
            books.append(make_book(ig_post_data(post), books))
            added += 1
        save_books(books)
        return jsonify(ok=True, agregados=added)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


@app.post("/api/publicar")
def api_publish():
    publish_site()
    return jsonify(ok=True, carpeta=str(SITE_DIR))


# ----------------------- sitio público -----------------------
def publish_site():
    SITE_DIR.mkdir(exist_ok=True)
    shutil.copytree(IMG_DIR, SITE_DIR / "imagenes", dirs_exist_ok=True)
    books_json = json.dumps(load_books(), ensure_ascii=False).replace("</", "<\\/")
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
      <textarea id="ig-caption" rows="2" placeholder="(Opcional) Pegá acá la descripción de la publicación si Instagram no deja leerla" class="w-full px-3 py-2 border rounded text-sm mb-2"></textarea>
      <button onclick="importPost()" class="bg-amber-700 hover:bg-amber-600 text-white px-3 py-1.5 rounded text-sm">Importar y completar formulario</button>
      <p id="ig-msg" class="text-sm mt-2"></p>
    </div>

    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
      <label class="sm:col-span-2 text-sm">Título *<input id="f-title" class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">Autor<input id="f-author" class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">Año<input id="f-year" type="number" class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">Editorial<input id="f-publisher" class="w-full px-3 py-2 border rounded"></label>
      <label class="text-sm">Precio (ARS)<input id="f-price" type="number" min="0" class="w-full px-3 py-2 border rounded"></label>
      <label class="sm:col-span-2 text-sm">Categoría<input id="f-category" list="cats" placeholder="Clásicos, Infantil, Historia..." class="w-full px-3 py-2 border rounded"></label>
      <datalist id="cats"></datalist>
      <label class="sm:col-span-2 text-sm">Descripción<textarea id="f-description" rows="5" class="w-full px-3 py-2 border rounded"></textarea></label>
    </div>

    <div class="mt-4">
      <p class="text-sm mb-1">Imágenes</p>
      <label class="block border-2 border-dashed border-stone-300 rounded p-4 text-center text-sm text-stone-500 cursor-pointer hover:bg-stone-50">
        Hacé clic para elegir fotos (podés elegir varias)
        <input id="f-files" type="file" accept="image/*" multiple class="hidden" onchange="addFiles(this)">
      </label>
      <div id="thumbs" class="flex flex-wrap gap-2 mt-3"></div>
      <p class="text-xs text-stone-500 mt-1">La primera imagen es la portada.</p>
    </div>

    <div class="flex justify-end gap-2 mt-6">
      <button onclick="closeModal()" class="px-4 py-2 rounded border">Cancelar</button>
      <button id="btn-save" onclick="save()" class="px-5 py-2 rounded bg-stone-900 hover:bg-amber-800 text-white">Guardar</button>
    </div>
  </div>
</div>

<script>
let books = [], editingId = null, existing = [], newFiles = [];
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const FIELDS = ['title','author','year','publisher','price','category','description'];

async function load() { books = await (await fetch('/api/books')).json(); render(); }

function render() {
  const q = $('q').value.toLowerCase();
  const list = books.filter(b => [b.title,b.author,b.publisher,b.category,b.year].join(' ').toLowerCase().includes(q));
  $('count').textContent = `(${books.length} libros)`;
  $('cats').innerHTML = [...new Set(books.map(b => b.category))].map(c => `<option value="${esc(c)}">`).join('');
  $('grid').innerHTML = list.map(b => `
    <div class="bg-white rounded shadow-sm border overflow-hidden flex flex-col">
      <div class="h-44 bg-stone-200">${b.images[0] ? `<img src="${esc(b.images[0])}" class="w-full h-full object-cover">` : ''}</div>
      <div class="p-3 flex-1 flex flex-col justify-between">
        <div><p class="font-serif font-bold text-sm leading-tight">${esc(b.title)}</p>
        <p class="text-xs text-stone-500">${esc(b.author)} ${b.year ? '· ' + b.year : ''}</p>
        <p class="text-sm font-semibold mt-1">${b.price ? '$' + b.price.toLocaleString('es-AR') : '<span class="text-red-600">Sin precio</span>'}</p></div>
        <div class="flex gap-2 mt-2 text-xs">
          <button onclick="openModal('${b.id}')" class="flex-1 border rounded py-1 hover:bg-stone-100">Editar</button>
          <button onclick="del('${b.id}')" class="border rounded px-2 py-1 text-red-600 hover:bg-red-50">🗑️</button>
        </div>
      </div>
    </div>`).join('') || '<p class="col-span-full text-stone-500">No hay libros todavía. ¡Agregá el primero!</p>';
}

function openModal(id = null) {
  editingId = id; newFiles = [];
  const b = id ? books.find(x => x.id === id) : {};
  existing = [...(b.images || [])];
  FIELDS.forEach(f => $('f-' + f).value = b[f] || '');
  $('modal-title').textContent = id ? 'Editar libro' : 'Agregar libro';
  $('ig-box').classList.toggle('hidden', !!id);
  $('ig-url').value = ''; $('ig-caption').value = ''; $('ig-msg').textContent = '';
  thumbs(); $('modal').classList.replace('hidden', 'flex');
}
function closeModal() { $('modal').classList.replace('flex', 'hidden'); }

function addFiles(input) { newFiles.push(...input.files); input.value = ''; thumbs(); }
function thumbs() {
  const t = (src, fn, i) => `<div class="relative w-20 h-20"><img src="${src}" class="w-20 h-20 object-cover rounded border">
    <button onclick="${fn}(${i})" class="absolute -top-2 -right-2 bg-red-600 text-white rounded-full w-5 h-5 text-xs">×</button></div>`;
  $('thumbs').innerHTML = existing.map((s, i) => t(esc(s), 'rmExisting', i)).join('') +
    newFiles.map((f, i) => t(URL.createObjectURL(f), 'rmNew', i)).join('');
}
function rmExisting(i) { existing.splice(i, 1); thumbs(); }
function rmNew(i) { newFiles.splice(i, 1); thumbs(); }

async function save() {
  if (!$('f-title').value.trim()) { alert('Poné al menos el título.'); return; }
  const fd = new FormData();
  FIELDS.forEach(f => fd.append(f, $('f-' + f).value));
  fd.append('existing_images', JSON.stringify(existing));
  newFiles.forEach(f => fd.append('images', f));
  $('btn-save').disabled = true;
  const r = await fetch(editingId ? '/api/books/' + editingId : '/api/books', { method: editingId ? 'PUT' : 'POST', body: fd });
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
  const r = await fetch('/api/instagram', { method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ url: $('ig-url').value, caption: $('ig-caption').value }) });
  const j = await r.json();
  if (!j.ok) { msg.className = 'text-sm mt-2 text-red-600'; msg.textContent = j.error; return; }
  FIELDS.forEach(f => { if (j.data[f]) $('f-' + f).value = j.data[f]; });
  existing.push(...(j.data.images || [])); thumbs();
  msg.className = 'text-sm mt-2 ' + (j.warning ? 'text-amber-700' : 'text-green-700');
  msg.textContent = j.warning || '¡Listo! Revisá los datos y guardá.';
}

async function importProfile() {
  if (!confirm('Se van a traer las últimas publicaciones de Instagram como libros nuevos (revisalos y completá precios). ¿Seguir?')) return;
  const r = await fetch('/api/instagram_perfil', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}' });
  const j = await r.json();
  alert(j.ok ? `Se agregaron ${j.agregados} publicaciones nuevas.` : 'Instagram no respondió: ' + j.error +
    '\n\nProbá más tarde, iniciá sesión con instaloader, o importá publicación por publicación con "+ Agregar libro".');
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
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Catálogo de Libros - Libros Británicos</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-stone-100 text-stone-800 font-sans">
<header class="bg-stone-900 text-stone-100 py-6 shadow-md">
  <div class="container mx-auto px-4 flex justify-between items-center">
    <h1 class="text-2xl font-serif tracking-wide">Libros Británicos & Colección</h1>
    <button onclick="toggleCart()" class="bg-amber-700 hover:bg-amber-600 px-4 py-2 rounded text-sm font-medium">🛒 Carrito (<span id="cart-count">0</span>)</button>
  </div>
</header>

<main class="container mx-auto px-4 py-8">
  <div class="mb-8 max-w-xl mx-auto">
    <input id="search" oninput="filterBooks()" placeholder="Buscar por título, autor, año o temática..."
      class="w-full px-4 py-3 rounded-lg border border-stone-300 focus:outline-none focus:ring-2 focus:ring-amber-700 shadow-sm">
  </div>
  <div id="grid" class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-6"></div>
</main>

<!-- Detalle -->
<div id="detail" class="fixed inset-0 bg-black/60 hidden items-center justify-center z-40 p-4" onclick="if(event.target===this)closeDetail()">
  <div class="bg-white rounded-lg max-w-3xl w-full max-h-[90vh] overflow-y-auto p-5 grid md:grid-cols-2 gap-5" id="detail-body"></div>
</div>

<!-- Carrito -->
<div id="cart-modal" class="fixed inset-0 bg-black/50 hidden justify-end z-50">
  <div class="bg-white w-full max-w-md h-full p-6 flex flex-col shadow-xl">
    <div class="flex justify-between items-center border-b pb-4">
      <h2 class="text-xl font-serif font-bold">Tu pedido</h2>
      <button onclick="toggleCart()" class="text-stone-500 text-2xl font-bold">&times;</button>
    </div>
    <div id="cart-items" class="flex-1 overflow-y-auto py-4 space-y-3"></div>
    <div class="border-t pt-4">
      <div class="flex justify-between font-bold text-lg mb-4"><span>Total estimado:</span><span id="cart-total">$0 ARS</span></div>
      <button onclick="checkout()" class="w-full bg-green-600 hover:bg-green-700 text-white py-3 rounded-lg font-medium shadow">Enviar pedido por WhatsApp 📱</button>
    </div>
  </div>
</div>

<script>
const books = __BOOKS__;
const PHONE = "__PHONE__";
let cart = [];
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = n => '$' + Number(n).toLocaleString('es-AR');

function renderBooks(list) {
  $('grid').innerHTML = list.map(b => `
    <div class="bg-white rounded-lg shadow-sm border border-stone-200 overflow-hidden flex flex-col">
      <div class="h-64 bg-stone-200 overflow-hidden relative cursor-pointer" onclick="openDetail('${b.id}')">
        ${b.images[0] ? `<img src="${esc(b.images[0])}" alt="${esc(b.title)}" loading="lazy" class="w-full h-full object-cover">` : ''}
        ${b.year ? `<span class="absolute top-2 right-2 bg-stone-900 text-white text-xs px-2 py-1 rounded">${b.year}</span>` : ''}
      </div>
      <div class="p-4 flex-1 flex flex-col justify-between">
        <div class="cursor-pointer" onclick="openDetail('${b.id}')">
          <span class="text-xs uppercase tracking-wider text-amber-800 font-semibold">${esc(b.category)}</span>
          <h3 class="font-serif font-bold text-lg text-stone-900 mt-1">${esc(b.title)}</h3>
          <p class="text-sm text-stone-600">${esc(b.author)}</p>
        </div>
        <div class="mt-4 pt-4 border-t border-stone-100 flex items-center justify-between">
          <span class="font-bold text-lg">${b.price ? money(b.price) : 'Consultar'}</span>
          <button onclick="addToCart('${b.id}')" class="bg-stone-900 hover:bg-amber-800 text-white text-sm px-3 py-2 rounded">Agregar</button>
        </div>
      </div>
    </div>`).join('') || '<p class="col-span-full text-center text-stone-500 py-10">No se encontraron libros.</p>';
}

function filterBooks() {
  const q = $('search').value.toLowerCase();
  renderBooks(books.filter(b => [b.title, b.author, b.publisher, b.year, b.category, b.description].join(' ').toLowerCase().includes(q)));
}

function openDetail(id) {
  const b = books.find(x => x.id === id);
  $('detail-body').innerHTML = `
    <div>
      <img id="main-img" src="${esc(b.images[0] || '')}" class="w-full rounded object-contain max-h-96 bg-stone-100">
      <div class="flex gap-2 mt-2 flex-wrap">${b.images.map(s => `<img src="${esc(s)}" onclick="document.getElementById('main-img').src=this.src" class="w-16 h-16 object-cover rounded cursor-pointer border">`).join('')}</div>
    </div>
    <div class="flex flex-col">
      <button onclick="closeDetail()" class="self-end text-2xl text-stone-500">&times;</button>
      <span class="text-xs uppercase tracking-wider text-amber-800 font-semibold">${esc(b.category)}</span>
      <h2 class="font-serif font-bold text-2xl">${esc(b.title)}</h2>
      <p class="text-stone-600">${esc(b.author)}</p>
      <p class="text-sm text-stone-500 mb-3">${[b.publisher, b.year || ''].filter(Boolean).map(esc).join(' · ')}</p>
      <p class="text-sm whitespace-pre-line flex-1">${esc(b.description)}</p>
      <div class="mt-4 flex items-center justify-between">
        <span class="font-bold text-xl">${b.price ? money(b.price) : 'Consultar'}</span>
        <button onclick="addToCart('${b.id}');closeDetail()" class="bg-stone-900 hover:bg-amber-800 text-white px-4 py-2 rounded">Agregar</button>
      </div>
    </div>`;
  $('detail').classList.replace('hidden', 'flex');
}
function closeDetail() { $('detail').classList.replace('flex', 'hidden'); }

function toggleCart() { const m = $('cart-modal'); m.classList.toggle('hidden'); m.classList.toggle('flex'); }

function addToCart(id) {
  const b = books.find(x => x.id === id), ex = cart.find(i => i.id === id);
  if (ex) ex.quantity++; else cart.push({ ...b, quantity: 1 });
  updateCart();
}
function removeFromCart(id) { cart = cart.filter(i => i.id !== id); updateCart(); }

function updateCart() {
  $('cart-count').innerText = cart.reduce((a, i) => a + i.quantity, 0);
  let total = 0;
  $('cart-items').innerHTML = cart.map(i => {
    total += i.price * i.quantity;
    return `<div class="flex justify-between items-center bg-stone-50 p-3 rounded">
      <div><h4 class="font-bold text-sm">${esc(i.title)}</h4>
      <p class="text-xs text-stone-500">${money(i.price)} x ${i.quantity}</p></div>
      <button onclick="removeFromCart('${i.id}')" class="text-red-500 font-bold">🗑️</button></div>`;
  }).join('');
  $('cart-total').innerText = money(total) + ' ARS';
}

function checkout() {
  if (!cart.length) { alert('Tu carrito está vacío.'); return; }
  let msg = '¡Hola! Quiero encargar los siguientes libros de tu catálogo:\n\n', total = 0;
  cart.forEach(i => {
    msg += `• ${i.quantity}x ${i.title}${i.year ? ' (' + i.year + ')' : ''} - ${money(i.price * i.quantity)}\n`;
    total += i.price * i.quantity;
  });
  msg += `\n*Total estimado:* ${money(total)} ARS`;
  window.open(`https://wa.me/${PHONE}?text=${encodeURIComponent(msg)}`, '_blank');
}

renderBooks(books);
</script>
</body></html>
"""

if __name__ == "__main__":
    if "--build" in sys.argv:  # modo para GitHub Actions: solo genera sitio/ y termina
        publish_site()
        print("Sitio generado en", SITE_DIR)
        sys.exit(0)
    publish_site()  # deja el sitio actualizado al abrir
    threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    app.run(host="127.0.0.1", port=PORT, debug=False)
    publish_site()  # deja el sitio actualizado al abrir
    threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    app.run(host="127.0.0.1", port=PORT, debug=False)
