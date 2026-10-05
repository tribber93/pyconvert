"""
File Manager: penelusuran isi folder, halaman daftar file, serta endpoint
hapus/kompres. Halaman HTML-nya memakai templates/file_manager.html.
"""
import os
import shutil
import time
import zipfile
from pathlib import Path

from flask import jsonify, render_template, request, send_from_directory

from config import (BASE_DIR, OUTPUT_DIR, TEMP_DIR, UPLOADS_DIR, ZENIUS_DIR,
                    ZENIUS_SCAN_MAX_DEPTH)
from utils import resolve_allowed_path

# Pemetaan ekstensi -> MIME type untuk pratinjau/streaming di browser.
MIMETYPES = {
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
    ".log": "text/plain",
}


def _classify(ext, is_dir):
    """Tentukan ikon & kategori tampilan berdasarkan ekstensi."""
    if is_dir:
        return "fa-folder text-amber", "folder"
    if ext in [".mp4", ".avi", ".mov", ".mkv"]:
        return "fa-file-video text-cyan", "video"
    if ext in [".mp3", ".wav", ".flac", ".ogg", ".aac"]:
        return "fa-file-audio text-emerald", "audio"
    if ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"]:
        return "fa-file-image text-rose", "image"
    if ext == ".swf":
        return "fa-bolt text-yellow", "flash"
    if ext in [".txt", ".log", ".json", ".xml"]:
        return "fa-file-code text-indigo", "code"
    return "fa-file text-gray", "file"


def _format_size(size_bytes):
    """Ukuran file dalam MB/KB, '-' untuk folder."""
    if size_bytes > 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    return f"{size_bytes / 1024:.1f} KB"


def _list_items(target_path, base_folder):
    """Susun daftar isi folder untuk ditampilkan di template."""
    items = []
    try:
        for item in sorted(target_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            # If viewing the root BASE_DIR, only include the known folders
            if target_path == BASE_DIR.resolve():
                if item.name not in ["uploads", "temp", "output", "zenius"]:
                    continue

            rel_item = item.relative_to(base_folder)
            is_dir = item.is_dir()
            size_bytes = 0 if is_dir else item.stat().st_size

            ext = item.suffix.lower()
            icon, category = _classify(ext, is_dir)

            items.append({
                "name": item.name,
                "is_dir": is_dir,
                "size": size_bytes,
                "ext": ext,
                "icon": icon,
                "category": category,
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item.stat().st_mtime)),
                "rel_path": str(rel_item).replace("\\", "/"),
                "size_str": "-" if is_dir else _format_size(size_bytes),
            })
    except Exception:
        pass
    return items


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
            mtype = MIMETYPES.get(target_path.suffix.lower())
            return send_from_directory(target_path.parent, target_path.name, mimetype=mtype)
        return send_from_directory(target_path.parent, target_path.name, as_attachment=True)

    if not target_path.is_dir():
        return "File atau folder tidak ditemukan", 404

    items = _list_items(target_path, base_folder)
    # URL tiap item dipakai tombol preview/unduh/hapus di template.
    for item in items:
        item["url"] = f"{url_prefix}/{item['rel_path']}"

    folder_name = base_folder.name

    # Build breadcrumbs
    crumbs = [{"name": folder_name, "link": url_prefix}]
    if subpath:
        parts = [p for p in subpath.split("/") if p]
        curr_link = url_prefix
        for part in parts:
            curr_link += f"/{part}"
            crumbs.append({"name": part, "link": curr_link})

    parent_link = url_prefix
    if subpath:
        parent_subpath = "/".join(subpath.rstrip("/").split("/")[:-1])
        parent_link = f"{url_prefix}/{parent_subpath}" if parent_subpath else url_prefix

    # Tombol "Konversi Terpilih" hanya muncul di dalam folder zenius, karena
    # folder itulah yang dipakai sebagai sumber SWF untuk konversi lokal.
    is_zenius = (base_folder.resolve() == ZENIUS_DIR.resolve())

    return render_template(
        "file_manager.html",
        folder_name=folder_name,
        subpath=subpath,
        items=items,
        crumbs=crumbs,
        parent_link=parent_link,
        is_zenius=is_zenius,
        span_cols=6 if is_zenius else 5,
        active_root='active' if folder_name == BASE_DIR.name else '',
        active_uploads='active' if folder_name == 'uploads' else '',
        active_temp='active' if folder_name == 'temp' else '',
        active_output='active' if folder_name == 'output' else '',
        active_zenius='active' if folder_name == 'zenius' else '',
    )


def scan_zenius_tree(directory, rel="", depth=0):
    """
    Telusuri folder zenius/ dan kembalikan struktur bersarang berisi HANYA
    file .swf (folder kosong pun tetap ditampilkan agar strukturnya jelas).
    Dipakai oleh modal pemilih file di halaman utama, sehingga pengguna tidak
    perlu membuka File Manager terpisah untuk memilih file konversi.
    """
    node = {"name": directory.name, "path": rel, "files": [], "dirs": []}
    if depth >= ZENIUS_SCAN_MAX_DEPTH:
        return node

    try:
        entries = sorted(
            os.scandir(directory),
            key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower())
        )
    except OSError:
        return node

    for entry in entries:
        # Lewati symlink: mencegah penelusuran keluar dari folder zenius
        if entry.is_symlink():
            continue
        child_rel = f"{rel}/{entry.name}" if rel else entry.name
        if entry.is_dir(follow_symlinks=False):
            node["dirs"].append(scan_zenius_tree(Path(entry.path), child_rel, depth + 1))
        elif entry.name.lower().endswith(".swf") and entry.is_file(follow_symlinks=False):
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            node["files"].append({"name": entry.name, "path": child_rel, "size": size})

    return node


def init_filemanager(app):
    """Daftarkan rute halaman File Manager dan endpoint hapus/kompres."""

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

    @app.route("/zenius", defaults={"subpath": ""})
    @app.route("/zenius/<path:subpath>")
    def serve_zenius(subpath):
        # Folder zenius: tempat menaruh file/folder .swf yang akan dikonversi
        return serve_folder_files(ZENIUS_DIR, subpath)

    @app.route("/api/zenius-scan", methods=["GET"])
    def zenius_scan():
        """Daftar isi folder zenius/ (khusus .swf) untuk modal pemilih di beranda."""
        return jsonify(scan_zenius_tree(ZENIUS_DIR))

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
