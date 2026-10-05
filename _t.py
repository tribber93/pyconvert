"""Uji cepat: autentikasi + alur konversi folder zenius.

Jalankan:  python _t.py
Skrip ini TIDAK menjalankan ffdec/ffmpeg sungguhan (executor di-stub),
jadi aman dan cepat.
"""
import io
import shutil
import tempfile
from pathlib import Path

import app as appmod

PASSWORD = appmod.APP_PASSWORD
print("PASSWORD_LOADED", PASSWORD == "432187659")

# --- Stub executor supaya konversi tidak benar-benar dijalankan -----------
submitted = []
appmod.executor.submit = lambda fn, *a, **kw: submitted.append((fn.__name__, a)) or None

# --- Login helper ---------------------------------------------------------
def logged_in_client():
    c = appmod.app.test_client()
    c.post("/login", data={"password": PASSWORD})
    return c


# =========================================================================
# BAGIAN 1: Autentikasi
# =========================================================================
c = appmod.app.test_client()
print("ROOT_NOAUTH", c.get("/").status_code, "->", c.get("/").headers.get("Location"))
print("API_NOAUTH", c.get("/api/tasks").status_code)
print("STREAM_NOAUTH", c.get("/api/stream").status_code)
print("LOGIN_PAGE", c.get("/login").status_code)
r = c.post("/login", data={"password": "salah"})
print("LOGIN_WRONG", r.status_code, "| masih ditolak:", c.get("/api/tasks").status_code)
r = c.post("/login", data={"password": PASSWORD})
print("LOGIN_OK", r.status_code, "->", r.headers.get("Location"))
print("API_AFTER_LOGIN", c.get("/api/tasks").status_code)
print("ROOT_AFTER_LOGIN", c.get("/").status_code)

for path in ["/files", "/uploads", "/temp", "/output", "/zenius", "/api/env", "/api/tasks"]:
    print("AUTH_ROUTE", path, c.get(path).status_code)

c2 = appmod.app.test_client()
r = c2.post("/login?next=https://evil.example.com", data={"password": PASSWORD})
print("OPEN_REDIRECT", r.status_code, "->", r.headers.get("Location"))
c3 = appmod.app.test_client()
r = c3.post("/login?next=/files", data={"password": PASSWORD})
print("NEXT_INTERNAL", r.status_code, "->", r.headers.get("Location"))
print("LOGOUT", c.post("/logout").status_code)
print("API_AFTER_LOGOUT", c.get("/api/tasks").status_code)
c4 = appmod.app.test_client()
r = c4.post("/api/upload", data={"files[]": (io.BytesIO(b"FWS"), "a.swf")},
            content_type="multipart/form-data")
print("UPLOAD_NOAUTH", r.status_code)
print("CONVERT_LOCAL_NOAUTH", c4.post("/api/convert-local", json={"items": ["a.swf"]}).status_code)

# =========================================================================
# BAGIAN 2: Folder zenius
# =========================================================================
print()
print("=== FOLDER ZENIUS ===")

# Buat contoh file .swf di dalam subfolder (nama berspasi & non-ASCII)
sample_rel = "Kursus IPA/bab1/a.swf"
sample = appmod.ZENIUS_DIR / "Kursus IPA" / "bab1" / "a.swf"
sample.parent.mkdir(parents=True, exist_ok=True)
sample.write_bytes(b"FWS\x0a")

notswf = appmod.ZENIUS_DIR / "catatan.txt"
notswf.write_text("hello")

cl = logged_in_client()

# (a) Halaman /zenius terbuka dan memuat file
r = cl.get("/zenius")
body = r.get_data(as_text=True)
print("ZENIUS_PAGE", r.status_code,
      "| file tampil:", "a.swf" in body,
      "| toolbar konversi:", "convertSelected()" in body,
      "| judul folder berspasi:", "Kursus" in body)

# Halaman root File Manager (/files) tetap memuat nav zenius
r = cl.get("/files")
print("FILES_ROOT", r.status_code, "| nav zenius:", "/zenius" in r.get_data(as_text=True))

# (b) convert-local membuat task dengan source zenius
r = cl.post("/api/convert-local", json={"items": [sample_rel]})
data = r.get_json()
print("CONVERT_LOCAL", r.status_code, "|", data)
if data.get("success"):
    t = data["tasks"][0]
    print("  source:", t["source"], "| rel_dir:", repr(t["rel_dir"]),
          "| stem:", repr(t["filename_stem"]), "| status:", t["status"])
    print("  multipart paths terkirim:", submitted[-1][1])

# (c) output mengikuti struktur folder asal
out = appmod.build_output_path("Kursus IPA/bab1", "a")
print("OUTPUT_PATH", out.relative_to(appmod.OUTPUT_DIR))

# (d) jalur berbahaya / file non-swf ditolak
for bad in ["../../etc/passwd", "/etc/passwd", "C:\\\\windows\\\\x.swf", "..\\\\..\\\\x.swf"]:
    r = cl.post("/api/convert-local", json={"items": [bad]})
    print("REJECT", repr(bad), r.status_code, r.get_json().get("error"))
r = cl.post("/api/convert-local", json={"items": ["catatan.txt"]})
print("REJECT non-swf", r.status_code, r.get_json().get("error"))
r = cl.post("/api/convert-local", json={"items": []})
print("REJECT kosong", r.status_code, r.get_json().get("error"))

# (e) antre ganda ditolak selagi masih pending
r = cl.post("/api/convert-local", json={"items": [sample_rel]})
print("DUPLICATE", r.status_code, r.get_json().get("error"))

# (f) membatalkan task zenius tidak menghapus file sumber
if not data.get("success"):
    print("LEWATI uji pembatalan: convert-local gagal")
    raise SystemExit(1)
task_id = data["tasks"][0]["id"]
r = cl.post(f"/api/cancel/{task_id}")
print("CANCEL_ZENIUS", r.status_code, r.get_json())
print("  file sumber masih ada:", sample.exists())
print("  task sudah keluar dari daftar:",
      all(t["id"] != task_id for t in cl.get("/api/tasks").get_json()))

# (g) purge_task tidak menyentuh file zenius
tasks_before = len(cl.get("/api/tasks").get_json())
appmod.tasks["__uji"] = {"id": "__uji", "source": "zenius", "upload_path": str(sample),
                         "status": "completed", "logs": []}
appmod.purge_task("__uji")
print("PURGE_KEEPS_SOURCE", sample.exists(), "| tasks:", tasks_before, "->",
      len(cl.get("/api/tasks").get_json()))

# Bersihkan sampel
sample.unlink(missing_ok=True)
notswf.unlink(missing_ok=True)
shutil.rmtree(appmod.ZENIUS_DIR / "Kursus IPA", ignore_errors=True)
print()
print("SELESAI")
