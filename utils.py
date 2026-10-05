"""
Helper umum: pembersihan nama file/folder dan penentuan jalur output.
"""
import re

from config import BASE_DIR, OUTPUT_DIR, UPLOADS_DIR, TEMP_DIR, ZENIUS_DIR

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


def resolve_allowed_path(folder_name, subpath):
    """
    Ubah (nama folder, jalur relatif) menjadi path absolut, tapi hanya bila
    hasilnya benar-benar berada di dalam salah satu folder yang diizinkan.
    Mengembalikan None bila folder tidak dikenal atau jalurnya keluar dari area
    yang diizinkan.
    """
    folder_map = {
        "uploads": UPLOADS_DIR,
        "temp": TEMP_DIR,
        "output": OUTPUT_DIR,
        "zenius": ZENIUS_DIR,
        BASE_DIR.name: BASE_DIR,
    }
    base = folder_map.get(folder_name)
    if not base:
        return None

    target = (base / subpath).resolve()
    # Security check: must be strictly inside allowed directories
    allowed_roots = [UPLOADS_DIR.resolve(), TEMP_DIR.resolve(), OUTPUT_DIR.resolve(), ZENIUS_DIR.resolve()]
    is_valid = any(str(target).startswith(str(root)) for root in allowed_roots)
    if not is_valid:
        return None
    return target
