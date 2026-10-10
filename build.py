#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow==12.3.0"]
# ///
"""Build a Meshtastic MUI map archive: US z15 / Illinois z16 / Chicago metro z18.

Bundle tiles (Oxed's pre-built PMTiles) and locally rendered tiles (TileServer GL, see
docker-compose.yml) are collected into an MBTiles file; go-pmtiles `convert` then turns it
into the PMTiles archive MUI reads from maps/osm-bright/osm-bright.pmtiles.
Normally driven by the Makefile. Sizing and the reasons behind each layer:
plans/2026-10-10-chicago-il-us-tiles.md

    uv run build.py pin                      # pin bundle URLs (work/bundles.json)
    uv run build.py style                    # vendored upstream style -> local style.json
    uv run build.py parity                   # local renderer vs Oxed's server, pixel diff
    uv run build.py bench                    # 1% render sample: tiles/s, ETA, sizes
    uv run build.py fetch [--bbox ...] [--sample F] [--layers ...]
    uv run build.py report [--budget-bytes N]
    uv run build.py verify --archive A       # counts + tier-boundary probes
    uv run build.py card --mount /Volumes/X  # copy the archive to a card, check sha256
    uv run build.py calibrate                # our 8-bit tiles vs the bundles' (quantizer check)
"""
import argparse
import gzip
import hashlib
import io
import json
import math
import os
import random
import shutil
import sqlite3
import statistics
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import Pool

METADATA_URL = "https://bundle.tiles.coalition.space/metadata.json"
OXED_RENDER_URL = "https://tiles.coalition.space/raster/styles/osm-bright/{z}/{x}/{y}.png"
LOCAL_RENDER_URL = "http://127.0.0.1:8080/styles/osm-bright/{z}/{x}/{y}.png"
USER_AGENT = "meshtastic-maps-builder/1.0 (personal offline map build)"

WORK = "work"
SOURCES = os.path.join(WORK, "bundles.json")
DEFAULT_DB = os.path.join(WORK, "tiles.mbtiles")
GO_PMTILES = os.path.join(WORK, "bin", "go-pmtiles")
STYLE_DIR = os.path.join("render", "styles", "osm-bright")

# Pilot B (Oxed's server, 1% sample x100, 2026-10-10): the reference bench compares against
PILOT_B_GB = {"il16": 2.50, "metro17": 1.72, "metro18": 3.99}

# tier-boundary probes for verify: (name, lon, lat, deepest zoom present)
PROBES = [
    ("Denver", -104.9903, 39.7392, 15),
    ("Anchorage", -149.9003, 61.2181, 13),
    ("Springfield IL", -89.6501, 39.7817, 16),
    ("Naperville", -88.1535, 41.7508, 18),
    ("the Loop", -87.6298, 41.8781, 18),
]

# W, S, E, N
METRO = (-88.94, 41.20, -87.52, 42.50)  # CMAP 7 counties, clipped to the Illinois bundle
COOK = (-88.2634, 41.4697, -87.5241, 42.1543)
CITY = (-87.9401, 41.6445, -87.5241, 42.0230)

# layer name -> (kind, zooms)
LAYERS = {
    "world": ("bundle", (0, 9)),
    "us": ("bundle", (10, 13)),
    "states": ("bundle", (14, 15)),
    "il16": ("render", (16, 16)),
    "metro17": ("render", (17, 17)),
    "metro18": ("render", (18, 18)),
}

CHUNK_BYTES = 64 << 20
CHUNK_GAP = 1 << 20
HTTP_ATTEMPTS = 6


# ---------------------------------------------------------------- tile math

def tz_acc(z):
    """Number of tile ids below zoom z (PMTiles v3 Hilbert numbering)."""
    return ((1 << (2 * z)) - 1) // 3


def zxy_to_id(z, x, y):
    n = 1 << z
    d = 0
    s = n >> 1
    while s:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = n - 1 - x
                y = n - 1 - y
            x, y = y, x
        s >>= 1
    return tz_acc(z) + d


def id_to_zxy(tile_id):
    z = 0
    while tz_acc(z + 1) <= tile_id:
        z += 1
    t = tile_id - tz_acc(z)
    n = 1 << z
    x = y = 0
    s = 1
    while s < n:
        rx = 1 & (t // 2)
        ry = 1 & (t ^ rx)
        if ry == 0:
            if rx == 1:
                x = s - 1 - x
                y = s - 1 - y
            x, y = y, x
        x += s * rx
        y += s * ry
        t //= 4
        s *= 2
    return z, x, y


def tile_lon(x, z):
    return x / (1 << z) * 360.0 - 180.0


def tile_lat(y, z):
    return math.degrees(math.atan(math.sinh(math.pi - 2 * math.pi * y / (1 << z))))


def tile_center(z, x, y):
    return tile_lon(x + 0.5, z), tile_lat(y + 0.5, z)


def in_box(lon, lat, box):
    w, s, e, n = box
    return w <= lon <= e and s <= lat <= n


def tile_hits_box(z, x, y, box):
    w, s, e, n = box
    return not (tile_lon(x + 1, z) < w or tile_lon(x, z) > e or tile_lat(y, z) < s or tile_lat(y + 1, z) > n)


def area_of(z, x, y):
    lon, lat = tile_center(z, x, y)
    if in_box(lon, lat, CITY):
        return "city"
    if in_box(lon, lat, COOK):
        return "cook"
    if in_box(lon, lat, METRO):
        return "metro"
    return "other"


# ---------------------------------------------------------------- http

def http_get(url, rng=None):
    headers = {"User-Agent": USER_AGENT}
    if rng:
        headers["Range"] = "bytes=%d-%d" % (rng[0], rng[0] + rng[1] - 1)
    delay = 2
    for attempt in range(HTTP_ATTEMPTS):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
                data = r.read()
            if rng and (r.status != 206 or len(data) != rng[1]):
                raise IOError("range %s of %s: status %s, %d bytes" % (rng, url, r.status, len(data)))
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # for a bundle this means it was rebuilt: re-pin and start a new database
                raise FileNotFoundError("404 for %s" % url)
            err = e
        except (urllib.error.URLError, IOError, TimeoutError) as e:
            err = e
        if attempt == HTTP_ATTEMPTS - 1:
            raise IOError("giving up on %s: %s" % (url, err))
        time.sleep(delay)
        delay *= 2


# ---------------------------------------------------------------- pmtiles reading

def varints(b):
    i = 0
    while i < len(b):
        v = s = 0
        while True:
            c = b[i]
            i += 1
            v |= (c & 0x7F) << s
            s += 7
            if c < 0x80:
                break
        yield v


def parse_dir(raw):
    g = varints(gzip.decompress(raw))
    n = next(g)
    ids, acc = [], 0
    for _ in range(n):
        acc += next(g)
        ids.append(acc)
    run_lengths = [next(g) for _ in range(n)]
    lengths = [next(g) for _ in range(n)]
    offsets = []
    for i in range(n):
        v = next(g)
        offsets.append(offsets[i - 1] + lengths[i - 1] if v == 0 and i > 0 else v - 1)
    return list(zip(ids, run_lengths, lengths, offsets))


class Bundle:
    def __init__(self, url):
        self.url = url
        h = http_get(url, (0, 127))
        if h[:7] != b"PMTiles" or h[7] != 3:
            raise SystemExit("%s is not a PMTiles v3 archive" % url)
        (self.root_off, self.root_len, _, _, self.leaf_off, self.leaf_len,
         self.data_off, _) = struct.unpack("<8Q", h[8:72])
        # internal compression gzip, tile compression none, tile type png: what MUI accepts
        if (h[97], h[98], h[99]) != (2, 1, 2):
            raise SystemExit("%s: unexpected compression/type %s" % (url, tuple(h[97:100])))

    def entries(self, zmin, zmax):
        """[(tile_id, data_offset, length)] for every tile in [zmin, zmax], runs expanded."""
        lo, hi = tz_acc(zmin), tz_acc(zmax + 1)
        leaves = http_get(self.url, (self.leaf_off, self.leaf_len)) if self.leaf_len else b""
        out = []

        def walk(ents):
            for k, (tid, rl, ln, off) in enumerate(ents):
                end = ents[k + 1][0] if k + 1 < len(ents) else hi
                if rl == 0:
                    if tid < hi and end > lo:
                        walk(parse_dir(leaves[off:off + ln]))
                else:
                    for t in range(max(tid, lo), min(tid + rl, hi)):
                        out.append((t, off, ln))

        walk(parse_dir(http_get(self.url, (self.root_off, self.root_len))))
        return out

    def chunks(self, entries):
        """Group the distinct blobs into range reads of at most CHUNK_BYTES."""
        blobs = sorted({(off, ln) for _, off, ln in entries})
        chunk = []
        for off, ln in blobs:
            if chunk and (off > chunk[-1][0] + chunk[-1][1] + CHUNK_GAP
                          or off + ln - chunk[0][0] > CHUNK_BYTES):
                yield chunk
                chunk = []
            chunk.append((off, ln))
        if chunk:
            yield chunk

    def read_chunk(self, chunk):
        start = chunk[0][0]
        end = chunk[-1][0] + chunk[-1][1]
        data = http_get(self.url, (self.data_off + start, end - start))
        return {(off, ln): data[off - start:off - start + ln] for off, ln in chunk}


# ---------------------------------------------------------------- rendering

def quantize(png, method):
    from PIL import Image

    im = Image.open(io.BytesIO(png)).convert("RGB")
    methods = {"mediancut": Image.Quantize.MEDIANCUT, "fastoctree": Image.Quantize.FASTOCTREE}
    q = im.quantize(colors=256, method=methods[method], dither=Image.Dither.NONE)
    buf = io.BytesIO()
    q.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_raw(url, z, x, y):
    raw = http_get(url.format(z=z, x=x, y=y))
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise IOError("not a PNG (%d bytes)" % len(raw))
    return raw


def render_one(args):
    z, x, y, url, method = args
    try:
        return z, x, y, quantize(render_raw(url, z, x, y), method), None
    except Exception as e:  # recorded in the failures table, retried on the next run
        return z, x, y, None, str(e)


# ---------------------------------------------------------------- mbtiles

def open_db(path):
    new = not os.path.exists(path)
    # the bundle and render fetches write concurrently: WAL + a long busy timeout (`seal` undoes WAL)
    db = sqlite3.connect(path, timeout=120)
    if new:
        db.execute("PRAGMA page_size = 65536")
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS tiles (zoom_level INTEGER NOT NULL, tile_column INTEGER NOT NULL,
                                          tile_row INTEGER NOT NULL, tile_data BLOB NOT NULL, h BLOB NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS tile_index ON tiles (zoom_level, tile_column, tile_row);
        CREATE TABLE IF NOT EXISTS done_sources (name TEXT PRIMARY KEY, finished TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS failures (z INTEGER, x INTEGER, y INTEGER, err TEXT, at TEXT,
                                             PRIMARY KEY (z, x, y));
    """)
    meta = {
        "name": "osm-bright: US z15, Illinois z16, Chicago metro z18",
        "format": "png",
        "type": "baselayer",
        "bounds": "-180,-85.0511,180,85.0511",
        "center": "-87.6298,41.8781,12",
        "attribution": "© OpenStreetMap contributors",
        "description": "Meshtastic MUI raster tiles: download.tiles.coalition.space bundles + local TileServer GL renders",
    }
    db.executemany("INSERT OR REPLACE INTO metadata VALUES (?, ?)", meta.items())
    db.commit()
    return db


UPSERT = """INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data, h) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (zoom_level, tile_column, tile_row) DO UPDATE SET tile_data = excluded.tile_data, h = excluded.h
            WHERE length(excluded.tile_data) > length(tiles.tile_data)"""


def row(z, x, y, data):
    # MBTiles rows are TMS: y counts from the south
    return z, x, (1 << z) - 1 - y, data, hashlib.blake2b(data, digest_size=16).digest()


def existing(db, z):
    return {(x, (1 << z) - 1 - r) for x, r in db.execute(
        "SELECT tile_column, tile_row FROM tiles WHERE zoom_level = ?", (z,))}


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S %Z")


def log(msg):
    line = "%s  %s" % (now(), msg)
    print(line, flush=True)
    with open(os.path.join(WORK, "fetch.log"), "a") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- commands

def cmd_pin(args):
    if os.path.exists(SOURCES) and not args.force:
        raise SystemExit("%s exists; bundles are pinned. --force re-pins (use with a new database)." % SOURCES)
    meta = json.loads(http_get(METADATA_URL))
    by_slug = {b["slug"]: b for b in meta}
    states = sorted(b["slug"] for b in meta if b["group"] == "us-state" and b["slug"] != "all-united-states")
    pinned = {}
    for slug in ["world", "all-united-states"] + states:
        b = by_slug[slug]
        req = urllib.request.Request(b["url"], method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as r:
            size = int(r.headers["Content-Length"])
        if size != b["fileSizeBytes"]:
            raise SystemExit("%s: Content-Length %d != metadata %d" % (slug, size, b["fileSizeBytes"]))
        pinned[slug] = {k: b[k] for k in ("url", "fileSizeBytes", "createdAt", "minZoom", "maxZoom", "bbox")}
    os.makedirs(WORK, exist_ok=True)
    with open(SOURCES, "w") as f:
        json.dump(pinned, f, indent=1)
    print("pinned %d bundles (%d states), built %s" % (len(pinned), len(states), pinned["world"]["createdAt"]))


def load_sources():
    with open(SOURCES) as f:
        return json.load(f)


def bundle_layer(db, name, slugs, zooms, box):
    src = load_sources()
    for slug in slugs:
        key = "%s:%s" % (name, slug)
        if db.execute("SELECT 1 FROM done_sources WHERE name = ?", (key,)).fetchone():
            continue
        w, s, e, n = src[slug]["bbox"]
        if box and slug not in ("world", "all-united-states") and (
                e < box[0] or w > box[2] or n < box[1] or s > box[3]):
            continue  # pilot: state does not touch the bbox
        b = Bundle(src[slug]["url"])
        ents = b.entries(*zooms)
        by_blob = {}
        for tid, off, ln in ents:
            z, x, y = id_to_zxy(tid)
            if box and not tile_hits_box(z, x, y, box):
                continue
            by_blob.setdefault((off, ln), []).append((z, x, y))
        sel = [(0, off, ln) for off, ln in by_blob]
        chunks = list(b.chunks(sel))
        tiles = nbytes = 0
        with ThreadPoolExecutor(4) as ex:
            for blobs in ex.map(b.read_chunk, chunks):
                rows = [row(z, x, y, data) for k, data in blobs.items() for z, x, y in by_blob[k]]
                db.executemany(UPSERT, rows)
                db.commit()
                tiles += len(rows)
                nbytes += sum(len(r[3]) for r in rows)
        db.execute("INSERT OR REPLACE INTO done_sources VALUES (?, ?)", (key, now()))
        db.commit()
        log("%-10s %-22s z%d-%d  %9d tiles  %7.1f MB" % (name, slug, zooms[0], zooms[1], tiles, nbytes / 1e6))


def illinois_z15():
    b = Bundle(load_sources()["illinois"]["url"])
    return [id_to_zxy(tid)[1:] for tid, _, _ in b.entries(15, 15)]


def render_targets(name, il15):
    z = LAYERS[name][1][0]
    base = il15 if name == "il16" else [(x, y) for x, y in il15 if in_box(*tile_center(15, x, y), METRO)]
    f = 1 << (z - 15)
    return [(z, x * f + dx, y * f + dy) for x, y in base for dx in range(f) for dy in range(f)]


def render_layer(db, name, il15, box, sample, seed, method, url, workers):
    """Render missing tiles of a layer into db; returns (rendered, failed, bytes, seconds)."""
    targets = render_targets(name, il15)
    if box:
        targets = [t for t in targets if tile_hits_box(*t, box)]
    if sample:
        targets = random.Random(seed).sample(targets, max(1, int(len(targets) * sample)))
    have = existing(db, LAYERS[name][1][0])
    urls = url.split(",")  # several renderers: spread tiles across them round-robin
    todo = [(z, x, y, urls[i % len(urls)], method)
            for i, (z, x, y) in enumerate(t for t in targets if (t[1], t[2]) not in have)]
    log("%-10s %d targets, %d already stored, %d to render" % (name, len(targets), len(targets) - len(todo), len(todo)))
    if not todo:
        return 0, 0, 0, 0.0
    done = failed = nbytes = 0
    t0 = last = time.time()
    batch = []

    def flush():
        db.executemany(UPSERT, batch)
        db.executemany("DELETE FROM failures WHERE z = ? AND x = ? AND y = ?", [r[:3] for r in batch])
        db.commit()
        batch.clear()

    with Pool(workers) as pool:
        for z, x, y, data, err in pool.imap_unordered(render_one, todo, chunksize=8):
            if err:
                failed += 1
                db.execute("INSERT OR REPLACE INTO failures VALUES (?, ?, ?, ?, ?)", (z, x, y, err, now()))
            else:
                batch.append(row(z, x, y, data))
                nbytes += len(data)
                done += 1
            if len(batch) >= 500:
                flush()
            if time.time() - last > 60:
                last = time.time()
                rate = (done + failed) / (last - t0)
                left = len(todo) - done - failed
                log("%-10s %d/%d  %.1f tiles/s  avg %.1f KB  failed %d  ETA %.1f h" % (
                    name, done + failed, len(todo), rate, nbytes / max(done, 1) / 1e3, failed, left / rate / 3600))
    flush()
    el = time.time() - t0
    log("%-10s done: %d rendered, %d failed, %.1f MB, %.1f tiles/s over %.0f s" % (
        name, done, failed, nbytes / 1e6, (done + failed) / el, el))
    return done, failed, nbytes, el


def cmd_fetch(args):
    db = open_db(args.db)
    box = tuple(args.bbox) if args.bbox else None
    layers = args.layers or list(LAYERS)
    src = load_sources()
    states = sorted(s for s in src if s not in ("world", "all-united-states"))
    il15 = None
    for name in layers:
        kind, zooms = LAYERS[name]
        if kind == "bundle":
            if args.sample:
                continue
            slugs = {"world": ["world"], "us": ["all-united-states"], "states": states}[name]
            bundle_layer(db, name, slugs, zooms, box)
        else:
            il15 = il15 or illinois_z15()
            render_layer(db, name, il15, box, args.sample, args.seed, args.quant, args.render_url, args.workers)
    nfail = db.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
    if nfail:
        log("%d tiles failed; re-run fetch to retry them" % nfail)
        sys.exit(1)


def cmd_report(args):
    db = sqlite3.connect(args.db)
    print("zoom      tiles        MB   avg KB")
    for z, n, b in db.execute("SELECT zoom_level, COUNT(*), SUM(length(tile_data)) FROM tiles GROUP BY zoom_level"):
        print("z%-3d %10d %9.1f %8.2f" % (z, n, b / 1e6, b / n / 1e3))
    print("\narea split for z15-18 (by tile centre): city / cook (outside city) / metro (outside cook) / other")
    for z in range(15, 19):
        acc = {}
        for x, r, ln in db.execute(
                "SELECT tile_column, tile_row, length(tile_data) FROM tiles WHERE zoom_level = ?", (z,)):
            a = area_of(z, x, (1 << z) - 1 - r)
            c = acc.setdefault(a, [0, 0])
            c[0] += 1
            c[1] += ln
        print("z%d  " % z + "  ".join("%s %d tiles %.1f MB" % (a, c[0], c[1] / 1e6) for a, c in sorted(acc.items())))
    total, rows = db.execute("SELECT SUM(length(tile_data)), COUNT(*) FROM tiles").fetchone()
    payload, distinct = db.execute(
        "SELECT SUM(l), COUNT(*) FROM (SELECT MAX(length(tile_data)) AS l FROM tiles GROUP BY h)").fetchone()
    nfail = db.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
    print("\nrows %d, raw %.2f GB; distinct blobs %d, payload %.2f GB; failures %d" % (
        rows, total / 1e9, distinct, payload / 1e9, nfail))
    if args.budget_bytes is not None:
        ok = payload <= args.budget_bytes and nfail == 0
        print("fit check: payload %.2f GB vs budget %.2f GB, failures %d -> %s" % (
            payload / 1e9, args.budget_bytes / 1e9, nfail, "OK" if ok else "FAIL"))
        sys.exit(0 if ok else 1)


def cmd_calibrate(args):
    """Quantize renderer tiles and compare them with the bundle's own 8-bit tiles."""
    b = Bundle(load_sources()["illinois"]["url"])
    ents = b.entries(15, 15)
    rnd = random.Random(args.seed)
    metro = [e for e in ents if in_box(*tile_center(*id_to_zxy(e[0])), METRO)]
    rest = [e for e in ents if not in_box(*tile_center(*id_to_zxy(e[0])), METRO)]
    pick = rnd.sample(metro, args.n // 2) + rnd.sample(rest, args.n - args.n // 2)
    blobs = {}
    for chunk in b.chunks(pick):
        blobs.update(b.read_chunk(chunk))
    out = os.path.join(WORK, "calibrate")
    os.makedirs(out, exist_ok=True)
    ratios = {"mediancut": [], "fastoctree": []}
    for tid, off, ln in pick:
        z, x, y = id_to_zxy(tid)
        raw = render_raw(OXED_RENDER_URL, z, x, y)
        ref = blobs[(off, ln)]
        with open(os.path.join(out, "%d_%d_%d_bundle.png" % (z, x, y)), "wb") as f:
            f.write(ref)
        for m in ratios:
            q = quantize(raw, m)
            ratios[m].append(len(q) / len(ref))
            with open(os.path.join(out, "%d_%d_%d_%s.png" % (z, x, y, m)), "wb") as f:
                f.write(q)
    for m, r in ratios.items():
        r.sort()
        print("%-10s ours/bundle size: mean %.3f  median %.3f  min %.3f  max %.3f  (n=%d)" % (
            m, sum(r) / len(r), r[len(r) // 2], r[0], r[-1], len(r)))
    print("samples in", out)


def cmd_style(args):
    """Point the vendored upstream style at the local renderer's data, fonts and sprites."""
    with open(os.path.join(STYLE_DIR, "style.upstream.json")) as f:
        style = json.load(f)
    style["sources"]["openmaptiles"]["url"] = "mbtiles://{illinois}"
    style["glyphs"] = "{fontstack}/{range}.pbf"
    style["sprite"] = "{styleJsonFolder}/sprite"
    with open(os.path.join(STYLE_DIR, "style.json"), "w") as f:
        json.dump(style, f, indent=1, ensure_ascii=False)
        f.write("\n")


# render targets per zoom: Illinois bundle z15 tiles (178,984) and their metro subset (19,436) x 4^(z-15)
RENDER_TARGETS = {16: 715_936, 17: 310_976, 18: 1_243_904}
STAMPS = [".preflight", "osm/us.osm.pbf", "vector/illinois.mbtiles", ".renderer", ".parity.ok",
          ".bundles.done", ".renders.done", ".tiles.done", ".fit.ok", "osm-bright.pmtiles", "verify.txt"]


def cmd_status(args):
    """Build progress from recorded state only (stamps, the tile database, fetch.log); safe mid-build."""
    print("stages: " + "  ".join("%s %s" % ("✓" if os.path.exists(os.path.join(WORK, s)) else "·", s)
                                 for s in STAMPS))
    pending = [s for s in STAMPS if not os.path.exists(os.path.join(WORK, s))]
    read = lambda name: open(os.path.join(WORK, name)).read().splitlines() if os.path.exists(os.path.join(WORK, name)) else []
    cards = [l for l in read("card.log") if "card OK" in l or "MISMATCH" in l]
    published = [l for l in read("publish.log") if "published OK" in l]
    print("build: %s" % ("archive verified (work/osm-bright.pmtiles)" if not pending else "next stage %s" % pending[0]))
    print("cards: %d verified%s" % (sum("card OK" in l for l in cards), "".join("\n  " + l for l in cards)))
    rsyncing = any(len(p.split()) > 1 and p.split()[1] == "rsync" for p in subprocess.run(
        ["ps", "-Ao", "etime=,command="], capture_output=True, text=True).stdout.splitlines())
    print("publish: %s" % ("in progress (see publish progress below)" if rsyncing else
                           published[-1] if published else "not done (`make publish DEST=host:/path`)"))
    procs = subprocess.run(["ps", "-Ao", "etime=,command="], capture_output=True, text=True).stdout.splitlines()
    for p in procs:
        if (("build.py" in p and " status" not in p and "uv run" not in p) or ("go-pmtiles" in p and "go install" not in p)
                or (len(p.split()) > 1 and p.split()[1] in ("rsync", "ssh") and "osm-bright" in p)):
            print("running: %s" % " ".join(p.split())[:140])
    # convert writes its temp file, then the archive; both grow towards the database payload (~58 GB)
    for path in [os.path.join(WORK, "tmp", f) for f in sorted(os.listdir(os.path.join(WORK, "tmp")))
                 ] if os.path.isdir(os.path.join(WORK, "tmp")) else []:
        print("convert temp: %s %.1f GB" % (path, os.path.getsize(path) / 1e9))
    for path in (os.path.join(WORK, "osm-bright.tmp.pmtiles"), os.path.join(WORK, "osm-bright.pmtiles")):
        if os.path.exists(path):
            print("archive: %s %.1f GB" % (path, os.path.getsize(path) / 1e9))
    # card writes: copy progress from the file on the card and cp's elapsed time; then read-back (card.log)
    master = os.path.join(WORK, "osm-bright.pmtiles")
    for p in procs:
        fields = p.split()
        if len(fields) > 2 and fields[1] == "cp" and fields[-1].endswith("osm-bright.pmtiles") and os.path.exists(master):
            done, total, secs = os.path.getsize(fields[-1]), os.path.getsize(master), etime_seconds(fields[0])
            rate = done / max(secs, 1)
            print("card copy: %.1f / %.1f GB (%.0f%%) at %.0f MB/s, ~%.0f min left, then read-back" % (
                done / 1e9, total / 1e9, 100 * done / total, rate / 1e6, (total - done) / max(rate, 1) / 60))
    card_job = any("build.py card" in p for p in procs)
    copying = any(len(p.split()) > 1 and p.split()[1] == "cp" for p in procs)
    if card_job and not copying:
        print("card: not copying, so hashing (master before the copy, or the read-back after it)")
    if card_job:
        print("card log: " + (read("card.log") or ["(none)"])[-1][:120])
    if any(len(p.split()) > 1 and p.split()[1] == "rsync" for p in procs):
        for name in ("publish-progress.log", "make-publish.log"):
            chunks = [c.strip() for c in "\n".join(read(name)).replace("\r", "\n").splitlines() if "%" in c]
            if chunks:
                print("publish progress: " + chunks[-1])
                break
    if os.path.exists(args.db) and not os.path.exists(os.path.join(WORK, ".tiles.done")):  # fetch detail
        db = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True, timeout=30)
        done = {r[0] for r in db.execute("SELECT name FROM done_sources")}
        states = sum(1 for d in done if d.startswith("states:"))
        print("bundles: world %s, all-US %s, states %d/49" % (
            "✓" if "world:world" in done else "·", "✓" if "us:all-united-states" in done else "·", states))
        counts = dict(db.execute("SELECT zoom_level, COUNT(*) FROM tiles WHERE zoom_level >= 16 GROUP BY zoom_level"))
        print("renders: " + "  ".join("z%d %d/%d (%.0f%%)" % (z, counts.get(z, 0), n, 100 * counts.get(z, 0) / n)
                                      for z, n in RENDER_TARGETS.items()))
        print("failures: %d" % db.execute("SELECT COUNT(*) FROM failures").fetchone()[0])
        size = sum(os.path.getsize(p) for p in (args.db, args.db + "-wal") if os.path.exists(p))
        print("database: %.1f GB" % (size / 1e9))
    log_path = os.path.join(WORK, "fetch.log")
    if os.path.exists(log_path) and not os.path.exists(os.path.join(WORK, ".tiles.done")):
        lines = open(log_path).read().splitlines()
        rate = [l for l in lines if "tiles/s" in l and "ETA" in l]
        print("last log: " + lines[-1][:120])
        if rate:
            print("last rate: " + rate[-1][:120])
    st = os.statvfs(".")
    print("disk free: %.0f GB" % (st.f_bavail * st.f_frsize / 1e9))


def cmd_initdb(args):
    """Create the database (and its WAL mode) before concurrent writers start: two processes
    creating it at once race on the journal-mode switch ("database is locked")."""
    open_db(args.db).close()


def cmd_seal(args):
    """Fold the WAL back into the database and leave rollback-journal mode for go-pmtiles."""
    db = sqlite3.connect(args.db, timeout=120)
    busy, wal_pages, moved = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    mode = db.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
    db.close()
    if busy or mode != "delete" or os.path.exists(args.db + "-wal"):
        raise SystemExit("seal failed: busy=%d mode=%s (is a fetch still running?)" % (busy, mode))
    print("sealed %s: checkpointed %d WAL pages, journal_mode=%s" % (args.db, moved, mode))


def write_report(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def pixel_diff(png_a, png_b, threshold=8):
    """Fraction of pixels whose RGB differs by more than threshold in any channel, plus a diff mask."""
    from PIL import Image, ImageChops

    a = Image.open(io.BytesIO(png_a)).convert("RGB")
    b = Image.open(io.BytesIO(png_b)).convert("RGB")
    r, g, bl = ImageChops.difference(a, b).split()
    worst = ImageChops.lighter(ImageChops.lighter(r, g), bl)
    hist = worst.histogram()
    frac = sum(hist[threshold + 1:]) / (a.width * a.height)
    return frac, a, b, worst.point(lambda v: 255 if v > threshold else 0).convert("RGB")


def cmd_parity(args):
    """Local renders vs Oxed's server for the same tiles (structural gate: median <= 2%)."""
    from PIL import Image, ImageDraw

    il15 = illinois_z15()
    pools = {15: [(15, x, y) for x, y in il15], 16: render_targets("il16", il15),
             17: render_targets("metro17", il15), 18: render_targets("metro18", il15)}
    strata = [(z, a) for z in (15, 16) for a in ("city", "cook", "metro", "other")]
    strata += [(z, a) for z in (17, 18) for a in ("city", "cook", "metro")]
    rnd = random.Random(args.seed)
    picks = []
    for z, area in strata:
        pool = [t for t in pools[z] if area_of(*t) == area]
        picks += [(z, area, t) for t in rnd.sample(pool, args.per_stratum)]
    sheet = Image.new("RGB", (3 * 256, 256 * len(picks)), "white")
    lines, fracs = [], []
    for i, (z, area, (tz, x, y)) in enumerate(picks):
        frac, a, b, mask = pixel_diff(render_raw(OXED_RENDER_URL, tz, x, y), render_raw(args.render_url, tz, x, y))
        fracs.append(frac)
        for col, im in enumerate((a, b, mask)):
            sheet.paste(im, (col * 256, i * 256))
        ImageDraw.Draw(sheet).text((4, i * 256 + 4), "%d/%d/%d %s %.1f%%" % (tz, x, y, area, frac * 100), fill="red")
        lines.append("z%d %-5s %d/%d/%d  differing %.2f%%" % (z, area, tz, x, y, frac * 100))
    out = os.path.join(WORK, "parity")
    os.makedirs(out, exist_ok=True)
    sheet.save(os.path.join(out, "sheet.png"))
    med = statistics.median(fracs)
    over = sum(1 for f in fracs if f > 0.10)
    ok = med <= 0.02
    lines += ["", "tiles %d  median %.2f%%  max %.2f%%  over 10%%: %d  (columns: Oxed | local | diff>8)" % (
        len(fracs), med * 100, max(fracs) * 100, over), "parity gate (median <= 2%%): %s" % ("OK" if ok else "FAIL")]
    write_report(os.path.join(out, "report.txt"), lines)
    sys.exit(0 if ok else 1)


def cmd_bench(args):
    """Render the Pilot B sample locally into a scratch db: tiles/s, ETA, projected sizes."""
    path = os.path.join(WORK, "bench.mbtiles")
    if os.path.exists(path):
        os.remove(path)
    db = open_db(path)
    il15 = illinois_z15()
    lines = ["layer     targets  sampled  failed   tiles/s   sample MB  projected GB  Pilot B GB"]
    tiles = secs = failed_all = left = 0
    for name in ("il16", "metro17", "metro18"):
        n = len(render_targets(name, il15))
        done, failed, nbytes, el = render_layer(db, name, il15, None, args.sample, args.seed, args.quant,
                                                args.render_url, args.workers)
        tiles += done + failed
        secs += el
        failed_all += failed
        left += n - done - failed
        lines.append("%-8s %8d %8d %7d %9.1f %11.1f %13.2f %11.2f" % (
            name, n, done, failed, (done + failed) / el, nbytes / 1e6, nbytes / args.sample / 1e9, PILOT_B_GB[name]))
    rate = tiles / secs
    lines += ["", "overall %.1f tiles/s with %d workers; full render of the remaining %d tiles: ~%.1f h" % (
        rate, args.workers, left, left / rate / 3600)]
    write_report(os.path.join(WORK, "bench", "report.txt"), lines)
    sys.exit(1 if failed_all else 0)


def pmtiles_tile(archive, z, x, y):
    r = subprocess.run([GO_PMTILES, "tile", archive, str(z), str(x), str(y)], capture_output=True)
    return r.stdout if r.returncode == 0 else b""


def lonlat_tile(lon, lat, z):
    n = 1 << z
    lr = math.radians(lat)
    return int((lon + 180) / 360 * n), int((1 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2 * n)


def cmd_verify(args):
    """Archive vs MBTiles: header, addressed count, pmtiles verify, tier-boundary probes."""
    lines, ok = [], True
    show = subprocess.run([GO_PMTILES, "show", args.archive], capture_output=True, text=True).stdout
    fields = dict(l.split(": ", 1) for l in show.splitlines() if ": " in l)
    rows = sqlite3.connect(args.db).execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
    expect = {"tile type": "png", "tile compression": "none", "internal compression": "gzip",
              "min zoom": "0", "max zoom": "18", "addressed tiles count": str(rows)}
    for k, v in expect.items():
        good = fields.get(k, "").strip() == v
        ok &= good
        lines.append("%-24s %-12s expected %-12s %s" % (k, fields.get(k, "").strip(), v, "OK" if good else "FAIL"))
    vr = subprocess.run([GO_PMTILES, "verify", args.archive], capture_output=True, text=True)
    ok &= vr.returncode == 0
    lines.append("pmtiles verify: exit %d %s" % (vr.returncode, "OK" if vr.returncode == 0 else "FAIL"))
    for name, lon, lat, deepest in PROBES:
        for z, want in ((deepest, True), (deepest + 1, False)):
            x, y = lonlat_tile(lon, lat, z)
            have = len(pmtiles_tile(args.archive, z, x, y)) > 0
            ok &= have == want
            lines.append("%-15s z%-2d %d/%d %-8s expected %-8s %s" % (
                name, z, x, y, "present" if have else "missing", "present" if want else "missing",
                "OK" if have == want else "FAIL"))
    lines.append("verify: %s" % ("OK" if ok else "FAIL"))
    write_report(os.path.join(WORK, "verify.txt"), lines)
    sys.exit(0 if ok else 1)


def etime_seconds(t):
    """ps etime ([[dd-]hh:]mm:ss) -> seconds."""
    days, _, rest = t.rpartition("-")
    parts = [int(p) for p in rest.split(":")]
    parts = [0] * (3 - len(parts)) + parts
    return (int(days) if days else 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def sha256_file(path, progress=None):
    h = hashlib.sha256()
    total = os.path.getsize(path)
    n = mark = 0
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 << 20), b""):
            h.update(block)
            n += len(block)
            if progress and n - mark >= 5e9:
                mark = n
                progress("read-back %.0f / %.0f GB (%.0f%%)" % (n / 1e9, total / 1e9, 100 * n / total))
    return h.hexdigest()


def card_log(msg):
    line = "%s  %s" % (now(), msg)
    print(line, flush=True)
    with open(os.path.join(WORK, "card.log"), "a") as f:
        f.write(line + "\n")


def cmd_card(args):
    """Copy the archive to a mounted exFAT card and check it by a full read-back."""
    mount, archive = args.mount.rstrip("/"), args.archive
    if not os.path.ismount(mount):
        raise SystemExit("%s is not a mount point" % mount)
    info = subprocess.run(["diskutil", "info", mount], capture_output=True, text=True, check=True).stdout
    field = dict(l.strip().split(":", 1) for l in info.splitlines() if ":" in l)
    if field.get("File System Personality", "").strip() != "ExFAT":
        raise SystemExit("%s is not exFAT" % mount)
    device = field["Device Identifier"].strip()
    maps = os.path.join(mount, "maps")
    style_dir = os.path.join(maps, "osm-bright")
    os.makedirs(style_dir, exist_ok=True)
    extra = [os.path.join("maps", e) for e in os.listdir(maps) if not e.startswith(".") and e != "osm-bright"]
    extra += [os.path.join("maps/osm-bright", e) for e in os.listdir(style_dir)
              if not e.startswith(".") and e != "osm-bright.pmtiles"]
    if extra:
        raise SystemExit("refusing: card has other map data, remove it first: %s" % ", ".join(sorted(extra)))
    dst = os.path.join(style_dir, "osm-bright.pmtiles")
    st = os.statvfs(mount)
    free = st.f_bavail * st.f_frsize + (os.path.getsize(dst) if os.path.exists(dst) else 0)
    size = os.path.getsize(archive)
    if free < size + 500_000_000:
        raise SystemExit("not enough space: %.2f GB free, archive %.2f GB + 0.5 GB margin" % (free / 1e9, size / 1e9))
    card_log("card %s (%s): hashing master %s" % (mount, device, archive))
    master = sha256_file(archive)
    card_log("copying %.2f GB to %s" % (size / 1e9, dst))
    subprocess.run(["cp", "-X", archive, dst], check=True)
    card_log("copy done")
    os.sync()
    for root, _, files in os.walk(maps):
        for f in files:
            if f.startswith("._"):
                os.remove(os.path.join(root, f))
    if os.path.exists(os.path.join(mount, "._maps")):
        os.remove(os.path.join(mount, "._maps"))
    # remount so the read-back comes from the card, not the page cache
    subprocess.run(["diskutil", "unmount", mount], check=True)
    subprocess.run(["diskutil", "mount", device], check=True)
    card_log("remounted; reading back from the card")
    card = sha256_file(dst, progress=card_log)
    card_log("master %s / card %s" % (master, card))
    if card != master:
        card_log("sha256 MISMATCH: do not use this card")
        raise SystemExit(1)
    card_log("card OK")
    if args.eject:
        subprocess.run(["diskutil", "eject", device], check=True)


def cmd_manifest(args):
    """Record the inputs this build used (work/build-manifest.json)."""
    src = load_sources()
    sources_dir = os.path.join(WORK, "planetiler", "sources")
    manifest = {
        "written": now(),
        "bundles": {"count": len(src), "createdAt": sorted({b["createdAt"] for b in src.values()})},
        "osm_extract": {"url": args.osm_url, "md5": open(os.path.join(WORK, "osm", "us.osm.pbf.md5")).read().split()[0],
                        "last_modified": open(os.path.join(WORK, "osm", "us.osm.pbf.last-modified")).read().strip()},
        "planetiler_sources": {f: sha256_file(os.path.join(sources_dir, f)) for f in sorted(os.listdir(sources_dir))
                               if os.path.isfile(os.path.join(sources_dir, f))},
        "images": {"planetiler": args.planetiler_image, "tileserver": args.tileserver_image},
        "fonts": {"url": args.fonts_url, "sha256": args.fonts_sha256},
        "style": open(os.path.join(STYLE_DIR, "SHA256SUMS")).read().splitlines(),
        "go_pmtiles": args.go_pmtiles,
    }
    with open(os.path.join(WORK, "build-manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
        f.write("\n")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("pin")
    s.add_argument("--force", action="store_true")
    for name in ("fetch", "bench"):
        s = sub.add_parser(name)
        if name == "fetch":
            s.add_argument("--db", default=DEFAULT_DB)
            s.add_argument("--layers", nargs="+", choices=list(LAYERS))
            s.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"), help="restrict every layer (pilot)")
        s.add_argument("--sample", type=float, default=None if name == "fetch" else 0.01,
                       help="render only this fraction of each render layer")
        s.add_argument("--seed", type=int, default=1)
        s.add_argument("--quant", choices=["mediancut", "fastoctree"], default="mediancut")
        s.add_argument("--render-url", default=LOCAL_RENDER_URL)
        s.add_argument("--workers", type=int, default=16)
    s = sub.add_parser("report")
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--budget-bytes", type=int)
    s = sub.add_parser("calibrate")
    s.add_argument("--n", type=int, default=40)
    s.add_argument("--seed", type=int, default=1)
    sub.add_parser("style")
    for name in ("seal", "initdb", "status"):
        s = sub.add_parser(name)
        s.add_argument("--db", default=DEFAULT_DB)
    s = sub.add_parser("parity")
    s.add_argument("--render-url", default=LOCAL_RENDER_URL)
    s.add_argument("--per-stratum", type=int, default=4)
    s.add_argument("--seed", type=int, default=1)
    s = sub.add_parser("verify")
    s.add_argument("--archive", default=os.path.join(WORK, "osm-bright.pmtiles"))
    s.add_argument("--db", default=DEFAULT_DB)
    s = sub.add_parser("card")
    s.add_argument("--mount", required=True)
    s.add_argument("--archive", default=os.path.join(WORK, "osm-bright.pmtiles"))
    s.add_argument("--eject", action="store_true")
    s = sub.add_parser("manifest")
    for opt in ("--osm-url", "--planetiler-image", "--tileserver-image", "--fonts-url", "--fonts-sha256", "--go-pmtiles"):
        s.add_argument(opt, required=True)
    args = p.parse_args()
    os.makedirs(WORK, exist_ok=True)
    {"pin": cmd_pin, "fetch": cmd_fetch, "report": cmd_report, "calibrate": cmd_calibrate, "style": cmd_style,
     "seal": cmd_seal, "initdb": cmd_initdb, "status": cmd_status,
     "parity": cmd_parity, "bench": cmd_bench, "verify": cmd_verify, "card": cmd_card,
     "manifest": cmd_manifest}[args.cmd](args)


if __name__ == "__main__":
    main()
