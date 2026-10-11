# meshtastic-maps

Builds one offline map archive for a LILYGO T-Deck running Meshtastic UI (MUI ≥ 2.8.1). The archive goes on the T-Deck's SD card at `maps/osm-bright/osm-bright.pmtiles`.

The map is a detailed area, then zoomed-out areas around it, then the world. You describe those tiers in [`areas.toml`](areas.toml), and `make plan` shows what they cost on your card before anything is downloaded. This repo's `areas.toml` is one build, for a 64 GB card:

| Area | Zoom levels |
|---|---|
| World | 0–9 |
| Alaska | 0–13 |
| Rest of the US | 0–15 |
| Illinois | 0–16 |
| Chicago metro (CMAP 7 counties) | 0–18 |

It comes to about 57 GB. Measurements and the reasons for each choice are in [`docs/DESIGN.md`](docs/DESIGN.md).

## Requirements

- **macOS:** the Makefile uses `diskutil`, `md5` and macOS's `make`.
- **Docker Desktop:** with at least 8 GB of memory, which Planetiler's heap needs (checked). This build gave it about 14 CPUs.
- **[uv](https://docs.astral.sh/uv/):** `build.py` pins its own dependency (Pillow) inline.
- **Free disk:** sized from your plan and checked before any download. This build needed about 200 GB at its peak.
- **Network:** Oxed's servers (bundles, and the renderer check) and Geofabrik (the OpenStreetMap extract).

## Making your own map

### 1. Describe your areas in `areas.toml`

List the tiers innermost first. Each tier is drawn from one zoom past the next tier's `max_zoom` down to its own; the last tier starts at z0.

```toml
card_free_bytes = 62_496_178_176  # free space on the formatted card (smallest card, if several)

[[tier]]
name = "chicago-metro"
max_zoom = 18
bbox = [-88.94, 41.20, -87.52, 42.50]  # W, S, E, N in degrees

[[tier]]
name = "illinois"
max_zoom = 16
bundles = ["illinois"]

[[tier]]
name = "us"
max_zoom = 15
bundles = ["us-state"]

[[tier]]
name = "world"
max_zoom = 9
bundles = ["world"]
```

This gives world z0–9, US z10–15, Illinois z16 and Chicago metro z17–18.

- **Choosing bundles:** `make bundles AT=lon,lat` lists Oxed's pre-built bundles that cover a point, with their zoom range and size. A tier's `bundles` takes slugs (`illinois`) or groups (`us-state`, `eu-country`, …).
  - **Bundle depth:** most state and country bundles go to z15, all-US and all-Europe to z13, world to z9.
  - **Rendered zooms:** zooms past a tier's bundles are rendered locally.
- **Your detailed area:** a `bbox`, as the coordinates of its south-west and north-east corners. It is snapped to the next tier's z15 tiles (about 1 km) and clipped to that tier.
- **`card_free_bytes`:** format the card first (see [Card](#5-put-it-on-the-card)), then read `diskutil info /Volumes/MESHMAP | grep "Free Space"`.
  - Cards sold as the same size differ: this build's two "64 GB" cards are 64.0 and 62.5 GB.
  - The budget is that free space minus 0.5 GB.
- **`osm_extract`** is optional. Left out, `plan` picks the smallest [Geofabrik](https://download.geofabrik.de/) extract that contains every rendered tile.
- **`probes`** are optional. They're places `verify` checks in the finished archive. This repo's are Chicago-specific: replace or delete them. `plan` adds one sampled point per tier either way.

### 2. `make plan`, until it fits

`make plan` derives everything from `areas.toml` and fetches no tiles. The first run takes a few minutes: it pins the bundles and reads their tile directories. After that it takes seconds.

```
layer                  zooms  source                                     tiles        GB
world-z0-9             0-9    world                                     83,567      0.53
us-z10-13              10-13  all-united-states                      1,254,631      3.78
us-z14-15              14-15  49 bundles (alabama ... wyoming)      11,359,616     44.94
illinois-z16           16     render from 178,984 z15 base tiles       715,936     ≈2.53
chicago-metro-z17      17     render from 19,436 z15 base tiles        310,976     ≈1.68
chicago-metro-z18      18     render from 19,436 z15 base tiles      1,243,904     ≈3.93
total                  0-18                                         14,968,630    ≈57.39

card: 62.50 GB free - 0.50 GB margin = 62.00 GB budget
size: 49.26 GB from bundles (exact) + ≈8.14 GB rendered (±16%) = 56.09-58.70 GB -> fits, at least 3.30 GB spare

one zoom deeper / shallower per tier (≈: estimated):
  chicago-metro    z18  deeper z19: no estimate past z18                    shallower z17: -3.93 GB ≈ -> fits
  illinois         z16  deeper z17: +4.87 GB ≈ -> may not fit               shallower n/a: us is z15
  us               z15  deeper n/a: illinois is z16                         shallower z14: -34.15 GB -> fits
  world            z9   deeper z10: no estimate from a z9 base              shallower z8: -0.34 GB -> fits
```

- **Sizes:** exact for zooms that come from bundles. Rendered zooms are estimated from the bundles' z15 tiles: within ±16% on the areas they were checked against ([`docs/DESIGN.md`](docs/DESIGN.md#size-estimates)).
- **Deeper / shallower:** the change in total for moving each tier one zoom, and whether the result fits. "n/a" means the neighbouring tier is in the way.
- **Also printed:** the chosen extract and its size, the download total, the render time on a measured machine, and the probes.
- **It fails** if a probe disagrees with the derived tiles, if a tier isn't inside the one around it, or if the extract doesn't cover a rendered tile. When the extract doesn't cover, it names the smallest one that does.
- **It never fails on size:** the verdict is fits, may not fit, or over. The build checks size by measurement (next steps).

### 3. Optional: `make bench`

`make bench` renders a 1% sample of each rendered layer into a scratch database. It reports this machine's speed, the time the full render will take, and the projected archive size against the card. It needs the renderer, so it downloads the extract and runs Planetiler first.

### 4. `make`

```sh
make            # everything up to a verified work/osm-bright.pmtiles
```

Steps are resumable: re-running `make` skips any step whose output exists. In order:

1. **Preflight:** Docker is running and has enough memory; the disk can hold this plan's peak.
2. **Inputs:** the extract, Planetiler's vector tiles for the rendered area, then the renderer.
3. **Parity:** local renders are compared with Oxed's server.
4. **Size gate:** renders a 1% sample into the real database, so those tiles count toward the build. It fails if the measured projection is clearly over the card.
5. **Fetch:** bundle tiles download while the rest renders.
6. **Finish:** the fit check, conversion to PMTiles, then verification.

This build took about 2 hours on an M4 Pro: 49 GB of bundles, a 12 GB extract and 2.3 million rendered tiles.

### 5. Put it on the card

Format the card by hand, because erasing is destructive. Check the disk ID first:

```sh
diskutil list external
diskutil eraseDisk ExFAT MESHMAP MBR diskN
make card CARD=/Volumes/MESHMAP
```

`make card` refuses to copy if `maps/` on the card holds other map data. It checks the copy by reading it back after a remount.

### 6. On the device

- **Firmware:** MUI needs firmware ≥ 2.8.1; 2.7.x has no PMTiles support.
- **Where the map opens:** MUI opens on London (its default) unless the T-Deck has a GPS fix, a saved home position, or nodes with positions. Get a GPS fix, or set a fixed position in the Meshtastic app.

## Reference

### How tiers become tiles

- **Source per zoom:** a tier uses the bundles with the smallest max zoom that still reach that zoom. For `us-state`, that's the all-US bundle for z10–13 (it includes Alaska), then the state bundles for z14–15. Zooms past every bundle are rendered from the tier's z15 tiles.
- **Bundle pins:** `work/bundles.json` records each bundle's URL, which names one monthly build. `make plan` pins names it hasn't seen yet. Names that are already pinned never move, so a build resumes against the same data. `uv run build.py pin --force` re-pins everything.
- **Extract coverage:** a tile on a border reaches into the neighbouring area, and renders blank wherever the extract has no data.
  - **Geofabrik extracts:** `plan` checks every rendered tile against the extract's `.poly` boundary. The check is strict over open sea, which is drawn from Planetiler's own water data.
  - **Other sources:** `plan` warns that coverage is unchecked.
- **Planetiler's bounds:** the rendered tiles' extent plus 2 km.

### Gates

| Gate | What it checks | Output |
|---|---|---|
| `plan` | The tiers nest, each probe is where the derived tiles put it, and the extract covers every rendered tile | `work/plan/` |
| `preflight` | Docker is running, its memory covers Planetiler's heap, and the disk covers this plan's peak | — |
| `parity` | Local renders vs Oxed's server, 4 tiles per zoom × area × density (dense = busiest tenth): 48 for this build. Fails if the median tile has more than 2% differing pixels. | `work/parity/report.txt`, `work/parity/sheet.png` |
| `size-gate` | A 1% render sample projects the archive. Fails if even the low end (−1.96 standard errors) is over the card budget. | `work/size/report.txt` |
| `fit` | The payload fits the card, and the database holds exactly the planned tiles (row counts per zoom). Also prints each rendered zoom's estimate vs its stored size, so every build checks the size model. | from `build.py report --fit` |
| `verify` | Header, tile count, and the probes in the finished archive | `work/verify.txt` |
| `card` | SHA-256 of the copy read back from the card, after a remount so the page cache can't answer for it | — |

### Other targets

- `make status`: progress from recorded state (stages done, the running step, renders, conversion, card copy). Safe while a build runs.
- `make publish DEST=host:/path/to/maps`: rsyncs everything that's expensive to reproduce, then has the destination host re-hash the archive and compare. Code is not copied.
  - the archive + its `.sha256`
  - `vector/openmaptiles.mbtiles`
  - build records, including the plan
- `make renderer-down`: stops the renderer containers.

`work/build-manifest.json` records the inputs used: the `areas.toml` hash, extract md5 and date, image digests, and font and style checksums.

### Changing the areas after a build

A tile database belongs to one plan: `fetch` refuses a database built for different tiers or pins. Start a new one:

```sh
rm -f work/tiles.mbtiles* work/.size.ok work/.bundles.done work/.renders.done work/.tiles.done
make
```

`make` re-runs Planetiler only if the bounds changed, and re-downloads the extract only if it changed. A new `card_free_bytes` re-runs only the size checks.

## Credits and licenses

- **Map data:** © OpenStreetMap contributors, [ODbL](https://opendatacommons.org/licenses/odbl/). Vector schema: © [OpenMapTiles](https://openmaptiles.org/). The archive metadata carries the OpenStreetMap attribution.
- **Style:** `render/styles/osm-bright/` holds the OSM Bright GL style and its sprites. It was fetched on 2026-10-10 from Oxed's tile server (`tiles.coalition.space/raster/styles/osm-bright/`), and `SHA256SUMS` records the files.
  - Code is BSD-3-Clause; the design is CC-BY 4.0. © MapTiler.com & OpenMapTiles contributors and © Mapbox: see [`render/styles/osm-bright/LICENSE.md`](render/styles/osm-bright/LICENSE.md).
  - `make` writes the derived `style.json`, changing only `sources`, `glyphs` and `sprite`.
- **Fonts:** Noto Sans from [openmaptiles/fonts v2.0](https://github.com/openmaptiles/fonts), downloaded during the build (SIL OFL). They are not stored in this repo.
- **Pre-built tiles:** [Oxed's Map Tile Downloader](https://download.tiles.coalition.space/) bundles, downloaded during the build.
- **go-pmtiles:** [protomaps/go-pmtiles](https://github.com/protomaps/go-pmtiles) release binary, downloaded and checked by sha256.
