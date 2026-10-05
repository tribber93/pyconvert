"""
Konfigurasi aplikasi: lokasi folder, password, dan konstanta bersama.

Modul ini tidak boleh mengimpor modul aplikasi lain (paling dasar), supaya
tidak ada impor melingkar.
"""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"
TEMP_DIR = BASE_DIR / "temp"
ZENIUS_DIR = BASE_DIR / "zenius"
FFDEC_DIR = BASE_DIR / "ffdec"
FFDEC_JAR = FFDEC_DIR / "ffdec.jar"
INSTANCE_DIR = BASE_DIR / "instance"

TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

for folder in [UPLOADS_DIR, OUTPUT_DIR, TEMP_DIR, ZENIUS_DIR, FFDEC_DIR, INSTANCE_DIR]:
    folder.mkdir(parents=True, exist_ok=True)

# Batas ukuran upload (500 MB)
MAX_CONTENT_LENGTH = 500 * 1024 * 1024

# Password aplikasi. Untuk deploy, sebaiknya diatur lewat environment variable
# PYCONVERT_PASSWORD; nilai di bawah adalah default yang diminta.
APP_PASSWORD = os.environ.get("PYCONVERT_PASSWORD") or "432187659"

# Status khusus "dibatalkan". Hanya status ini (dan completed/error) yang
# menyimpan completed_at, supaya task yang dibatalkan tercatat selesai.
TERMINAL_STATUSES = {"completed", "error", "cancelled"}

# Batas kedalaman penelusuran folder zenius, untuk mencegah rekursi tak
# terkendali (mis. struktur folder yang sangat dalam / aneh).
ZENIUS_SCAN_MAX_DEPTH = 12


def load_secret_key():
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
