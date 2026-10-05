import io
import pathlib
import app as appmod

client = appmod.app.test_client()

# Jangan benar-benar jalankan konversi; cukup catat submit-nya
submitted = []
appmod.executor.submit = lambda fn, tid: submitted.append(tid)

data = {
    "files[]": [
        (io.BytesIO(b"FWS\x0a\x00\x00\x00"), "a.swf"),
        (io.BytesIO(b"FWS\x0a\x00\x00\x00"), "b.swf"),
    ],
    "paths[]": ["Kursus IPA/bab1/a.swf", "Kursus IPA/bab1/sub/b.swf"],
}
r = client.post("/api/upload", data=data, content_type="multipart/form-data")
body = r.get_json()
print("UPLOAD", r.status_code, "count:", body.get("count"))
ids = [t["id"] for t in body["tasks"]]
print("IDS", ids)

# /api/tasks harus tetap bisa di-JSON-kan (tidak ada objek Popen di dict)
rt = client.get("/api/tasks")
print("TASKS", rt.status_code, "items:", len(rt.get_json()))

# 1. Batalkan task pertama (masih antre) -> harus LANGSUNG hilang dari daftar
rc = client.post(f"/api/cancel/{ids[0]}")
print("CANCEL", rc.status_code, rc.get_json())
rt = client.get("/api/tasks").get_json()
print("ITEMS_AFTER_CANCEL", len(rt), "remaining:", [t["id"] for t in rt])
print("CANCELLED_STILL_LISTED", any(t["id"] == ids[0] for t in rt))
print("DETAIL_AFTER_CANCEL", client.get(f"/api/tasks/{ids[0]}").status_code)

# 2. Task yang sudah dibatalkan tidak boleh diproses walau worker akhirnya dipanggil
appmod.convert_swf_to_mp4(ids[0])
print("ITEMS_AFTER_WORKER", len(client.get("/api/tasks").get_json()))

# 3. Retry task yang sudah dihapus -> 404
print("RETRY_DELETED", client.post(f"/api/retry/{ids[0]}").status_code)

# 4. Retry dari status berjalan harus ditolak (409)
print("RETRY_RUNNING", client.post(f"/api/retry/{ids[1]}").status_code)

# 5. Batal task yang tidak ada
print("CANCEL_404", client.post("/api/cancel/tidakada").status_code)

# 6. Output harus mengikuti nama folder asal
for rel_dir, stem in [("Kursus IPA/bab1", "a"), ("Kursus IPA/bab1/sub", "b")]:
    p = appmod.build_output_path(rel_dir, stem)
    print("OUT", repr(rel_dir), "->", p.relative_to(appmod.BASE_DIR))
print("TRAVERSAL ->", appmod.build_output_path("../../etc", "x").relative_to(appmod.BASE_DIR))
print("NON-ASCII ->", appmod.build_output_path("りんご/赤", "青").relative_to(appmod.BASE_DIR))
