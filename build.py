#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow==12.3.0"]
# ///
"""Build a Meshtastic MUI map archive from nested map areas (areas.toml).

Each tier in areas.toml is a zoomed-out area around the one inside it. Its zoom levels come from
Oxed's pre-built PMTiles bundles where those reach, and from a local TileServer GL renderer (see
docker-compose.yml) beyond them. The tiles are collected into an MBTiles file; go-pmtiles `convert`
then turns it into the PMTiles archive MUI reads from maps/osm-bright/osm-bright.pmtiles.
Normally driven by the Makefile. Sizing and the reasons behind this build's tiers:
docs/DESIGN.md

    uv run build.py bundles [--at=LON,LAT]   # Oxed's bundles: what a tier's `bundles` can name
    uv run build.py pin                      # pin the bundles areas.toml names (work/bundles.json)
    uv run build.py plan                     # areas.toml -> layers, sizes vs the card, extract, bounds, probes
    uv run build.py preflight --heap-gb N    # Docker memory, disk space for this plan
    uv run build.py style                    # vendored upstream style -> local style.json
    uv run build.py parity                   # local renderer vs Oxed's server, pixel diff
    uv run build.py bench                    # 1% render sample into a scratch db: tiles/s, ETA, size
    uv run build.py size-gate                # 1% render sample into the real db: fail if clearly over the card
    uv run build.py fetch [--kind bundle|render] [--layers ...] [--bbox ...] [--sample F]
    uv run build.py report [--fit]
    uv run build.py verify --archive A       # counts + tier-boundary probes
    uv run build.py card --mount /Volumes/X  # copy the archive to a card, check sha256
    uv run build.py calibrate                # our 8-bit tiles vs the bundles' (quantizer check)
"""
import argparse
import bisect
import gzip
import hashlib
import heapq
import io
import json
import math
import os
import random
import re
import shutil
import sqlite3
import statistics
import struct
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from array import array
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import Pool

METADATA_URL = "https://bundle.tiles.coalition.space/metadata.json"
OXED_RENDER_URL = "https://tiles.coalition.space/raster/styles/osm-bright/{z}/{x}/{y}.png"
LOCAL_RENDER_URL = "http://127.0.0.1:8080/styles/osm-bright/{z}/{x}/{y}.png"
USER_AGENT = "meshtastic-maps-builder/1.0 (personal offline map build)"

AREAS = "areas.toml"
WORK = "work"
SOURCES = os.path.join(WORK, "bundles.json")
PLAN_DIR = os.path.join(WORK, "plan")
PLAN = os.path.join(PLAN_DIR, "plan.json")
PROBES = os.path.join(PLAN_DIR, "probes.json")
BUDGET = os.path.join(PLAN_DIR, "budget.txt")
EXTRACT_BYTES = os.path.join(PLAN_DIR, "extract-bytes.txt")
DIR_CACHE = os.path.join(WORK, "cache", "dirs")
DEFAULT_DB = os.path.join(WORK, "tiles.mbtiles")
GO_PMTILES = os.path.join(WORK, "bin", "go-pmtiles")
STYLE_DIR = os.path.join("render", "styles", "osm-bright")

# Planetiler bounds: the rendered tiles' extent plus this margin, rounded outwards to 0.05°
BOUNDS_PAD_KM = 2.0

# Rendered size estimate (docs/DESIGN.md, "Size estimates"; measured on the 2026-10-10 build): a z16-18
# tile's children total RENDER_RATIO[z] x its z15 bundle tile's bytes, but at least 4^d empty tiles of
# BLANK_TILE_BYTES each. Largest error on held-out data: RENDER_ERR.
RENDER_RATIO = {16: 2.727, 17: 7.050, 18: 16.496}
BLANK_TILE_BYTES = 103
RENDER_ERR = 0.16
# render speed of docker-compose.yml's two renderers on an M4 Pro (14 CPUs for Docker)
RENDER_TILES_PER_S = 710
# `card` leaves this much of the card free beyond the archive
CARD_MARGIN = 500_000_000
# Disk peaks: Planetiler's temp files (its own estimate for the US extract: 60 GB, 4.9x the extract),
# then tiles DB + convert temp + archive at the end (~190 GB measured: 3.1x a 57 GB payload + the extract)
PLANETILER_TEMP_PER_EXTRACT = 4.9
DISK_PER_PAYLOAD = 3.1
# the size gate renders this fraction of each render layer into the real database before the fetch
SAMPLE = 0.01
GEOFABRIK_INDEX = "https://download.geofabrik.de/index-v1.json"

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


def boxes_overlap(a, b):
    # Oxed's all-US bbox runs past 180° (E 293), so a shifted copy is tried too
    return any(not (a[2] + k < b[0] or a[0] + k > b[2] or a[3] < b[1] or a[1] > b[3]) for k in (0, -360, 360))


def lonlat_tile(lon, lat, z):
    n = 1 << z
    lr = math.radians(lat)
    return int((lon + 180) / 360 * n), int((1 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2 * n)


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
    """A pinned PMTiles bundle on Oxed's server. Its header and directories are cached in DIR_CACHE
    (a pinned URL names one build, so the cache can't go stale); tile data is read with range requests."""

    def __init__(self, url):
        self.url = url
        cache = os.path.join(DIR_CACHE, url.rsplit("/", 1)[-1] + ".dirs")
        blob = open(cache, "rb").read() if os.path.exists(cache) else None
        h = blob[:127] if blob else http_get(url, (0, 127))
        if h[:7] != b"PMTiles" or h[7] != 3:
            raise SystemExit("%s is not a PMTiles v3 archive" % url)
        (self.root_off, self.root_len, _, _, self.leaf_off, self.leaf_len,
         self.data_off, _) = struct.unpack("<8Q", h[8:72])
        # internal compression gzip, tile compression none, tile type png: what MUI accepts
        if (h[97], h[98], h[99]) != (2, 1, 2):
            raise SystemExit("%s: unexpected compression/type %s" % (url, tuple(h[97:100])))
        if blob is None:
            blob = h + http_get(url, (self.root_off, self.root_len)) + (
                http_get(url, (self.leaf_off, self.leaf_len)) if self.leaf_len else b"")
            os.makedirs(DIR_CACHE, exist_ok=True)
            tmp = "%s.%d.tmp" % (cache, os.getpid())
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, cache)
        if len(blob) != 127 + self.root_len + self.leaf_len:
            raise SystemExit("%s: wrong size for this bundle's directories; delete it" % cache)
        self.root = blob[127:127 + self.root_len]
        self.leaves = blob[127 + self.root_len:]

    def entries(self, zmin, zmax):
        """[(tile_id, data_offset, length)] for every tile in [zmin, zmax], runs expanded."""
        lo, hi = tz_acc(zmin), tz_acc(zmax + 1)
        out = []

        def walk(ents):
            for k, (tid, rl, ln, off) in enumerate(ents):
                end = ents[k + 1][0] if k + 1 < len(ents) else hi
                if rl == 0:
                    if tid < hi and end > lo:
                        walk(parse_dir(self.leaves[off:off + ln]))
                else:
                    for t in range(max(tid, lo), min(tid + rl, hi)):
                        out.append((t, off, ln))

        walk(parse_dir(self.root))
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


# ---------------------------------------------------------------- areas and the plan

def sha256_json(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def load_areas():
    with open(AREAS, "rb") as f:
        cfg = tomllib.load(f)
    tiers = cfg.get("tier") or []

    def bad(msg):
        raise SystemExit("%s: %s" % (AREAS, msg))

    if not tiers:
        bad("no [[tier]] tables")
    if "osm_extract" in cfg and not isinstance(cfg["osm_extract"], str):
        bad("osm_extract must be the URL of an OpenStreetMap .osm.pbf extract (or leave it out: `plan` picks one)")
    if not isinstance(cfg.get("card_free_bytes"), int) or cfg["card_free_bytes"] <= CARD_MARGIN:
        bad("card_free_bytes (the formatted card's free space, in bytes) is missing")
    for i, t in enumerate(tiers):
        name = t.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            bad("tier %d: name must be lowercase letters, digits and '-'" % (i + 1))
        if not isinstance(t.get("max_zoom"), int) or not 0 <= t["max_zoom"] <= 24:
            bad("tier %s: max_zoom must be a whole number 0-24" % name)
        if ("bbox" in t) == ("bundles" in t):
            bad("tier %s: give exactly one of bbox or bundles" % name)
        box = t.get("bbox")
        if box is not None and not (len(box) == 4 and all(isinstance(v, (int, float)) for v in box)
                                    and -180 <= box[0] < box[2] <= 180 and -85.06 <= box[1] < box[3] <= 85.06):
            bad("tier %s: bbox must be [W, S, E, N] with W < E and S < N" % name)
        if "bundles" in t and not (t["bundles"] and all(isinstance(b, str) for b in t["bundles"])):
            bad("tier %s: bundles must be a list of bundle slugs or groups" % name)
    if len({t["name"] for t in tiers}) != len(tiers):
        bad("tier names must be unique")
    for inner, outer in zip(tiers, tiers[1:]):
        if inner["max_zoom"] <= outer["max_zoom"]:
            bad("tiers go innermost first: %s (z%d) must be deeper than %s (z%d)" % (
                inner["name"], inner["max_zoom"], outer["name"], outer["max_zoom"]))
    if "bbox" in tiers[-1]:
        bad("the outermost tier (%s) needs bundles: a bbox tier takes its base tiles from the tier around it"
            % tiers[-1]["name"])
    for p in cfg.get("probes", []):
        if not (isinstance(p.get("name"), str) and isinstance(p.get("zoom"), int)
                and all(isinstance(p.get(k), (int, float)) for k in ("lon", "lat"))):
            bad("each probe needs name, lon, lat and zoom")
    return cfg


def config_sha256(cfg, src):
    """What the plan is derived from: the tiers, the extract and the pins (not probes, not comments)."""
    return sha256_json({"tier": cfg["tier"], "osm_extract": cfg.get("osm_extract"),
                        "pins": {s: [b["url"], b.get("group")] for s, b in src.items()}})


def resolve_bundles(names, src):
    """A tier's `bundles` entries -> pinned slugs; an entry is a slug or an Oxed group."""
    out = []
    for n in names:
        slugs = [n] if n in src else sorted(s for s, b in src.items() if b.get("group") == n)
        if not slugs:
            raise SystemExit("%s: %r is not a pinned bundle or group in %s. `uv run build.py pin --force` "
                             "re-pins every bundle from Oxed's current metadata; then start a new database."
                             % (AREAS, n, SOURCES))
        out += [s for s in slugs if s not in out]
    return out


def bundles_at(src, slugs, z):
    """The tier bundles that supply zoom z: those with the smallest maxZoom that still reach it. For
    "us-state": the all-US bundle to z13 (it has Alaska, and one copy of each border tile), then the states."""
    reach = [s for s in slugs if src[s]["minZoom"] <= z <= src[s]["maxZoom"]]
    return [s for s in reach if src[s]["maxZoom"] == min(src[r]["maxZoom"] for r in reach)]


def derive(cfg, src):
    """Tiers (innermost first) and layers (outermost first, by zoom) from areas.toml and the pins."""
    tiers = []
    for i, t in enumerate(cfg["tier"]):
        outer = cfg["tier"][i + 1] if i + 1 < len(cfg["tier"]) else None
        slugs = resolve_bundles(t["bundles"], src) if "bundles" in t else []
        tiers.append({"name": t["name"], "zooms": [outer["max_zoom"] + 1 if outer else 0, t["max_zoom"]],
                      "bbox": t.get("bbox"), "bundles": slugs, "outer": outer["name"] if outer else None,
                      "base_zoom": max(src[s]["maxZoom"] for s in slugs) if slugs else None})
    for i in range(len(tiers) - 2, -1, -1):  # a bbox tier's base tiles are the next-outer tier's
        if tiers[i]["base_zoom"] is None:
            tiers[i]["base_zoom"] = tiers[i + 1]["base_zoom"]
    layers = []
    for t in reversed(tiers):
        for z in range(t["zooms"][0], t["zooms"][1] + 1):
            use = bundles_at(src, t["bundles"], z)
            if not use:
                layers.append({"tier": t["name"], "kind": "render", "zooms": [z, z]})
            elif layers and layers[-1]["tier"] == t["name"] and layers[-1].get("bundles") == use:
                layers[-1]["zooms"][1] = z
            else:
                layers.append({"tier": t["name"], "kind": "bundle", "zooms": [z, z], "bundles": use})
    for layer in layers:
        a, b = layer["zooms"]
        layer["name"] = "%s-z%d" % (layer["tier"], a) + ("-%d" % b if b != a else "")
    return tiers, layers


def load_plan(strict=True):
    """work/plan/plan.json; strict also checks it was derived from the current areas.toml and pins."""
    if not os.path.exists(PLAN):
        raise SystemExit("no %s: run `make plan` (or `uv run build.py plan`)" % PLAN)
    with open(PLAN) as f:
        plan = json.load(f)
    if strict and plan["config_sha256"] != config_sha256(load_areas(), load_sources()):
        raise SystemExit("%s or %s changed since %s was written: run `make plan`" % (AREAS, SOURCES, PLAN))
    return plan


def load_probes(plan):
    with open(PROBES) as f:
        probes = json.load(f)
    if (probes["config_sha256"] != plan["config_sha256"]
            or probes["probes_sha256"] != sha256_json(load_areas().get("probes", []))):
        raise SystemExit("%s is older than %s: run `make plan`" % (PROBES, AREAS))
    return probes["probes"]


def tier_of(plan, name):
    return next(t for t in plan["tiers"] if t["name"] == name)


_bases = {}


def base_tiles(plan, name, within=None):
    """{(x, y): bundle tile size} at the tier's base zoom. A bundle tier's are its deepest bundles'
    tiles; a bbox tier's are the next-outer tier's base tiles whose centre is inside the bbox."""
    key = (name, tuple(within) if within else None)
    if key not in _bases:
        t = tier_of(plan, name)
        if t["bbox"]:
            box = t["bbox"] if within is None else [max(t["bbox"][0], within[0]), max(t["bbox"][1], within[1]),
                                                    min(t["bbox"][2], within[2]), min(t["bbox"][3], within[3])]
            _bases[key] = base_tiles(plan, t["outer"], box) if box[0] <= box[2] and box[1] <= box[3] else {}
        else:
            src, bz, out = load_sources(), t["base_zoom"], {}
            for slug in t["bundles"]:
                if src[slug]["maxZoom"] != bz or (within and not boxes_overlap(src[slug]["bbox"], within)):
                    continue
                for tid, _, ln in Bundle(src[slug]["url"]).entries(bz, bz):
                    _, x, y = id_to_zxy(tid)
                    if within is None or in_box(*tile_center(bz, x, y), within):
                        out[(x, y)] = max(ln, out.get((x, y), 0))
            _bases[key] = out
    return _bases[key]


def area_at(plan, name, z):
    """A tier's area as tiles at zoom z: its base tiles' descendants, or their ancestors above the base zoom."""
    bz = tier_of(plan, name)["base_zoom"]
    base = base_tiles(plan, name)
    if z >= bz:
        f = 1 << (z - bz)
        return [(z, x * f + dx, y * f + dy) for x, y in base for dx in range(f) for dy in range(f)]
    return sorted({(z, x >> (bz - z), y >> (bz - z)) for x, y in base})


def area_count(plan, name, z):
    bz = tier_of(plan, name)["base_zoom"]
    base = base_tiles(plan, name)
    if z >= bz:
        return len(base) << (2 * (z - bz))
    return len({(x >> (bz - z), y >> (bz - z)) for x, y in base})


def area_extent(plan, name, z):
    """W, S, E, N of a tier's area at zoom z."""
    bz = tier_of(plan, name)["base_zoom"]
    s, zz = max(bz - z, 0), min(z, bz)
    xs = [x >> s for x, _ in base_tiles(plan, name)]
    ys = [y >> s for _, y in base_tiles(plan, name)]
    return tile_lon(min(xs), zz), tile_lat(max(ys) + 1, zz), tile_lon(max(xs) + 1, zz), tile_lat(min(ys), zz)


def planetiler_bounds(w, s, e, n):
    dlat = BOUNDS_PAD_KM / 111.32
    dlon = BOUNDS_PAD_KM / (111.32 * math.cos(math.radians(max(abs(s), abs(n)))))
    return (max(-180.0, math.floor((w - dlon) * 20) / 20), max(-85.05, math.floor((s - dlat) * 20) / 20),
            min(180.0, math.ceil((e + dlon) * 20) / 20), min(85.05, math.ceil((n + dlat) * 20) / 20))


def geofabrik_poly_url(extract_url):
    """Geofabrik publishes each extract's boundary next to it: .../us-latest.osm.pbf -> .../us.poly."""
    m = re.fullmatch(r"(https://download\.geofabrik\.de/.+?)-(?:latest|\d{6})\.osm\.pbf", extract_url)
    return m.group(1) + ".poly" if m else None


def load_poly(url):
    """The rings of a .poly file (holes too: even-odd decides what is inside), cached in work/cache/poly/."""
    path = os.path.join(WORK, "cache", "poly", url.split("://", 1)[1].split("/", 1)[1].replace("/", "_"))
    if not os.path.exists(path):
        data = http_get(url)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
    rings, ring = [], None
    for line in open(path).read().splitlines()[1:]:  # the first line is the region's name
        line = line.strip()
        if not line:
            continue
        if ring is None:
            if line == "END":  # end of file
                break
            ring = []  # a section header: "1", or "!2" for a hole
        elif line == "END":
            rings.append(ring)
            ring = None
        else:
            ring.append(tuple(float(v) for v in line.split()[:2]))
    return rings


def head_bytes(url):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers["Content-Length"])


def geofabrik_regions():
    """Geofabrik's extracts, smallest region first: [(area, id, pbf url, rings, bbox)], from its
    index-v1.json (cached in work/cache/). area is in square degrees scaled by cos(latitude): for ordering."""
    path = os.path.join(WORK, "cache", "geofabrik-index-v1.json")
    if not os.path.exists(path):
        data = http_get(GEOFABRIK_INDEX)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)

    def ring_area(r):
        if not r:
            return 0.0
        cos = math.cos(math.radians(sum(p[1] for p in r) / len(r)))
        return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(r, r[1:] + r[:1]))) / 2 * cos

    out = []
    for f in json.load(open(path))["features"]:
        g, props = f.get("geometry"), f["properties"]
        if not g or "pbf" not in props.get("urls", {}):
            continue
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        rings = [[(p[0], p[1]) for p in ring] for poly in polys for ring in poly if ring]  # holes too (even-odd)
        area = sum(ring_area(poly[0]) - sum(ring_area(h) for h in poly[1:]) for poly in polys if poly)
        if not rings and geofabrik_poly_url(props["urls"]["pbf"]):
            # 19 regions (us/alabama, japan, ...) have empty coordinates in the index: use their .poly
            rings = [r for r in load_poly(geofabrik_poly_url(props["urls"]["pbf"])) if r]
            area = sum(ring_area(r) for r in rings)
        if not rings:
            continue
        lons, lats = [p[0] for r in rings for p in r], [p[1] for r in rings for p in r]
        out.append((area, props["id"], props["urls"]["pbf"], rings, (min(lons), min(lats), max(lons), max(lats))))
    return sorted(out, key=lambda r: r[0])


def covering_extracts(footprint, extent):
    """[(id, url)] of the Geofabrik extracts that wholly contain every rendered tile, smallest region first.
    footprint: {zoom: {(x, y)}}; extent: W, S, E, N of those tiles."""
    return [(rid, url) for _, rid, url, rings, bb in geofabrik_regions()
            if bb[0] <= extent[0] and bb[1] <= extent[1] and bb[2] >= extent[2] and bb[3] >= extent[3]
            and not any(tiles_outside(rings, z, tiles) for z, tiles in footprint.items())]


def tiles_outside(rings, z, tiles, coarse=10):
    """[(x, y)] of the tiles at zoom z that are not wholly inside the polygon. Their z`coarse` parents are
    checked first: the children of a parent that is wholly inside are too."""
    if z > coarse:
        s = z - coarse
        bad = set(_tiles_outside(rings, coarse, {(x >> s, y >> s) for x, y in tiles}))
        tiles = [(x, y) for x, y in tiles if (x >> s, y >> s) in bad]
    return _tiles_outside(rings, z, tiles)


def ids_outside(rings, z, ids, coarse=10):
    """tiles_outside for a sorted array of tile ids at zoom z (a bundle's), decoding only the ids under
    z`coarse` parents that aren't wholly inside: a parent's Hilbert index is its child's >> 2 per level."""
    s, base, cbase = 2 * (z - coarse), tz_acc(z), tz_acc(coarse)
    parents = {id_to_zxy(cbase + d)[1:]: d for d in {(t - base) >> s for t in ids}}
    out = []
    for p in _tiles_outside(rings, coarse, parents):
        d = parents[p]
        lo, hi = bisect.bisect_left(ids, base + (d << s)), bisect.bisect_left(ids, base + ((d + 1) << s))
        out += _tiles_outside(rings, z, [id_to_zxy(t)[1:] for t in ids[lo:hi]])
    return out


def _tiles_outside(rings, z, tiles):
    """[(x, y)] of the tiles at zoom z that are not wholly inside the polygon: an edge crosses them, or
    they lie outside it. Edges are taken as straight lines in tile (Mercator) coordinates."""
    n = 1 << z

    def txy(lon, lat):
        r = math.radians(lat)
        return (lon + 180) / 360 * n, (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n

    segs = [(txy(*a), txy(*b)) for ring in rings for a, b in zip(ring, ring[1:] + ring[:1])]
    crossed = set()
    for (x0, y0), (x1, y1) in segs:  # every tile an edge passes through
        if x0 > x1:
            x0, y0, x1, y1 = x1, y1, x0, y0
        for cx in range(math.floor(x0), math.floor(x1) + 1):
            ta = 0.0 if x1 == x0 else max(0.0, (cx - x0) / (x1 - x0))
            tb = 1.0 if x1 == x0 else min(1.0, (cx + 1 - x0) / (x1 - x0))
            ya, yb = y0 + (y1 - y0) * ta, y0 + (y1 - y0) * tb
            for cy in range(math.floor(min(ya, yb)), math.floor(max(ya, yb)) + 1):
                crossed.add((cx, cy))
    rows, out = {}, []
    for x, y in tiles:
        if (x, y) in crossed:
            out.append((x, y))
            continue
        # no edge in the tile, so its centre decides: count edge crossings to its right along the row
        if y not in rows:
            c = y + 0.5
            rows[y] = sorted(xa + (c - ya) * (xb - xa) / (yb - ya) for (xa, ya), (xb, yb) in segs if (ya <= c) != (yb <= c))
        if (len(rows[y]) - bisect.bisect_right(rows[y], x + 0.5)) % 2 == 0:
            out.append((x, y))
    return out


def bundle_summary(job):
    """Pool worker: (layer, slug, sorted tile ids in the layer's zooms, their sizes)."""
    layer, slug, url, zmin, zmax = job
    ents = Bundle(url).entries(zmin, zmax)
    return layer, slug, array("Q", (t for t, _, _ in ents)), array("L", (ln for _, _, ln in ents))


def merge_distinct(parts):
    """[(ids, sizes)] -> (the distinct ids, their sizes). A tile in several bundles (on a state border)
    is kept once, at its larger size, as the database's UPSERT keeps it."""
    if len(parts) == 1:
        return parts[0]
    out, out_sizes = array("Q"), array("L")
    for t, ln in heapq.merge(*[zip(ids, sizes) for ids, sizes in parts]):
        if out and out[-1] == t:
            out_sizes[-1] = max(out_sizes[-1], ln)
        else:
            out.append(t)
            out_sizes.append(ln)
    return out, out_sizes


def write_if_changed(path, text):
    """Leave an unchanged file alone, so make doesn't rebuild what depends on it."""
    if os.path.exists(path) and open(path).read() == text:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.replace(path + ".tmp", path)


def verdict(exact, estimate, unknown, budget):
    """Sizes vs the card budget: fits / may not fit / over, from exact bytes plus estimated bytes
    (+-RENDER_ERR), or unknown when a rendered zoom has no estimate."""
    if unknown:
        return "unknown"
    low, high = exact + estimate * (1 - RENDER_ERR), exact + estimate * (1 + RENDER_ERR)
    return "fits" if high <= budget else "over" if low > budget else "may not fit"


def cmd_plan(args):
    """Derive layers, sizes, the extract, Planetiler bounds and probes from areas.toml; check them; write
    work/plan/. Sizes are compared with the card but never fail the plan: `size-gate` measures them."""
    cfg, src = load_areas(), load_sources()
    tiers, layers = derive(cfg, src)
    plan = {"config_sha256": config_sha256(cfg, src), "tiers": tiers, "layers": layers}
    budget = cfg["card_free_bytes"] - CARD_MARGIN
    if not any(l["kind"] == "render" for l in layers):
        raise SystemExit("%s: no tier goes deeper than its bundles, so there is nothing to render" % AREAS)

    # bundle zooms: every tile id and size, from the bundle directories (cached; no tile data is read)
    jobs = [(l["name"], s, src[s]["url"], *l["zooms"]) for l in layers if l["kind"] == "bundle" for s in l["bundles"]]
    with Pool(min(8, len(jobs))) as pool:
        got = pool.map(bundle_summary, jobs)
    per_zoom = {}  # (slug, z) -> (ids, sizes)
    for (_, slug, _, zmin, zmax), (_, _, ids, sizes) in zip(jobs, got):
        for z in range(zmin, zmax + 1):
            lo, hi = bisect.bisect_left(ids, tz_acc(z)), bisect.bisect_left(ids, tz_acc(z + 1))
            per_zoom[(slug, z)] = (ids[lo:hi], sizes[lo:hi])
    merged = {}

    def bundle_zoom(slugs, z):
        """(distinct ids, sizes) of these bundles' tiles at zoom z"""
        if (tuple(slugs), z) not in merged:
            for s in slugs:
                if (s, z) not in per_zoom:
                    ents = Bundle(src[s]["url"]).entries(z, z)
                    per_zoom[(s, z)] = (array("Q", (t for t, _, _ in ents)), array("L", (ln for _, _, ln in ents)))
            merged[(tuple(slugs), z)] = merge_distinct([per_zoom[(s, z)] for s in slugs])
        return merged[(tuple(slugs), z)]

    sized = {}

    def zoom_size(t, z):
        """(tiles, bytes, kind) of tier t's area at zoom z. kind: exact (from bundles), estimate (rendered),
        or why there is no estimate (bytes None)."""
        if (t["name"], z) not in sized:
            use, bz = bundles_at(src, t["bundles"], z), t["base_zoom"]
            if use:
                ids, sizes = bundle_zoom(use, z)
                sized[(t["name"], z)] = (len(ids), sum(sizes), "exact")
            else:
                lens = (list(base_tiles(plan, t["name"]).values()) if t["bbox"]
                        else bundle_zoom(bundles_at(src, t["bundles"], bz), bz)[1])
                n = len(lens) << 2 * (z - bz) if z >= bz else area_count(plan, t["name"], z)
                if bz == 15 and z in RENDER_RATIO:
                    d, r = z - 15, RENDER_RATIO[z]
                    sized[(t["name"], z)] = (n, round(sum(max(r * ln, 4 ** d * BLANK_TILE_BYTES) for ln in lens)), "estimate")
                elif bz == 15 and z == 15:  # a bbox tier at its base zoom renders like the bundle tiles (parity)
                    sized[(t["name"], z)] = (n, sum(lens), "estimate")
                else:
                    sized[(t["name"], z)] = (n, None, "no estimate past z18" if z > 18 else
                                             "no estimate from a z%d base" % bz if bz != 15 else "no estimate below z15")
        return sized[(t["name"], z)]

    counts = {}
    for layer in layers:
        a, b = layer["zooms"]
        t = tier_of(plan, layer["tier"])
        if layer["kind"] == "render":
            layer["base_tiles"] = len(base_tiles(plan, t["name"]))
        zs = [zoom_size(t, z) for z in range(a, b + 1)]
        counts.update({z: n for z, (n, _, _) in zip(range(a, b + 1), zs)})
        layer["tiles"] = sum(n for n, _, _ in zs)
        kinds = [k for _, _, k in zs]
        layer["size"] = next((k for k in kinds if k not in ("exact", "estimate")), "estimate" if "estimate" in kinds else "exact")
        layer["bytes"] = sum(by for _, by, _ in zs) if layer["size"] in ("exact", "estimate") else None
    plan["counts"] = {str(z): n for z, n in sorted(counts.items())}
    exact = sum(l["bytes"] for l in layers if l["size"] == "exact")
    estimate = sum(l["bytes"] for l in layers if l["size"] == "estimate")
    unknown = [l["name"] for l in layers if l["bytes"] is None]
    plan["size"] = {"exact": exact, "estimate": estimate, "unknown": unknown,
                    "low": round(exact + estimate * (1 - RENDER_ERR)), "high": round(exact + estimate * (1 + RENDER_ERR))}

    parents = {}

    def has(z, x, y):
        """Is tile z/x/y in the plan?"""
        layer = next((l for l in layers if l["zooms"][0] <= z <= l["zooms"][1]), None)
        if layer is None or not (0 <= x < 1 << z and 0 <= y < 1 << z):
            return False
        if layer["kind"] == "bundle":
            a, tid = bundle_zoom(layer["bundles"], z)[0], zxy_to_id(z, x, y)
            i = bisect.bisect_left(a, tid)
            return i < len(a) and a[i] == tid
        bz = tier_of(plan, layer["tier"])["base_zoom"]
        if z >= bz:
            return (x >> (z - bz), y >> (z - bz)) in base_tiles(plan, layer["tier"])
        if (layer["tier"], z) not in parents:
            parents[(layer["tier"], z)] = {(px, py) for _, px, py in area_at(plan, layer["tier"], z)}
        return (x, y) in parents[(layer["tier"], z)]

    empty = ["tier %s has no tiles at z%d" % (t["name"], t["zooms"][1]) for t in tiers if not counts[t["zooms"][1]]]
    if empty:
        raise SystemExit("plan FAIL:\n  " + "\n  ".join(empty + ["(a bbox tier's box must overlap the tier around it)"]))
    # nesting: a tier's tiles at its first zoom need their parents in the tier around it (MUI never
    # upscales, so a missing parent is a blank tile). A tier in the wrong place misses most of them; a
    # few are expected where an outer bundle leaves out empty ocean (Oxed's world bundle has 60,581 of
    # the 262,144 z9 tiles, and 3 that all-US z10 tiles sit under are among the missing). Over 1% fails.
    problems, warnings = [], []
    for t in tiers:
        if not t["outer"]:
            continue
        fz = t["zooms"][0]
        first = next(l for l in layers if l["tier"] == t["name"] and l["zooms"][0] == fz)
        if first["kind"] == "bundle":
            tops = {(x >> 1, y >> 1) for _, x, y in map(id_to_zxy, bundle_zoom(first["bundles"], fz)[0])}
        else:
            tops = {(x, y) for _, x, y in area_at(plan, t["name"], fz - 1)}
        gaps = [(x, y) for x, y in tops if not has(fz - 1, x, y)]
        if gaps:
            lon, lat = tile_center(fz - 1, *gaps[0])
            (problems if len(gaps) > len(tops) / 100 else warnings).append(
                "tier %s: %d of its %d parent tiles at z%d are not in %s, so the map is blank at z%d there "
                "(e.g. near %.4f,%.4f)" % (t["name"], len(gaps), len(tops), fz - 1, t["outer"], fz - 1, lon, lat))

    rendered = [area_extent(plan, l["tier"], l["zooms"][0]) for l in layers if l["kind"] == "render"]
    extent = (min(r[0] for r in rendered), min(r[1] for r in rendered),
              max(r[2] for r in rendered), max(r[3] for r in rendered))
    plan["render_extent"] = [round(v, 4) for v in extent]
    plan["bounds"] = [round(v, 2) for v in planetiler_bounds(*extent)]

    # the extract must have data under every rendered tile: a tile it covers only partly renders blank
    # where the data stops (seen with a Rhode Island extract: RI's border tiles were empty in Massachusetts)
    footprint = {}
    for layer in layers:
        if layer["kind"] == "render":
            z, bz = layer["zooms"][0], tier_of(plan, layer["tier"])["base_zoom"]
            footprint.setdefault(min(z, bz), set()).update(
                base_tiles(plan, layer["tier"]) if z >= bz else {(x, y) for _, x, y in area_at(plan, layer["tier"], z)})
    given, extract, rings = cfg.get("osm_extract"), None, None
    if given and not geofabrik_poly_url(given):
        extract, how = given, "given; not a Geofabrik download, so coverage is not checked"
        warnings.append("extract coverage not checked: osm_extract is not a Geofabrik download, so there is no "
                        ".poly boundary to check the rendered tiles against")
    elif given:
        rings = load_poly(geofabrik_poly_url(given))
        out = [(z, xy) for z, tiles in sorted(footprint.items()) for xy in tiles_outside(rings, z, tiles)]
        if out:
            cands = covering_extracts(footprint, extent)
            lon, lat = tile_center(out[0][0], *out[0][1])
            problems.append("osm_extract doesn't cover the rendered area: %d rendered tiles are not wholly inside "
                            "%s (e.g. near %.4f,%.4f). %s" % (len(out), geofabrik_poly_url(given), lon, lat,
                            "The smallest Geofabrik extract that covers them: %s" % cands[0][1] if cands
                            else "No single Geofabrik extract covers them."))
        else:
            extract, how = given, "given; covers every rendered tile"
    else:
        cands = covering_extracts(footprint, extent)
        if cands:
            extract, how = cands[0][1], "picked: the smallest Geofabrik extract that covers every rendered tile"
            rings = load_poly(geofabrik_poly_url(extract))
        else:
            problems.append("no single Geofabrik extract covers every rendered tile: set osm_extract (coverage of "
                            "a non-Geofabrik extract is not checked)")
    plan["osm_extract"] = extract
    extract_bytes = None
    if extract:
        try:
            extract_bytes = head_bytes(extract)
        except (urllib.error.URLError, OSError) as e:
            warnings.append("couldn't get the extract's size (%s); `make` needs it for the disk check" % e)

    # one zoom deeper / shallower per tier: what each costs, and whether the result fits the card
    whatif = []
    for i, t in enumerate(tiers):
        m, inner, outer = t["zooms"][1], tiers[i - 1] if i else None, tiers[i + 1] if i + 1 < len(tiers) else None
        row = []
        for zoom, gain, lose, blocker in ((m + 1, t, inner, inner if inner and m + 1 >= inner["zooms"][1] else None),
                                          (m - 1, inner, t, outer if outer and m - 1 <= outer["zooms"][1] else None)):
            if blocker or zoom < 0:
                row.append("n/a: %s is z%d" % (blocker["name"], blocker["zooms"][1]) if blocker else "n/a")
                continue
            z = max(zoom, m)  # the zoom that changes hands: m+1 when deeper, m when shallower
            parts = [(+1, zoom_size(gain, z)) if gain else None, (-1, zoom_size(lose, z)) if lose else None]
            reason = next((s[2] for p in parts if p for s in [p[1]] if s[1] is None), None)
            if reason:
                row.append("z%d: %s" % (zoom, reason))
                continue
            d_exact = sum(sign * s[1] for p in parts if p for sign, s in [p] if s[2] == "exact")
            d_est = sum(sign * s[1] for p in parts if p for sign, s in [p] if s[2] == "estimate")
            v = verdict(exact + d_exact, estimate + d_est, unknown, budget)
            flag = ""
            if rings and zoom > m and zoom_size(t, zoom)[2] == "estimate" and not any(
                    l["tier"] == t["name"] and l["kind"] == "render" for l in layers):
                # t starts rendering: its base must be inside the extract too
                bz = t["base_zoom"]
                outside = (tiles_outside(rings, bz, base_tiles(plan, t["name"])) if t["bbox"]
                           else ids_outside(rings, bz, bundle_zoom(bundles_at(src, t["bundles"], bz), bz)[0]))
                flag = ", beyond the extract" if outside else ""
            row.append("z%d: %+.2f GB%s -> %s%s" % (zoom, (d_exact + d_est) / 1e9, " ≈" if d_est else "", v, flag))
        whatif.append((t["name"], m, row))

    inner = tiers[0]
    c = area_extent(plan, inner["name"], inner["zooms"][0])
    plan["metadata"] = {"name": "osm-bright: " + ", ".join("%s z%d" % (t["name"], t["zooms"][1]) for t in tiers),
                        "center": "%.4f,%.4f,%d" % ((c[0] + c[2]) / 2, (c[1] + c[3]) / 2, inner["zooms"][0])}
    plan["warnings"] = warnings
    plan["layers_sha256"] = sha256_json({"tiers": tiers, "layers": [{k: l.get(k) for k in ("name", "kind", "zooms", "bundles")}
                                                                    for l in layers],
                                         "pins": {s: src[s]["url"] for t in tiers for s in t["bundles"]}})

    # probes: the ones in areas.toml, then a sampled point per tier (inside it, present at its max_zoom,
    # and with no tile one zoom deeper)
    probes = []
    for p in cfg.get("probes", []):
        z = p["zoom"]
        ok = has(z, *lonlat_tile(p["lon"], p["lat"], z)) and not has(z + 1, *lonlat_tile(p["lon"], p["lat"], z + 1))
        deepest = next((d - 1 for d in range(0, 26) if not has(d, *lonlat_tile(p["lon"], p["lat"], d))), None)
        probes.append(dict(p, ok=ok, note="" if ok else "present down to z%d in this plan; edit or delete this probe in %s"
                           % (deepest, AREAS)))
    rnd = random.Random(0)
    for t in tiers:
        m = t["zooms"][1]
        layer = next(l for l in layers if l["tier"] == t["name"] and l["zooms"][0] <= m <= l["zooms"][1])
        if layer["kind"] == "bundle":
            a = bundle_zoom(layer["bundles"], m)[0]
        else:
            keys, bz = list(base_tiles(plan, t["name"])), t["base_zoom"]
        for _ in range(500):
            if layer["kind"] == "bundle":
                _, x, y = id_to_zxy(a[rnd.randrange(len(a))])
            else:
                bx, by = keys[rnd.randrange(len(keys))]
                f = 1 << max(m - bz, 0)
                x, y = (bx * f + rnd.randrange(f), by * f + rnd.randrange(f)) if m >= bz else (bx >> (bz - m), by >> (bz - m))
            if not any(has(m + 1, 2 * x + dx, 2 * y + dy) for dx in (0, 1) for dy in (0, 1)):
                lon, lat = tile_center(m, x, y)
                probes.append({"name": "%s (sampled)" % t["name"], "lon": round(lon, 5), "lat": round(lat, 5),
                               "zoom": m, "ok": True, "note": ""})
                break
        else:
            problems.append("tier %s: no point found that is present at z%d and missing at z%d" % (t["name"], m, m + 1))

    gb = lambda v: "%.2f" % (v / 1e9)
    row_fmt = "%-22s %-6s %-36s %11s %9s"
    print(row_fmt % ("layer", "zooms", "source", "tiles", "GB"))
    for layer in layers:
        a, b = layer["zooms"]
        if layer["kind"] == "bundle":
            n = len(layer["bundles"])
            source = ", ".join(layer["bundles"]) if n <= 2 else "%d bundles (%s ... %s)" % (n, layer["bundles"][0], layer["bundles"][-1])
        else:
            source = "render from {:,} z{} base tiles".format(layer["base_tiles"], tier_of(plan, layer["tier"])["base_zoom"])
        print(row_fmt % (layer["name"], "%d-%d" % (a, b) if a != b else a, source[:36], "{:,}".format(layer["tiles"]),
                         "?" if layer["bytes"] is None else ("≈" if layer["size"] == "estimate" else "") + gb(layer["bytes"])))
    print(row_fmt % ("total", "0-%d" % tiers[0]["zooms"][1], "", "{:,}".format(sum(counts.values())),
                     ("≈" if estimate else "") + gb(exact + estimate) + ("+?" if unknown else "")))
    print("\ncard: %s GB free - %s GB margin = %s GB budget" % (gb(cfg["card_free_bytes"]), gb(CARD_MARGIN), gb(budget)))
    v = verdict(exact, estimate, unknown, budget)
    print("size: %s GB from bundles (exact) + ≈%s GB rendered (±%d%%) = %s-%s GB -> %s" % (
        gb(exact), gb(estimate), RENDER_ERR * 100, gb(plan["size"]["low"]), gb(plan["size"]["high"]),
        {"fits": "fits, at least %s GB spare" % gb(budget - plan["size"]["high"]),
         "may not fit": "may not fit: `make bench` measures it, and the size gate before the fetch will",
         "over": "over by at least %s GB: see the shallower options below" % gb(plan["size"]["low"] - budget),
         "unknown": "unknown: %s has no estimate; `make bench` measures it" % ", ".join(unknown)}[v]))
    print("\none zoom deeper / shallower per tier (≈: estimated):")
    for name, m, (deeper, shallower) in whatif:
        print("  %-16s z%-3d deeper %-44s shallower %s" % (name, m, deeper, shallower))
    rendered_tiles = sum(l["tiles"] for l in layers if l["kind"] == "render")
    print("\nosm_extract: %s (%s GB; %s)" % (extract, gb(extract_bytes) if extract_bytes else "size unknown", how)
          if extract else "\nosm_extract: %s" % ("%s doesn't cover the rendered area (below)" % given if given else "none found"))
    print("downloads: bundles %s GB + extract %s GB" % (gb(exact), gb(extract_bytes) if extract_bytes else "?"))
    secs = rendered_tiles / RENDER_TILES_PER_S
    print("renders {:,} tiles: ~{} at {} tiles/s (two renderers on an M4 Pro; `make bench` measures this machine)".format(
        rendered_tiles, "%.1f h" % (secs / 3600) if secs >= 3600 else "%.0f min" % max(secs / 60, 1), RENDER_TILES_PER_S))
    print("rendered extent W %.4f / S %.4f / E %.4f / N %.4f; Planetiler bounds %s (+%.0f km, rounded out to 0.05°)" % (
        *extent, ",".join("%.2f" % v for v in plan["bounds"]), BOUNDS_PAD_KM))
    print("probes (present at zoom, missing one deeper):")
    for p in probes:
        print("  %-24s z%-2d %10.5f,%-9.5f %s %s" % (p["name"], p["zoom"], p["lon"], p["lat"], "OK" if p["ok"] else "FAIL", p["note"]))
    for w in warnings:
        print("warning: " + w)
    problems += ["probe %s: expected present at z%d and missing at z%d; %s" % (p["name"], p["zoom"], p["zoom"] + 1, p["note"])
                 for p in probes if not p["ok"]]
    if problems:
        raise SystemExit("plan FAIL:\n  " + "\n  ".join(problems))
    write_if_changed(PLAN, json.dumps(plan, indent=1) + "\n")
    write_if_changed(PROBES, json.dumps({"config_sha256": plan["config_sha256"],
                                         "probes_sha256": sha256_json(cfg.get("probes", [])),
                                         "probes": [{k: p[k] for k in ("name", "lon", "lat", "zoom")} for p in probes]},
                                        indent=1) + "\n")
    write_if_changed(os.path.join(PLAN_DIR, "bounds.txt"), ",".join("%.2f" % v for v in plan["bounds"]) + "\n")
    write_if_changed(os.path.join(PLAN_DIR, "osm-url.txt"), extract + "\n")
    # kept out of plan.json: a new card or a new day's extract size re-runs only what compares with them
    write_if_changed(BUDGET, "%d\n" % budget)
    if extract_bytes:
        write_if_changed(EXTRACT_BYTES, "%d\n" % extract_bytes)
    print("plan OK: %s" % PLAN_DIR)


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

def open_db(path, plan):
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
        CREATE TABLE IF NOT EXISTS targets (layer TEXT PRIMARY KEY, zoom INTEGER NOT NULL, tiles INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS failures (z INTEGER, x INTEGER, y INTEGER, err TEXT, at TEXT,
                                             PRIMARY KEY (z, x, y));
    """)
    # a database holds one plan's tiles: refuse to mix in another's (a database from before plans were
    # recorded has no key, and is adopted)
    built_for = db.execute("SELECT value FROM metadata WHERE name = 'plan_sha256'").fetchone()
    if built_for and built_for[0] != plan["layers_sha256"] and db.execute("SELECT 1 FROM tiles LIMIT 1").fetchone():
        raise SystemExit("%s holds tiles for a different areas.toml (or bundle pins). Start a new database:\n"
                         "  rm -f %s*; rm -f work/.size.ok work/.bundles.done work/.renders.done work/.tiles.done" % (path, path))
    meta = {
        "name": plan["metadata"]["name"],
        "format": "png",
        "type": "baselayer",
        "bounds": "-180,-85.0511,180,85.0511",
        "center": plan["metadata"]["center"],
        "attribution": "© OpenStreetMap contributors",
        "description": "Meshtastic MUI raster tiles: download.tiles.coalition.space bundles + local TileServer GL renders",
        "plan_sha256": plan["layers_sha256"],
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
    """Pin the Oxed bundles areas.toml names. A name that already resolves against the pins is left
    alone, so a resumed build's plan never shifts; others are pinned from Oxed's current metadata (fetched
    only then, so re-planning works offline). --force re-pins everything."""
    names = sorted({n for t in load_areas()["tier"] for n in t.get("bundles", [])})
    pinned = {} if args.force or not os.path.exists(SOURCES) else load_sources()
    missing = [n for n in names if n not in pinned and not any(b.get("group") == n for b in pinned.values())]
    if not missing:
        print("pins: every bundle name in %s resolves (%d bundles pinned)" % (AREAS, len(pinned)))
        return
    meta = json.loads(http_get(METADATA_URL))
    by_slug = {b["slug"]: b for b in meta}
    new = set()
    for n in missing:
        members = [n] if n in by_slug else [b["slug"] for b in meta if b["group"] == n]
        if not members:
            raise SystemExit("%s: %r is not a bundle slug or group in %s (`make bundles` lists them)" % (AREAS, n, METADATA_URL))
        new.update(m for m in members if m not in pinned)
    for slug in sorted(new):
        b = by_slug[slug]
        if head_bytes(b["url"]) != b["fileSizeBytes"]:
            raise SystemExit("%s: the download's size differs from Oxed's metadata (%d B)" % (slug, b["fileSizeBytes"]))
        pinned[slug] = {k: b[k] for k in ("url", "fileSizeBytes", "createdAt", "minZoom", "maxZoom", "bbox", "group")}
    write_if_changed(SOURCES, json.dumps(pinned, indent=1))
    print("pinned %d new bundle%s for %s (built %s); %d pinned in all" % (
        len(new), "" if len(new) == 1 else "s", ", ".join(missing), ", ".join(sorted({by_slug[s]["createdAt"] for s in new})), len(pinned)))


def load_sources():
    with open(SOURCES) as f:
        return json.load(f)


def bundle_layer(db, layer, box):
    src = load_sources()
    zooms = layer["zooms"]
    for slug in layer["bundles"]:
        key = "%s:%s" % (layer["name"], slug)
        if db.execute("SELECT 1 FROM done_sources WHERE name = ?", (key,)).fetchone():
            continue
        if box and not boxes_overlap(src[slug]["bbox"], box):
            continue  # pilot: bundle does not touch the bbox
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
        log("%-18s %-22s z%d-%d  %9d tiles  %7.1f MB" % (layer["name"], slug, zooms[0], zooms[1], tiles, nbytes / 1e6))


def layer_targets(plan, layer, box, sample, seed):
    """The tiles of a render layer, optionally only those touching box, or a seeded random sample."""
    targets = area_at(plan, layer["tier"], layer["zooms"][0])
    if box:
        targets = [t for t in targets if tile_hits_box(*t, box)]
    if sample:
        targets = random.Random(seed).sample(targets, max(1, int(len(targets) * sample)))
    return targets


def render_layer(db, plan, layer, box, sample, seed, method, url, workers):
    """Render missing tiles of a layer into db; returns (rendered, failed, bytes, seconds)."""
    name, z = layer["name"], layer["zooms"][0]
    targets = layer_targets(plan, layer, box, sample, seed)
    if not box and not sample:  # the full layer: `status` reports progress against this
        db.execute("INSERT OR REPLACE INTO targets VALUES (?, ?, ?)", (name, z, len(targets)))
        db.commit()
    have = existing(db, z)
    urls = url.split(",")  # several renderers: spread tiles across them round-robin
    todo = [(z, x, y, urls[i % len(urls)], method)
            for i, (z, x, y) in enumerate(t for t in targets if (t[1], t[2]) not in have)]
    log("%-18s %d targets, %d already stored, %d to render" % (name, len(targets), len(targets) - len(todo), len(todo)))
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
                log("%-18s %d/%d  %.1f tiles/s  avg %.1f KB  failed %d  ETA %.1f h" % (
                    name, done + failed, len(todo), rate, nbytes / max(done, 1) / 1e3, failed, left / rate / 3600))
    flush()
    el = time.time() - t0
    log("%-18s done: %d rendered, %d failed, %.1f MB, %.1f tiles/s over %.0f s" % (
        name, done, failed, nbytes / 1e6, (done + failed) / el, el))
    return done, failed, nbytes, el


def cmd_fetch(args):
    plan = load_plan()
    db = open_db(args.db, plan)
    box = tuple(args.bbox) if args.bbox else None
    unknown = set(args.layers or []) - {l["name"] for l in plan["layers"]}
    if unknown:
        raise SystemExit("no such layer in the plan: %s (have: %s)" % (
            ", ".join(sorted(unknown)), ", ".join(l["name"] for l in plan["layers"])))
    for layer in plan["layers"]:
        if (args.kind and layer["kind"] != args.kind) or (args.layers and layer["name"] not in args.layers):
            continue
        if layer["kind"] == "bundle":
            if not args.sample:
                bundle_layer(db, layer, box)
        else:
            render_layer(db, plan, layer, box, args.sample, args.seed, args.quant, args.render_url, args.workers)
    nfail = db.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
    if nfail:
        log("%d tiles failed; re-run fetch to retry them" % nfail)
        sys.exit(1)


def load_budget():
    """The card budget `plan` wrote: card_free_bytes minus CARD_MARGIN."""
    if not os.path.exists(BUDGET):
        raise SystemExit("no %s: run `make plan`" % BUDGET)
    return int(open(BUDGET).read())


def cmd_report(args):
    """Per-zoom rows against the plan, rendered bytes against the plan's estimate, payload, failures.
    With --fit it is the fit gate (payload vs the card budget, rows vs the plan)."""
    plan = load_plan()
    db = sqlite3.connect(args.db)
    stored = {z: (n, b) for z, n, b in db.execute(
        "SELECT zoom_level, COUNT(*), SUM(length(tile_data)) FROM tiles GROUP BY zoom_level")}
    planned = {int(z): n for z, n in plan["counts"].items()}
    differ = []
    print("zoom      tiles    planned        MB   avg KB")
    for z in sorted(set(stored) | set(planned)):
        n, b = stored.get(z, (0, 0))
        print("z%-3d %10d %10d %9.1f %8.2f%s" % (z, n, planned.get(z, 0), b / 1e6, b / max(n, 1) / 1e3,
                                                "" if n == planned.get(z) else "  <- differs from the plan"))
        if n != planned.get(z):
            differ.append(z)
    total, rows = db.execute("SELECT SUM(length(tile_data)), COUNT(*) FROM tiles").fetchone()
    payload, distinct = db.execute(
        "SELECT SUM(l), COUNT(*) FROM (SELECT MAX(length(tile_data)) AS l FROM tiles GROUP BY h)").fetchone()
    nfail = db.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
    # the size model predicts each rendered zoom's raw bytes: every build checks it
    for layer in plan["layers"]:
        z = layer["zooms"][0]
        if layer["kind"] == "render" and layer["bytes"] and stored.get(z, (0, 0))[1]:
            print("z%d rendered: plan estimated %.2f GB, stored %.2f GB (%+.1f%%)" % (
                z, layer["bytes"] / 1e9, stored[z][1] / 1e9, 100 * (stored[z][1] / layer["bytes"] - 1)))
    print("\nrows %d, raw %.2f GB; distinct blobs %d, payload %.2f GB; failures %d" % (
        rows, total / 1e9, distinct, payload / 1e9, nfail))
    if differ:
        # after a complete fetch the database holds every planned tile, so equal counts mean equal sets
        print("rows differ from %s at z%s: the database is from another areas.toml, or a fetch is incomplete.\n"
              "Start a new database: rm -f %s*; rm -f work/.size.ok work/.bundles.done work/.renders.done work/.tiles.done"
              % (PLAN, ",".join(map(str, differ)), args.db))
    if args.fit:
        budget = load_budget()
        ok = payload <= budget and nfail == 0 and not differ
        print("fit check: payload %.2f GB vs budget %.2f GB, failures %d, rows %s the plan -> %s" % (
            payload / 1e9, budget / 1e9, nfail, "differ from" if differ else "match", "OK" if ok else "FAIL"))
        sys.exit(0 if ok else 1)


def cmd_calibrate(args):
    """Quantize renderer tiles and compare them with the bundle's own 8-bit tiles."""
    plan, src = load_plan(), load_sources()
    t = next(t for t in plan["tiers"] if t["bundles"])  # the innermost tier read from bundles
    slug = next(s for s in t["bundles"] if src[s]["maxZoom"] == t["base_zoom"])
    b = Bundle(src[slug]["url"])
    ents = b.entries(t["base_zoom"], t["base_zoom"])
    cut = sorted(ln for _, _, ln in ents)[len(ents) * 9 // 10]  # dense: the top decile by tile size
    rnd = random.Random(args.seed)
    pick = (rnd.sample([e for e in ents if e[2] >= cut], args.n // 2)
            + rnd.sample([e for e in ents if e[2] < cut], args.n - args.n // 2))
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
    """Point the vendored upstream style at the local renderer's data (the one source in
    render/config.json), fonts and sprites."""
    with open(os.path.join(STYLE_DIR, "style.upstream.json")) as f:
        style = json.load(f)
    with open(os.path.join("render", "config.json")) as f:
        (data_id,) = json.load(f)["data"]
    style["sources"]["openmaptiles"]["url"] = "mbtiles://{%s}" % data_id
    style["glyphs"] = "{fontstack}/{range}.pbf"
    style["sprite"] = "{styleJsonFolder}/sprite"
    with open(os.path.join(STYLE_DIR, "style.json"), "w") as f:
        json.dump(style, f, indent=1, ensure_ascii=False)
        f.write("\n")


STAMPS = [".plan", ".preflight", "osm/extract.osm.pbf", "vector/openmaptiles.mbtiles", ".renderer", ".parity.ok",
          ".size.ok", ".bundles.done", ".renders.done", ".tiles.done", ".fit.ok", "osm-bright.pmtiles", "verify.txt"]


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
        if (("build.py" in p and " status" not in p and "uv run" not in p) or "go-pmtiles" in p
                or (len(p.split()) > 1 and p.split()[1] in ("rsync", "ssh") and "osm-bright" in p)):
            print("running: %s" % " ".join(p.split())[:140])
    # convert writes its temp file, then the archive; both grow towards the database payload
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
        plan = load_plan(strict=False) if os.path.exists(PLAN) else None
        if plan:
            print("bundles: " + "  ".join("%s %d/%d" % (l["name"], sum("%s:%s" % (l["name"], s) in done for s in l["bundles"]),
                                                        len(l["bundles"])) for l in plan["layers"] if l["kind"] == "bundle"))
        try:
            targets = list(db.execute("SELECT layer, zoom, tiles FROM targets"))  # recorded by fetch
        except sqlite3.OperationalError:  # database created before targets were recorded
            targets = []
        counts = dict(db.execute("SELECT zoom_level, COUNT(*) FROM tiles WHERE zoom_level IN (%s) GROUP BY zoom_level"
                                 % ",".join(str(z) for _, z, _ in targets))) if targets else {}
        print("renders: " + ("  ".join("%s z%d %d/%d (%.0f%%)" % (l, z, counts.get(z, 0), n, 100 * counts.get(z, 0) / n)
                                       for l, z, n in targets) or "no render layer started yet"))
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
    open_db(args.db, load_plan()).close()


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


def parity_strata(plan):
    """[(zoom, region, dense|sparse, base zoom, base tiles)]. A region is the part of a rendering tier's
    area outside the deeper rendering tiers; dense is its top decile of base tiles by bundle tile size,
    so busy (labelled) tiles are always sampled. Zooms: the base zoom and every rendered zoom over it."""
    tiers = plan["tiers"]
    render_zooms = {t["name"]: [l["zooms"][0] for l in plan["layers"] if l["tier"] == t["name"] and l["kind"] == "render"]
                    for t in tiers}
    strata, seen = [], set()
    for i, t in enumerate(tiers):  # innermost first
        if not render_zooms[t["name"]]:
            continue
        base = base_tiles(plan, t["name"])
        own = [(xy, ln) for xy, ln in base.items() if xy not in seen]
        seen.update(base)
        if not own:
            continue
        cut = sorted(ln for _, ln in own)[len(own) * 9 // 10]
        zooms = sorted({t["base_zoom"]} | {z for o in tiers[i:] for z in render_zooms[o["name"]]})
        for z in zooms:
            for label, part in (("dense", [xy for xy, ln in own if ln >= cut]), ("sparse", [xy for xy, ln in own if ln < cut])):
                if part:
                    strata.append((z, t["name"], label, t["base_zoom"], part))
    return strata


def cmd_parity(args):
    """Local renders vs Oxed's server for the same tiles (structural gate: median <= 2%)."""
    from PIL import Image, ImageDraw

    plan = load_plan()
    rnd = random.Random(args.seed)
    picks = []
    for z, region, label, bz, part in parity_strata(plan):
        for x, y in rnd.sample(part, min(args.per_stratum, len(part))):
            f = 1 << max(z - bz, 0)
            tile = (z, x * f + rnd.randrange(f), y * f + rnd.randrange(f)) if z >= bz else (z, x >> (bz - z), y >> (bz - z))
            picks.append(("%s %s" % (region, label), tile))
    sheet = Image.new("RGB", (3 * 256, 256 * len(picks)), "white")
    lines, fracs = [], []
    for i, (area, (tz, x, y)) in enumerate(picks):
        frac, a, b, mask = pixel_diff(render_raw(OXED_RENDER_URL, tz, x, y), render_raw(args.render_url, tz, x, y))
        fracs.append(frac)
        for col, im in enumerate((a, b, mask)):
            sheet.paste(im, (col * 256, i * 256))
        ImageDraw.Draw(sheet).text((4, i * 256 + 4), "%d/%d/%d %s %.1f%%" % (tz, x, y, area, frac * 100), fill="red")
        lines.append("z%d %-24s %d/%d/%d  differing %.2f%%" % (tz, area, tz, x, y, frac * 100))
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


def sample_fraction(layer, fraction):
    """The fraction of a render layer to sample: `fraction`, but at least 100 tiles (all, if fewer), so
    the standard error is meaningful for small layers."""
    return min(1.0, max(fraction, 100 / layer["tiles"]))


def sample_projection(db, plan, layer, fraction, seed):
    """(projected bytes, standard error, sampled tiles) of a render layer, from its sample's stored tiles."""
    z = layer["zooms"][0]
    want = {(x, (1 << z) - 1 - y) for _, x, y in layer_targets(plan, layer, None, fraction, seed)}
    sizes = [ln for x, r, ln in db.execute(
        "SELECT tile_column, tile_row, length(tile_data) FROM tiles WHERE zoom_level = ?", (z,)) if (x, r) in want]
    n, total = len(sizes), layer["tiles"]
    if not n:  # every sample tile failed to render; the caller reports the failures
        return 0.0, 0.0, 0
    se = statistics.stdev(sizes) / math.sqrt(n) * math.sqrt(1 - n / total) if n > 1 else 0.0
    return total * statistics.fmean(sizes), total * se, n


def render_sample(db, plan, args, seed):
    """Render each render layer's sample into db; lines and the projected (exact + rendered, standard error)."""
    lines = ["layer                 tiles  sampled  failed  tiles/s  projected GB  plan estimate GB"]
    projected, var, failed_all, rate = plan["size"]["exact"], 0.0, 0, []
    for layer in plan["layers"]:
        if layer["kind"] != "render":
            continue
        frac = sample_fraction(layer, args.sample)
        done, failed, _, el = render_layer(db, plan, layer, None, frac, seed, args.quant, args.render_url, args.workers)
        failed_all += failed
        if done:
            rate.append((done + failed, el))
        proj, se, n = sample_projection(db, plan, layer, frac, seed)
        projected += proj
        var += se * se
        lines.append("%-20s %7d %8d %7d %8s %13.2f %17s" % (
            layer["name"], layer["tiles"], n, failed, "%.0f" % ((done + failed) / el) if done else "-",
            proj / 1e9, "-" if layer["bytes"] is None else "%.2f" % (layer["bytes"] / 1e9)))
    return lines, projected, math.sqrt(var), failed_all, rate


def cmd_bench(args):
    """Render a sample of each render layer into a scratch db: tiles/s, ETA, projected size vs the card."""
    plan, budget = load_plan(), load_budget()
    path = os.path.join(WORK, "bench.mbtiles")
    if os.path.exists(path):
        os.remove(path)
    db = open_db(path, plan)
    lines, projected, se, failed, rate = render_sample(db, plan, args, args.seed)
    tiles_per_s = sum(n for n, _ in rate) / max(sum(el for _, el in rate), 1e-9)
    left = sum(l["tiles"] for l in plan["layers"] if l["kind"] == "render")
    lines += ["", "rendering %.0f tiles/s with %d workers: all %d rendered tiles take ~%.1f h" % (
        tiles_per_s, args.workers, left, left / tiles_per_s / 3600),
        "projected archive %.2f GB ±%.2f (95%%): %.2f GB bundles (exact) + rendered from the sample; card budget %.2f GB" % (
        projected / 1e9, 1.96 * se / 1e9, plan["size"]["exact"] / 1e9, budget / 1e9)]
    write_report(os.path.join(WORK, "bench", "report.txt"), lines)
    sys.exit(1 if failed else 0)


def cmd_size_gate(args):
    """Before the fetch: render a sample of each render layer into the real database (the fetch then skips
    those tiles) and project the archive from it. Fails if even the low end of the projection (-1.96
    standard errors) is over the card budget; a projection is raw bytes, so it errs high."""
    plan, budget = load_plan(), load_budget()
    db = open_db(args.db, plan)
    lines, projected, se, failed, _ = render_sample(db, plan, args, args.seed)
    if failed:
        raise SystemExit("%d sample tiles failed to render: re-run" % failed)
    low, high = projected - 1.96 * se, projected + 1.96 * se
    v = "over" if low > budget else "fits" if high <= budget else "may not fit"
    lines += ["", "projected archive %.2f-%.2f GB (95%%) vs card budget %.2f GB: %s" % (low / 1e9, high / 1e9, budget / 1e9, v),
              "size gate: %s" % ("FAIL: make the tiers smaller (`make plan` shows what each zoom costs)" if v == "over"
                                 else "OK" + (" (the fit gate after the fetch decides)" if v == "may not fit" else ""))]
    write_report(os.path.join(WORK, "size", "report.txt"), lines)
    sys.exit(1 if v == "over" else 0)


def cmd_bundles(args):
    """Oxed's bundles with their zoom range and size; --at lon,lat: only those whose bbox contains it."""
    meta = json.loads(http_get(METADATA_URL))
    if args.at:
        lon, lat = (float(v) for v in args.at.split(","))
        meta = [b for b in meta if boxes_overlap(b["bbox"], [lon, lat, lon, lat])]
    print("%-11s %-24s %-7s %7s  %s" % ("group", "slug", "zooms", "GB", "bbox W, S, E, N"))
    for b in sorted(meta, key=lambda b: (b["group"], b["maxZoom"], b["slug"])):
        print("%-11s %-24s z%d-%-4d %7.2f  %s%s" % (
            b["group"], b["slug"], b["minZoom"], b["maxZoom"], b["fileSizeBytes"] / 1e9, ", ".join("%.2f" % v for v in b["bbox"]),
            "" if b["maxZoom"] >= 15 else "  (as a tier's deepest bundle: no size estimate past z%d)" % b["maxZoom"]))
    print("\nA tier's `bundles` in %s takes slugs or groups." % AREAS)


def cmd_preflight(args):
    """Before any download: Docker is up with memory for Planetiler's heap, and the disk holds the
    build's peak (from the plan's sizes and the extract's)."""
    plan = load_plan()
    r = subprocess.run(["docker", "info", "--format", "{{.MemTotal}}"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("Docker is not running")
    mem = int(r.stdout.strip())
    if mem < args.heap_gb * 2 ** 30:
        raise SystemExit("Docker has %.1f GiB of memory and Planetiler is given %d GiB (PLANETILER_HEAP_GB in the "
                         "Makefile): give Docker more (Docker Desktop: Settings > Resources)" % (mem / 2 ** 30, args.heap_gb))
    if not os.path.exists(EXTRACT_BYTES):
        raise SystemExit("the extract's size is unknown (`make plan` couldn't reach it): re-run `make plan` online")
    extract = int(open(EXTRACT_BYTES).read())
    payload = plan["size"]["high"]
    need = extract + max(PLANETILER_TEMP_PER_EXTRACT * extract, DISK_PER_PAYLOAD * payload) + 3e9  # 3 GB: sources, vector tiles
    st = os.statvfs(WORK)
    built = sum(os.path.getsize(p) for p in (os.path.join(WORK, "osm", "extract.osm.pbf"), DEFAULT_DB, DEFAULT_DB + "-wal",
                                             os.path.join(WORK, "osm-bright.pmtiles")) if os.path.exists(p))
    have = st.f_bavail * st.f_frsize + built
    print("Docker memory %.1f GiB (Planetiler heap %d GiB); disk: need ~%.0f GB at the peak, have %.0f GB%s%s" % (
        mem / 2 ** 30, args.heap_gb, need / 1e9, have / 1e9, " (counting %.0f GB already built)" % (built / 1e9) if built else "",
        "; rendered size of %s unknown, not counted" % ", ".join(plan["size"]["unknown"]) if plan["size"]["unknown"] else ""))
    if have < need:
        raise SystemExit("not enough disk for this build: free ~%.0f GB more" % ((need - have) / 1e9))


def pmtiles_tile(archive, z, x, y):
    r = subprocess.run([GO_PMTILES, "tile", archive, str(z), str(x), str(y)], capture_output=True)
    return r.stdout if r.returncode == 0 else b""


def cmd_verify(args):
    """Archive vs MBTiles: header, addressed count, pmtiles verify, tier-boundary probes."""
    plan = load_plan()
    lines, ok = [], True
    show = subprocess.run([GO_PMTILES, "show", args.archive], capture_output=True, text=True).stdout
    fields = dict(l.split(": ", 1) for l in show.splitlines() if ": " in l)
    rows = sqlite3.connect(args.db).execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
    expect = {"tile type": "png", "tile compression": "none", "internal compression": "gzip",
              "min zoom": "0", "max zoom": str(plan["tiers"][0]["zooms"][1]), "addressed tiles count": str(rows)}
    for k, v in expect.items():
        good = fields.get(k, "").strip() == v
        ok &= good
        lines.append("%-24s %-12s expected %-12s %s" % (k, fields.get(k, "").strip(), v, "OK" if good else "FAIL"))
    vr = subprocess.run([GO_PMTILES, "verify", args.archive], capture_output=True, text=True)
    ok &= vr.returncode == 0
    lines.append("pmtiles verify: exit %d %s" % (vr.returncode, "OK" if vr.returncode == 0 else "FAIL"))
    for p in load_probes(plan):
        for z, want in ((p["zoom"], True), (p["zoom"] + 1, False)):
            x, y = lonlat_tile(p["lon"], p["lat"], z)
            have = len(pmtiles_tile(args.archive, z, x, y)) > 0
            ok &= have == want
            lines.append("%-24s z%-2d %d/%d %-8s expected %-8s %s" % (
                p["name"], z, x, y, "present" if have else "missing", "present" if want else "missing",
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
    plan = load_plan()
    src = load_sources()
    sources_dir = os.path.join(WORK, "planetiler", "sources")
    manifest = {
        "written": now(),
        "areas": {"sha256": sha256_file(AREAS), "plan_sha256": plan["layers_sha256"]},
        "bundles": {"count": len(src), "createdAt": sorted({b["createdAt"] for b in src.values()})},
        "osm_extract": {"url": plan["osm_extract"],
                        "md5": open(os.path.join(WORK, "osm", "extract.osm.pbf.md5")).read().split()[0],
                        "last_modified": open(os.path.join(WORK, "osm", "extract.osm.pbf.last-modified")).read().strip()},
        "planetiler_bounds": plan["bounds"],
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
    sub.add_parser("plan")
    s = sub.add_parser("bundles")
    s.add_argument("--at", metavar="LON,LAT", help="only bundles whose bbox contains this point")
    s = sub.add_parser("preflight")
    s.add_argument("--heap-gb", type=int, required=True, help="Planetiler's Java heap")
    for name in ("fetch", "bench", "size-gate"):
        s = sub.add_parser(name)
        if name == "fetch":
            s.add_argument("--kind", choices=["bundle", "render"], help="only this kind of layer")
            s.add_argument("--layers", nargs="+", help="only these layers (names from `plan`)")
            s.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"), help="restrict every layer (pilot)")
        if name != "bench":
            s.add_argument("--db", default=DEFAULT_DB)
        s.add_argument("--sample", type=float, default=None if name == "fetch" else SAMPLE,
                       help="render only this fraction of each render layer")
        s.add_argument("--seed", type=int, default=1)
        s.add_argument("--quant", choices=["mediancut", "fastoctree"], default="mediancut")
        s.add_argument("--render-url", default=LOCAL_RENDER_URL)
        s.add_argument("--workers", type=int, default=16)
    s = sub.add_parser("report")
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--fit", action="store_true", help="the fit gate: exit 1 unless payload <= the card budget")
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
    for opt in ("--planetiler-image", "--tileserver-image", "--fonts-url", "--fonts-sha256", "--go-pmtiles"):
        s.add_argument(opt, required=True)
    args = p.parse_args()
    os.makedirs(WORK, exist_ok=True)
    {"pin": cmd_pin, "plan": cmd_plan, "bundles": cmd_bundles, "preflight": cmd_preflight, "size-gate": cmd_size_gate,
     "fetch": cmd_fetch, "report": cmd_report, "calibrate": cmd_calibrate,
     "style": cmd_style, "seal": cmd_seal, "initdb": cmd_initdb, "status": cmd_status,
     "parity": cmd_parity, "bench": cmd_bench, "verify": cmd_verify, "card": cmd_card,
     "manifest": cmd_manifest}[args.cmd](args)


if __name__ == "__main__":
    main()
