"""
Proses SWF -> MP4: cek lingkungan, jalankan perintah (ffdec/ffmpeg) sambil
menyiarkan log-nya ke task, dan worker utamanya.
"""
import glob
import os
import shutil
import subprocess
import threading
from pathlib import Path

from config import FFDEC_JAR, TEMP_DIR
from storage import (ConversionCancelled, add_log, cancelled, kill_process_tree,
                     purge_task, register_process, tasks, tasks_lock,
                     unregister_process, update_task_status)
from utils import build_output_path, unique_path


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


# ---------------------------------------------------------------------------
# Langkah-langkah konversi (dipisah supaya worker utamanya mudah dibaca)
# ---------------------------------------------------------------------------

def _find_avi_files(avi_out_dir):
    """Cari file AVI hasil ekspor; cek subfolder bila ffdec membuat struktur sendiri."""
    files = sorted(glob.glob(os.path.join(str(avi_out_dir), "*.avi")))
    if not files:
        files = sorted(glob.glob(os.path.join(str(avi_out_dir), "**", "*.avi"), recursive=True))
    return files


def _find_png_files(png_out_dir):
    """Cari rangkaian PNG hasil ekspor kondisi kedua."""
    files = sorted(glob.glob(os.path.join(str(png_out_dir), "*.png")))
    if not files:
        files = sorted(glob.glob(os.path.join(str(png_out_dir), "**", "*.png"), recursive=True))
    return files


def _export_frames(task, input_swf, avi_out_dir, png_out_dir):
    """
    TAHAP 1: ekspor frame, dengan dua kondisi.

    Kondisi pertama (AVI) kadang gagal, jadi disiapkan kondisi kedua (PNG):
    bila ekspor AVI error atau tidak menghasilkan file, ulangi dengan PNG.
    Mengembalikan daftar file AVI (kosong bila memakai kondisi kedua).
    """
    add_log(task["id"], "=== TAHAP 1: Ekstraksi Frame SWF (kondisi pertama: AVI) ===")

    cmd_frames = [
        "java", "-jar", str(FFDEC_JAR),
        "-format", "frame:avi",
        "-export", "frame", str(avi_out_dir),
        str(input_swf)
    ]
    ret_frames = run_command_with_logging(task, cmd_frames)
    if cancelled(task["id"]):
        raise ConversionCancelled()

    avi_files = _find_avi_files(avi_out_dir)

    if ret_frames != 0 or not avi_files:
        # Kondisi pertama gagal -> jalankan kondisi kedua
        reason = f"return code {ret_frames}" if ret_frames != 0 else "tidak ada file AVI yang dihasilkan"
        add_log(task["id"], f"Peringatan: export AVI gagal ({reason}). Beralih ke kondisi kedua (PNG).")

        add_log(task["id"], "=== TAHAP 1 (LANJUTAN): Ekstraksi Frame SWF ke PNG ===")
        cmd_png = [
            "java", "-jar", str(FFDEC_JAR),
            "-export", "frame", str(png_out_dir),
            str(input_swf)
        ]
        ret_png = run_command_with_logging(task, cmd_png)
        if cancelled(task["id"]):
            raise ConversionCancelled()
        if ret_png != 0:
            add_log(task["id"], f"Peringatan: FFDec export PNG return code: {ret_png}")

    # Sebagian file AVI bisa saja tetap terbuat meski return code bukan 0;
    # dalam kasus itu kita masih pakai hasil kondisi pertama.
    return avi_files


def _export_sound(task, input_swf, sound_out_dir):
    """TAHAP 2: ekspor audio SWF (kalau ada)."""
    update_task_status(task["id"], status="exporting_sound", progress=45)
    add_log(task["id"], "=== TAHAP 2: Ekstraksi Suara/Audio SWF ===")

    cmd_sound = [
        "java", "-jar", str(FFDEC_JAR),
        "-export", "sound", str(sound_out_dir),
        str(input_swf)
    ]
    ret_sound = run_command_with_logging(task, cmd_sound)
    if cancelled(task["id"]):
        raise ConversionCancelled()
    if ret_sound != 0:
        add_log(task["id"], f"Peringatan: FFDec sound export return code: {ret_sound}")

    # Cari file audio; -1.mp3 diprioritaskan (format umum dari ffdec)
    sound_files = sorted(glob.glob(os.path.join(str(sound_out_dir), "*.*")))
    sound_files = [f for f in sound_files
                   if os.path.isfile(f) and f.lower().endswith(('.mp3', '.wav', '.flac', '.aac', '.ogg'))]

    preferred = os.path.join(str(sound_out_dir), "-1.mp3")
    if os.path.exists(preferred):
        return preferred
    if sound_files:
        return sound_files[0]
    return None


def _build_video_input(avi_files, png_out_dir, task_temp_dir, task_id):
    """
    Tentukan argumen input video untuk ffmpeg.
    Prioritas: hasil kondisi pertama (AVI). Bila tidak ada, pakai rangkaian PNG
    dari kondisi kedua (urutan nama harus dijaga).
    """
    if avi_files:
        main_avi = avi_files[0]
        add_log(task_id, f"File AVI ditemukan: {main_avi}")
        return ["-i", main_avi]

    png_files = _find_png_files(png_out_dir)
    if not png_files:
        raise FileNotFoundError("Tidak ada file frame (AVI maupun PNG) yang berhasil diekstrak dari SWF!")

    add_log(task_id, f"File PNG ditemukan: {len(png_files)} frame (contoh: {png_files[0]})")

    # ffmpeg butuh pola berurutan; pakai %d.png bila namanya angka 1..N,
    # kalau tidak gunakan concat demuxer dengan daftar file yang sudah diurutkan.
    def numeric_index(path):
        stem = os.path.splitext(os.path.basename(path))[0]
        return int(stem) if stem.isdigit() else None

    numeric = [numeric_index(p) for p in png_files]
    if all(n is not None for n in numeric) and sorted(numeric) == list(range(1, len(numeric) + 1)):
        pattern = os.path.join(str(png_out_dir), "%d.png").replace("\\", "/")
        return ["-i", pattern]

    concat_file = task_temp_dir / "png_frames_concat.txt"
    with open(concat_file, "w", encoding="utf-8") as fh:
        for path in png_files:
            fh.write(f"file '{path.replace(chr(92), '/')}'\n")
    add_log(task_id, "Urutan nama PNG tidak berurutan; memakai concat demuxer.")
    return ["-f", "concat", "-safe", "0", "-i", str(concat_file)]


def _encode_mp4(task, video_input_args, sound_file, output_mp4):
    """TAHAP 3: gabungkan frame + audio, lalu encode ke MP4."""
    update_task_status(task["id"], status="encoding_mp4", progress=75)
    add_log(task["id"], "=== TAHAP 3: Penggabungan & Encoding dengan FFmpeg ===")

    cmd_ffmpeg = ["ffmpeg", "-y"] + video_input_args
    if sound_file:
        add_log(task["id"], f"File audio ditemukan: {sound_file}")
        cmd_ffmpeg.extend(["-i", sound_file, "-c:v", "libx264", "-c:a", "aac", "-shortest"])
    else:
        add_log(task["id"], "Tidak ada audio ditemukan di SWF, mengencode video tanpa audio...")
        cmd_ffmpeg.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p"])

    cmd_ffmpeg.append(str(output_mp4))

    ret_ffmpeg = run_command_with_logging(task, cmd_ffmpeg)
    if cancelled(task["id"]):
        raise ConversionCancelled()
    if ret_ffmpeg != 0 or not output_mp4.exists():
        raise RuntimeError(f"FFmpeg gagal mengkonversi file (Exit Code: {ret_ffmpeg})")


def convert_swf_to_mp4(task_id):
    """
    Background worker thread function for SWF -> MP4 conversion process.
    Steps:
    1. java -jar ffdec/ffdec.jar -format frame:avi -export frame avi_out input.swf
       (kondisi pertama; bila gagal, fallback ke:)
       java -jar ffdec/ffdec.jar -export frame frame_out input.swf
    2. java -jar ffdec/ffdec.jar -export sound sound_out input.swf
    3. ffmpeg -i avi_out/*.avi -i sound_out/-1.mp3 -c:v libx264 -c:a aac -shortest output.mp4 -y
       (atau -i frame_out/%d.png bila memakai kondisi kedua)
    """
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        return

    input_swf = Path(task["upload_path"])
    # File hasil upload (di uploads/) selalu dihapus setelah proses selesai supaya
    # tidak menumpuk. File sumber di zenius/ milik pengguna -> hanya dihapus bila
    # konversi berhasil; bila gagal file tetap ada agar bisa dicoba ulang.
    delete_input_always = task.get("source") != "zenius"
    # Simpan hasil ke output/<folder asal>/<nama file>.mp4
    output_mp4 = unique_path(build_output_path(task.get("rel_dir", ""), task["filename_stem"]))
    task["output_path"] = str(output_mp4)

    task_temp_dir = TEMP_DIR / task_id
    avi_out_dir = task_temp_dir / "avi_out"
    png_out_dir = task_temp_dir / "frame_out"
    sound_out_dir = task_temp_dir / "sound_out"

    # Hanya True bila konversi benar-benar dihentikan di tengah jalan. Dipakai
    # untuk menghapus MP4 setengah jadi; tidak memakai flag cancel_requested
    # mentah karena task yang sudah selesai bisa saja baru saja diklik Batal.
    was_cancelled = False
    # True hanya bila konversi benar-benar berhasil. Dipakai untuk memutuskan
    # apakah file sumber di zenius/ boleh dihapus.
    was_successful = False

    try:
        task_temp_dir.mkdir(parents=True, exist_ok=True)
        avi_out_dir.mkdir(parents=True, exist_ok=True)
        png_out_dir.mkdir(parents=True, exist_ok=True)
        sound_out_dir.mkdir(parents=True, exist_ok=True)

        if not FFDEC_JAR.exists():
            raise FileNotFoundError(f"ffdec.jar tidak ditemukan di '{FFDEC_JAR}'. Harap unduh atau tempatkan ffdec.jar di folder ffdec/")

        # TAHAP 1: frame (AVI, fallback PNG)
        update_task_status(task_id, status="exporting_frames", progress=15)
        avi_files = _export_frames(task, input_swf, avi_out_dir, png_out_dir)

        # TAHAP 2: audio
        sound_file = _export_sound(task, input_swf, sound_out_dir)

        # TAHAP 3: gabung & encode
        video_input_args = _build_video_input(avi_files, png_out_dir, task_temp_dir, task_id)
        _encode_mp4(task, video_input_args, sound_file, output_mp4)

        add_log(task_id, "=== KONVERSI BERHASIL DILAKUKAN! ===")
        update_task_status(task_id, status="completed", progress=100)
        was_successful = True

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
        # Bersihkan folder sementara (avi_out/frame_out/sound_out); file SWF
        # sumber ditangani terpisah di bawah.
        try:
            if task_temp_dir.exists():
                shutil.rmtree(task_temp_dir, ignore_errors=True)
        except Exception:
            pass

        # uploads/ selalu dibersihkan (berhasil maupun gagal) supaya tidak menumpuk.
        # zenius/ hanya dibersihkan bila konversi berhasil, supaya sumbernya masih
        # ada untuk dicoba ulang ketika gagal atau dibatalkan.
        if delete_input_always or was_successful:
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
