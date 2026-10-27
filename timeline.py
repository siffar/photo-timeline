#!/usr/bin/env python3
"""
Photo Timeline — one timeline for all your photos and videos, on all your drives.

    python timeline.py                     # open the app (pick drives in the browser)
    python timeline.py scan "E:\\" "D:\\Takeout"   # or scan from the command line
    python timeline.py organize --out "D:\\Photos Sorted"

Your original files are only ever READ. Nothing is moved, renamed or deleted.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

try:
    import core as C
except ImportError as e:
    if "PIL" in str(e):
        sys.exit("Pillow is missing. Run:  pip install -r requirements.txt")
    raise

import smart

WEB = C.APP_DIR / "web"


def scan_and_tag(job, lib, **kw):
    """A scan, followed by smart tags for any photos not looked at yet (Mac only)."""
    res = C.run_scan(job, lib, **kw)
    try:
        if smart.status()["installed"]:
            res["smart"] = smart.run_tagging(job, lib)
    except C.Cancelled:
        raise
    except Exception as e:  # never let tagging spoil a finished scan
        res["smart_error"] = str(e)
    return res


def enable_smart(job, lib):
    smart.set_off(lib, False)
    if not smart.status()["installed"]:
        smart.install(job)
    return {"smart": smart.run_tagging(job, lib)}


# ───────────────────────────── background jobs ─────────────────────────────
class Job(C.Progress):
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.state = {"running": False}
        self.seq = 0

    def update(self, **kw):
        with self.lock:
            self.state.update(kw)

    def snap(self):
        with self.lock:
            return dict(self.state)


JOB = Job()


def start_job(kind, label, fn, *args, **kw):
    with JOB.lock:
        if JOB.state.get("running"):
            return False
        JOB.seq += 1
        JOB.state = {"running": True, "kind": kind, "label": label, "phase": "starting", "done": 0,
                     "total": 0, "found": 0, "message": "", "started": time.time(), "id": JOB.seq}
    JOB.cancel.clear()

    def run():
        try:
            res = fn(JOB, *args, **kw)
            JOB.update(result=res, status="done")
        except C.Cancelled:
            JOB.update(status="cancelled", message="Stopped.")
        except Exception as e:
            traceback.print_exc()
            JOB.update(status="error", error=str(e))
        finally:
            JOB.update(running=False, finished=time.time())

    threading.Thread(target=run, daemon=True).start()
    return True


# ───────────────────────────── data access ─────────────────────────────
class App:
    lib: Path = C.APP_DIR / "library"
    port = 8765
    share_server = None
    zip_tokens: dict = {}


def q_all(sql, args=()):
    db = sqlite3.connect(str(App.lib / "timeline.db"), timeout=60)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def q_write(fn):
    db = sqlite3.connect(str(App.lib / "timeline.db"), timeout=60)
    try:
        out = fn(db)
        db.commit()
        return out
    finally:
        db.close()


VISIBLE = "dup_of IS NULL AND live_of IS NULL AND ignored IS NULL"
MAP_ID = "COALESCE(m.dup_of, m.live_of, m.id)"


def api_media():
    rows = q_all("SELECT id, taken, kind, width, height, source, camera, lat, lon, date_source, duration, "
                 f"path, fav, people, descr, place, admin1, country, tags, faces, substr(vtext, 1, 300) "
                 f"FROM media WHERE {VISIBLE} ORDER BY taken DESC, id DESC")
    live = {r[0] for r in q_all("SELECT live_of FROM media WHERE live_of IS NOT NULL")}
    sources, cams, places, items = {}, {}, {}, []
    for (i, t, k, w, h, src, cam, lat, lon, ds, dur, path, fav, people, descr, pl, a1, co, tg, fc, vt) in rows:
        si = sources.setdefault(src, len(sources))
        ci = cams.setdefault(cam, len(cams)) if cam else None
        pi = places.setdefault((pl, a1 or "", co or ""), len(places)) if pl else None
        txt = " ".join(x for x in (people, descr) if x) or None
        items.append([i, t, "v" if k == "video" else "p", w, h, si, ci, lat, lon, ds, 1 if i in live else 0,
                      round(dur, 1) if dur else None, os.path.basename(path), fav or 0, txt, pi, tg, fc or 0, vt])
    stats = dict(q_all("SELECT 'files', COUNT(*) FROM media WHERE ignored IS NULL UNION ALL "
                       "SELECT 'exact', COUNT(*) FROM media WHERE dup_kind='exact' UNION ALL "
                       "SELECT 'similar', COUNT(*) FROM media WHERE dup_kind='similar' UNION ALL "
                       "SELECT 'sources', COUNT(*) FROM sources"))
    return {"items": items, "sources": list(sources), "cameras": list(cams), "places": [list(p) for p in places],
            "stats": stats, "synonyms": smart.SYNONYMS}


def api_item(mid):
    cols = ["id", "path", "source", "ext", "kind", "size", "taken", "date_source", "width", "height",
            "camera", "lat", "lon", "duration", "error", "people", "descr", "fav", "place", "admin1", "country",
            "tags", "faces", "vtext"]
    r = q_all(f"SELECT {','.join(cols)} FROM media WHERE id=?", (mid,))
    if not r:
        return None
    d = dict(zip(cols, r[0]))
    d["copies"] = [dict(zip(["id", "path", "source", "size", "kind"], x)) for x in
                   q_all("SELECT id, path, source, size, dup_kind FROM media WHERE dup_of=?", (mid,))]
    lv = q_all("SELECT id FROM media WHERE live_of=?", (mid,))
    d["live"] = lv[0][0] if lv else None
    d["albums"] = [{"id": a, "name": n, "kind": k} for a, n, k in q_all(
        f"SELECT DISTINCT a.id, a.name, a.kind FROM albums a JOIN album_items ai ON ai.album_id=a.id "
        f"JOIN media m ON m.id=ai.media_id WHERE {MAP_ID}=? ORDER BY a.name", (mid,))]
    d["available"] = os.path.exists(d["path"])
    d["shot"] = shot_settings(d["path"], d["ext"]) if d["available"] and d["kind"] == "photo" else {}
    folder, home = os.path.dirname(d["path"]), str(Path.home())
    d["folder"] = "~" + folder[len(home):] if folder.startswith(home) else folder
    d["name"] = os.path.basename(d["path"])
    return d


def shot_settings(path, ext):
    """Aperture, shutter, ISO, focal length, lens — read from the file only when Details is opened."""
    out = {}
    try:
        if ext in C.RAW_EXT:
            if not C.exifread:
                return out
            with open(path, "rb") as f:
                t = C.exifread.process_file(f, details=False)
            g = lambda k: t.get(k)
            vals = {"f": g("EXIF FNumber"), "t": g("EXIF ExposureTime"), "iso": g("EXIF ISOSpeedRatings"),
                    "mm": g("EXIF FocalLength"), "lens": g("EXIF LensModel")}
            num = lambda v: float(C._f(v.values[0])) if v is not None and getattr(v, "values", None) else None
            f_, t_, mm = num(vals["f"]), num(vals["t"]), num(vals["mm"])
            iso = vals["iso"].values[0] if vals["iso"] is not None and vals["iso"].values else None
            lens = str(vals["lens"]) if vals["lens"] is not None else None
        else:
            from PIL import Image
            with Image.open(path) as im:
                ex = im.getexif()
                sub = ex.get_ifd(0x8769) or {}
            fl = lambda v: float(v) if v is not None else None
            f_, t_, mm = fl(sub.get(33437)), fl(sub.get(33434)), fl(sub.get(37386))
            iso = sub.get(34855)
            iso = iso[0] if isinstance(iso, (tuple, list)) else iso
            lens = sub.get(42036)
        if f_:
            out["aperture"] = f"ƒ/{f_:.1f}".replace(".0", "")
        if t_:
            out["shutter"] = f"1/{round(1 / t_)} s" if t_ < 1 else f"{t_:g} s"
        if iso:
            out["iso"] = f"ISO {iso}"
        if mm:
            out["focal"] = f"{mm:g} mm"
        if lens:
            out["lens"] = str(lens).strip("\x00 ")
    except Exception:
        pass
    return out


def api_duplicates():
    rows = q_all("SELECT d.dup_of, d.id, d.path, d.size, d.source, d.dup_kind, p.path, p.size, p.source "
                 "FROM media d JOIN media p ON p.id = d.dup_of ORDER BY d.dup_of")
    groups, order = {}, []
    exact_bytes = similar_bytes = 0
    for pid, did, dpath, dsize, dsrc, kind, ppath, psize, psrc in rows:
        if pid not in groups:
            groups[pid] = {"id": pid, "path": ppath, "size": psize, "source": psrc, "copies": []}
            order.append(pid)
        groups[pid]["copies"].append({"id": did, "path": dpath, "size": dsize, "source": dsrc, "kind": kind})
        if kind == "exact":
            exact_bytes += dsize or 0
        else:
            similar_bytes += dsize or 0
    return {"groups": [groups[p] for p in order[:400]], "total_groups": len(order),
            "exact_bytes": exact_bytes, "similar_bytes": similar_bytes}


def api_places():
    d = C.list_places()
    srcs = []
    for path, label, last in q_all("SELECT path, label, last_scan FROM sources ORDER BY path"):
        prefix = path.rstrip("\\/") + os.sep
        n = q_all("SELECT COUNT(*) FROM media WHERE ignored IS NULL AND substr(path,1,?)=?", (len(prefix), prefix))[0][0]
        srcs.append({"path": path, "label": label, "last_scan": last, "files": n, "connected": os.path.isdir(path)})
    d["sources"] = srcs
    desk = Path.home() / "Desktop"
    d["default_dest"] = str(desk if desk.is_dir() else Path.home())
    return d


# albums ----------------------------------------------------------------
def album_ids(aid):
    return [r[0] for r in q_all(
        f"SELECT DISTINCT {MAP_ID} AS pid FROM album_items ai JOIN media m ON m.id = ai.media_id "
        f"WHERE ai.album_id=? AND m.ignored IS NULL", (aid,))]


def api_albums():
    out = []
    for aid, name, kind, created, tok, meta in q_all("SELECT id, name, kind, created, share_token, meta FROM albums"):
        ids = album_ids(aid)
        cover = q_all(f"SELECT id, taken FROM media WHERE id IN ({','.join('?' * len(ids))}) "
                      "ORDER BY fav DESC, kind='photo' DESC, (lat IS NOT NULL) DESC, taken DESC LIMIT 1", ids) if ids else []
        span = q_all(f"SELECT MIN(taken), MAX(taken) FROM media WHERE id IN ({','.join('?' * len(ids))})", ids) if ids else [(None, None)]
        out.append({"id": aid, "name": name, "kind": kind, "created": created, "shared": bool(tok),
                    "count": len(ids), "cover": cover[0][0] if cover else None, "from": span[0][0], "to": span[0][1],
                    "meta": json.loads(meta) if meta else None})
    out.sort(key=lambda a: (a["kind"] != "user", -(a["created"] or 0)))
    return out


def _expand(db, ids):
    ids = [int(i) for i in ids]
    out = set(ids)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        q = ",".join("?" * len(part))
        out.update(r[0] for r in db.execute(f"SELECT id FROM media WHERE dup_of IN ({q}) OR live_of IN ({q})", part + part))
    return out


def album_add(aid, ids):
    def f(db):
        now = time.time()
        db.executemany("INSERT OR IGNORE INTO album_items VALUES(?,?,?)", [(aid, int(i), now) for i in ids])
    q_write(f)


def album_remove(aid, ids):
    def f(db):
        every = list(_expand(db, ids))
        for i in range(0, len(every), 500):
            part = every[i:i + 500]
            db.execute(f"DELETE FROM album_items WHERE album_id=? AND media_id IN ({','.join('?' * len(part))})",
                       [aid] + part)
    q_write(f)


# ───────────────────────────── Wi-Fi sharing ─────────────────────────────
def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def share_url(tok):
    return f"http://{lan_ip()}:{App.port + 1}/s/{tok}"


def ensure_share_server():
    if App.share_server:
        return True
    try:
        srv = QuietServer(("0.0.0.0", App.port + 1), ShareHandler)
    except OSError:
        return False
    srv.daemon_threads = True
    App.share_server = srv
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return True


_share_cache = {}


def shared_album(tok):
    hit = _share_cache.get(tok)
    if hit and time.time() - hit[0] < 20:
        return hit[1]
    r = q_all("SELECT id, name FROM albums WHERE share_token=?", (tok,))
    val = None
    if r:
        aid, name = r[0]
        ids = album_ids(aid)
        val = {"id": aid, "name": name, "ids": set(ids)}
    _share_cache[tok] = (time.time(), val)
    return val


# ───────────────────────────── HTTP plumbing ─────────────────────────────
class QuietServer(ThreadingHTTPServer):
    """Browsers drop connections all the time (e.g. thumbnails scrolled past) — don't print those."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, OSError)):
            return
        super().handle_error(request, client_address)


class Base(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj, separators=(",", ":"), default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_path(self, path, ctype, cache="max-age=86400", download=None):
        try:
            size = os.path.getsize(path)
        except OSError:
            return self.send_error(404)
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", cache)
        if download:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(download)}")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def send_zip(self, ids, name):
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(C.safe_name(name))}.zip")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        C.zip_stream(self.wfile, App.lib, ids)

    def send_media(self, what, mid, allowed=None):
        """what: thumb | view | live | file"""
        if what == "live":
            r = q_all("SELECT path, ext, kind, hash FROM media WHERE live_of=? LIMIT 1", (mid,))
        else:
            r = q_all("SELECT path, ext, kind, hash FROM media WHERE id=?", (mid,))
        if not r:
            return self.send_error(404)
        path, ext, kind, h = r[0]
        if what == "thumb":
            return self.send_path(C.thumb_file(App.lib, h), "image/jpeg", "max-age=31536000, immutable")
        if not os.path.exists(path):
            return self.send_error(410, "The drive with this file isn't connected")
        if what == "file":
            return self.send_path(path, "application/octet-stream", download=os.path.basename(path))
        if kind == "video":
            ctype = "video/mp4" if ext in (".mp4", ".mov", ".m4v") else (
                "video/webm" if ext == ".webm" else "application/octet-stream")
            return self.send_path(path, ctype)
        if ext in C.BROWSER_IMG and what == "view":
            ctype = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                     ".avif": "image/avif"}.get(ext, "image/jpeg")
            return self.send_path(path, ctype)
        try:
            prev = C.preview_file(App.lib, path, ext, h)
        except Exception:
            prev = None
        if prev:
            return self.send_path(prev, "image/jpeg", "max-age=31536000, immutable")
        return self.send_path(C.thumb_file(App.lib, h), "image/jpeg")

    def do_HEAD(self):
        self.do_GET()


class Handler(Base):
    """The app itself — only reachable from this computer."""

    def host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        return host in (f"localhost:{App.port}", f"127.0.0.1:{App.port}", f"[::1]:{App.port}")

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 50 << 20:
            raise ValueError("too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if not self.host_ok():
            return self.send_error(403)
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)
        try:
            if p in ("/", "/index.html"):
                return self.send_path(WEB / "index.html", "text/html; charset=utf-8", "no-store")
            if p == "/api/media":
                return self.send_json(api_media())
            if p == "/api/duplicates":
                return self.send_json(api_duplicates())
            if p == "/api/places":
                return self.send_json(api_places())
            if p == "/api/fs":
                return self.send_json(C.list_dir(qs.get("path", [""])[0]))
            if p == "/api/qr":
                import qr
                body = qr.svg(qs.get("text", [""])[0][:300]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if p == "/api/smart":
                return self.send_json(smart.status(App.lib))
            if p == "/api/job":
                return self.send_json(JOB.snap())
            if p == "/api/albums":
                return self.send_json(api_albums())
            m = re.fullmatch(r"/api/albums/(\d+)", p)
            if m:
                aid = int(m.group(1))
                r = q_all("SELECT id, name, kind, share_token, meta FROM albums WHERE id=?", (aid,))
                if not r:
                    return self.send_error(404)
                _, name, kind, tok, meta = r[0]
                return self.send_json({"id": aid, "name": name, "kind": kind, "ids": album_ids(aid),
                                       "meta": json.loads(meta) if meta else None,
                                       "share": share_url(tok) if tok and App.share_server else None})
            m = re.fullmatch(r"/api/item/(\d+)", p)
            if m:
                d = api_item(int(m.group(1)))
                return self.send_json(d) if d else self.send_error(404)
            m = re.fullmatch(r"/api/zip/(\w+)", p)
            if m:
                t = App.zip_tokens.pop(m.group(1), None)
                if not t or time.time() - t[2] > 600:
                    return self.send_error(404)
                return self.send_zip(t[0], t[1])
            m = re.fullmatch(r"/(thumb|view|live|file)/(\d+)", p)
            if m:
                return self.send_media(m.group(1), int(m.group(2)))
            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self):
        if not self.host_ok() or self.headers.get("X-Timeline") != "1":
            return self.send_error(403)
        p = urlparse(self.path).path
        try:
            b = self.body()
            return self.send_json(self.post(p, b))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except KeyError as e:
            self.send_json({"error": f"missing {e}"}, 400)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)

    def post(self, p, b):
        if p == "/api/sources":       # add drives/folders and scan
            adds = [C.parse_labeled(x) for x in b.get("add", []) if os.path.isdir(x)]
            ok = start_job("scan", "Scanning", scan_and_tag, App.lib, add=adds)
            return {"started": ok}
        if p == "/api/scan":
            return {"started": start_job("scan", "Checking for new photos", scan_and_tag, App.lib)}
        if p == "/api/sources/remove":
            # Works even while a scan is running: pause it, remove, then carry on scanning the rest.
            snap = JOB.snap()
            was_scan = snap.get("running") and snap.get("kind") == "scan"
            if snap.get("running"):
                JOB.cancel.set()
                for _ in range(600):
                    if not JOB.snap().get("running"):
                        break
                    time.sleep(0.1)
                else:
                    return {"error": "Still finishing the current task — try again in a few seconds."}
            C.remove_source(App.lib, b["path"])
            if was_scan and q_all("SELECT 1 FROM sources LIMIT 1"):
                start_job("scan", "Checking for new photos", scan_and_tag, App.lib)
            return {"ok": True, "resumed": bool(was_scan)}
        if p == "/api/smart/enable":
            return {"started": start_job("smart", "Smart tags", enable_smart, App.lib)}
        if p == "/api/smart/off":
            smart.set_off(App.lib, True)
            return {"ok": True}
        if p == "/api/job/cancel":
            JOB.cancel.set()
            return {"ok": True}
        if p == "/api/fav":
            ids, val = [int(i) for i in b["ids"]], 1 if b.get("fav") else 0
            q_write(lambda db: db.executemany("UPDATE media SET fav=? WHERE id=?", [(val, i) for i in ids]))
            return {"ok": True}
        if p == "/api/copy":
            dest = b["dest"]
            if b.get("subfolder"):
                dest = os.path.join(dest, C.safe_name(b["subfolder"]))
            return {"started": start_job("copy", "Copying", C.run_copy, App.lib, b["ids"], dest,
                                         b.get("date_names", True))}
        if p == "/api/zip":
            tok = secrets.token_urlsafe(12)
            App.zip_tokens[tok] = (b["ids"], b.get("name") or "Photos", time.time())
            return {"url": f"/api/zip/{tok}"}
        if p == "/api/mkdir":
            d = Path(b["path"]) / C.safe_name(b["name"])
            d.mkdir(parents=True, exist_ok=True)
            return {"path": str(d)}
        if p == "/api/reveal":
            target = b.get("path")
            if b.get("id"):
                r = q_all("SELECT path FROM media WHERE id=?", (int(b["id"]),))
                target = r[0][0] if r else None
            if target and os.path.exists(target):
                reveal(target)
                return {"ok": True}
            return {"ok": False}
        if p == "/api/albums":
            name = (b.get("name") or "").strip() or "New album"
            aid = q_write(lambda db: db.execute("INSERT INTO albums(name, kind, created) VALUES(?,?,?)",
                                                (name, "user", time.time())).lastrowid)
            album_add(aid, b.get("ids", []))
            return {"id": aid}
        m = re.fullmatch(r"/api/albums/(\d+)/(add|remove|rename|delete|share|export)", p)
        if m:
            aid, act = int(m.group(1)), m.group(2)
            if act == "add":
                album_add(aid, b["ids"])
            elif act == "remove":
                album_remove(aid, b["ids"])
            elif act == "rename":
                q_write(lambda db: db.execute("UPDATE albums SET name=?, renamed=1 WHERE id=?", (b["name"].strip() or "Album", aid)))
            elif act == "delete":
                def f(db):
                    db.execute("DELETE FROM album_items WHERE album_id=?", (aid,))
                    db.execute("DELETE FROM albums WHERE id=? AND kind='user'", (aid,))
                q_write(f)
            elif act == "share":
                if not b.get("on"):
                    q_write(lambda db: db.execute("UPDATE albums SET share_token=NULL WHERE id=?", (aid,)))
                    _share_cache.clear()
                    return {"url": None}
                if not ensure_share_server():
                    return {"error": f"Port {App.port + 1} is busy — close other apps using it and try again."}
                tok = q_all("SELECT share_token FROM albums WHERE id=?", (aid,))[0][0] or secrets.token_urlsafe(9)
                q_write(lambda db: db.execute("UPDATE albums SET share_token=? WHERE id=?", (tok, aid)))
                _share_cache.clear()
                return {"url": share_url(tok), "local": f"http://localhost:{App.port + 1}/s/{tok}"}
            elif act == "export":
                return {"started": start_job("export", "Making a shareable album", C.run_export, App.lib, aid,
                                             b["dest"], b.get("quality", "web"))}
            return {"ok": True}
        return {"error": "unknown"}


class ShareHandler(Base):
    """Read-only pages for albums you chose to share on your Wi-Fi."""

    def do_GET(self):
        try:
            p = urlparse(self.path).path
            m = re.fullmatch(r"/s/([\w-]+)(?:/(t|p|v|l|f|zip)(?:/(\d+))?)?/?", p)
            if not m:
                return self.send_html("<h1 style='font-family:sans-serif'>Nothing here.</h1>")
            tok, what, mid = m.group(1), m.group(2), m.group(3)
            alb = shared_album(tok)
            if not alb:
                return self.send_html("<h1 style='font-family:sans-serif'>This album isn't shared any more.</h1>")
            if what is None:
                db = C.open_db(App.lib)
                try:
                    items = C.media_rows(db, sorted(alb["ids"]), True)
                finally:
                    db.close()

                def url_for(i, kind, has_live):
                    base = f"/s/{tok}"
                    d = {"th": f"{base}/t/{i}", "src": f"{base}/{'v' if kind == 'video' else 'p'}/{i}",
                         "dl": f"{base}/f/{i}"}
                    if has_live:
                        d["live"] = f"{base}/l/{i}"
                    return d
                payload = {"title": alb["name"], "items": C.album_payload_items(items, App.lib, url_for),
                           "zip": f"/s/{tok}/zip", "created": None}
                return self.send_html(C.render_album_html(payload))
            if what == "zip":
                return self.send_zip(sorted(alb["ids"]), alb["name"])
            mid = int(mid or 0)
            if mid not in alb["ids"]:
                return self.send_error(404)
            return self.send_media({"t": "thumb", "p": "preview", "v": "view", "l": "live", "f": "file"}[what], mid)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def reveal(path):
    import subprocess
    if sys.platform.startswith("win"):
        if os.path.isdir(path):
            subprocess.Popen(["explorer", os.path.normpath(path)])
        else:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)])


# ───────────────────────────── commands ─────────────────────────────
def cmd_app(args):
    App.lib = Path(args.library).resolve()
    App.port = args.port
    url = f"http://localhost:{App.port}/"
    # Claim the port first: if another copy is already running, just open it instead.
    try:
        srv = QuietServer(("127.0.0.1", App.port), Handler)
    except OSError:
        print(f"Photo Timeline is already running — opening {url}")
        webbrowser.open(url)
        return
    srv.daemon_threads = True
    try:
        C.open_db(App.lib).close()
    except sqlite3.OperationalError as e:
        srv.server_close()
        sys.exit(f"\n  Can't open the library ({e}).\n  Another Photo Timeline window may still be open — close it "
                 f"(or press Ctrl+C in it) and start again.\n  Library folder: {App.lib}\n")
    # Re-share albums that were shared last time.
    if q_all("SELECT 1 FROM albums WHERE share_token IS NOT NULL LIMIT 1"):
        ensure_share_server()
    print(f"\n  Photo Timeline is running at {url}")
    print("  Keep this window open while you use it. Close it (or press Ctrl+C) to stop.\n")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    if not args.no_autoscan and q_all("SELECT 1 FROM sources LIMIT 1"):
        threading.Timer(2.0, lambda: start_job("scan", "Checking for new photos", scan_and_tag, App.lib)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


def parse_tz(s):
    if not s:
        return None
    m = re.fullmatch(r"([+-])?(\d{1,2})(?::?(\d{2}))?", s.strip())
    if not m:
        sys.exit("--tz must look like +05:30 or -04:00")
    mins = int(m.group(2)) * 60 + int(m.group(3) or 0)
    return -mins if m.group(1) == "-" else mins


_last = {}


def printer(st):
    ph = st.get("phase")
    if ph == "finding":
        if _last.get("found") == st.get("found"):
            return
        _last["found"] = st.get("found")
        print(f"\r  looking… {st.get('found', 0):,} found", end="", flush=True)
    elif ph == "processing" and st.get("total"):
        eta = st.get("eta")
        print(f"\r  {st.get('done', 0):,}/{st['total']:,}" + (f"  (~{eta / 60:.0f} min left)   " if eta else ""),
              end="", flush=True)
    elif ph == "tagging" and st.get("total"):
        print(f"\r  recognising what's in photos: {st.get('done', 0):,}/{st['total']:,}   ", end="", flush=True)
    elif ph in ("finishing", "copying", "organizing", "installing") and st.get("message"):
        print(f"\n  {st['message']}", end="", flush=True)
        st["message"] = ""


def cmd_scan(args):
    prog = C.Progress(printer)
    try:
        s = scan_and_tag(prog, args.library, add=args.folders, tz_min=parse_tz(args.tz), workers=args.workers)
    except KeyboardInterrupt:
        prog.cancel.set()
        sys.exit("\nStopped. Run again to continue where it left off.")
    print(f"\n\n  {s['files']:,} files indexed → {s['shown']:,} in your timeline")
    print(f"  {s['exact']:,} exact copies and {s['similar']:,} look-alike copies hidden, "
          f"{s['live']:,} Live Photo clips attached, {s['albums']:,} Google albums found")
    print(f"  {s['trips']:,} trips and {s['events']:,} events found, photos from {s['places']:,} places")
    for e, n in s["errors"]:
        print(f"  ! {n:,} × {e[:100]}")
    print("\nNext:  python timeline.py")


def cmd_organize(args):
    r = C.run_organize(C.Progress(), args.library, args.out, args.link, args.dry_run)
    for a, b in r["preview"]:
        print(f"  {a}\n     → {b}")
    print(f"{'would copy' if args.dry_run else 'copied'} {r['copied']:,} files into {r['dest']} "
          f"({r['skipped']:,} already there). Duplicates were left out.")


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--library", default=str(C.APP_DIR / "library"),
                        help="where the index & thumbnails are kept (default: ./library)")
    ap = argparse.ArgumentParser(description="One timeline for all your photos.", parents=[common])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-autoscan", action="store_true", help="don't check for new photos on start")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("scan", parents=[common], help="index folders/drives from the command line")
    s.add_argument("folders", nargs="*", help='folders or drives, optionally named: iPhone="D:/iPhone export"')
    s.add_argument("--workers", type=int)
    s.add_argument("--tz", help="time zone for Google Takeout / video timestamps, e.g. +05:30")
    s.set_defaults(fn=cmd_scan)
    o = sub.add_parser("organize", parents=[common], help="copy everything (minus duplicates) into YEAR/YEAR-MONTH folders")
    o.add_argument("--out", required=True)
    o.add_argument("--dry-run", action="store_true")
    o.add_argument("--link", action="store_true", help="hard-link instead of copy (same drive only)")
    o.set_defaults(fn=cmd_organize)
    args = ap.parse_args()
    (getattr(args, "fn", None) or cmd_app)(args)


if __name__ == "__main__":
    main()
