# Design notes

Why the archive has the zoom levels it has, where each tile comes from, and what was measured on the way. Measurements are from one build on 2026-10-10.

## Zoom depth per area

| Area | Zooms | Source | Tiles | Size |
|---|---|---|---|---|
| World | 0–9 | World bundle | 84k | 0.53 GB |
| US | 10–13 | All-US bundle | 1.25M | 3.73 GB |
| US except Alaska (no state bundle) | 14–15 | 49 state bundles, z14/z15 slices | 11.5M | ~45.6 GB |
| Illinois | 16 | rendered locally | 716k | 2.53 GB |
| Chicago metro (CMAP 7 counties) | 17–18 | rendered locally | 1.55M | 5.60 GB |

The payload is **57.00 GB**: 14,968,630 tiles, 11,860,774 of them unique. A 64 GB exFAT card had 63.98 GB free.

**Why these depths:**
- **z18 is the top.** Overzoomed tiles were compared at z15–z20. z19 and z20 show the same features as z18, only larger.
- **US z15.** The bundles stop at z15, so z16 would have to be rendered for the whole country. Most z15 bytes are rural: in Kansas, tiles over 8 KB are 3% of tiles but 16% of bytes. Built-up-only z15 saves space nothing else could use, and it leaves the countryside blank past z14.
- **Illinois z16, metro z17–18.** These fill the remaining space. Downstate z17 (~5 GB) would push the archive over a 64 GB card's budget, and it would mostly add house numbers on farmland.

What each level shows in osm-bright, checked by eye on sampled tiles:

| Zoom | Shows |
|---|---|
| z13 | Arterials, no street names |
| z14 | Residential street names |
| z15 | Town POIs; dense city areas name major streets only |
| z16 | Most streets named; one-way arrows |
| z17 | House numbers, where OSM has addresses |
| z18 | Alleys and address ranges |

## Where tiles come from

- **z0–15:** [Oxed's Map Tile Downloader](https://download.tiles.coalition.space/) publishes monthly PMTiles bundles. `build.py` reads only the zoom slices it needs, using HTTP range requests against a pinned `bundles.json`.
  - The world bundle leaves out empty ocean: it has 60,581 of the 262,144 z9 tiles. Three of the missing tiles are in the Aleutians, above all-US z10 tiles, so those spots are blank at z9. `build.py plan` reports this as a warning.
- **z16–18:** rendered locally. Oxed's renderer took ~450 ms per tile (server-side), which would have meant ~21 h for 2.27M tiles. Its stack is open source:
  - **TileServer GL v5.6.0** for rendering
  - **Planetiler 0.10.2**, OpenMapTiles schema 3.16.0, maxzoom 15, for the vector data
  - the **OSM Bright** style

  The build runs the same versions and style locally.
- **Parity check:** across 56 sampled tiles, local renders vs Oxed's are pixel-identical for 53. The 3 that differ are z15 city tiles showing about a month of OSM edits. All z16–18 samples are identical.
  - That run sampled hand-drawn Chicago areas (city, Cook County, metro, rest of Illinois). The strata now come from the plan: zoom × tier × dense/sparse, where dense is the busiest tenth by bundle tile size.
- **Rendering isn't byte-reproducible.** Re-rendering the same tiles with the same container, data and style gave different PNG bytes for about 1 tile in 10. At most 0.035% of pixels differed: anti-aliasing noise that shifts the 8-bit palette. A rebuild reproduces the map, not the archive's hash.
- **8-bit palette PNG:** Pillow `MEDIANCUT`, no dither. Our tiles average 1.009× the size of the bundle tiles for the same tile, and look the same by eye.

## Throughput

Measured with `scripts/bench_renderers.sh`:

| Renderers | M4 Pro, 14 CPUs | 24-vCPU EPYC 7282 VM |
|---|---|---|
| 1 | 452 tiles/s | 118 tiles/s |
| 2 | 710 tiles/s | 207 tiles/s |
| 4 | 710 tiles/s | 297 tiles/s |
| 8 | — | 375 tiles/s |

- **What limits a renderer:** one TileServer GL process saturates at ~2 cores. On the M4 Pro, 2 processes already nearly fill the CPU.
- **Where my per-tile CPU goes:** of ~6.6 ms, 4.4 ms is the quantize step.
- **Bundle reads:** ~23 MB/s on one stream, ~50 MB/s with 4 parallel range reads.
- **The full build on the M4 Pro:**

  | Step | Time |
  |---|---|
  | US extract download | 12.2 GB, 11 min |
  | Planetiler | 8 min |
  | Bundle download | ~20 min |
  | Rendering, overlapping the download | ~80 min in total (z18 alone 44 min at 475 tiles/s) |
  | Convert + verify | ~12 min |

## Device facts (Meshtastic UI on LILYGO T-Deck)

- **PMTiles needs MUI ≥ 2.8.1.** Firmware 2.7.x bundles a device-ui without `PMTileService`, so it reads only loose `maps/<style>/z/x/y.png` files. In the serial log that shows as `Failed to load tile …png from SD`, and the archive is never opened.
- **exFAT** is needed for a file over 4 GiB. On the T-Deck, MUI reads the archive with 64-bit seeks (`SdFatMapFileSystem::readAt` → `seekSet`).
- **Where the map opens:** MUI opens on its default location (London) unless the device has a GPS fix, a home position, or nodes with positions.
- **GPS on the T-Deck Plus is off by default.** T-Deck builds default the GPS mode to NOT_PRESENT. Long-press the home screen's location button to enable it.

## Pitfalls hit while building

- **SQLite:** two processes creating the WAL database at the same moment fail with `database is locked`. `make` runs `build.py initdb` first.
- **Renderer healthcheck:** with `serve_data: false`, TileServer GL doesn't publish `style.json`. The healthcheck probes the rendered TileJSON instead.
- **An extract that stops at the border.** A rendered tile that crosses it draws only background on the far side.
  - Seen in a Rhode Island test: Massachusetts parts of Rhode Island's z16 tiles were empty, and parity still passed, because it judges the median of 48 samples.
  - `plan` now checks every rendered tile against the extract's Geofabrik `.poly`.
  - The check rejects `us-midwest` for this build: 1,183 Illinois tiles reach into Kentucky. That's why the build uses the whole-US extract.
- **macOS on exFAT:** writes `._*` AppleDouble files even with `cp -X`. `build.py card` removes them. MUI ignores dot-names anyway.
- **Card read-back:** has to come from the card, not the page cache. `build.py card` unmounts and remounts before hashing.
