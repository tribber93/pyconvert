"""
Endpoint JSON untuk unggah, konversi lokal, pemantauan task, unduh, dan
pembatalan/pengulangan.
"""
import json
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path

from flask import (Response, jsonify, render_template, request, send_file,
                   session, stream_with_context)
from werkzeug.utils import secure_filename

from config import OUTPUT_DIR, TEMP_DIR, TERMINAL_STATUSES, UPLOADS_DIR, ZENIUS_DIR
from conversion import check_environment, convert_swf_to_mp4
from storage import executor, request_cancel, tasks, tasks_lock
from utils import safe_folder_segment


def _new_task_id():
    return uuid.uuid4().hex[:10]


def _log_prefix():
    return f"[{time.strftime('%H:%M:%S')}]"


def init_api(app):
    """Daftarkan seluruh rute JSON (API) ke app."""

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

            task_id = _new_task_id()
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
                "logs": [f"{_log_prefix()} File {display_name} berhasil diunggah."],
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

    @app.route("/api/convert-local", methods=["POST"])
    def convert_local_files():
        """
        Konversi file .swf yang sudah ada di folder zenius/ tanpa upload.
        File sumber dipertahankan kecuali konversi berhasil (lihat conversion.py).
        """
        data = request.json or {}
        items = data.get("items", [])
        if not items:
            return jsonify({"error": "Tidak ada file yang dipilih"}), 400

        zenius_root = ZENIUS_DIR.resolve()
        created_tasks = []
        errors = []

        for rel in items:
            rel = str(rel).replace("\\", "/").strip()
            # Tolak jalur absolut / berisi drive letter
            if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
                errors.append(f"Jalur tidak valid: {rel}")
                continue

            raw_segs = [p for p in rel.split("/") if p not in ("", ".")]
            # Tolak ".." secara eksplisit, jangan diam-diam dibuang
            if not raw_segs or any(p == ".." for p in raw_segs):
                errors.append(f"Jalur tidak valid: {rel}")
                continue
            segs = raw_segs

            target = (ZENIUS_DIR / Path(*segs)).resolve()
            # Pastikan hasil resolve tetap berada di dalam folder zenius
            if target != zenius_root and zenius_root not in target.parents:
                errors.append(f"Akses ditolak: {rel}")
                continue
            if not target.is_file():
                errors.append(f"Bukan sebuah file: {rel}")
                continue
            if target.suffix.lower() != ".swf":
                errors.append(f"Bukan file .swf: {rel}")
                continue

            # Jangan antre file yang sama dua kali selagi masih diproses
            with tasks_lock:
                already = any(
                    t.get("source") == "zenius"
                    and t.get("upload_path") == str(target)
                    and t["status"] not in TERMINAL_STATUSES
                    for t in tasks.values()
                )
            if already:
                errors.append(f"Sudah ada dalam antrean: {rel}")
                continue

            stem = safe_folder_segment(target.stem)
            rel_dir = "/".join(safe_folder_segment(p) for p in segs[:-1])

            task_id = _new_task_id()
            display_name = f"{rel_dir}/{target.name}" if rel_dir else target.name

            task_data = {
                "id": task_id,
                "filename": display_name,
                "filename_stem": stem,
                "rel_dir": rel_dir,
                "upload_path": str(target),
                "source": "zenius",
                "output_path": "",
                "status": "pending",
                "progress": 0,
                "cancel_requested": False,
                "logs": [f"{_log_prefix()} Konversi dari folder zenius: {display_name}"],
                "error_message": "",
                "file_size": target.stat().st_size,
                "created_at": time.time(),
                "completed_at": None,
            }

            with tasks_lock:
                tasks[task_id] = task_data

            created_tasks.append(task_data)
            executor.submit(convert_swf_to_mp4, task_id)

        if not created_tasks:
            return jsonify({"error": "; ".join(errors) or "Tidak ada file .swf yang valid"}), 400

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
            task["logs"].append(f"{_log_prefix()} Mencoba ulang proses konversi...")

        executor.submit(convert_swf_to_mp4, task_id)
        return jsonify({"success": True})

    @app.route("/api/clear", methods=["POST"])
    def clear_tasks():
        with tasks_lock:
            to_delete_ids = [k for k, v in tasks.items() if v["status"] in TERMINAL_STATUSES]
            for task_id in to_delete_ids:
                task = tasks[task_id]
                # Clean up upload file if still exists. File sumber di zenius/ milik
                # pengguna -> jangan pernah dihapus, termasuk saat daftar dibersihkan.
                if task.get("source") != "zenius":
                    upload_p = Path(task.get("upload_path", ""))
                    if upload_p.exists():
                        try:
                            os.remove(upload_p)
                        except Exception:
                            pass
                # Hapus folder sementara task bila masih ada
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
