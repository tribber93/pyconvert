import os
import re
import sys
import glob
import json
import time
import uuid
import hmac
import shutil
import secrets
import zipfile
import threading
import subprocess
from pathlib import Path
from queue import Queue
from concurrent.futures import ThreadPoolExecutor
from flask import (Flask, render_template, request, jsonify, send_file,
                   send_from_directory, Response, stream_with_context,
                   session, redirect, url_for)
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"
TEMP_DIR = BASE_DIR / "temp"
FFDEC_DIR = BASE_DIR / "ffdec"
FFDEC_JAR = FFDEC_DIR / "ffdec.jar"
INSTANCE_DIR = BASE_DIR / "instance"

for folder in [UPLOADS_DIR, OUTPUT_DIR, TEMP_DIR, FFDEC_DIR, INSTANCE_DIR]:
    folder.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB max upload limit

# ---------------------------------------------------------------------------
# Autentikasi
# ---------------------------------------------------------------------------
# Password aplikasi. Untuk deploy, sebaiknya diatur lewat environment variable
# PYCONVERT_PASSWORD; nilai di bawah adalah default yang diminta.
APP_PASSWORD = os.environ.get("PYCONVERT_PASSWORD") or "432187659"


def _load_secret_key():
    """
    SECRET_KEY untuk menandatangani cookie session.
    Dibuat acak sekali lalu disimpan di instance/secret_key supaya session tetap
    valid setelah server restart (kalau dibiarkan acak tiap start, semua orang
    akan ter-logout setiap kali server dijalankan ulang).
    """
    env_key = os.environ.get("PYCONVERT_SECRET_KEY")
    if env_key:
        return env_key
    key_file = INSTANCE_DIR / "secret_key"
    try:
        if key_file.exists():
            return key_file.read_bytes()
    except Exception:
        pass
    key = secrets.token_bytes(32)
    try:
        key_file.write_bytes(key)
        os.chmod(key_file, 0o600)
    except Exception:
        pass
    return key


app.config.update(
    SECRET_KEY=_load_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,   # cookie tidak bisa dibaca JavaScript
    SESSION_COOKIE_SAMESITE="Lax",  # mencegah cookie ikut pada POST lintas situs
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 7,  # 7 hari
)

# Endpoint yang boleh diakses tanpa login (halaman login & file statis).
PUBLIC_ENDPOINTS = {"login", "static"}


def _password_valid(candidate):
    """Bandingkan password dengan waktu konstan agar tidak bocor lewat timing."""
    return hmac.compare_digest(str(candidate), str(APP_PASSWORD))


def _safe_next_url(url):
    """Hanya izinkan redirect internal, cegah open redirect ke situs lain."""
    return bool(url) and url.startswith("/") and not url.startswith("//") and "\\" not in url


@app.before_request
def require_login():
    """Gerbang utama: semua halaman & API wajib login dulu."""
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if request.path.startswith("/static/"):
        return None
    if session.get("authed"):
        return None
    # Endpoint API balas JSON (dipakai fetch), halaman biasa dialihkan ke login
    if request.path.startswith("/api/"):
        return jsonify({"error": "Silakan login terlebih dahulu"}), 401
    nxt = request.path
    if request.query_string:
        nxt += "?" + request.query_string.decode("utf-8", "ignore")
    return redirect(url_for("login", next=nxt))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authed"):
        return redirect(url_for("index"))

    error = None
    next_url = request.values.get("next", "")

    if request.method == "POST":
        if _password_valid(request.form.get("password", "")):
            session.clear()
            session["authed"] = True
            session.permanent = True
            return redirect(next_url if _safe_next_url(next_url) else url_for("index"))
        error = "Password salah. Silakan coba lagi."
        # Perlambat upaya coba-coba password
        time.sleep(0.5)

    return render_template("login.html", error=error, next_url=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


_ILLEGAL_NAME_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED_WIN_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *[f"COM{i}" for i in range(1, 10)],
    *[f"LPT{i}" for i in range(1, 10)],
}


def safe_folder_segment(name):
    """
    Bersihkan satu segmen nama folder/file dari path traversal & karakter ilegal,
    tapi tetap mempertahankan spasi dan karakter non-ASCII supaya nama folder asli
    tidak berubah (secure_filename akan mengubah 'Kursus IPA' jadi 'Kursus_IPA').
    """
    name = str(name).replace("\\", "/").split("/")[-1].strip()
    name = _ILLEGAL_NAME_CHARS.sub("_", name)
    # Windows tidak mengizinkan nama berakhir dengan titik/spasi
    name = name.rstrip(" .")
    if not name or name in (".", ".."):
        return "folder"
    if name.split(".")[0].upper() in _RESERVED_WIN_NAMES:
        name = f"_{name}"
    return name


def build_output_path(folder_rel, stem):
    """
    Tentukan lokasi file MP4 hasil konversi.
    folder_rel: folder relatif (relatif ke uploads/) tempat SWF berasal, mis. "kursus_ipa/bab1".
                Kosong berarti file diupload langsung (tanpa folder).
    Hasil: output/<folder_rel>/<stem>.mp4 — jadi file dari sebuah folder
           dikeluarkan ke folder dengan nama yang sama di dalam output/.
    """
    out_dir = OUTPUT_DIR
    if folder_rel:
        for seg in folder_rel.split("/"):
            seg = safe_folder_segment(seg)
            if seg:
                out_dir = out_dir / seg
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{safe_folder_segment(stem)}.mp4"


def unique_path(path):
    """Hindari menimpa file lama dengan menambahkan sufiks _1, _2, dst."""
    if not path.exists():
        return path
    for i in range(1, 10000):
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path


# Helper to serve static folder contents with a modern File Manager UI
def serve_folder_files(base_folder, subpath="", url_prefix=None):
    # url_prefix is the URL mount for this folder; defaults to /<folder name>
    if url_prefix is None:
        url_prefix = f"/{base_folder.name}"
    # Allow optional preview parameter e.g. ?raw=true
    is_raw = request.args.get("raw") == "true"
    target_path = (base_folder / subpath).resolve()
    if not str(target_path).startswith(str(base_folder.resolve())):
        return "Akses ditolak", 403

    if target_path.is_file():
        if is_raw or request.args.get("download") != "true":
            # Serve for viewing or streaming
            ext = target_path.suffix.lower()
            mimetypes = {
                ".mp4": "video/mp4",
                ".avi": "video/x-msvideo",
                ".mp3": "audio/mpeg",
                ".wav": "audio/wav",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".swf": "application/x-shockwave-flash",
                ".json": "application/json",
                ".txt": "text/plain",
                ".log": "text/plain"
            }
            mtype = mimetypes.get(ext)
            return send_from_directory(target_path.parent, target_path.name, mimetype=mtype)
        else:
            return send_from_directory(target_path.parent, target_path.name, as_attachment=True)
    
    elif target_path.is_dir():
        items = []
        try:
            for item in sorted(target_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                # If viewing the root BASE_DIR, only include 'uploads', 'temp', and 'output'
                if target_path == BASE_DIR.resolve():
                    if item.name not in ["uploads", "temp", "output"]:
                        continue

                rel_item = item.relative_to(base_folder)
                size_bytes = item.stat().st_size if item.is_file() else 0
                
                # Determine icon & type category
                ext = item.suffix.lower()
                category = "folder" if item.is_dir() else "file"
                icon = "fa-folder text-amber" if item.is_dir() else "fa-file text-gray"
                
                if item.is_file():
                    if ext in [".mp4", ".avi", ".mov", ".mkv"]:
                        icon = "fa-file-video text-cyan"
                        category = "video"
                    elif ext in [".mp3", ".wav", ".flac", ".ogg", ".aac"]:
                        icon = "fa-file-audio text-emerald"
                        category = "audio"
                    elif ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"]:
                        icon = "fa-file-image text-rose"
                        category = "image"
                    elif ext == ".swf":
                        icon = "fa-bolt text-yellow"
                        category = "flash"
                    elif ext in [".txt", ".log", ".json", ".xml"]:
                        icon = "fa-file-code text-indigo"
                        category = "code"

                items.append({
                    "name": item.name,
                    "is_dir": item.is_dir(),
                    "size": size_bytes,
                    "ext": ext,
                    "icon": icon,
                    "category": category,
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item.stat().st_mtime)),
                    "rel_path": str(rel_item).replace("\\", "/")
                })
        except Exception as e:
            pass

        folder_name = base_folder.name
        
        # Build breadcrumbs
        crumbs = [{"name": folder_name, "link": url_prefix}]
        if subpath:
            parts = [p for p in subpath.split("/") if p]
            curr_link = url_prefix
            for part in parts:
                curr_link += f"/{part}"
                crumbs.append({"name": part, "link": curr_link})

        crumb_html = ' <span class="divider">/</span> '.join([f'<a href="{c["link"]}">{c["name"]}</a>' for c in crumbs])

        active_root = 'active' if folder_name == BASE_DIR.name else ''
        active_uploads = 'active' if folder_name == 'uploads' else ''
        active_temp = 'active' if folder_name == 'temp' else ''
        active_output = 'active' if folder_name == 'output' else ''

        html = f"""<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>File Manager - /{folder_name}/{subpath}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root {{
            --bg: #0b1120;
            --card-bg: #1e293b;
            --card-hover: #334155;
            --border: #334155;
            --text: #f8fafc;
            --muted: #94a3b8;
            --primary: #6366f1;
            --primary-hover: #4f46e5;
            --cyan: #06b6d4;
            --amber: #f59e0b;
            --emerald: #10b981;
            --rose: #f43f5e;
            --indigo: #818cf8;
            --yellow: #eab308;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', sans-serif; }}
        body {{ background-color: var(--bg); color: var(--text); padding: 24px; min-height: 100vh; }}
        .fm-header {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }}
        .fm-title {{ display: flex; align-items: center; gap: 12px; font-size: 1.25rem; font-weight: 600; }}
        .fm-title i {{ color: var(--primary); font-size: 1.5rem; }}
        .breadcrumbs {{ background: var(--card-bg); padding: 10px 16px; border-radius: 8px; border: 1px solid var(--border); font-size: 0.95rem; color: var(--muted); }}
        .breadcrumbs a {{ color: #38bdf8; text-decoration: none; font-weight: 500; }}
        .breadcrumbs a:hover {{ text-decoration: underline; }}
        .breadcrumbs .divider {{ margin: 0 6px; color: var(--muted); }}
        .nav-links {{ display: flex; gap: 8px; }}
        .nav-btn {{ display: inline-flex; align-items: center; gap: 6px; background: var(--card-bg); color: var(--text); padding: 8px 14px; border-radius: 6px; border: 1px solid var(--border); text-decoration: none; font-size: 0.875rem; font-weight: 500; transition: all 0.2s; }}
        .nav-btn:hover {{ background: var(--card-hover); border-color: #475569; color: #fff; }}
        .nav-btn.active {{ background: var(--primary); border-color: var(--primary); }}
        
        .search-box {{ width: 100%; margin-bottom: 20px; position: relative; }}
        .search-box input {{ width: 100%; padding: 12px 16px 12px 42px; background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; color: var(--text); font-size: 0.95rem; outline: none; }}
        .search-box input:focus {{ border-color: var(--primary); }}
        .search-box i {{ position: absolute; left: 14px; top: 50%; transform: translateY(-50%); color: var(--muted); }}

        .table-container {{ background: var(--card-bg); border-radius: 12px; border: 1px solid var(--border); overflow: hidden; box-shadow: 0 10px 25px -5px rgba(0,0,0,0.3); }}
        table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 0.9rem; }}
        th {{ background: #0f172a; padding: 14px 18px; color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 0.75rem; letter-spacing: 0.05em; border-bottom: 1px solid var(--border); }}
        td {{ padding: 14px 18px; border-bottom: 1px solid var(--border); vertical-align: middle; }}
        tr:last-child td {{ border-bottom: none; }}
        tr:hover td {{ background: rgba(255,255,255,0.03); }}
        
        .file-item {{ display: flex; align-items: center; gap: 12px; font-weight: 500; }}
        .file-item i {{ font-size: 1.2rem; width: 24px; text-align: center; }}
        .file-item a {{ color: var(--text); text-decoration: none; word-break: break-all; }}
        .file-item a:hover {{ color: #38bdf8; text-decoration: underline; }}
        
        .text-amber {{ color: var(--amber); }}
        .text-cyan {{ color: var(--cyan); }}
        .text-emerald {{ color: var(--emerald); }}
        .text-rose {{ color: var(--rose); }}
        .text-indigo {{ color: var(--indigo); }}
        .text-yellow {{ color: var(--yellow); }}
        .text-gray {{ color: var(--muted); }}

        .btn-action {{ display: inline-flex; align-items: center; gap: 4px; padding: 6px 10px; background: #0f172a; border: 1px solid var(--border); color: var(--text); border-radius: 6px; text-decoration: none; font-size: 0.8rem; margin-right: 4px; transition: all 0.15s; }}
        .btn-action:hover {{ background: var(--primary); border-color: var(--primary); }}
        .badge-type {{ padding: 4px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; background: #0f172a; color: var(--muted); border: 1px solid var(--border); }}

        /* Preview Modal */
        .modal {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.85); backdrop-filter: blur(4px); z-index: 1000; align-items: center; justify-content: center; padding: 20px; }}
        .modal.active {{ display: flex; }}
        .modal-body {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; max-width: 900px; width: 100%; max-height: 90vh; overflow: hidden; display: flex; flex-direction: column; }}
        .modal-header {{ padding: 16px 20px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; }}
        .modal-content {{ padding: 20px; text-align: center; overflow-y: auto; max-height: 75vh; }}
        .modal-content video, .modal-content img, .modal-content audio {{ max-width: 100%; max-height: 65vh; border-radius: 8px; }}
        .close-modal {{ background: none; border: none; color: var(--muted); font-size: 1.5rem; cursor: pointer; }}
        .close-modal:hover {{ color: #fff; }}
    </style>
</head>
<body>
    <div class="fm-header">
        <div class="fm-title">
            <i class="fa-solid fa-folder-tree"></i>
            <span>File Manager</span>
        </div>
        <div class="nav-links">
            <a href="/" class="nav-btn"><i class="fa-solid fa-house"></i> Home Studio</a>
            <a href="/files" class="nav-btn {active_root}"><i class="fa-solid fa-folder-tree"></i> Root File Manager</a>
            <a href="/uploads" class="nav-btn {active_uploads}"><i class="fa-solid fa-upload"></i> Uploads</a>
            <a href="/temp" class="nav-btn {active_temp}"><i class="fa-solid fa-clock-rotate-left"></i> Temp</a>
            <a href="/output" class="nav-btn {active_output}"><i class="fa-solid fa-circle-check"></i> Output</a>
        </div>
    </div>

    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; flex-wrap: wrap; gap: 12px;">
        <div class="breadcrumbs">
            <i class="fa-solid fa-hard-drive" style="margin-right: 8px; color: var(--primary);"></i>
            {crumb_html}
        </div>
        <div style="font-size: 0.85rem; color: var(--muted);">
            Total Item: <strong>{len(items)}</strong>
        </div>
    </div>

    <div class="search-box">
        <i class="fa-solid fa-magnifying-glass"></i>
        <input type="text" id="searchInput" placeholder="Cari file atau folder..." onkeyup="filterItems()">
    </div>

    <!-- Batch Selection Toolbar -->
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; background: #1e293b; padding: 10px 16px; border-radius: 8px; border: 1px solid var(--border); flex-wrap: wrap; gap: 8px;">
        <div style="display: flex; align-items: center; gap: 10px;">
            <input type="checkbox" id="selectAllCheckbox" onchange="toggleSelectAll(this)" style="width: 18px; height: 18px; cursor: pointer;">
            <label for="selectAllCheckbox" style="font-size: 0.9rem; font-weight: 500; cursor: pointer;">Pilih Semua</label>
            <span style="font-size: 0.85rem; color: var(--muted); margin-left: 8px;" id="selectedCountText">(0 item dipilih)</span>
        </div>
        <div style="display: flex; gap: 8px;">
            <button class="btn-action" style="background: var(--rose); border-color: var(--rose); color: white; padding: 8px 14px; font-weight: 600;" onclick="deleteSelectedItems()">
                <i class="fa-solid fa-trash-can"></i> Hapus Terpilih
            </button>
            <button class="btn-action" style="background: var(--primary); border-color: var(--primary); color: white; padding: 8px 14px; font-weight: 600;" onclick="compressSelectedItems()">
                <i class="fa-solid fa-file-zipper"></i> Kompres (.zip)
            </button>
        </div>
    </div>

    <div class="table-container">
        <table>
            <thead>
                <tr>
                    <th style="width: 40px; text-align: center;"></th>
                    <th>Nama File / Folder</th>
                    <th>Tipe</th>
                    <th>Ukuran</th>
                    <th>Modifikasi Terakhir</th>
                    <th style="text-align: right;">Aksi</th>
                </tr>
            </thead>
            <tbody id="fileTable">
"""
        if subpath:
            parent_subpath = "/".join(subpath.rstrip("/").split("/")[:-1])
            parent_link = f"{url_prefix}/{parent_subpath}" if parent_subpath else url_prefix
            html += f"""
                <tr>
                    <td></td>
                    <td colspan="5">
                        <div class="file-item">
                            <i class="fa-solid fa-arrow-left text-amber"></i>
                            <a href="{parent_link}">.. (Kembali ke folder atas)</a>
                        </div>
                    </td>
                </tr>
            """

        if not items:
            html += f"""
                <tr>
                    <td colspan="6" style="text-align: center; padding: 40px; color: var(--muted);">
                        <i class="fa-regular fa-folder-open" style="font-size: 2.5rem; margin-bottom: 12px; display: block;"></i>
                        Folder ini kosong.
                    </td>
                </tr>
            """

        for item in items:
            item_url = f"{url_prefix}/{item['rel_path']}"
            size_str = "-"
            if not item["is_dir"]:
                size_str = f"{item['size'] / (1024 * 1024):.2f} MB" if item['size'] > 1024*1024 else f"{item['size'] / 1024:.1f} KB"
            
            preview_btn = ""
            if item["category"] in ["video", "image", "audio"]:
                preview_btn = f'<button class="btn-action" onclick="openPreview(\'{item_url}\', \'{item["category"]}\', \'{item["name"]}\')"><i class="fa-solid fa-eye"></i> Preview</button>'
            elif not item["is_dir"]:
                preview_btn = f'<a class="btn-action" href="{item_url}?raw=true" target="_blank"><i class="fa-solid fa-up-right-from-square"></i> Buka</a>'

            download_btn = ""
            if not item["is_dir"]:
                download_btn = f'<a class="btn-action" href="{item_url}?download=true" download><i class="fa-solid fa-download"></i> Unduh</a>'

            delete_btn = f'<button class="btn-action" style="border-color: #f43f5e; color: #f43f5e;" onclick="deleteSingleItem(\'{item["rel_path"]}\')"><i class="fa-solid fa-trash"></i></button>'

            html += f"""
                <tr class="item-row" data-name="{item['name'].lower()}">
                    <td style="text-align: center;">
                        <input type="checkbox" class="item-checkbox" value="{item['rel_path']}" onchange="updateSelectedCount()" style="width: 16px; height: 16px; cursor: pointer;">
                    </td>
                    <td>
                        <div class="file-item">
                            <i class="fa-solid {item['icon']}"></i>
                            <a href="{item_url}">{item['name']}</a>
                        </div>
                    </td>
                    <td><span class="badge-type">{item['category']}</span></td>
                    <td>{size_str}</td>
                    <td style="color: var(--muted); font-size: 0.85rem;">{item['mtime']}</td>
                    <td style="text-align: right;">
                        {preview_btn}
                        {download_btn}
                        {delete_btn}
                    </td>
                </tr>
            """

        html += f"""
            </tbody>
        </table>
    </div>

    <!-- Preview Modal -->
    <div class="modal" id="previewModal">
        <div class="modal-body">
            <div class="modal-header">
                <h4 id="modalTitle">Preview File</h4>
                <button class="close-modal" onclick="closePreview()">&times;</button>
            </div>
            <div class="modal-content" id="modalContainer"></div>
        </div>
    </div>

    <script>
        const currentFolderName = "{folder_name}";

        function filterItems() {{
            const query = document.getElementById('searchInput').value.toLowerCase();
            const rows = document.querySelectorAll('.item-row');
            rows.forEach(row => {{
                const name = row.getAttribute('data-name');
                if (name.includes(query)) {{
                    row.style.display = '';
                }} else {{
                    row.style.display = 'none';
                }}
            }});
        }}

        function toggleSelectAll(master) {{
            const checkboxes = document.querySelectorAll('.item-checkbox');
            checkboxes.forEach(cb => {{
                if (cb.closest('tr').style.display !== 'none') {{
                    cb.checked = master.checked;
                }}
            }});
            updateSelectedCount();
        }}

        function getSelectedItems() {{
            const checkboxes = document.querySelectorAll('.item-checkbox:checked');
            return Array.from(checkboxes).map(cb => cb.value);
        }}

        function updateSelectedCount() {{
            const selected = getSelectedItems();
            document.getElementById('selectedCountText').textContent = `(${{selected.length}} item dipilih)`;
        }}

        async function deleteSingleItem(relPath) {{
            if (!confirm('Apakah Anda yakin ingin menghapus item ini?')) return;
            await sendDelete([relPath]);
        }}

        async function deleteSelectedItems() {{
            const selected = getSelectedItems();
            if (selected.length === 0) {{
                alert('Pilih setidaknya satu file/folder untuk dihapus!');
                return;
            }}
            if (!confirm(`Apakah Anda yakin ingin menghapus ${{selected.length}} item terpilih?`)) return;
            await sendDelete(selected);
        }}

        async function sendDelete(items) {{
            try {{
                const res = await fetch('/api/files/delete', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ folder_name: currentFolderName, items: items }})
                }});
                const data = await res.json();
                if (data.success) {{
                    location.reload();
                }} else {{
                    alert('Gagal menghapus: ' + (data.error || 'Terjadi kesalahan'));
                }}
            }} catch (e) {{
                alert('Gagal melakukan koneksi ke server.');
            }}
        }}

        async function compressSelectedItems() {{
            const selected = getSelectedItems();
            if (selected.length === 0) {{
                alert('Pilih setidaknya satu file/folder untuk dikompres!');
                return;
            }}

            try {{
                const res = await fetch('/api/files/compress', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ folder_name: currentFolderName, items: selected }})
                }});
                const data = await res.json();
                if (data.success && data.zip_url) {{
                    window.location.href = data.zip_url;
                }} else {{
                    alert('Gagal mengompres: ' + (data.error || 'Terjadi kesalahan'));
                }}
            }} catch (e) {{
                alert('Gagal membuat arsip zip.');
            }}
        }}

        function openPreview(url, category, name) {{
            const modal = document.getElementById('previewModal');
            const container = document.getElementById('modalContainer');
            const title = document.getElementById('modalTitle');
            title.textContent = name;
            container.innerHTML = '';

            if (category === 'video') {{
                container.innerHTML = `<video src="${{url}}" controls autoplay style="width: 100%;"></video>`;
            }} else if (category === 'image') {{
                container.innerHTML = `<img src="${{url}}" alt="${{name}}">`;
            }} else if (category === 'audio') {{
                container.innerHTML = `<audio src="${{url}}" controls autoplay style="width: 100%; margin-top: 20px;"></audio>`;
            }}

            modal.classList.add('active');
        }}

        function closePreview() {{
            const modal = document.getElementById('previewModal');
            const container = document.getElementById('modalContainer');
            modal.classList.remove('active');
            container.innerHTML = '';
        }}
    </script>
</body>
</html>
"""
        return html
    
    return "File atau folder tidak ditemukan", 404

@app.route("/files", defaults={"subpath": ""})
@app.route("/files/<path:subpath>")
def serve_files_root(subpath):
    # Root file manager endpoint showing BASE_DIR folders (uploads, temp, output)
    return serve_folder_files(BASE_DIR, subpath, url_prefix="/files")

@app.route("/uploads", defaults={"subpath": ""})
@app.route("/uploads/<path:subpath>")
def serve_uploads(subpath):
    return serve_folder_files(UPLOADS_DIR, subpath)

@app.route("/temp", defaults={"subpath": ""})
@app.route("/temp/<path:subpath>")
def serve_temp(subpath):
    return serve_folder_files(TEMP_DIR, subpath)

@app.route("/output", defaults={"subpath": ""})
@app.route("/output/<path:subpath>")
def serve_output(subpath):
    return serve_folder_files(OUTPUT_DIR, subpath)

def resolve_allowed_path(folder_name, subpath):
    folder_map = {
        "uploads": UPLOADS_DIR,
        "temp": TEMP_DIR,
        "output": OUTPUT_DIR,
        BASE_DIR.name: BASE_DIR
    }
    base = folder_map.get(folder_name)
    if not base:
        return None
    
    target = (base / subpath).resolve()
    # Security check: must be strictly inside allowed directories (UPLOADS_DIR, TEMP_DIR, OUTPUT_DIR)
    allowed_roots = [UPLOADS_DIR.resolve(), TEMP_DIR.resolve(), OUTPUT_DIR.resolve()]
    is_valid = any(str(target).startswith(str(root)) for root in allowed_roots)
    if not is_valid:
        return None
    return target

@app.route("/api/files/delete", methods=["POST"])
def delete_file_manager_items():
    data = request.json or {}
    folder_name = data.get("folder_name", "")
    items = data.get("items", [])  # list of relative paths
    
    if not items:
        return jsonify({"error": "Tidak ada item yang dipilih untuk dihapus."}), 400

    deleted_count = 0
    errors = []

    for item_rel in items:
        target = resolve_allowed_path(folder_name, item_rel)
        if not target or not target.exists():
            errors.append(f"Akses ditolak atau item tidak ditemukan: {item_rel}")
            continue

        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                os.remove(target)
            deleted_count += 1
        except Exception as e:
            errors.append(f"Gagal menghapus {item_rel}: {str(e)}")

    return jsonify({
        "success": True,
        "deleted_count": deleted_count,
        "errors": errors
    })

@app.route("/api/files/compress", methods=["POST"])
def compress_file_manager_items():
    data = request.json or {}
    folder_name = data.get("folder_name", "")
    items = data.get("items", [])
    
    if not items:
        return jsonify({"error": "Tidak ada item yang dipilih untuk dikompres."}), 400

    targets = []
    for item_rel in items:
        target = resolve_allowed_path(folder_name, item_rel)
        if target and target.exists():
            targets.append((item_rel, target))

    if not targets:
        return jsonify({"error": "Item yang dipilih tidak ditemukan."}), 400

    zip_filename = f"compressed_{int(time.time())}.zip"
    zip_path = TEMP_DIR / zip_filename

    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for item_rel, target in targets:
                if target.is_file():
                    z.write(target, arcname=target.name)
                elif target.is_dir():
                    for root, dirs, files in os.walk(target):
                        for file in files:
                            full_p = Path(root) / file
                            arc_name = full_p.relative_to(target.parent)
                            z.write(full_p, arcname=str(arc_name))
        
        return jsonify({
            "success": True,
            "zip_url": f"/temp/{zip_filename}?download=true",
            "zip_name": zip_filename
        })
    except Exception as e:
        return jsonify({"error": f"Gagal mengompres file: {str(e)}"}), 500

# In-memory storage for conversion tasks
# Task format:
# {
#    "id": str,
#    "filename": str,
#    "upload_path": str,
#    "output_path": str,
#    "status": "pending" | "exporting_frames" | "exporting_sound" | "encoding_mp4" | "completed" | "error",
#    "progress": int (0..100),
#    "logs": list of str,
#    "error_message": str,
#    "file_size": int,
#    "created_at": float,
#    "completed_at": float
# }
tasks_lock = threading.Lock()
tasks = {}
# max_workers=1 -> konversi dijalankan satu file pada satu waktu (antrean FIFO),
# supaya tidak ada beberapa Java/FFmpeg yang berebut CPU & memori sekaligus.
executor = ThreadPoolExecutor(max_workers=1)

# Registry proses yang sedang berjalan per task, dipakai untuk membatalkan.
# Disimpan TERPISAH dari dict 'tasks' karena /api/tasks men-jsonify seluruh isi
# tasks; objek Popen tidak bisa diserialisasi ke JSON.
running_procs = {}
running_procs_lock = threading.Lock()

# Status khusus "dibatalkan". Hanya status ini (dan completed/error) yang
# menyimpan completed_at, supaya task yang dibatalkan tercatat selesai.
TERMINAL_STATUSES = {"completed", "error", "cancelled"}


class ConversionCancelled(Exception):
    """Dilempar saat konversi dihentikan karena permintaan pembatalan pengguna."""


def register_process(task_id, process):
    """Tautkan proses Popen aktif ke sebuah task (untuk pembatalan)."""
    with running_procs_lock:
        running_procs[task_id] = process


def unregister_process(task_id):
    with running_procs_lock:
        running_procs.pop(task_id, None)


def purge_task(task_id):
    """
    Hapus task dari daftar konversi beserta file sementaranya.
    Dipakai saat task dibatalkan, supaya task yang dibatalkan tidak menumpuk
    di daftar. Aman dipanggil berkali-kali.
    """
    with tasks_lock:
        task = tasks.pop(task_id, None)
    if not task:
        return
    upload_p = Path(task.get("upload_path", ""))
    if upload_p.exists():
        try:
            os.remove(upload_p)
        except Exception:
            pass
    temp_p = TEMP_DIR / task_id
    if temp_p.exists():
        try:
            shutil.rmtree(temp_p, ignore_errors=True)
        except Exception:
            pass


def request_cancel(task_id):
    """
    Minta konversi dibatalkan, lalu buang task dari daftar.
    - Jika proses sedang berjalan: matikan proses beserta anak-anaknya
      (Java/FFmpeg) lewat taskkill. Task baru dihapus dari daftar setelah
      prosesnya benar-benar berhenti (lihat convert_swf_to_mp4).
    - Jika masih menunggu di antrean: langsung dihapus dari daftar.
    Return nilai status task setelah permintaan ('cancelled' / 'cancelling').
    """
    with tasks_lock:
        task = tasks.get(task_id)
        if not task:
            return None
        if task["status"] in TERMINAL_STATUSES:
            return task["status"]  # sudah selesai/gagal, tidak perlu apa-apa
        task["cancel_requested"] = True

    with running_procs_lock:
        process = running_procs.get(task_id)

    if process is None:
        # Belum mulai (masih antre) -> langsung buang dari daftar
        purge_task(task_id)
        return "cancelled"

    # Proses sedang berjalan -> hentikan paksa seluruh proses anaknya
    kill_process_tree(process)
    with tasks_lock:
        if task_id in tasks:
            tasks[task_id]["logs"].append(
                f"[{time.strftime('%H:%M:%S')}] Permintaan batal diterima, menghentikan proses..."
            )
    return "cancelling"


def kill_process_tree(process):
    """Matikan sebuah proses beserta seluruh child process-nya (java/ffmpeg)."""
    try:
        if process.poll() is not None:
            return  # sudah berhenti sendiri
        if os.name == "nt":
            # /T = seluruh pohon proses, /F = paksa
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        else:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def cancelled(task_id):
    """True bila konversi task ini diminta berhenti."""
    with tasks_lock:
        return bool(tasks.get(task_id, {}).get("cancel_requested"))


def check_environment():
    """Check availability of Java, FFmpeg, and FFDec jar."""
    java_ok = False
    ffmpeg_ok = False
    ffdec_ok = FFDEC_JAR.exists()

    try:
        res = subprocess.run(["java", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        java_ok = res.returncode == 0
    except Exception:
        java_ok = False

    try:
        res = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ffmpeg_ok = res.returncode == 0
    except Exception:
        ffmpeg_ok = False

    return {
        "java": java_ok,
        "ffmpeg": ffmpeg_ok,
        "ffdec": ffdec_ok,
        "ffdec_path": str(FFDEC_JAR)
    }

def add_log(task_id, message):
    """Append a log message to a task."""
    with tasks_lock:
        if task_id in tasks:
            timestamp = time.strftime("%H:%M:%S")
            log_line = f"[{timestamp}] {message}"
            tasks[task_id]["logs"].append(log_line)
            # Limit logs memory length if necessary
            if len(tasks[task_id]["logs"]) > 500:
                tasks[task_id]["logs"].pop(0)

def update_task_status(task_id, status=None, progress=None, error_message=None):
    """Update task status and progress."""
    with tasks_lock:
        task = tasks.get(task_id)
        if task is not None:
            # Jangan timpa status terminal yang sudah tercapai (mis. task yang
            # baru dibatalkan) kecuali memang diminta secara eksplisit.
            if task["status"] == "cancelled" and task.get("cancel_requested"):
                return
            if status is not None:
                task["status"] = status
            if progress is not None:
                task["progress"] = progress
            if error_message is not None:
                task["error_message"] = error_message
            if status in TERMINAL_STATUSES:
                task["completed_at"] = time.time()

def run_command_with_logging(task, cmd, cwd=None):
    """
    Execute command and stream output line by line into task logs.
    Menerima dict task (bukan task_id) agar bisa mengecek permintaan batal.
    Mengembalikan exit code; -1 berarti proses dihentikan karena dibatalkan.
    """
    task_id = task["id"]
    add_log(task_id, f"Running command: {' '.join(cmd)}")
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        cwd=cwd
    )
    register_process(task_id, process)

    # Watchdog: beberapa proses (mis. Java saat startup) bisa tidak mengeluarkan
    # output sama sekali, sehingga pemeriksaan per-baris di bawah tidak pernah
    # jalan. Thread ini memastikan permintaan batal tetap dieksekusi.
    stop_watchdog = threading.Event()

    def watch_cancel():
        while not stop_watchdog.wait(0.25):
            if cancelled(task_id):
                kill_process_tree(process)
                return

    watchdog = threading.Thread(target=watch_cancel, daemon=True)
    watchdog.start()

    try:
        for line in iter(process.stdout.readline, ''):
            if line:
                clean_line = line.strip()
                if clean_line:
                    add_log(task_id, clean_line)
            if cancelled(task_id):
                kill_process_tree(process)
                break
    finally:
        stop_watchdog.set()
        try:
            process.stdout.close()
        except Exception:
            pass

    return_code = process.wait()
    unregister_process(task_id)

    if cancelled(task_id):
        add_log(task_id, "Proses dihentikan karena pembatalan.")
        return -1

    return return_code

def convert_swf_to_mp4(task_id):
    """
    Background worker thread function for SWF -> MP4 conversion process.
    Steps:
    1. java -jar ffdec/ffdec.jar -format frame:avi -export frame avi_out input.swf
    2. java -jar ffdec/ffdec.jar -export sound sound_out input.swf
    3. ffmpeg -i avi_out/*.avi -i sound_out/-1.mp3 -c:v libx264 -c:a aac -shortest output.mp4 -y
    """
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        return

    input_swf = Path(task["upload_path"])
    # Simpan hasil ke output/<folder asal>/<nama file>.mp4
    output_mp4 = unique_path(build_output_path(task.get("rel_dir", ""), task["filename_stem"]))
    task["output_path"] = str(output_mp4)

    task_temp_dir = TEMP_DIR / task_id
    avi_out_dir = task_temp_dir / "avi_out"
    sound_out_dir = task_temp_dir / "sound_out"

    # Hanya True bila konversi benar-benar dihentikan di tengah jalan. Dipakai
    # untuk menghapus MP4 setengah jadi; tidak memakai flag cancel_requested
    # mentah karena task yang sudah selesai bisa saja baru saja diklik Batal.
    was_cancelled = False

    try:
        task_temp_dir.mkdir(parents=True, exist_ok=True)
        avi_out_dir.mkdir(parents=True, exist_ok=True)
        sound_out_dir.mkdir(parents=True, exist_ok=True)

        if not FFDEC_JAR.exists():
            raise FileNotFoundError(f"ffdec.jar tidak ditemukan di '{FFDEC_JAR}'. Harap unduh atau tempatkan ffdec.jar di folder ffdec/")

        # STEP 1: Export frames to AVI
        update_task_status(task_id, status="exporting_frames", progress=15)
        add_log(task_id, "=== TAHAP 1: Ekstraksi Frame SWF ke AVI ===")

        cmd_frames = [
            "java", "-jar", str(FFDEC_JAR),
            "-format", "frame:avi",
            "-export", "frame", str(avi_out_dir),
            str(input_swf)
        ]
        ret_frames = run_command_with_logging(task, cmd_frames)
        if cancelled(task_id):
            raise ConversionCancelled()
        if ret_frames != 0:
            add_log(task_id, f"Peringatan: FFDec frame export return code: {ret_frames}")

        # STEP 2: Export sound
        update_task_status(task_id, status="exporting_sound", progress=45)
        add_log(task_id, "=== TAHAP 2: Ekstraksi Suara/Audio SWF ===")

        cmd_sound = [
            "java", "-jar", str(FFDEC_JAR),
            "-export", "sound", str(sound_out_dir),
            str(input_swf)
        ]
        ret_sound = run_command_with_logging(task, cmd_sound)
        if cancelled(task_id):
            raise ConversionCancelled()
        if ret_sound != 0:
            add_log(task_id, f"Peringatan: FFDec sound export return code: {ret_sound}")

        # STEP 3: Encoding with FFmpeg
        update_task_status(task_id, status="encoding_mp4", progress=75)
        add_log(task_id, "=== TAHAP 3: Penggabungan & Encoding dengan FFmpeg ===")

        # Locate exported AVI files
        avi_files = sorted(glob.glob(os.path.join(str(avi_out_dir), "*.avi")))
        if not avi_files:
            # Check subdirectories if ffdec created subfolders
            avi_files = sorted(glob.glob(os.path.join(str(avi_out_dir), "**", "*.avi"), recursive=True))

        if not avi_files:
            raise FileNotFoundError("Tidak ada file frame AVI yang berhasil diekstrak dari SWF!")

        main_avi = avi_files[0]
        add_log(task_id, f"File AVI ditemukan: {main_avi}")

        # Locate exported sound file
        # User specified sound_out/-1.mp3 or general sound
        sound_files = sorted(glob.glob(os.path.join(str(sound_out_dir), "*.*")))
        sound_files = [f for f in sound_files if os.path.isfile(f) and f.lower().endswith(('.mp3', '.wav', '.flac', '.aac', '.ogg'))]

        sound_file = None
        if os.path.exists(os.path.join(str(sound_out_dir), "-1.mp3")):
            sound_file = os.path.join(str(sound_out_dir), "-1.mp3")
        elif sound_files:
            sound_file = sound_files[0]

        cmd_ffmpeg = ["ffmpeg", "-y", "-i", main_avi]
        if sound_file:
            add_log(task_id, f"File audio ditemukan: {sound_file}")
            cmd_ffmpeg.extend(["-i", sound_file, "-c:v", "libx264", "-c:a", "aac", "-shortest"])
        else:
            add_log(task_id, "Tidak ada audio ditemukan di SWF, mengencode video tanpa audio...")
            cmd_ffmpeg.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p"])

        cmd_ffmpeg.append(str(output_mp4))

        ret_ffmpeg = run_command_with_logging(task, cmd_ffmpeg)
        if cancelled(task_id):
            raise ConversionCancelled()
        if ret_ffmpeg != 0 or not output_mp4.exists():
            raise RuntimeError(f"FFmpeg gagal mengkonversi file (Exit Code: {ret_ffmpeg})")

        add_log(task_id, "=== KONVERSI BERHASIL DILAKUKAN! ===")
        update_task_status(task_id, status="completed", progress=100)

    except ConversionCancelled:
        was_cancelled = True
        update_task_status(task_id, status="cancelled")
    except Exception as e:
        error_msg = str(e)
        add_log(task_id, f"ERROR: {error_msg}")
        update_task_status(task_id, status="error", error_message=error_msg)
    finally:
        unregister_process(task_id)
        # Hapus hasil MP4 setengah jadi bila proses dibatalkan di tengah jalan
        if was_cancelled:
            try:
                if output_mp4.exists():
                    output_mp4.unlink()
            except Exception:
                pass
        # Cleanup temp folder and uploaded swf file
        try:
            if task_temp_dir.exists():
                shutil.rmtree(task_temp_dir, ignore_errors=True)
        except Exception:
            pass

        try:
            if input_swf.exists():
                os.remove(input_swf)
        except Exception:
            pass

        # Task yang dibatalkan langsung dibuang dari daftar (beserta file
        # sementaranya). Dilakukan setelah proses benar-benar berhenti dan
        # semua handle file ditutup, supaya aman di Windows.
        if was_cancelled:
            purge_task(task_id)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/env", methods=["GET"])
def get_env_status():
    return jsonify(check_environment())

@app.route("/api/upload", methods=["POST"])
def upload_files():
    if "files" not in request.files and "files[]" not in request.files:
        return jsonify({"error": "Tidak ada file yang diunggah"}), 400

    uploaded_files = request.files.getlist("files") or request.files.getlist("files[]")
    if not uploaded_files:
        return jsonify({"error": "File kosong"}), 400

    # Jalur relatif tiap file (dikirim browser saat upload folder), mis. "folder/sub/a.swf"
    rel_paths = request.form.getlist("paths") or request.form.getlist("paths[]")

    created_tasks = []

    for idx, file in enumerate(uploaded_files):
        if not file.filename:
            continue

        raw_name = rel_paths[idx] if idx < len(rel_paths) and rel_paths[idx] else file.filename

        # Pisahkan folder dari nama file; normalkan pemisah path Windows
        parts = str(raw_name).replace("\\", "/").split("/")
        parts = [p for p in parts if p and p not in (".", "..")]
        if not parts:
            continue
        # Abaikan segmen atas hasil drag folder (kadang berisi nama folder root)
        raw_filename = parts[-1]

        if not raw_filename.lower().endswith(".swf"):
            continue

        # Nama file asli dipertahankan (spasi & non-ASCII tetap utuh) untuk
        # tampilan UI dan penamaan output; hanya karakter ilegal yang dibersihkan.
        orig_filename = safe_folder_segment(raw_filename)
        stem = Path(orig_filename).stem
        if not stem:
            continue

        # Folder asal (tanpa nama file), dibersihkan tiap segmennya
        rel_dir = "/".join(s for s in (safe_folder_segment(p) for p in parts[:-1]) if s)

        task_id = uuid.uuid4().hex[:10]
        # File disimpan datar di uploads/ dengan nama ASCII (aman untuk CLI
        # Java/FFmpeg) dan awalan task_id agar unik; struktur folder asli cukup
        # disimpan di rel_dir lalu dipakai untuk menentukan folder output.
        disk_stem = secure_filename(stem) or "file"
        save_name = f"{task_id}_{disk_stem}.swf"
        save_path = UPLOADS_DIR / save_name

        file.save(str(save_path))
        file_size = save_path.stat().st_size

        display_name = f"{rel_dir}/{orig_filename}" if rel_dir else orig_filename

        task_data = {
            "id": task_id,
            "filename": display_name,
            "filename_stem": stem,
            "rel_dir": rel_dir,
            "upload_path": str(save_path),
            "output_path": "",
            "status": "pending",
            "progress": 0,
            "cancel_requested": False,
            "logs": [f"[{time.strftime('%H:%M:%S')}] File {display_name} berhasil diunggah."],
            "error_message": "",
            "file_size": file_size,
            "created_at": time.time(),
            "completed_at": None
        }

        with tasks_lock:
            tasks[task_id] = task_data

        created_tasks.append(task_data)

        # Submit background task to pool
        executor.submit(convert_swf_to_mp4, task_id)

    if not created_tasks:
        return jsonify({"error": "Tidak ada file .swf yang valid dalam pilihan tersebut"}), 400

    return jsonify({"success": True, "count": len(created_tasks), "tasks": created_tasks})

@app.route("/api/tasks", methods=["GET"])
def get_tasks():
    with tasks_lock:
        return jsonify(list(tasks.values()))

@app.route("/api/tasks/<task_id>", methods=["GET"])
def get_task_detail(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
        if not task:
            return jsonify({"error": "Task tidak ditemukan"}), 404
        return jsonify(task)

@app.route("/api/download/<task_id>", methods=["GET"])
def download_mp4(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    
    if not task:
        return "Task tidak ditemukan", 404
    
    out_path = Path(task["output_path"])
    if not out_path.exists():
        return "File MP4 belum tersedia atau gagal diproses", 404

    download_name = f"{task['filename_stem']}.mp4"
    return send_file(str(out_path), as_attachment=True, download_name=download_name)

@app.route("/api/preview/<task_id>", methods=["GET"])
def preview_mp4(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    
    if not task:
        return "Task tidak ditemukan", 404

    out_path = Path(task["output_path"])
    if not out_path.exists():
        return "File MP4 belum tersedia", 404

    return send_file(str(out_path), mimetype="video/mp4")

@app.route("/api/download-all", methods=["GET"])
def download_all_zip():
    """Create a zip archive of all completed MP4 files."""
    completed_files = []
    with tasks_lock:
        for t in tasks.values():
            if t["status"] == "completed" and t["output_path"] and Path(t["output_path"]).exists():
                out_path = Path(t["output_path"])
                # Pakai jalur nyata relatif terhadap OUTPUT_DIR supaya struktur
                # folder asal ikut terbawa dan nama tetap unik (mis. a.mp4, a_1.mp4).
                try:
                    arc = str(out_path.relative_to(OUTPUT_DIR))
                except ValueError:
                    arc = out_path.name
                completed_files.append((out_path, arc))

    if not completed_files:
        return jsonify({"error": "Belum ada file yang selesai dikonversi."}), 400

    zip_filename = TEMP_DIR / f"batch_converted_{int(time.time())}.zip"
    with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as z:
        for filepath, name in completed_files:
            z.write(filepath, arcname=name)

    return send_file(str(zip_filename), as_attachment=True, download_name="converted_mp4_batch.zip")

@app.route("/api/cancel/<task_id>", methods=["POST"])
def cancel_task(task_id):
    """Hentikan konversi yang sedang berjalan / masih mengantre."""
    result = request_cancel(task_id)
    if result is None:
        return jsonify({"error": "Task tidak ditemukan"}), 404
    return jsonify({"success": True, "status": result})

@app.route("/api/retry/<task_id>", methods=["POST"])
def retry_task(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
        if not task:
            return jsonify({"error": "Task tidak ditemukan"}), 404

        # Hanya boleh diulang dari status akhir. Proses yang masih berjalan harus
        # dibatalkan dulu lewat /api/cancel supaya tidak ada dua proses menulis
        # ke file output yang sama.
        if task["status"] not in TERMINAL_STATUSES:
            return jsonify({"error": "Hentikan proses terlebih dahulu sebelum mengulang"}), 409

        task["status"] = "pending"
        task["progress"] = 0
        task["cancel_requested"] = False
        task["error_message"] = ""
        task["completed_at"] = None
        task["logs"].append(f"[{time.strftime('%H:%M:%S')}] Mencoba ulang proses konversi...")

    executor.submit(convert_swf_to_mp4, task_id)
    return jsonify({"success": True})

@app.route("/api/clear", methods=["POST"])
def clear_tasks():
    global tasks
    with tasks_lock:
        to_delete_ids = [k for k, v in tasks.items() if v["status"] in TERMINAL_STATUSES]
        for task_id in to_delete_ids:
            task = tasks[task_id]
            # Clean up upload file if still exists
            upload_p = Path(task.get("upload_path", ""))
            if upload_p.exists():
                try:
                    os.remove(upload_p)
                except Exception:
                    pass
            # Clean up temp dir if still exists
            temp_p = TEMP_DIR / task_id
            if temp_p.exists():
                try:
                    shutil.rmtree(temp_p, ignore_errors=True)
                except Exception:
                    pass
            del tasks[task_id]
    return jsonify({"success": True})

@app.route("/api/stream", methods=["GET"])
def stream_updates():
    """Server-Sent Events (SSE) stream for real-time task updates."""
    def event_stream():
        while True:
            # Tutup stream bila session sudah tidak valid (mis. logout / kedaluwarsa)
            # supaya halaman lama tidak terus menerima update tanpa hak akses.
            if not session.get("authed"):
                break
            with tasks_lock:
                task_list = list(tasks.values())
            data = json.dumps(task_list)
            yield f"data: {data}\n\n"
            time.sleep(1.0)

    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")

if __name__ == "__main__":
    print("=" * 60)
    print("  SWF to MP4 Converter Server (Flask + FFDec + FFmpeg)")
    print("  Status Lingkungan:")
    env = check_environment()
    print(f"   - Java Installed:  {'[OK]' if env['java'] else '[MISSING]'}")
    print(f"   - FFmpeg Installed:{'[OK]' if env['ffmpeg'] else '[MISSING]'}")
    print(f"   - FFDec Jar Exist: {'[OK]' if env['ffdec'] else '[MISSING]'}")
    print("=" * 60)
    print("  Login diperlukan. Password diambil dari PYCONVERT_PASSWORD")
    print("  (default terpasang bila environment variable tidak diisi).")
    print("=" * 60)
    # debug dimatikan secara default karena aplikasi ini untuk dideploy:
    # mode debug membuka Werkzeug debugger (bisa jadi eksekusi kode dari jarak
    # jauh) dan auto-reloader. Nyalakan hanya saat pengembangan lokal lewat
    # PYCONVERT_DEBUG=1.
    debug_mode = os.environ.get("PYCONVERT_DEBUG") == "1"
    app.run(host="0.0.0.0", port=5000, debug=debug_mode, threaded=True)
