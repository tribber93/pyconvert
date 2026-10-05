import os
import sys
import glob
import json
import time
import uuid
import shutil
import zipfile
import threading
import subprocess
from pathlib import Path
from queue import Queue
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, jsonify, send_file, Response, stream_with_context
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"
TEMP_DIR = BASE_DIR / "temp"
FFDEC_DIR = BASE_DIR / "ffdec"
FFDEC_JAR = FFDEC_DIR / "ffdec.jar"

for folder in [UPLOADS_DIR, OUTPUT_DIR, TEMP_DIR, FFDEC_DIR]:
    folder.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB max upload limit

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
executor = ThreadPoolExecutor(max_workers=2)

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
        if task_id in tasks:
            if status is not None:
                tasks[task_id]["status"] = status
            if progress is not None:
                tasks[task_id]["progress"] = progress
            if error_message is not None:
                tasks[task_id]["error_message"] = error_message
            if status in ["completed", "error"]:
                tasks[task_id]["completed_at"] = time.time()

def run_command_with_logging(cmd, task_id, cwd=None):
    """Execute command and stream output line by line into task logs."""
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

    for line in iter(process.stdout.readline, ''):
        if line:
            clean_line = line.strip()
            if clean_line:
                add_log(task_id, clean_line)

    process.stdout.close()
    return_code = process.wait()
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
    stem = input_swf.stem
    output_mp4 = OUTPUT_DIR / f"{task_id}_{secure_filename(task['filename_stem'])}.mp4"
    task["output_path"] = str(output_mp4)

    task_temp_dir = TEMP_DIR / task_id
    avi_out_dir = task_temp_dir / "avi_out"
    sound_out_dir = task_temp_dir / "sound_out"

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
        ret_frames = run_command_with_logging(cmd_frames, task_id)
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
        ret_sound = run_command_with_logging(cmd_sound, task_id)
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

        ret_ffmpeg = run_command_with_logging(cmd_ffmpeg, task_id)
        if ret_ffmpeg != 0 or not output_mp4.exists():
            raise RuntimeError(f"FFmpeg gagal mengkonversi file (Exit Code: {ret_ffmpeg})")

        add_log(task_id, "=== KONVERSI BERHASIL DILAKUKAN! ===")
        update_task_status(task_id, status="completed", progress=100)

    except Exception as e:
        error_msg = str(e)
        add_log(task_id, f"ERROR: {error_msg}")
        update_task_status(task_id, status="error", error_message=error_msg)
    finally:
        # Cleanup temp directory
        try:
            if task_temp_dir.exists():
                shutil.rmtree(task_temp_dir, ignore_errors=True)
        except Exception as e:
            add_log(task_id, f"Gagal menghapus temp directory: {e}")

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

    created_tasks = []

    for file in uploaded_files:
        if not file.filename:
            continue
        
        orig_filename = secure_filename(file.filename)
        if not orig_filename.lower().endswith(".swf"):
            # Allow fallback if secure_filename stripped it or original has .swf
            if not file.filename.lower().endswith(".swf"):
                continue
            orig_filename = file.filename

        task_id = uuid.uuid4().hex[:10]
        stem = Path(orig_filename).stem
        save_name = f"{task_id}_{orig_filename}"
        save_path = UPLOADS_DIR / save_name

        file.save(str(save_path))
        file_size = save_path.stat().st_size

        task_data = {
            "id": task_id,
            "filename": file.filename,
            "filename_stem": stem,
            "upload_path": str(save_path),
            "output_path": "",
            "status": "pending",
            "progress": 0,
            "logs": [f"[{time.strftime('%H:%M:%S')}] File {file.filename} berhasil diunggah."],
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
                completed_files.append((Path(t["output_path"]), f"{t['filename_stem']}.mp4"))

    if not completed_files:
        return jsonify({"error": "Belum ada file yang selesai dikonversi."}), 400

    zip_filename = TEMP_DIR / f"batch_converted_{int(time.time())}.zip"
    with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as z:
        for filepath, name in completed_files:
            z.write(filepath, arcname=name)

    return send_file(str(zip_filename), as_attachment=True, download_name="converted_mp4_batch.zip")

@app.route("/api/retry/<task_id>", methods=["POST"])
def retry_task(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
        if not task:
            return jsonify({"error": "Task tidak ditemukan"}), 404
        
        task["status"] = "pending"
        task["progress"] = 0
        task["logs"].append(f"[{time.strftime('%H:%M:%S')}] Mencoba ulang proses konversi...")
        task["error_message"] = ""

    executor.submit(convert_swf_to_mp4, task_id)
    return jsonify({"success": True})

@app.route("/api/clear", methods=["POST"])
def clear_tasks():
    global tasks
    with tasks_lock:
        # Keep pending/running tasks only
        running_ids = [k for k, v in tasks.items() if v["status"] not in ["completed", "error"]]
        tasks = {k: tasks[k] for k in running_ids}
    return jsonify({"success": True})

@app.route("/api/stream", methods=["GET"])
def stream_updates():
    """Server-Sent Events (SSE) stream for real-time task updates."""
    def event_stream():
        while True:
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
    app.run(host="0.0.0.0", port=5000, debug=True)
