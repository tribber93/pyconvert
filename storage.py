"""
Penyimpanan task konversi beserta mesin pembatalannya.

State-nya in-memory (tidak ada database): daftar task, lock-nya, thread pool
executor, dan registry proses yang sedang berjalan.
"""
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import TEMP_DIR, TERMINAL_STATUSES

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


class ConversionCancelled(Exception):
    """Dilempar di tengah proses ketika pengguna menekan Batal."""


def add_log(task_id, message):
    """Append a log message to a task (dengan timestamp, dibatasi 500 baris)."""
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


def cancelled(task_id):
    """True bila pengguna sudah meminta task ini dibatalkan."""
    with tasks_lock:
        return bool(tasks.get(task_id, {}).get("cancel_requested"))


def purge_task(task_id):
    """
    Hapus task dari daftar konversi beserta file sementaranya.
    Dipakai saat task dibatalkan, supaya task yang dibatalkan tidak menumpuk
    di daftar. Aman dipanggil berkali-kali.

    File sumber di zenius/ milik pengguna -> tidak pernah dihapus di sini,
    termasuk task yang dibatalkan (file tetap ada supaya bisa dicoba ulang).
    """
    with tasks_lock:
        task = tasks.pop(task_id, None)
    if not task:
        return
    if task.get("source") != "zenius":
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


def register_process(task_id, process):
    """Tautkan proses Popen aktif ke sebuah task (untuk pembatalan)."""
    with running_procs_lock:
        running_procs[task_id] = process


def unregister_process(task_id):
    with running_procs_lock:
        running_procs.pop(task_id, None)


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
            process.kill()
    except Exception:
        pass


def request_cancel(task_id):
    """
    Minta konversi dibatalkan, lalu buang task dari daftar.
    - Jika proses sedang berjalan: matikan proses beserta anak-anaknya
      (Java/FFmpeg) lewat taskkill. Task baru dihapus dari daftar setelah
      worker-nya benar-benar berhenti (lihat purge_task di conversion.py).
    - Jika task masih mengantre (belum dijalankan): langsung buang dari daftar.

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

    # Proses sedang jalan: worker akan membersihkan diri setelah berhenti.
    kill_process_tree(process)
    return "cancelling"
