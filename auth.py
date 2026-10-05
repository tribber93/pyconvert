"""
Autentikasi: gerbang login untuk seluruh halaman & API.

Catatan: rute di sini sengaja didaftarkan langsung ke app (bukan lewat
Blueprint) supaya nama endpoint-nya tetap 'login' dan 'logout'. Kedua nama itu
dipakai url_for() di templates/login.html dan templates/index.html, dan juga
dicek oleh gerbang require_login di bawah.
"""
import hmac
import time

from flask import jsonify, redirect, render_template, request, session, url_for

from config import APP_PASSWORD

# Endpoint yang boleh diakses tanpa login (halaman login & file statis).
PUBLIC_ENDPOINTS = {"login", "static"}


def _password_valid(candidate):
    """Bandingkan password dengan waktu konstan agar tidak bocor lewat timing."""
    return hmac.compare_digest(str(candidate), str(APP_PASSWORD))


def _safe_next_url(url):
    """Hanya izinkan redirect internal, cegah open redirect ke situs lain."""
    return bool(url) and url.startswith("/") and not url.startswith("//") and "\\" not in url


def init_auth(app):
    """Daftarkan gerbang login + rute login/logout ke app."""

    @app.before_request
    def require_login():
        """Gerbang utama: semua halaman & API wajib login dulu."""
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if request.path.startswith("/static/"):
            return None
        if session.get("authed"):
            return None
        # Endpoint API balas JSON (dipakai fetch), halaman biasa dialihkan ke login
        if request.path.startswith("/api/"):
            return jsonify({"error": "Silakan login terlebih dahulu"}), 401
        nxt = request.path
        if request.query_string:
            nxt += "?" + request.query_string.decode("utf-8", "ignore")
        return redirect(url_for("login", next=nxt))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("authed"):
            return redirect(url_for("index"))

        error = None
        next_url = request.values.get("next", "")

        if request.method == "POST":
            if _password_valid(request.form.get("password", "")):
                session.clear()
                session["authed"] = True
                session.permanent = True
                return redirect(next_url if _safe_next_url(next_url) else url_for("index"))
            error = "Password salah. Silakan coba lagi."
            # Perlambat upaya coba-coba password
            time.sleep(0.5)

        return render_template("login.html", error=error, next_url=next_url)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))
