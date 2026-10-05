"""
Entry point aplikasi SWF -> MP4 Converter.

File ini sengaja dibuat tipis: seluruh logika ada di modul-modul terpisah, dan
di sini hanya dirakit menjadi satu aplikasi Flask.

- config.py      : lokasi folder, password, konstanta
- utils.py       : helper nama file & jalur
- storage.py     : state task, lock, executor, pembatalan
- conversion.py  : proses ffdec/ffmpeg (worker konversi)
- filemanager.py : File Manager + hapus/kompres
- auth.py        : login/logout
- api.py         : endpoint JSON konversi & task

Jalankan dengan:  python app.py
"""
import os

from flask import Flask

from config import MAX_CONTENT_LENGTH, TEMPLATES_DIR, STATIC_DIR, load_secret_key

app = Flask(
    __name__,
    template_folder=str(TEMPLATES_DIR),
    static_folder=str(STATIC_DIR),
)
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

app.config.update(
    SECRET_KEY=load_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,   # cookie tidak bisa dibaca JavaScript
    SESSION_COOKIE_SAMESITE="Lax",  # mencegah cookie ikut pada POST lintas situs
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 7,  # 7 hari
)

# Urutan pendaftaran tidak masalah selama semuanya selesai sebelum request
# pertama; gerbang login dipasang oleh init_auth.
from auth import init_auth              # noqa: E402
from filemanager import init_filemanager  # noqa: E402
from api import init_api                # noqa: E402

init_auth(app)
init_filemanager(app)
init_api(app)


# ---------------------------------------------------------------------------
# Kompatibilitas: nama-nama lama tetap bisa diimpor dari `app`.
#
# _t.py (skrip uji) memakai app.APP_PASSWORD, app.tasks, app.executor,
# app.purge_task, app.ZENIUS_DIR, app.build_output_path, dan sejenisnya. Supaya
# skrip itu tidak perlu diubah, nama-nama tersebut di-ekspor ulang di sini.
# ---------------------------------------------------------------------------
from config import (BASE_DIR, FFDEC_DIR, FFDEC_JAR, INSTANCE_DIR,  # noqa: E402,F401
                    OUTPUT_DIR, TEMP_DIR, TERMINAL_STATUSES,
                    UPLOADS_DIR, ZENIUS_DIR, ZENIUS_SCAN_MAX_DEPTH,
                    APP_PASSWORD)
from utils import (build_output_path, resolve_allowed_path,  # noqa: E402,F401
                   safe_folder_segment, unique_path)
from storage import (ConversionCancelled, add_log, cancelled, executor,  # noqa: E402,F401
                     kill_process_tree, purge_task, register_process,
                     request_cancel, running_procs, tasks, tasks_lock,
                     unregister_process, update_task_status)
from conversion import (check_environment, convert_swf_to_mp4,  # noqa: E402,F401
                        run_command_with_logging)
from filemanager import scan_zenius_tree, serve_folder_files  # noqa: E402,F401


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
