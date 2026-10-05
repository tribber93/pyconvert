import io
import app as appmod

PASSWORD = appmod.APP_PASSWORD
print("PASSWORD_LOADED", PASSWORD == "432187659")


def new_client():
    return appmod.app.test_client()


# 1. Tanpa login: halaman utama dialihkan ke /login
c = new_client()
r = c.get("/")
print("ROOT_NOAUTH", r.status_code, "->", r.headers.get("Location"))

# 2. Tanpa login: API harus 401 (bukan redirect HTML)
print("API_NOAUTH", c.get("/api/tasks").status_code)
print("STREAM_NOAUTH", c.get("/api/stream").status_code)

# 3. Halaman login sendiri harus bisa diakses
print("LOGIN_PAGE", c.get("/login").status_code)

# 4. Password salah -> tetap di halaman login, tidak ada session
r = c.post("/login", data={"password": "salah"})
print("LOGIN_WRONG", r.status_code, "| masih ditolak:", c.get("/api/tasks").status_code)

# 5. Password benar -> login sukses dan session berlaku
r = c.post("/login", data={"password": PASSWORD})
print("LOGIN_OK", r.status_code, "->", r.headers.get("Location"))
print("API_AFTER_LOGIN", c.get("/api/tasks").status_code)
print("ROOT_AFTER_LOGIN", c.get("/").status_code)

# 6. Semua route penting harus terbuka setelah login
for path in ["/files", "/uploads", "/temp", "/output", "/api/env", "/api/tasks"]:
    print("AUTH_ROUTE", path, c.get(path).status_code)

# 7. Open redirect harus dicegah: next ke situs luar diabaikan
c2 = new_client()
r = c2.post("/login?next=https://evil.example.com", data={"password": PASSWORD})
print("OPEN_REDIRECT", r.status_code, "->", r.headers.get("Location"))

# 8. next internal tetap dihormati
c3 = new_client()
r = c3.post("/login?next=/files", data={"password": PASSWORD})
print("NEXT_INTERNAL", r.status_code, "->", r.headers.get("Location"))

# 9. Logout menghapus session
print("LOGOUT", c.post("/logout").status_code)
print("API_AFTER_LOGOUT", c.get("/api/tasks").status_code)

# 10. Operasi tulis (upload) juga wajib login
c4 = new_client()
r = c4.post("/api/upload", data={"files[]": (io.BytesIO(b"FWS"), "a.swf")},
            content_type="multipart/form-data")
print("UPLOAD_NOAUTH", r.status_code)
