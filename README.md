import json
import os
import re
import shutil
import ipaddress
import socket
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, abort, jsonify, request, send_file

app = Flask(__name__)

JOBS = Path(tempfile.gettempdir()) / "bilstudio_jobs"
JOBS.mkdir(parents=True, exist_ok=True)

ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "10"))
MAX_FILESIZE_MB = int(os.getenv("MAX_FILESIZE_MB", "4096"))
JOB_TTL = 15 * 60

SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)
HAS_FFMPEG = shutil.which("ffmpeg") is not None
_hits = defaultdict(deque)
_lock = threading.Lock()
ID_RE = re.compile(r"^[0-9a-f]{32}$")
QUALITIES = {"best", "2160", "1440", "1080", "720", "480", "360", "mp3"}


@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    r.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
    return r


def err(msg, code=400):
    return jsonify(error=msg), code


def client_ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "")).split(",")[0].strip()


def rate_limited():
    now = time.time()
    with _lock:
        q = _hits[client_ip()]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return True
        q.append(now)
        return False


def safe_url(url):
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        for info in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def friendly(e):
    m = str(e)
    if "Unsupported URL" in m:
        return "Bu link desteklenmiyor. Bir video sayfasının linkini yapıştır."
    if any(k in m for k in ("Private video", "Sign in", "login", "members-only", "age")):
        return "Bu video gizli, yaş sınırlı veya giriş gerektiriyor."
    if "not available" in m or "removed" in m or "unavailable" in m:
        return "Video artık yayında değil ya da bulunduğun ülkede erişilemiyor."
    if "HTTP Error 429" in m or "bot" in m.lower():
        return "Site sunucuyu geçici olarak engelledi. Biraz sonra tekrar dene."
    return "Video okunamadı. Linkin doğru ve herkese açık olduğundan emin ol."


def format_for(q):
    if q == "mp3":
        return "bestaudio/best"
    h = "" if q == "best" else f"[height<={q}]"
    if not HAS_FFMPEG:
        return f"b{h}/b"
    return f"bv*{h}+ba/b{h}/b"


def read_status(d):
    try:
        return json.loads((d / "status.json").read_text())
    except Exception:
        return {}


def write_status(d, **kw):
    s = read_status(d)
    s.update(kw)
    tmp = d / "status.tmp"
    tmp.write_text(json.dumps(s))
    tmp.replace(d / "status.json")


def cleanup():
    now = time.time()
    for p in JOBS.iterdir():
        try:
            if now - p.stat().st_mtime > JOB_TTL:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


def run_job(jid, url, q):
    d = JOBS / jid
    out = d / "out"
    SLOTS.acquire()
    try:
        out.mkdir(exist_ok=True)
        write_status(d, state="downloading", percent=0)
        st = {"finished": 0, "last": 0.0}

        def hook(h):
            n = len((h.get("info_dict") or {}).get("requested_formats") or [1])
            if h["status"] == "finished":
                st["finished"] += 1
                write_status(d, percent=round(min(st["finished"] / n, 1) * 100, 1))
            elif h["status"] == "downloading":
                now = time.time()
                if now - st["last"] < 0.5:
                    return
                st["last"] = now
                total = h.get("total_bytes") or h.get("total_bytes_estimate")
                frac = (h.get("downloaded_bytes", 0) / total) if total else 0
                pct = min((st["finished"] + frac) / n, 1) * 100
                write_status(d, state="downloading", percent=round(pct, 1), speed=h.get("speed"))

        def pp_hook(h):
            if h.get("status") == "started":
                write_status(d, state="processing")

        opts = {
            "format": format_for(q),
            "outtmpl": str(out / "%(title).100B.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "concurrent_fragment_downloads": 16,
            "http_chunk_size": 10 * 1024 * 1024,
            "retries": 10,
            "fragment_retries": 10,
            "progress_hooks": [hook],
            "postprocessor_hooks": [pp_hook],
        }
        if MAX_FILESIZE_MB:
            opts["max_filesize"] = MAX_FILESIZE_MB * 1024 * 1024
        if q == "mp3" and HAS_FFMPEG:
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}]
        elif HAS_FFMPEG:
            opts["format_sort"] = ["res", "fps", "hdr", "vcodec", "acodec"]
            opts["merge_output_format"] = "mp4/mkv"

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        files = [p for p in out.iterdir() if p.is_file()]
        if not files:
            write_status(d, state="error", error="Dosya oluşturulamadı. Video çok büyük olabilir, başka bir kalite dene.")
            return
        f = max(files, key=lambda p: p.stat().st_size)
        write_status(d, state="done", percent=100, name=f.name, size=f.stat().st_size)
    except Exception as e:
        write_status(d, state="error", error=friendly(e))
    finally:
        SLOTS.release()


@app.get("/")
def health():
    return jsonify(ok=True, ffmpeg=HAS_FFMPEG)


@app.post("/api/info")
def info():
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    if not safe_url(url):
        return err("Geçersiz link. http veya https ile başlayan bir video linki gir.")
    if rate_limited():
        return err("Çok fazla istek gönderdin. Bir dakika bekleyip tekrar dene.", 429)
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True,
                               "skip_download": True, "socket_timeout": 20}) as ydl:
            d = ydl.extract_info(url, download=False)
        heights = sorted({f["height"] for f in d.get("formats", [])
                          if f.get("height") and f.get("vcodec") not in (None, "none")}, reverse=True)
        return jsonify(title=d.get("title"), thumbnail=d.get("thumbnail"), duration=d.get("duration"),
                       uploader=d.get("uploader") or d.get("channel"), heights=heights)
    except Exception as e:
        return err(friendly(e), 422)


@app.post("/api/prepare")
def prepare():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    q = body.get("q") if body.get("q") in QUALITIES else "best"
    if not safe_url(url):
        return err("Geçersiz link.")
    if rate_limited():
        return err("Çok fazla istek gönderdin. Bir dakika bekleyip tekrar dene.", 429)
    cleanup()
    jid = uuid.uuid4().hex
    d = JOBS / jid
    d.mkdir()
    write_status(d, state="queued")
    threading.Thread(target=run_job, args=(jid, url, q), daemon=True).start()
    return jsonify(id=jid)


@app.get("/api/status/<jid>")
def status(jid):
    if not ID_RE.match(jid):
        abort(404)
    s = read_status(JOBS / jid)
    if not s:
        return err("Bu indirme bulunamadı ya da süresi doldu. Videoyu yeniden bul.", 404)
    return jsonify(s)


@app.get("/api/file/<jid>")
def file(jid):
    if not ID_RE.match(jid):
        abort(404)
    d = JOBS / jid
    s = read_status(d)
    if s.get("state") != "done":
        return err("Dosya hazır değil ya da süresi doldu. Videoyu yeniden indir.", 404)
    f = (d / "out" / s["name"]).resolve()
    if not f.is_file() or (d / "out").resolve() not in f.parents:
        return err("Dosya bulunamadı. Videoyu yeniden indir.", 404)
    return send_file(f, as_attachment=True, download_name=f.name)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), threaded=True)
