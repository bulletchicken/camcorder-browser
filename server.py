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
from urllib.parse import parse_qs, unquote, urlparse

SD_ROOTS = [
    Path("/Volumes/Untitled/SD_VIDEO"),
    Path("/Volumes/Untitled/DCIM"),
]
CACHE_DIR = Path.home() / "Library" / "Caches" / "camcorder-browser"
THUMBS_DIR = CACHE_DIR / "thumbs"
DEFAULT_OUTPUT = Path.home() / "Movies" / "Camcorder"
INDEX_HTML = Path(__file__).parent / "index.html"

VIDEO_EXTS = {".MOD", ".MPG", ".MTS", ".M2TS", ".AVI", ".MP4", ".MOV", ".MPEG"}

CACHE_DIR.mkdir(parents=True, exist_ok=True)
THUMBS_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)

JOBS = {}
JOBS_LOCK = threading.Lock()


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
