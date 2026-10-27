"""
Photo Timeline — indexing engine.

Reads photos & videos (never modifies them), works out when each was taken,
makes thumbnails, finds duplicates / Live Photos / Google albums, and keeps
everything in a small SQLite database inside the library folder.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import string
import subprocess
import sys
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = None

try:  # iPhone HEIC / HEIF
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pillow_heif = None
try:  # camera RAW (NEF, CR2, ARW, DNG …)
    import rawpy
except Exception:
    rawpy = None
try:  # EXIF from RAW files
    import exifread
except Exception:
    exifread = None
import logging
logging.getLogger("exifread").setLevel(logging.CRITICAL)  # it warns about every file without EXIF

APP_DIR = Path(__file__).resolve().parent


def _find_tool(name):
    p = shutil.which(name)
    if p:
        return p
    exe = name + (".exe" if sys.platform.startswith("win") else "")
    for d in (APP_DIR / "ffmpeg", APP_DIR / "ffmpeg" / "bin",
              Path(os.environ.get("LOCALAPPDATA", "~")).expanduser() / "Microsoft" / "WinGet" / "Links",
              Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
        if (d / exe).exists():
            return str(d / exe)
    return None


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")

PHOTO_EXT = {".jpg", ".jpeg", ".jpe", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
             ".heic", ".heif", ".avif"}
RAW_EXT = {".nef", ".nrw", ".cr2", ".cr3", ".crw", ".arw", ".srf", ".sr2", ".dng", ".orf",
           ".rw2", ".raf", ".pef", ".srw", ".x3f", ".3fr", ".iiq"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".mts", ".m2ts", ".webm",
             ".wmv", ".mpg", ".mpeg"}
ALL_EXT = PHOTO_EXT | RAW_EXT | VIDEO_EXT
BROWSER_IMG = {".jpg", ".jpeg", ".jpe", ".png", ".gif", ".webp", ".bmp", ".avif"}
WEB_VIDEO = {".mp4", ".m4v", ".mov", ".webm"}

# Folders that never hold your own photos (system, apps, caches). Matched by name, any depth.
SKIP_DIRS = {
    "$recycle.bin", "system volume information", "recovery", "perflogs", "msocache",
    "$windows.~bt", "$windows.~ws", "$sysreset", "$winreagent", "windows", "program files",
    "program files (x86)", "programdata", "appdata", "windowsapps",
    "@eadir", ".thumbnails", "thumbnails", ".trash", ".trashes", ".spotlight-v100", ".fseventsd",
    ".cache", "cache", "caches", "temp", "tmp", "node_modules", ".git", ".svn", "site-packages",
    "__pycache__", "venv", ".venv", "steamapps", "steam", "epic games", "riot games",
}
SKIP_SUFFIXES = (".app", ".framework", ".bundle", ".lrdata", ".cocatalogdb")
MIN_SIDE = 200          # smaller than this on both sides = icon / sticker, ignored
THUMB_H = 400
PREVIEW_MAX = 2048
DATE_RANK = {"exif": 0, "quicktime": 0, "takeout": 1, "filename": 2, "video-meta": 2,
             "exif-modified": 3, "file-time": 9}


class Cancelled(Exception):
    pass


# ───────────────────────────── dates ─────────────────────────────
DT_RE = re.compile(r"(\d{4})[:\-./](\d{2})[:\-./](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
FN_DT = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
    r"(?:[-_ .T]|\s?at\s?)?(?:([01]\d|2[0-3])[-_.:]?([0-5]\d)[-_.:]?([0-5]\d))?")
NOW_LIMIT = datetime.now() + timedelta(days=2)


def sane(d):
    return d if d and datetime(1971, 1, 1) <= d <= NOW_LIMIT else None


def parse_dt(s):
    if s is None:
        return None
    if isinstance(s, bytes):
        s = s.decode("ascii", "ignore")
    m = DT_RE.search(str(s))
    if not m:
        return None
    try:
        return sane(datetime(*map(int, m.groups())))
    except ValueError:
        return None


def epoch_to_local(ts, tz_min):
    try:
        if tz_min is None:
            return sane(datetime.fromtimestamp(ts))
        return sane(datetime(1970, 1, 1) + timedelta(seconds=ts, minutes=tz_min))
    except (OverflowError, OSError, ValueError):
        return None


def date_from_name(name):
    for m in FN_DT.finditer(name):
        y, mo, d, hh, mi, ss = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d), int(hh or 12), int(mi or 0), int(ss or 0))
        except ValueError:
            continue
        if sane(dt):
            return dt
    return None


# ───────────────────── Google Takeout sidecars ─────────────────────
COUNTER_RE = re.compile(r"^(.*?)(\(\d+\))(\.[^.]*)?$")
EDIT_SUFFIXES = ("-edited", "-bearbeitet", "-modifié", "-modificato", "-editado", "-bewerkt",
                 "-edytowane", "-redigeret", "-muokattu")
ALBUM_META = {"metadata.json", "métadonnées.json", "metadaten.json", "metadatos.json",
              "metadati.json", "metadata(1).json"}


@lru_cache(maxsize=4096)
def json_index(dirpath):
    out = []
    try:
        for fn in os.listdir(dirpath):
            if fn.lower().endswith(".json"):
                m = re.match(r"^(.*?)(\(\d+\))?$", fn[:-5])
                out.append((m.group(1).lower(), m.group(2) or "", fn))
    except OSError:
        pass
    return tuple(out)


def find_sidecar(path):
    """IMG_1.jpg → IMG_1.jpg.json / .supplemental-metadata.json / truncated / (1) forms.
    Works the same for videos (VID_1.mp4 → VID_1.mp4.json …)."""
    d, name = os.path.split(path)
    idx = json_index(d)
    if not idx:
        return None
    counter = ""
    m = COUNTER_RE.match(name)
    if m:
        name, counter = m.group(1) + (m.group(3) or ""), m.group(2)
    stem, ext = os.path.splitext(name)
    for suf in EDIT_SUFFIXES:
        if stem.lower().endswith(suf):
            stem = stem[: -len(suf)]
            name = stem + ext
            break
    lname, lstem = name.lower(), stem.lower()
    best, best_len = None, -1
    for core, cnt, fn in idx:
        if cnt != counter or fn.lower() in ALBUM_META:
            continue
        ok = (core == lname or core == lstem or core.startswith(lname + ".")
              or (len(core) >= 30 and lname.startswith(core)))
        if ok and len(core) > best_len:
            best, best_len = fn, len(core)
    return os.path.join(d, best) if best else None


def read_sidecar(p):
    """→ (timestamp, (lat, lon) | None, extras{people, descr, gfav})"""
    try:
        with open(p, encoding="utf-8") as f:
            j = json.load(f)
    except Exception:
        return None, None, {}
    if not isinstance(j, dict):
        return None, None, {}
    ts = None
    v = j.get("photoTakenTime")
    if isinstance(v, dict) and v.get("timestamp"):
        try:
            ts = int(v["timestamp"])
        except (TypeError, ValueError):
            ts = None
    gps = None
    for key in ("geoData", "geoDataExif"):
        g = j.get(key) or {}
        try:
            lat, lon = float(g.get("latitude") or 0), float(g.get("longitude") or 0)
        except (TypeError, ValueError):
            continue
        if lat and lon:
            gps = (lat, lon)
            break
    people = ", ".join(p["name"] for p in (j.get("people") or [])
                       if isinstance(p, dict) and p.get("name"))
    extras = {"people": people or None,
              "descr": (str(j.get("description") or "").strip() or None),
              "gfav": 1 if j.get("favorited") else 0}
    return ts, gps, extras


# ───────────────────────────── EXIF ─────────────────────────────
def _f(x):
    try:
        return float(x)
    except Exception:
        try:
            return x.num / x.den
        except Exception:
            return float(x[0]) / float(x[1])


def _ref(x):
    x = getattr(x, "printable", x)
    if isinstance(x, bytes):
        x = x.decode("ascii", "ignore")
    return str(x or "").strip().upper()


def gps_from(lat_ref, lat, lon_ref, lon):
    try:
        if not lat or not lon:
            return None
        la = _f(lat[0]) + _f(lat[1]) / 60 + _f(lat[2]) / 3600
        lo = _f(lon[0]) + _f(lon[1]) / 60 + _f(lon[2]) / 3600
    except Exception:
        return None
    if math.isnan(la) or math.isnan(lo) or abs(la) > 90 or abs(lo) > 180:
        return None
    if abs(la) < 1e-6 and abs(lo) < 1e-6:
        return None
    if _ref(lat_ref).startswith("S"):
        la = -la
    if _ref(lon_ref).startswith("W"):
        lo = -lo
    return (round(la, 6), round(lo, 6))


def exif_pillow(img):
    out = {}
    try:
        ex = img.getexif()
    except Exception:
        return out
    if not ex:
        return out
    try:
        sub = ex.get_ifd(0x8769) or {}
    except Exception:
        sub = {}
    out["dto"] = parse_dt(sub.get(36867) or ex.get(36867))
    out["dtd"] = parse_dt(sub.get(36868))
    out["dt"] = parse_dt(ex.get(306))
    out["make"], out["model"] = ex.get(271), ex.get(272)
    try:
        g = ex.get_ifd(0x8825) or {}
        out["gps"] = gps_from(g.get(1), g.get(2), g.get(3), g.get(4))
    except Exception:
        pass
    return out


def exif_exifread(path):
    out = {}
    if exifread is None:
        return out
    try:
        with open(path, "rb") as f:
            t = exifread.process_file(f, details=False)
    except Exception:
        return out
    g = lambda k: str(t[k]) if k in t else None
    out["dto"] = parse_dt(g("EXIF DateTimeOriginal"))
    out["dtd"] = parse_dt(g("EXIF DateTimeDigitized"))
    out["dt"] = parse_dt(g("Image DateTime"))
    out["make"], out["model"] = g("Image Make"), g("Image Model")
    la, lo = t.get("GPS GPSLatitude"), t.get("GPS GPSLongitude")
    out["gps"] = gps_from(t.get("GPS GPSLatitudeRef"), la.values if la else None,
                          t.get("GPS GPSLongitudeRef"), lo.values if lo else None)
    return out


def camera_name(make, model):
    def clean(s):
        if isinstance(s, bytes):
            s = s.decode("ascii", "ignore")
        return str(s or "").replace("\x00", "").strip()
    make, model = clean(make), clean(model)
    if not model:
        return make or None
    first = make.split()[0].lower() if make else ""
    if not first or model.lower().startswith(first):
        return model
    make = re.sub(r"(?i) corporation| co\.,? ?ltd\.?| company", "", make)
    return f"{make} {model}"


# ───────────────────────────── video ─────────────────────────────
def _run(cmd, timeout):
    kw = {}
    if sys.platform.startswith("win"):
        kw["creationflags"] = 0x08000000  # no console window flashing
    return subprocess.run(cmd, capture_output=True, timeout=timeout, **kw)


def probe_video(path):
    if not FFPROBE:
        return {}
    try:
        j = json.loads(_run([FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format",
                             "-show_streams", path], 30).stdout or b"{}")
    except Exception:
        return {}
    fmt = j.get("format") or {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    out = {"qt": parse_dt(tags.get("com.apple.quicktime.creationdate"))}  # local wall clock
    ct = tags.get("creation_time")
    if ct:
        try:
            out["utc"] = datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    loc = tags.get("com.apple.quicktime.location.iso6709") or tags.get("location")
    if loc:
        m = re.match(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", loc)
        if m and (float(m.group(1)), float(m.group(2))) != (0.0, 0.0):
            out["gps"] = (float(m.group(1)), float(m.group(2)))
    try:
        out["duration"] = float(fmt.get("duration"))
    except (TypeError, ValueError):
        pass
    out["make"] = tags.get("com.apple.quicktime.make")
    out["model"] = tags.get("com.apple.quicktime.model")
    v = next((s for s in j.get("streams") or [] if s.get("codec_type") == "video"), {})
    out["codec"] = v.get("codec_name")
    return out


def video_thumb(path, out, duration):
    if not FFMPEG:
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    for ss in ("1", "0"):
        if ss == "1" and duration is not None and duration < 1.5:
            continue
        try:
            _run([FFMPEG, "-v", "error", "-y", "-ss", ss, "-i", path, "-frames:v", "1",
                  "-vf", f"scale=-2:{THUMB_H}", "-q:v", "4", str(out)], 90)
        except Exception:
            continue
        if out.exists() and out.stat().st_size > 0:
            return True
    return False


def web_video(src, dest):
    """Make a small H.264 MP4 that plays everywhere (phones, any browser)."""
    if not FFMPEG:
        return False
    try:
        _run([FFMPEG, "-v", "error", "-y", "-i", src, "-vf", "scale='min(1280,iw)':-2",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dest)], 3600)
    except Exception:
        return False
    return Path(dest).exists() and Path(dest).stat().st_size > 0


# ───────────────────────────── images ─────────────────────────────
def load_image(path, ext):
    if ext in RAW_EXT:
        if rawpy is None:
            return None
        with rawpy.imread(path) as raw:
            flip = raw.sizes.flip
            try:
                th = raw.extract_thumb()
                if th.format == rawpy.ThumbFormat.JPEG:
                    im = Image.open(io.BytesIO(th.data))
                    im.load()
                else:
                    im = Image.fromarray(th.data)
                if im.getexif().get(0x0112, 1) == 1:
                    if flip == 3:
                        im = im.rotate(180, expand=True)
                    elif flip == 5:
                        im = im.rotate(90, expand=True)
                    elif flip == 6:
                        im = im.rotate(-90, expand=True)
                return im
            except Exception:
                return Image.fromarray(raw.postprocess(half_size=True, use_camera_wb=True))
    return Image.open(path)


def oriented_size(im):
    w, h = im.size
    try:
        if im.getexif().get(0x0112, 1) in (5, 6, 7, 8):
            w, h = h, w
    except Exception:
        pass
    return w, h


def render_jpeg(im, out, box, quality=82):
    size = oriented_size(im)
    if im.format == "JPEG":
        try:
            im.draft("RGB", (box[0] * 2, box[1] * 2))
        except Exception:
            pass
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P", "PA"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    im.thumbnail(box, Image.LANCZOS)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + f".{os.getpid()}.tmp")
    im.save(tmp, "JPEG", quality=quality)
    os.replace(tmp, out)
    return size


def dhash(im):
    g = im.convert("L").resize((9, 8), Image.BILINEAR)
    px = g.tobytes()
    v = 0
    for r in range(8):
        for c in range(8):
            v = (v << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
    return f"{v:016x}"


def file_hash(path, size, kind):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        if kind == "video" and size > 16 << 20:  # big video: sample head + tail
            h.update(f.read(4 << 20))
            f.seek(-(4 << 20), 2)
            h.update(f.read())
            h.update(str(size).encode())
        else:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def thumb_file(lib, h):
    return Path(lib) / "thumbs" / h[:2] / f"{h}.jpg"


def preview_file(lib, path, ext, h):
    """A ≤2048px JPEG for viewing/sharing (cached)."""
    out = Path(lib) / "previews" / h[:2] / f"{h}.jpg"
    if not out.exists():
        im = load_image(path, ext)
        if im is None:
            return None
        render_jpeg(im, out, (PREVIEW_MAX, PREVIEW_MAX), 86)
    return out


# ───────────────────────── per-file worker ─────────────────────────
def analyze(job):
    path, label, tz_min, lib = job
    ext = os.path.splitext(path)[1].lower()
    kind = "video" if ext in VIDEO_EXT else "photo"
    rec = dict(path=path, source=label, ext=ext, kind=kind, size=None, mtime=None, hash=None,
               taken=None, date_source=None, width=None, height=None, camera=None, lat=None,
               lon=None, duration=None, phash=None, thumb=0, error=None, people=None, descr=None,
               gfav=0, ignored=None, place=None, admin1=None, country=None, cc=None,
               tags=None, faces=None, vtext=None, vdone=None, verr=None)
    try:
        st = os.stat(path)
        rec["size"], rec["mtime"] = st.st_size, st.st_mtime
        rec["hash"] = file_hash(path, st.st_size, kind)
    except Exception as e:
        rec["error"] = f"unreadable: {e}"
        return rec

    cands = []

    def add(src, dt):
        if dt:
            cands.append((DATE_RANK[src], len(cands), dt, src))

    side = find_sidecar(path)
    tk_ts, tk_gps, extras = read_sidecar(side) if side else (None, None, {})
    rec.update({k: v for k, v in extras.items() if v})
    tk_dt = epoch_to_local(tk_ts, tz_min) if tk_ts else None
    tpath = thumb_file(lib, rec["hash"])
    gps = None
    try:
        if kind == "photo":
            im, ex = None, {}
            try:
                im = load_image(path, ext)
                if im is not None:
                    ex = exif_pillow(im)
            except Exception as e:
                rec["error"] = f"can't decode: {e}"
                if ext in {".heic", ".heif"} and pillow_heif is None:
                    rec["error"] = "HEIC support not installed (pip install pillow-heif)"
            if ext in RAW_EXT or ext in {".tif", ".tiff"} or not ex.get("dto"):
                for k, v in exif_exifread(path).items():
                    if v and not ex.get(k):
                        ex[k] = v
            add("exif", ex.get("dto"))
            add("exif", ex.get("dtd"))
            add("takeout", tk_dt)
            add("filename", date_from_name(os.path.basename(path)))
            add("exif-modified", ex.get("dt"))
            gps = ex.get("gps") or tk_gps
            rec["camera"] = camera_name(ex.get("make"), ex.get("model"))
            if im is not None:
                rec["width"], rec["height"] = oriented_size(im)
                if max(rec["width"], rec["height"]) < MIN_SIDE:
                    rec["ignored"] = "tiny"
                elif not tpath.exists():
                    render_jpeg(im, tpath, (THUMB_H * 3, THUMB_H))
            elif ext in RAW_EXT and rawpy is None and not rec["error"]:
                rec["error"] = "RAW preview needs rawpy (pip install rawpy)"
        else:
            pv = probe_video(path)
            rec["duration"] = pv.get("duration")
            add("quicktime", pv.get("qt"))
            add("takeout", tk_dt)
            if pv.get("utc"):
                add("video-meta", epoch_to_local(pv["utc"], tz_min))
            add("filename", date_from_name(os.path.basename(path)))
            gps = pv.get("gps") or tk_gps
            rec["camera"] = camera_name(pv.get("make"), pv.get("model"))
            if tpath.exists() or video_thumb(path, tpath, rec["duration"]):
                with Image.open(tpath) as t:
                    rec["width"], rec["height"] = t.size
            elif not FFMPEG:
                rec["error"] = "video preview needs ffmpeg"
        if tpath.exists() and not rec["ignored"]:
            rec["thumb"] = 1
            with Image.open(tpath) as t:
                rec["phash"] = dhash(t)
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"

    add("file-time", epoch_to_local(rec["mtime"], None) or datetime.fromtimestamp(0))
    best = min(cands)
    rec["taken"] = best[2].isoformat(timespec="seconds")
    rec["date_source"] = best[3]
    if gps:
        rec["lat"], rec["lon"] = gps
    return rec


# ───────────────────────────── database ─────────────────────────────
COLS = ["path", "source", "ext", "kind", "size", "mtime", "hash", "taken", "date_source", "width",
        "height", "camera", "lat", "lon", "duration", "phash", "thumb", "error", "people", "descr",
        "gfav", "ignored", "place", "admin1", "country", "cc", "tags", "faces", "vtext", "vdone", "verr"]


def open_db(lib):
    Path(lib).mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(Path(lib) / "timeline.db"), timeout=60)
    try:  # WAL lets the gallery read while a scan writes; not every disk/folder supports it
        db.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    db.executescript("""
    CREATE TABLE IF NOT EXISTS media(
      id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, source TEXT, ext TEXT, kind TEXT,
      size INTEGER, mtime REAL, hash TEXT, taken TEXT, date_source TEXT, width INTEGER,
      height INTEGER, camera TEXT, lat REAL, lon REAL, duration REAL, phash TEXT,
      thumb INTEGER, error TEXT, dup_of INTEGER, dup_kind TEXT, live_of INTEGER);
    CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, label TEXT, added REAL, last_scan REAL);
    CREATE TABLE IF NOT EXISTS albums(id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT DEFAULT 'user',
      gkey TEXT, created REAL, share_token TEXT);
    CREATE TABLE IF NOT EXISTS album_items(album_id INTEGER, media_id INTEGER, added REAL,
      PRIMARY KEY(album_id, media_id));
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    have = {r[1] for r in db.execute("PRAGMA table_info(media)")}
    for col, typ in [("dup_kind", "TEXT"), ("people", "TEXT"), ("descr", "TEXT"), ("gfav", "INTEGER"),
                     ("fav", "INTEGER DEFAULT 0"), ("ignored", "TEXT"), ("place", "TEXT"), ("admin1", "TEXT"),
                     ("country", "TEXT"), ("cc", "TEXT"), ("tags", "TEXT"), ("faces", "INTEGER"), ("vtext", "TEXT"),
                     ("vdone", "INTEGER"), ("verr", "TEXT")]:
        if col not in have:
            db.execute(f"ALTER TABLE media ADD COLUMN {col} {typ}")
    have = {r[1] for r in db.execute("PRAGMA table_info(albums)")}
    for col, typ in [("meta", "TEXT"), ("renamed", "INTEGER DEFAULT 0")]:
        if col not in have:
            db.execute(f"ALTER TABLE albums ADD COLUMN {col} {typ}")
    db.executescript("""
    CREATE INDEX IF NOT EXISTS ix_hash ON media(hash);
    CREATE INDEX IF NOT EXISTS ix_taken ON media(taken);
    CREATE INDEX IF NOT EXISTS ix_dup ON media(dup_of);
    CREATE INDEX IF NOT EXISTS ix_live ON media(live_of);
    """)
    old = db.execute("SELECT v FROM meta WHERE k='roots'").fetchone()  # v1 → v2
    if old:
        for f in json.loads(old[0]):
            label, p = parse_labeled(f)
            db.execute("INSERT OR IGNORE INTO sources VALUES(?,?,?,NULL)", (p, label, time.time()))
        db.execute("DELETE FROM meta WHERE k='roots'")
    db.commit()
    return db


def upsert(db, rec):
    vals = [rec[c] for c in COLS]
    db.execute(f"INSERT INTO media({','.join(COLS)},fav) VALUES({','.join('?' * len(COLS))},?) "
               f"ON CONFLICT(path) DO UPDATE SET "
               + ",".join(f"{c}=excluded.{c}" for c in COLS[1:]), vals + [rec.get("gfav") or 0])


def hamming(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def post_process(db, lib=None, progress=None):
    db.execute("UPDATE media SET dup_of=NULL, dup_kind=NULL, live_of=NULL")
    ok = "ignored IS NULL"
    # 1) exact duplicates (same bytes)
    groups = defaultdict(list)
    for r in db.execute(f"SELECT id, hash, date_source, path, fav FROM media WHERE {ok} AND hash IN "
                        f"(SELECT hash FROM media WHERE hash IS NOT NULL AND {ok} GROUP BY hash HAVING COUNT(*)>1)"):
        groups[r[1]].append(r)
    for items in groups.values():
        items.sort(key=lambda r: (DATE_RANK.get(r[2], 9), len(r[3]), r[0]))
        keep = items[0][0]
        db.executemany("UPDATE media SET dup_of=?, dup_kind='exact' WHERE id=?", [(keep, r[0]) for r in items[1:]])
        if any(r[4] for r in items):  # a favourite on any copy counts
            db.execute("UPDATE media SET fav=1 WHERE id=?", (keep,))
    # 2) Live Photos: IMG_1234.HEIC + IMG_1234.MOV (short) in the same folder
    photos = {}
    for pid, path in db.execute(f"SELECT id, path FROM media WHERE kind='photo' AND dup_of IS NULL AND {ok}"):
        d, n = os.path.split(path)
        photos[(d.lower(), os.path.splitext(n)[0].lower())] = pid
    upd = []
    for vid, path, dur in db.execute(f"SELECT id, path, duration FROM media WHERE kind='video' AND dup_of IS NULL AND {ok}"):
        d, n = os.path.split(path)
        stem, ext = os.path.splitext(n)
        if ext.lower() in (".mov", ".mp4") and (dur is None or dur <= 5.5):
            pid = photos.get((d.lower(), stem.lower()))
            if pid:
                upd.append((pid, vid))
    db.executemany("UPDATE media SET live_of=? WHERE id=?", upd)
    # 3) look-alike copies: same second, near-identical picture, DIFFERENT folders
    #    (e.g. a Google Photos "storage saver" copy + the iPhone original). Burst shots
    #    sit in the same folder, so they're never merged.
    by_t = defaultdict(list)
    for r in db.execute(f"SELECT id, taken, phash, path, width, height FROM media WHERE dup_of IS NULL "
                        f"AND live_of IS NULL AND phash IS NOT NULL AND kind='photo' AND {ok} "
                        f"AND date_source != 'file-time'"):
        r = list(r)
        r[3] = os.path.dirname(r[3]).lower()
        by_t[r[1]].append(r)
    sim = []
    for items in by_t.values():
        if len(items) < 2 or len({r[3] for r in items}) < 2:
            continue
        items.sort(key=lambda r: -((r[4] or 0) * (r[5] or 0)))
        used = set()
        for i, a in enumerate(items):
            if a[0] in used:
                continue
            for b in items[i + 1:]:
                if b[0] not in used and b[3] != a[3] and hamming(a[2], b[2]) <= 6:
                    sim.append((a[0], b[0]))
                    used.add(b[0])
    db.executemany("UPDATE media SET dup_of=?, dup_kind='similar' WHERE id=?", sim)
    # 4) Google Photos albums from Takeout (folders that have a metadata.json with a title)
    import_google_albums(db)
    db.execute("DELETE FROM album_items WHERE media_id NOT IN (SELECT id FROM media)")
    db.commit()
    # 5) place names (offline) and automatic trips & events
    import geo
    if lib is not None:
        try:
            geo.geocode_missing(db, lib, progress)
        except Exception as e:
            print("place lookup skipped:", e)
    if progress:
        progress.update(message="Finding trips and events…")
    geo.rebuild_trips(db)


def import_google_albums(db):
    dirs = defaultdict(list)
    for mid, path in db.execute("SELECT id, path FROM media WHERE ignored IS NULL"):
        dirs[os.path.dirname(path)].append(mid)
    seen = set()
    for d, mids in dirs.items():
        title = None
        for fn in ALBUM_META:
            p = os.path.join(d, fn)
            if os.path.exists(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        j = json.load(f)
                    title = (j.get("title") or "").strip() if isinstance(j, dict) else None
                except Exception:
                    title = None
                if title:
                    break
        if not title:
            continue
        key = d.lower()
        seen.add(key)
        row = db.execute("SELECT id FROM albums WHERE kind='google' AND gkey=?", (key,)).fetchone()
        aid = row[0] if row else db.execute("INSERT INTO albums(name, kind, gkey, created) VALUES(?,?,?,?)",
                                            (title, "google", key, time.time())).lastrowid
        db.execute("DELETE FROM album_items WHERE album_id=?", (aid,))
        db.executemany("INSERT OR IGNORE INTO album_items VALUES(?,?,?)", [(aid, m, time.time()) for m in mids])
    for aid, key in db.execute("SELECT id, gkey FROM albums WHERE kind='google'").fetchall():
        if key not in seen:
            db.execute("DELETE FROM album_items WHERE album_id=?", (aid,))
            db.execute("DELETE FROM albums WHERE id=?", (aid,))


# ───────────────────────────── places & sources ─────────────────────────────
def parse_labeled(f):
    label = None
    if "=" in f:
        left, right = f.split("=", 1)
        if "/" not in left and "\\" not in left and left.strip():
            label, f = left.strip(), right
    p = str(Path(f).expanduser().resolve())
    return label or default_label(p), p


def default_label(p):
    name = Path(p).name
    if not name:  # drive root, e.g. "E:\"
        lab = drive_label(p)
        return f"{lab} ({p.rstrip(os.sep)})" if lab else p.rstrip(os.sep) or p
    return name


def drive_label(root):
    if not sys.platform.startswith("win"):
        return ""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(261)
        ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(root), buf, 261, None, None, None, None, 0)
        return buf.value
    except Exception:
        return ""


def list_places():
    """Drives (incl. external SSDs / SD cards) and the usual photo folders."""
    drives, folders = [], []

    def usage(p):
        try:
            u = shutil.disk_usage(p)
            return u.total, u.used
        except OSError:
            return None, None

    if sys.platform.startswith("win"):
        import ctypes
        kinds = {2: "removable", 3: "internal", 4: "network", 6: "ram"}
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, L in enumerate(string.ascii_uppercase):
            if not bitmask >> i & 1:
                continue
            root = f"{L}:\\"
            t = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
            if t == 5 or not os.path.exists(root):  # CD/DVD or not ready
                continue
            total, used = usage(root)
            sysdrive = os.environ.get("SystemDrive", "C:").upper() == f"{L}:"
            drives.append({"path": root, "label": drive_label(root) or ("Local Disk" if t == 3 else "Drive"),
                           "kind": "system" if sysdrive else kinds.get(t, "internal"), "total": total, "used": used})
    else:
        cands = []
        if sys.platform == "darwin":
            cands = [str(p) for p in Path("/Volumes").glob("*") if not os.path.islink(p)]
        else:
            user = os.environ.get("USER", "")
            for base in (f"/media/{user}", f"/run/media/{user}", "/media", "/mnt"):
                if os.path.isdir(base):
                    cands += [str(p) for p in Path(base).iterdir() if p.is_dir() and str(p) not in cands]
        for p in cands:
            if p in (f"/media/{os.environ.get('USER', '')}",):
                continue
            total, used = usage(p)
            drives.append({"path": p, "label": Path(p).name, "kind": "removable", "total": total, "used": used})
    home = Path.home()
    for name in ["Pictures", "Downloads", "Desktop", "Documents", "Videos", "Movies", "DCIM",
                 "OneDrive/Pictures", "OneDrive/Camera Roll", "Pictures/iCloud Photos", "iCloudDrive"]:
        p = home / name
        if p.is_dir():
            folders.append({"path": str(p), "label": name.replace("/", " › ")})
    return {"drives": drives, "folders": folders, "home": str(home), "sep": os.sep}


def list_dir(path):
    if not path:
        if sys.platform.startswith("win"):
            return {"path": "", "parent": None,
                    "dirs": [{"name": d["path"], "path": d["path"], "label": d["label"]} for d in list_places()["drives"]]}
        path = "/"
    p = Path(path)
    dirs = []
    try:
        for e in os.scandir(p):
            try:
                if e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "$")):
                    dirs.append({"name": e.name, "path": e.path})
            except OSError:
                pass
    except OSError as e:
        return {"path": str(p), "parent": str(p.parent) if p.parent != p else "", "dirs": [], "error": str(e)}
    dirs.sort(key=lambda d: d["name"].lower())
    parent = "" if p.parent == p else str(p.parent)
    return {"path": str(p), "parent": parent, "dirs": dirs}


def walk(root, skip=()):
    skip = {os.path.normcase(s) for s in skip}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in SKIP_DIRS and not d.startswith(".")
                       and not d.lower().endswith(SKIP_SUFFIXES)
                       and os.path.normcase(os.path.join(dirpath, d)) not in skip]
        for fn in filenames:
            if not fn.startswith("._") and os.path.splitext(fn)[1].lower() in ALL_EXT:
                yield os.path.join(dirpath, fn)


# ───────────────────────────── scanning ─────────────────────────────
class Progress:
    """Minimal progress sink; the app replaces it with one that feeds the UI."""
    def __init__(self, printer=None):
        import threading
        self.cancel = threading.Event()
        self.state = {}
        self.printer = printer

    def update(self, **kw):
        self.state.update(kw)
        if self.printer:
            self.printer(self.state)


def run_scan(progress, lib, add=(), tz_min=None, workers=None, only=None):
    """Scan every saved source (plus any new ones in `add`). Returns a summary dict."""
    db = open_db(lib)
    try:
        for f in add:
            label, p = parse_labeled(f) if isinstance(f, str) else f
            db.execute("INSERT OR IGNORE INTO sources VALUES(?,?,?,NULL)", (p, label, time.time()))
        db.commit()
        roots, missing = [], []
        for p, label in db.execute("SELECT path, label FROM sources ORDER BY path"):
            if only and p not in only:
                continue
            (roots if os.path.isdir(p) else missing).append((label, p))
        # a folder inside another source would be walked twice → keep the outer one
        roots = [r for r in roots if not any(o[1] != r[1] and r[1].startswith(o[1].rstrip("\\/") + os.sep)
                                             for o in roots)]
        progress.update(phase="finding", found=0, done=0, total=0, missing=[p for _, p in missing],
                        message=f"Looking for photos and videos in {len(roots)} place(s)…")
        known = {r[0]: (r[1], r[2], r[3]) for r in db.execute("SELECT path, size, mtime, error FROM media")}
        jobs, seen = [], set()
        libdir = str(Path(lib).resolve())
        for label, root in roots:
            progress.update(current=root)
            for path in walk(root, skip=[libdir]):
                if progress.cancel.is_set():
                    raise Cancelled()
                if path in seen:
                    continue
                seen.add(path)
                if len(seen) % 250 == 0:
                    progress.update(found=len(seen))
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                k = known.get(path)
                if k and k[0] == st.st_size and abs((k[1] or 0) - st.st_mtime) < 1:
                    continue
                jobs.append((path, label, tz_min, libdir))
            prefix = root.rstrip("\\/") + os.sep
            gone = [p for p in known if p.startswith(prefix) and p not in seen]
            if gone:
                db.executemany("DELETE FROM media WHERE path=?", [(p,) for p in gone])
        db.commit()
        progress.update(found=len(seen), total=len(jobs), phase="processing",
                        message=f"Reading {len(jobs):,} new photos & videos…" if jobs else "No new files.")
        t0, done, cancelled = time.time(), 0, False
        if jobs:
            ex = ProcessPoolExecutor(max_workers=workers or max(1, (os.cpu_count() or 2) - 1))
            try:
                futs = [ex.submit(analyze, j) for j in jobs]
                for f in as_completed(futs):
                    if progress.cancel.is_set():
                        cancelled = True
                        break
                    try:
                        rec = f.result()
                    except Exception:
                        continue
                    if rec.get("size") is not None:
                        upsert(db, rec)
                    done += 1
                    if done % 50 == 0 or done == len(jobs):
                        db.commit()
                        el = time.time() - t0
                        progress.update(done=done, rate=done / el, eta=el / done * (len(jobs) - done),
                                        current=rec["path"])
            finally:
                ex.shutdown(wait=True, cancel_futures=True)
        db.commit()
        progress.update(phase="finishing", message="Finding duplicates, Live Photos and Google albums…")
        post_process(db, lib, progress)
        if not cancelled:
            db.executemany("UPDATE sources SET last_scan=? WHERE path=?", [(time.time(), p) for _, p in roots])
            db.commit()
        if cancelled:
            raise Cancelled()
        return summary(db)
    finally:
        db.close()


def summary(db):
    q = lambda s: db.execute(s).fetchone()[0]
    return {
        "files": q("SELECT COUNT(*) FROM media WHERE ignored IS NULL"),
        "shown": q("SELECT COUNT(*) FROM media WHERE dup_of IS NULL AND live_of IS NULL AND ignored IS NULL"),
        "exact": q("SELECT COUNT(*) FROM media WHERE dup_kind='exact'"),
        "similar": q("SELECT COUNT(*) FROM media WHERE dup_kind='similar'"),
        "live": q("SELECT COUNT(*) FROM media WHERE live_of IS NOT NULL"),
        "ignored": q("SELECT COUNT(*) FROM media WHERE ignored IS NOT NULL"),
        "albums": q("SELECT COUNT(*) FROM albums WHERE kind='google'"),
        "trips": q("SELECT COUNT(*) FROM albums WHERE kind='trip'"),
        "events": q("SELECT COUNT(*) FROM albums WHERE kind='event'"),
        "places": q("SELECT COUNT(DISTINCT place) FROM media WHERE place IS NOT NULL AND place != ''"),
        "dates": db.execute("SELECT date_source, COUNT(*) FROM media WHERE dup_of IS NULL AND ignored IS NULL "
                            "GROUP BY date_source ORDER BY COUNT(*) DESC").fetchall(),
        "errors": db.execute("SELECT error, COUNT(*) FROM media WHERE error IS NOT NULL GROUP BY error "
                             "ORDER BY COUNT(*) DESC LIMIT 5").fetchall(),
    }


def remove_source(lib, path):
    db = open_db(lib)
    try:
        prefix = path.rstrip("\\/") + os.sep
        db.execute("DELETE FROM media WHERE substr(path, 1, ?) = ?", (len(prefix), prefix))
        db.execute("DELETE FROM sources WHERE path=?", (path,))
        db.commit()
        post_process(db, lib)
    finally:
        db.close()


# ───────────────────────────── copying & exporting ─────────────────────────────
def safe_name(s, maxlen=80):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")
    return (s or "Album")[:maxlen]


def unique_path(p):
    p = Path(p)
    if not p.exists():
        return p
    i = 2
    while True:
        q = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not q.exists():
            return q
        i += 1


def media_rows(db, ids, with_live=True):
    """Primary rows for ids (any copy id maps to the kept one), oldest first, + Live clips."""
    if not ids:
        return []
    ids = list(ids)
    rows, live = {}, defaultdict(list)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        qs = ",".join("?" * len(part))
        for r in db.execute(f"SELECT id, path, ext, kind, hash, taken, width, height, date_source FROM media "
                            f"WHERE id IN (SELECT COALESCE(dup_of, live_of, id) FROM media WHERE id IN ({qs}))", part):
            rows[r[0]] = r
    if with_live and rows:
        keys = list(rows)
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            for lid, path, ext, pid in db.execute(
                    f"SELECT id, path, ext, live_of FROM media WHERE live_of IN ({','.join('?' * len(part))})", part):
                live[pid].append((lid, path, ext))
    out = sorted(rows.values(), key=lambda r: (r[5] or "", r[0]))
    return [(r, live.get(r[0], [])) for r in out]


def dated_name(taken, path):
    return f"{taken[:10]} {taken[11:13]}.{taken[14:16]}.{taken[17:19]} {os.path.basename(path)}"


def run_copy(progress, lib, ids, dest, date_names=True, with_live=True):
    db = open_db(lib)
    try:
        items = media_rows(db, ids, with_live)
    finally:
        db.close()
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    files = [(r[1], r[5]) for r, lv in items] + [(p, r[5]) for r, lv in items for _, p, _ in lv]
    progress.update(phase="copying", total=len(files), done=0, message=f"Copying {len(files):,} files…")
    copied = missing = 0
    for i, (src, taken) in enumerate(files, 1):
        if progress.cancel.is_set():
            raise Cancelled()
        if not os.path.exists(src):
            missing += 1
            continue
        name = dated_name(taken, src) if date_names else os.path.basename(src)
        target = dest / name
        if target.exists() and target.stat().st_size == os.path.getsize(src):
            copied += 1
            continue
        shutil.copy2(src, unique_path(target))
        copied += 1
        progress.update(done=i, current=src)
    return {"copied": copied, "missing": missing, "dest": str(dest)}


def zip_stream(out, lib, ids, date_names=True):
    """Write a zip of originals to a (possibly unseekable) stream."""
    db = open_db(lib)
    try:
        items = media_rows(db, ids, True)
    finally:
        db.close()
    used = set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for r, lv in items:
            for p in [r[1]] + [x[1] for x in lv]:
                if not os.path.exists(p):
                    continue
                name = dated_name(r[5], p) if date_names else os.path.basename(p)
                base, n = name, 2
                while name.lower() in used:
                    stem, ext = os.path.splitext(base)
                    name = f"{stem} ({n}){ext}"
                    n += 1
                used.add(name.lower())
                z.write(p, name)


def album_payload_items(items, lib, url_for, progress=None, quality="web", folder=None):
    """Build the item list for a shareable album page. When `folder` is given, files are
    written there (export); otherwise `url_for` builds live URLs (Wi-Fi sharing)."""
    out = []
    jobs = []
    for idx, (r, lv) in enumerate(items):
        mid, path, ext, kind, h, taken, w, hgt, _ = r
        it = {"t": taken, "k": "v" if kind == "video" else "p", "w": w or 4, "h": hgt or 3,
              "name": os.path.basename(path)}
        if folder is None:
            it.update(url_for(mid, kind, bool(lv)))
        else:
            stem = f"{idx + 1:04d}"
            it["th"] = f"thumbs/{stem}.jpg"
            jobs.append(("thumb", r, folder / it["th"]))
            if kind == "video":
                it["src"] = f"videos/{stem}.mp4"
                jobs.append(("video", r, folder / it["src"]))
            else:
                it["src"] = f"photos/{stem}.jpg"
                jobs.append(("photo", r, folder / it["src"]))
                if lv:
                    it["live"] = f"videos/{stem}-live.mp4"
                    jobs.append(("live", (lv[0][1], lv[0][2]), folder / it["live"]))
            if quality == "original":
                it["dl"] = f"originals/{stem} {os.path.basename(path)}"
                jobs.append(("orig", r, folder / it["dl"]))
            else:
                it["dl"] = it["src"]
        out.append(it)
    if folder is not None:
        done = [0]

        def work(j):
            what, r, target = j
            target.parent.mkdir(parents=True, exist_ok=True)
            if what == "thumb":
                src = thumb_file(lib, r[4])
                if src.exists():
                    shutil.copyfile(src, target)
            elif what == "photo":
                if os.path.exists(r[1]):
                    p = preview_file(lib, r[1], r[2], r[4])
                    if p:
                        shutil.copyfile(p, target)
            elif what in ("video", "live"):
                src, ext = (r[1], r[2]) if what == "video" else r
                if os.path.exists(src):
                    if quality == "web" and FFMPEG:
                        if not web_video(src, target):
                            shutil.copyfile(src, target)
                    else:
                        shutil.copyfile(src, target)
            elif what == "orig":
                if os.path.exists(r[1]):
                    shutil.copy2(r[1], target)
            done[0] += 1
            if progress:
                progress.update(done=done[0])

        if progress:
            progress.update(phase="exporting", total=len(jobs), done=0, message=f"Preparing {len(items):,} items…")
        with ThreadPoolExecutor(max_workers=max(2, min(6, os.cpu_count() or 2))) as ex:
            for f in as_completed([ex.submit(work, j) for j in jobs]):
                if progress and progress.cancel.is_set():
                    ex.shutdown(wait=False, cancel_futures=True)
                    raise Cancelled()
                f.result()
    return out


def render_album_html(payload):
    tpl = (APP_DIR / "web" / "album.html").read_text(encoding="utf-8")
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return tpl.replace("/*__ALBUM__*/null", data)


def run_export(progress, lib, album_id, dest_parent, quality="web", make_zip=True):
    db = open_db(lib)
    try:
        row = db.execute("SELECT name FROM albums WHERE id=?", (album_id,)).fetchone()
        if not row:
            raise ValueError("Album not found")
        name = row[0]
        ids = [r[0] for r in db.execute("SELECT media_id FROM album_items WHERE album_id=?", (album_id,))]
        items = media_rows(db, ids, True)
    finally:
        db.close()
    folder = unique_path(Path(dest_parent) / safe_name(name))
    folder.mkdir(parents=True)
    payload = {"title": name, "items": album_payload_items(items, lib, None, progress, quality, folder),
               "created": datetime.now().strftime("%d %b %Y"), "zip": None}
    (folder / "index.html").write_text(render_album_html(payload), encoding="utf-8")
    result = {"folder": str(folder), "count": len(items)}
    if make_zip:
        progress.update(phase="zipping", message="Packing a .zip you can send…")
        zpath = unique_path(folder.with_suffix(".zip"))
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for p in sorted(folder.rglob("*")):
                if p.is_file():
                    z.write(p, f"{folder.name}/{p.relative_to(folder).as_posix()}")
        result["zip"] = str(zpath)
        result["zip_size"] = zpath.stat().st_size
    return result


def run_organize(progress, lib, out, link=False, dry_run=False):
    db = open_db(lib)
    try:
        rows = db.execute("SELECT id FROM media WHERE dup_of IS NULL AND live_of IS NULL AND ignored IS NULL").fetchall()
        items = media_rows(db, [r[0] for r in rows], True)
    finally:
        db.close()
    out = Path(out)
    n_copy = n_skip = 0
    preview = []
    progress.update(phase="organizing", total=len(items), done=0)
    for i, (r, lv) in enumerate(items, 1):
        if progress.cancel.is_set():
            raise Cancelled()
        taken, ds = r[5], r[8]
        dt = datetime.fromisoformat(taken)
        folder = (out / "_date uncertain" / f"{dt:%Y}") if ds == "file-time" else (out / f"{dt:%Y}" / f"{dt:%Y-%m}")
        for p in [r[1]] + [x[1] for x in lv]:
            if not os.path.exists(p):
                continue
            dest = folder / dated_name(taken, p)
            if dest.exists() and dest.stat().st_size == os.path.getsize(p):
                n_skip += 1
                continue
            dest = unique_path(dest)
            n_copy += 1
            if dry_run:
                if len(preview) < 25:
                    preview.append((p, str(dest)))
                continue
            folder.mkdir(parents=True, exist_ok=True)
            if link:
                try:
                    os.link(p, dest)
                except OSError:
                    shutil.copy2(p, dest)
            else:
                shutil.copy2(p, dest)
            ts = dt.timestamp()
            os.utime(dest, (ts, ts))
        progress.update(done=i)
    return {"copied": n_copy, "skipped": n_skip, "preview": preview, "dest": str(out)}
