"""Smart tags: what's in each photo (mountain, beach, dog, text…), recognised on this Mac.

Uses Apple's built-in Vision framework through a small worker module, so nothing leaves the
computer and no AI models are downloaded. Runs after each scan at low priority, only on photos
it hasn't looked at yet. On Windows/Linux the feature simply reports itself as unavailable.
"""
from __future__ import annotations

import importlib
import os
import site
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import core as C

VERSION = 1
IS_MAC = sys.platform == "darwin"
MODULE = os.environ.get("PT_VISION_MODULE", "vision_mac")   # swapped for a stand-in in tests
WORKERS = 2                                                  # deliberately small: stay light
PACKAGE = "pyobjc-framework-Vision"


def _mod():
    return importlib.import_module(MODULE)


def supported():
    return IS_MAC or MODULE != "vision_mac"


def status(lib=None):
    st = {"supported": supported(), "installed": False, "reason": ""}
    if not st["supported"]:
        st["reason"] = "Smart tags use Apple’s built-in recognition, so they work on a Mac only."
        return st
    try:
        ok, why = _mod().check()
    except Exception as e:
        ok, why = False, f"{type(e).__name__}: {e}"
    st["installed"], st["reason"] = ok, why
    if lib is not None:
        db = C.open_db(lib)
        try:
            base = "dup_of IS NULL AND live_of IS NULL AND ignored IS NULL AND thumb=1"
            st["total"] = db.execute(f"SELECT COUNT(*) FROM media WHERE {base}").fetchone()[0]
            st["done"] = db.execute(f"SELECT COUNT(*) FROM media WHERE {base} AND vdone >= ?", (VERSION,)).fetchone()[0]
            st["errors"] = db.execute(f"SELECT COUNT(*) FROM media WHERE {base} AND verr IS NOT NULL").fetchone()[0]
            row = db.execute("SELECT v FROM meta WHERE k='smart_off'").fetchone()
            st["off"] = bool(row and row[0] == "1")
        finally:
            db.close()
    return st


def install(progress):
    """One-time: install the small bridge to Apple's Vision framework (~15 MB)."""
    progress.update(phase="installing", message="Turning on smart tags (one-time download, ~15 MB)…", total=0, done=0)
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", PACKAGE]
    last = ""
    for extra in ([], ["--user"]):
        r = subprocess.run(base + extra, capture_output=True, text=True)
        if r.returncode == 0:
            break
        last = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
        last = last[0]
    else:
        raise RuntimeError(f"Couldn’t install {PACKAGE}: {last}")
    # make a fresh user site-packages folder visible without restarting the app
    try:
        p = site.getusersitepackages()
        if p not in sys.path and os.path.isdir(p):
            sys.path.append(p)
    except Exception:
        pass
    importlib.invalidate_caches()
    ok, why = _mod().check()
    if not ok:
        raise RuntimeError(f"Installed, but macOS Vision isn’t reachable: {why}")


def run_tagging(progress, lib, workers=WORKERS):
    """Tag every photo/video thumbnail that hasn't been looked at yet. Cancellable."""
    st = status()
    if not st["installed"]:
        return {"tagged": 0, "skipped": st["reason"]}
    db = C.open_db(lib)
    try:
        if (db.execute("SELECT v FROM meta WHERE k='smart_off'").fetchone() or ["0"])[0] == "1":
            return {"tagged": 0, "skipped": "turned off"}
        rows = db.execute("SELECT id, hash, path, ext, kind FROM media WHERE dup_of IS NULL AND live_of IS NULL "
                          "AND ignored IS NULL AND thumb=1 AND (vdone IS NULL OR vdone < ?) ORDER BY taken DESC",
                          (VERSION,)).fetchall()
        if not rows:
            return {"tagged": 0}
        jobs = [(mid, str(C.thumb_file(lib, h)), path, ext, kind) for mid, h, path, ext, kind in rows]
        progress.update(phase="tagging", total=len(jobs), done=0, current="",
                        message=f"Recognising what’s in {len(jobs):,} photos…")
        mod = _mod()
        done = fails = 0
        first_errors = []
        t0 = time.time()
        with ProcessPoolExecutor(max_workers=workers, initializer=mod.init) as ex:
            for i in range(0, len(jobs), 64):           # small batches → quick to stop
                if progress.cancel.is_set():
                    raise C.Cancelled()
                for f in as_completed([ex.submit(mod.analyze, j) for j in jobs[i:i + 64]]):
                    try:
                        r = f.result()
                    except Exception as e:
                        r = {"id": None, "error": str(e)}
                    if r.get("id") is None:
                        continue
                    if r.get("error"):
                        fails += 1
                        if len(first_errors) < 5:
                            first_errors.append(r["error"])
                        db.execute("UPDATE media SET vdone=?, verr=? WHERE id=?", (VERSION, r["error"][:300], r["id"]))
                    else:
                        db.execute("UPDATE media SET tags=?, faces=?, vtext=?, vdone=?, verr=NULL WHERE id=?",
                                   (r["tags"] or None, r["faces"] or 0, r["vtext"], VERSION, r["id"]))
                    done += 1
                db.commit()
                el = time.time() - t0
                progress.update(done=done, rate=done / el if el else 0,
                                eta=el / done * (len(jobs) - done) if done else None)
                # If the very first batch failed completely, Vision itself isn't working: stop and say why.
                if i == 0 and fails and fails == done:
                    db.execute("UPDATE media SET vdone=NULL WHERE verr IS NOT NULL")
                    db.commit()
                    raise RuntimeError("Apple’s image recognition didn’t work on this Mac: " + first_errors[0])
        return {"tagged": done - fails, "failed": fails}
    finally:
        db.close()


def set_off(lib, off):
    db = C.open_db(lib)
    try:
        db.execute("INSERT OR REPLACE INTO meta VALUES('smart_off', ?)", ("1" if off else "0",))
        db.commit()
    finally:
        db.close()


# Words people type → Apple's label names (search also matches plurals automatically)
SYNONYMS = {
    "mountain": "mountains hills hill peak himalaya", "hill": "hills", "beach": "beaches sea seaside coast shore",
    "ocean": "sea", "sea": "ocean", "seascape": "sea ocean", "lake": "lakes", "river": "rivers stream",
    "waterfall": "waterfalls falls", "snow": "winter snowy", "sunset_sunrise": "sunset sunrise dusk dawn golden",
    "sky": "skies clouds", "cloudy": "clouds", "forest": "woods jungle trees", "tree": "trees",
    "flower": "flowers bloom", "plant": "plants", "bird": "birds birding", "reptile": "snake snakes lizard",
    "snake": "snakes reptile", "dog": "dogs puppy", "cat": "cats kitten", "food": "meal dish eating",
    "dessert": "sweets cake", "cake": "birthday", "people": "person persons crowd", "selfie": "selfies",
    "bicycle": "cycle cycling bike", "motorcycle": "bike motorbike", "car": "cars", "temple": "temples shrine",
    "document": "documents paper", "screenshot": "screenshots screen", "night_sky": "stars night",
    "fireworks": "diwali", "wedding": "marriage shaadi",
}
