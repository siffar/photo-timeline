"""Offline place names and automatic trips.

Place names come from GeoNames (geonames.org, CC-BY 4.0): every town with 1,000+ people
worldwide. It's downloaded once (~10 MB) into the library folder, then used offline.
"""
from __future__ import annotations

import io
import json
import math
import os
import ssl
import time
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = "download.geonames.org/export/dump/"
FILES = {"cities": "cities1000.zip", "admin1": "admin1CodesASCII.txt", "countries": "countryInfo.txt"}
SKIP_CODES = {"PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH"}  # neighbourhoods, historical, abandoned…
CELL = 4  # grid cells per degree (0.25°)


def geo_dir(lib):
    return Path(lib) / "geo"


def _fetch(name, dest):
    ctx = None
    try:
        import certifi  # python.org Python on a Mac often lacks root certificates
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    last = None
    for scheme in ("https://", "http://"):
        try:
            with urllib.request.urlopen(scheme + BASE + name, timeout=60, context=ctx if scheme == "https://" else None) as r:
                data = r.read()
            tmp = dest.with_suffix(dest.suffix + ".part")
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            return
        except Exception as e:  # try plain http if https / certificates fail
            last = e
    raise last


def ensure_data(lib, progress=None):
    """Returns the path of the prepared places table, downloading it the first time."""
    d = geo_dir(lib)
    out = d / "places.tsv"
    if out.exists():
        return out
    d.mkdir(parents=True, exist_ok=True)
    for key, name in FILES.items():
        if not (d / name).exists():
            if progress:
                progress.update(message="Downloading place names (one time, ~10 MB)…")
            _fetch(name, d / name)
    admin1 = {}
    with open(d / FILES["admin1"], encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 2:
                admin1[p[0]] = p[1]
    countries = {}
    with open(d / FILES["countries"], encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                continue
            p = line.rstrip("\n").split("\t")
            if len(p) > 4:
                countries[p[0]] = p[4]
    rows = []
    with zipfile.ZipFile(d / FILES["cities"]) as z:
        inner = next(n for n in z.namelist() if n.endswith(".txt"))
        with z.open(inner) as fb:
            for line in io.TextIOWrapper(fb, encoding="utf-8"):
                p = line.rstrip("\n").split("\t")
                if len(p) < 15 or p[6] != "P" or p[7] in SKIP_CODES:
                    continue
                cc = p[8]
                rows.append("\t".join([p[4], p[5], p[1].replace("\t", " "), admin1.get(f"{cc}.{p[10]}", ""),
                                       countries.get(cc, cc), cc, p[14] or "0"]))
    tmp = out.with_suffix(".tmp")
    tmp.write_text("\n".join(rows), encoding="utf-8")
    os.replace(tmp, out)
    return out


class Geocoder:
    def __init__(self, path):
        self.lat, self.lon, self.info, self.pop = [], [], [], []
        self.grid = defaultdict(list)
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                p = line.rstrip("\n").split("\t")
                if len(p) < 7:
                    continue
                la, lo = float(p[0]), float(p[1])
                self.lat.append(la)
                self.lon.append(lo)
                self.info.append((p[2], p[3], p[4], p[5]))
                self.pop.append(max(int(p[6] or 0), 1000))
                self.grid[(math.floor(la * CELL), math.floor(lo * CELL))].append(len(self.lat) - 1)

    def _ring(self, ci, cj, r):
        if r == 0:
            yield ci, cj
            return
        for dj in range(-r, r + 1):
            yield ci - r, cj + dj
            yield ci + r, cj + dj
        for di in range(-r + 1, r):
            yield ci + di, cj - r
            yield ci + di, cj + r

    def lookup(self, lat, lon):
        """→ (place, state, country, cc) — nearest town, gently preferring bigger towns."""
        ci, cj = math.floor(lat * CELL), math.floor(lon * CELL)
        kx = math.cos(math.radians(lat)) * 111.2
        best, best_score, best_d = None, 1e18, 1e18
        cosl = max(0.2, math.cos(math.radians(lat)))
        for r in range(0, 13):
            for cell in self._ring(ci, cj, r):
                for i in self.grid.get(cell, ()):
                    dy = (self.lat[i] - lat) * 111.2
                    dx = (self.lon[i] - lon) * kx
                    dist = math.sqrt(dx * dx + dy * dy)
                    score = dist / (1 + math.log10(self.pop[i] / 1000))
                    if score < best_score:
                        best, best_score, best_d = i, score, dist
            if best is not None and r >= 1 and (best_d < 15 or r * (111.2 / CELL) * cosl > best_d):
                break
        if best is None or best_d > 150:
            return None
        return self.info[best]


def geocode_missing(db, lib, progress=None):
    rows = db.execute("SELECT id, lat, lon FROM media WHERE lat IS NOT NULL AND place IS NULL").fetchall()
    if not rows:
        return 0
    try:
        path = ensure_data(lib, progress)
    except Exception:
        return 0  # offline — try again next scan
    if progress:
        progress.update(message="Looking up place names…")
    gc = Geocoder(path)
    cache, upd = {}, []
    for mid, la, lo in rows:
        k = (round(la, 3), round(lo, 3))
        if k not in cache:
            cache[k] = gc.lookup(la, lo)
        r = cache[k]
        upd.append((r[0], r[1], r[2], r[3], mid) if r else ("", "", "", "", mid))
    db.executemany("UPDATE media SET place=?, admin1=?, country=?, cc=? WHERE id=?", upd)
    db.commit()
    return len(upd)


# ───────────────────────────── trips & events ─────────────────────────────
def _km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(min(1, math.sqrt(h)))


AWAY_KM = 50
MIN_TRIP_ITEMS = 8


def find_trips(rows):
    """rows: (id, taken, lat, lon, place, admin1, country) for every visible, reliably dated item.
    Returns trips and events: dicts with kind, key, name, ids, meta."""
    by_day = defaultdict(list)
    for r in rows:
        by_day[r[1][:10]].append(r)
    if not by_day:
        return []
    days = sorted(by_day)
    mi = lambda d: int(d[:4]) * 12 + int(d[5:7]) - 1
    cell = lambda la, lo: (round(la * CELL), round(lo * CELL))

    # each day's main location cell (from items that have GPS)
    dom = {}
    for d in days:
        c = Counter(cell(r[2], r[3]) for r in by_day[d] if r[2] is not None)
        if c:
            dom[d] = c.most_common(1)[0][0]
    by_month = defaultdict(list)
    for d, c in dom.items():
        by_month[mi(d)].append(c)

    home_cache = {}

    def home_cells(m):
        """Usual places around month m: cells seen on many different days within ±6 months."""
        if m in home_cache:
            return home_cache[m]
        cnt = Counter()
        for k in range(m - 6, m + 7):
            cnt.update(by_month.get(k, ()))
        res = set()
        if cnt:
            top = cnt.most_common(1)[0][1]
            res = {c for c, n in cnt.items() if n >= max(6, 0.35 * top)}
            if not res and top >= 3:
                res = {cnt.most_common(1)[0][0]}
        home_cache[m] = res
        return res

    status = {}
    for d in days:
        pts = [(r[2], r[3]) for r in by_day[d] if r[2] is not None]
        if not pts:
            status[d] = "unknown"
            continue
        homes = home_cells(mi(d))
        if not homes:
            status[d] = "unknown"
            continue
        centers = [(c[0] / CELL, c[1] / CELL) for c in homes]
        far = sum(1 for p in pts if min(_km(p, c) for c in centers) > AWAY_KM)
        status[d] = "away" if far / len(pts) >= 0.6 else "home"

    # group away days (≤3 days apart, no "home" day in between) into trips
    spans, cur = [], None
    for d in days:
        s = status[d]
        if s == "away":
            if cur and (date.fromisoformat(d) - date.fromisoformat(cur[1])).days <= 3:
                cur[1] = d
            else:
                cur = [d, d]
                spans.append(cur)
        elif s == "home":
            cur = None
    home_country = Counter(r[6] for r in rows if r[6]).most_common(1)
    home_country = home_country[0][0] if home_country else ""
    out, in_trip = [], set()
    for a, b in spans:
        items = [r for d in days if a <= d <= b for r in by_day[d]]
        if len(items) < MIN_TRIP_ITEMS:
            continue
        in_trip.update(d for d in days if a <= d <= b)
        centers = [(c[0] / CELL, c[1] / CELL) for c in home_cells(mi(a))]
        out.append(_make_trip(a, b, items, home_country, centers))

    # events: unusually busy days at home
    counts = sorted(len(v) for v in by_day.values())
    median = counts[len(counts) // 2]
    for d in days:
        n = len(by_day[d])
        if d not in in_trip and n >= max(40, 4 * median):
            items = by_day[d]
            places = Counter(r[4] for r in items if r[4])
            p = places.most_common(1)[0][0] if places else ""
            out.append({"kind": "event", "key": f"event:{d}", "name": p or _nice_date(d),
                        "ids": [r[0] for r in items],
                        "meta": {"start": d, "end": d, "days": 1, "places": [p] if p else []}})
    return out


def _nice_date(d):
    m = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return f"{int(d[8:10])} {m[int(d[5:7]) - 1]} {d[:4]}"


def _make_trip(a, b, items, home_country, centers=()):
    # name the trip from the photos taken away from home (not the ones on the way out/back)
    geo = [r for r in items if r[4] and r[2] is not None
           and (not centers or min(_km((r[2], r[3]), c) for c in centers) > AWAY_KM)] or [r for r in items if r[4]]
    places = Counter(r[4] for r in geo)
    states = Counter(r[5] for r in geo if r[5])
    countries = Counter(r[6] for r in geo if r[6])
    n = max(1, len(geo))
    top = places.most_common(3)
    if not top:
        name = "Trip"
    elif top[0][1] / n >= 0.5 or len(top) == 1:
        name = top[0][0]
    elif len(top) >= 2 and (top[0][1] + top[1][1]) / n >= 0.6 and top[1][1] / n >= 0.2:
        name = f"{top[0][0]} & {top[1][0]}"
    elif states and states.most_common(1)[0][1] / n >= 0.6:
        name = states.most_common(1)[0][0]
    else:
        two = [s for s, _ in states.most_common(2)] or [p for p, _ in top[:2]]
        name = " & ".join(two)
    main_country = countries.most_common(1)[0][0] if countries else ""
    if main_country and home_country and main_country != home_country and main_country not in name:
        name = f"{name}, {main_country}"
    days = (date.fromisoformat(b) - date.fromisoformat(a)).days + 1
    ordered = [p for p, _ in Counter(r[4] for r in sorted(geo, key=lambda r: r[1])).most_common()]
    return {"kind": "trip", "key": f"trip:{a}", "name": name, "ids": [r[0] for r in items],
            "meta": {"start": a, "end": b, "days": days, "places": ordered[:8],
                     "states": [s for s, _ in states.most_common(3)], "country": main_country}}


def rebuild_trips(db):
    rows = db.execute("SELECT id, taken, lat, lon, place, admin1, country FROM media "
                      "WHERE dup_of IS NULL AND live_of IS NULL AND ignored IS NULL "
                      "AND date_source != 'file-time' ORDER BY taken").fetchall()
    found = find_trips(rows)
    seen = set()
    now = time.time()
    for t in found:
        seen.add(t["key"])
        row = db.execute("SELECT id, renamed FROM albums WHERE kind=? AND gkey=?", (t["kind"], t["key"])).fetchone()
        if row:
            aid = row[0]
            if row[1]:
                db.execute("UPDATE albums SET meta=? WHERE id=?", (json.dumps(t["meta"]), aid))
            else:
                db.execute("UPDATE albums SET name=?, meta=? WHERE id=?", (t["name"], json.dumps(t["meta"]), aid))
        else:
            aid = db.execute("INSERT INTO albums(name, kind, gkey, created, meta) VALUES(?,?,?,?,?)",
                             (t["name"], t["kind"], t["key"], now, json.dumps(t["meta"]))).lastrowid
        db.execute("DELETE FROM album_items WHERE album_id=?", (aid,))
        db.executemany("INSERT OR IGNORE INTO album_items VALUES(?,?,?)", [(aid, i, now) for i in t["ids"]])
    for aid, key in db.execute("SELECT id, gkey FROM albums WHERE kind IN ('trip','event')").fetchall():
        if key not in seen:
            db.execute("DELETE FROM album_items WHERE album_id=?", (aid,))
            db.execute("DELETE FROM albums WHERE id=?", (aid,))
    db.commit()
    return len(found)
