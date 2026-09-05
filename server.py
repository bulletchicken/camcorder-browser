#!/usr/bin/env python3
"""Camcorder Browser — local web UI for browsing/converting camcorder SD card videos."""

import hashlib
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

SD_ROOTS = [
    Path("/Volumes/Untitled/SD_VIDEO"),
    Path("/Volumes/Untitled/DCIM"),
]
CACHE_DIR = Path.home() / "Library" / "Caches" / "camcorder-browser"
THUMBS_DIR = CACHE_DIR / "thumbs"
PREVIEWS_DIR = CACHE_DIR / "previews"
DEFAULT_OUTPUT = Path.home() / "Movies" / "Camcorder"
INDEX_HTML = Path(__file__).parent / "index.html"

VIDEO_EXTS = {".MOD", ".MPG", ".MTS", ".M2TS", ".AVI", ".MP4", ".MOV", ".MPEG"}

CACHE_DIR.mkdir(parents=True, exist_ok=True)
THUMBS_DIR.mkdir(parents=True, exist_ok=True)
PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)

JOBS = {}
JOBS_LOCK = threading.Lock()

PREVIEWS = {}  # hash -> {status, progress, error}
PREVIEWS_LOCK = threading.Lock()


def hash_path(p):
    return hashlib.md5(str(p).encode()).hexdigest()


def scan_videos():
    videos = []
    for root in SD_ROOTS:
        if not root.exists():
            continue
        for dirpath, _, files in os.walk(root):
            for f in files:
                if f.startswith("."):
                    continue
                p = Path(dirpath) / f
                if p.suffix.upper() in VIDEO_EXTS:
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    videos.append({
                        "path": str(p),
                        "name": p.stem,
                        "ext": p.suffix.upper().lstrip("."),
                        "folder": p.parent.name,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "date": datetime.fromtimestamp(st.st_mtime).isoformat(),
                    })
    videos.sort(key=lambda v: v["mtime"], reverse=True)
    return videos


def get_thumbnail(path):
    p = Path(path)
    if not p.exists():
        return None
    thumb = THUMBS_DIR / (hash_path(p) + ".jpg")
    if thumb.exists() and thumb.stat().st_size > 0:
        return thumb
    for seek in ("3", "1", "0"):
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", seek, "-i", str(p),
                 "-vframes", "1", "-vf", "scale=640:-2",
                 "-q:v", "3", str(thumb)],
                capture_output=True, timeout=45,
            )
        except Exception:
            continue
        if thumb.exists() and thumb.stat().st_size > 0:
            return thumb
    return None


def get_duration(path):
    p = Path(path)
    cache = THUMBS_DIR / (hash_path(p) + ".dur")
    if cache.exists():
        try:
            return float(cache.read_text().strip())
        except Exception:
            pass
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(p)],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(r.stdout)
        dur = float(data.get("format", {}).get("duration", 0))
        cache.write_text(str(dur))
        return dur
    except Exception:
        return 0.0


def preview_path_for(src):
    return PREVIEWS_DIR / (hash_path(src) + ".mp4")


def transcode_preview(src_path):
    """Encode a 720p H.264 MP4 preview of src_path. Blocks; updates PREVIEWS."""
    src = Path(src_path)
    h = hash_path(src)
    out = preview_path_for(src)
    tmp = out.with_suffix(".mp4.part")

    with PREVIEWS_LOCK:
        PREVIEWS[h] = {"status": "transcoding", "progress": 0, "error": ""}

    duration = get_duration(src_path) or 0
    cmd = [
        "ffmpeg", "-y", "-fflags", "+genpts", "-i", str(src),
        "-c:v", "h264_videotoolbox", "-b:v", "2M",
        "-vf", "scale=-2:720",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-f", "mp4",
        "-progress", "pipe:1", "-nostats",
        str(tmp),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_ms=") and duration:
            try:
                secs = int(line.split("=", 1)[1]) / 1_000_000
                pct = min(99, secs / duration * 100)
                with PREVIEWS_LOCK:
                    PREVIEWS[h]["progress"] = round(pct, 1)
            except Exception:
                pass
    proc.wait()
    if proc.returncode != 0:
        err = (proc.stderr.read() if proc.stderr else "")[-500:]
        try: tmp.unlink()
        except: pass
        with PREVIEWS_LOCK:
            PREVIEWS[h] = {"status": "error", "progress": 0, "error": err or "ffmpeg failed"}
        return
    tmp.rename(out)
    with PREVIEWS_LOCK:
        PREVIEWS[h] = {"status": "ready", "progress": 100, "error": ""}


def ensure_preview(src_path):
    """Return (status_dict, cached_path_or_None). Kicks off transcode if missing."""
    src = Path(src_path)
    h = hash_path(src)
    out = preview_path_for(src)
    if out.exists() and out.stat().st_size > 0:
        with PREVIEWS_LOCK:
            PREVIEWS[h] = {"status": "ready", "progress": 100, "error": ""}
        return {"status": "ready", "progress": 100}, out
    with PREVIEWS_LOCK:
        cur = PREVIEWS.get(h)
        if cur and cur["status"] == "transcoding":
            return cur, None
        if cur and cur["status"] == "error":
            # allow retry
            pass
        PREVIEWS[h] = {"status": "transcoding", "progress": 0, "error": ""}
    threading.Thread(target=transcode_preview, args=(src_path,), daemon=True).start()
    return {"status": "transcoding", "progress": 0}, None


def convert_video(input_path, output_dir, job_id, index):
    src = Path(input_path)
    parent_tag = src.parent.name  # e.g. PRG010
    stem = f"{parent_tag}_{src.stem}" if parent_tag and parent_tag != src.stem else src.stem
    out = Path(output_dir) / f"{stem}.mp4"
    i = 1
    while out.exists():
        out = Path(output_dir) / f"{stem}_{i}.mp4"
        i += 1
    cmd = [
        "ffmpeg", "-y", "-fflags", "+genpts", "-i", str(src),
        "-c:v", "h264_videotoolbox", "-b:v", "8M",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        str(out),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    duration = get_duration(input_path) or 0
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_ms="):
            try:
                us = int(line.split("=", 1)[1])
                secs = us / 1_000_000
                pct = min(100, (secs / duration * 100)) if duration else 0
                with JOBS_LOCK:
                    JOBS[job_id]["file_progress"] = round(pct, 1)
            except Exception:
                pass
    proc.wait()
    if proc.returncode != 0:
        err = proc.stderr.read()[-800:] if proc.stderr else "ffmpeg failed"
        raise RuntimeError(err)
    return out


def run_convert_job(job_id, paths, output_dir):
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["started"] = datetime.now().isoformat()
    try:
        os.makedirs(output_dir, exist_ok=True)
        for i, p in enumerate(paths):
            with JOBS_LOCK:
                if JOBS[job_id].get("cancel"):
                    JOBS[job_id]["status"] = "cancelled"
                    return
                JOBS[job_id]["current"] = Path(p).name
                JOBS[job_id]["current_index"] = i
                JOBS[job_id]["file_progress"] = 0
            try:
                out = convert_video(p, output_dir, job_id, i)
                with JOBS_LOCK:
                    JOBS[job_id]["completed"].append({"input": p, "output": str(out)})
            except Exception as e:
                with JOBS_LOCK:
                    JOBS[job_id]["failed"].append({"input": p, "error": str(e)})
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["file_progress"] = 100
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)


def rename_video(old_path, new_name):
    """Rename src file on disk; migrate cache entries to new hash. Returns new path str."""
    src = Path(old_path)
    if not src.exists():
        raise ValueError("Source file does not exist")
    name = (new_name or "").strip()
    if not name:
        raise ValueError("Name cannot be empty")
    if any(c in name for c in "/\\:\x00"):
        raise ValueError("Name cannot contain / \\ : or null characters")
    if name in (".", ".."):
        raise ValueError("Invalid name")
    if len(name) > 200:
        raise ValueError("Name too long (max 200 characters)")
    ext = src.suffix
    dst = src.with_name(name + ext)
    if dst == src:
        return str(src)
    if dst.exists():
        raise ValueError(f"A file named '{dst.name}' already exists in that folder")
    src.rename(dst)
    old_h = hash_path(src)
    new_h = hash_path(dst)
    for cache_dir, exts in [(THUMBS_DIR, [".jpg", ".dur"]), (PREVIEWS_DIR, [".mp4"])]:
        for e in exts:
            old_f = cache_dir / (old_h + e)
            if old_f.exists():
                try:
                    old_f.rename(cache_dir / (new_h + e))
                except OSError:
                    pass
    return str(dst)


def pick_folder():
    """Native macOS folder picker via osascript."""
    script = 'set p to POSIX path of (choose folder with prompt "Choose output folder")'
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, content_type):
        try:
            data = Path(path).read_bytes()
        except FileNotFoundError:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _range_file(self, path, content_type):
        try:
            size = os.path.getsize(path)
        except OSError:
            self.send_response(404); self.end_headers(); return
        rng = self.headers.get("Range", "")
        start, end = 0, size - 1
        partial = False
        if rng.startswith("bytes="):
            try:
                spec = rng[6:].split(",")[0]
                s, _, e = spec.partition("-")
                if s: start = int(s)
                if e: end = int(e)
                end = min(end, size - 1)
                if start > end or start < 0:
                    self.send_response(416); self.end_headers(); return
                partial = True
            except Exception:
                partial = False; start, end = 0, size - 1
        length = end - start + 1
        try:
            with open(path, "rb") as f:
                f.seek(start)
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk: break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    remaining -= len(chunk)
        except FileNotFoundError:
            self.send_response(404); self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path

        if path == "/":
            self._file(str(INDEX_HTML), "text/html; charset=utf-8")
        elif path == "/api/videos":
            self._json(200, {
                "videos": scan_videos(),
                "default_output": str(DEFAULT_OUTPUT),
            })
        elif path == "/api/thumb":
            p = unquote(q.get("path", [""])[0])
            tp = get_thumbnail(p)
            if tp:
                self._file(str(tp), "image/jpeg")
            else:
                self._file(str(THUMBS_DIR / "__missing.jpg"), "image/jpeg")
        elif path == "/api/duration":
            p = unquote(q.get("path", [""])[0])
            self._json(200, {"duration": get_duration(p)})
        elif path == "/api/preview_info":
            p = unquote(q.get("path", [""])[0])
            status, cached = ensure_preview(p)
            self._json(200, {
                "status": status["status"],
                "progress": status.get("progress", 0),
                "error": status.get("error", ""),
                "url": f"/api/preview?path={quote(p)}" if cached else None,
            })
        elif path == "/api/preview":
            p = unquote(q.get("path", [""])[0])
            src = Path(p)
            out = preview_path_for(src)
            if not (out.exists() and out.stat().st_size > 0):
                self.send_response(404); self.end_headers(); return
            self._range_file(str(out), "video/mp4")
        elif path == "/api/jobs":
            with JOBS_LOCK:
                self._json(200, {"jobs": list(JOBS.values())})
        elif path == "/api/pick_folder":
            folder = pick_folder()
            self._json(200, {"folder": folder})
        elif path == "/api/reveal":
            p = unquote(q.get("path", [""])[0])
            subprocess.Popen(["open", "-R", p])
            self._json(200, {"ok": True})
        elif path == "/api/open":
            p = unquote(q.get("path", [""])[0])
            subprocess.Popen(["open", p])
            self._json(200, {"ok": True})
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}

        if u.path == "/api/convert":
            paths = data.get("paths", [])
            output_dir = data.get("output_dir") or str(DEFAULT_OUTPUT)
            job_id = uuid.uuid4().hex[:8]
            with JOBS_LOCK:
                JOBS[job_id] = {
                    "id": job_id,
                    "status": "queued",
                    "total": len(paths),
                    "current_index": 0,
                    "current": "",
                    "completed": [],
                    "failed": [],
                    "output_dir": output_dir,
                    "file_progress": 0,
                    "cancel": False,
                }
            threading.Thread(target=run_convert_job,
                             args=(job_id, paths, output_dir), daemon=True).start()
            self._json(200, {"job_id": job_id})
        elif u.path == "/api/cancel":
            jid = data.get("job_id")
            with JOBS_LOCK:
                if jid in JOBS:
                    JOBS[jid]["cancel"] = True
            self._json(200, {"ok": True})
        elif u.path == "/api/rename":
            old = data.get("path", "")
            name = data.get("name", "")
            try:
                new_path = rename_video(old, name)
                p = Path(new_path)
                self._json(200, {
                    "ok": True,
                    "path": new_path,
                    "name": p.stem,
                    "folder": p.parent.name,
                })
            except Exception as e:
                self._json(400, {"ok": False, "error": str(e)})
        elif u.path == "/api/clear_jobs":
            with JOBS_LOCK:
                for jid in list(JOBS):
                    if JOBS[jid]["status"] in ("done", "error", "cancelled"):
                        JOBS.pop(jid)
            self._json(200, {"ok": True})
        else:
            self.send_response(404); self.end_headers()


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global SD_ROOTS
    if len(sys.argv) > 1:
        SD_ROOTS = [Path(a) for a in sys.argv[1:]]
    port = 8765
    url = f"http://127.0.0.1:{port}"
    server = ThreadingServer(("127.0.0.1", port), Handler)
    print(f"Camcorder Browser at {url}")
    print(f"Scanning: {', '.join(str(r) for r in SD_ROOTS)}")
    print(f"Output default: {DEFAULT_OUTPUT}")
    subprocess.Popen(["open", url])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
