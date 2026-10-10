# meshtastic-maps

Builds one offline map archive for a LILYGO T-Deck running Meshtastic UI (MUI ≥ 2.8.1). It goes on a 64 GB SD card at `maps/osm-bright/osm-bright.pmtiles`.

| Area | Zoom levels |
|---|---|
| World | 0–9 |
| Alaska | 0–13 |
| Rest of the US | 0–15 |
| Illinois | 0–16 |
| Chicago metro (CMAP 7 counties) | 0–18 |

Total size is about 58 GB. Sizing, measurements and the reasons for each choice are in `plans/2026-10-10-chicago-il-us-tiles.md`.

## Where the tiles come from

- **z0–15:** [Oxed's pre-built bundles](https://download.tiles.coalition.space/bundles) (OpenStreetMap, osm-bright style), read with HTTP range requests. Bundle URLs are pinned in `work/bundles.json`.
- **z16–18:** rendered locally with the same stack as Oxed's tile server:
  - Planetiler 0.10.2 builds OpenMapTiles vector data for Illinois from the Geofabrik US extract.
  - TileServer GL v5.6.0 draws Oxed's osm-bright style (`render/styles/osm-bright/`).

  Tiles are re-encoded to 8-bit palette PNGs, as MUI's tile docs recommend.

## Requirements

- Docker (Docker Desktop, about 14 CPUs and 16 GB for the VM)
- [uv](https://docs.astral.sh/uv/). `build.py` pins its own dependency (Pillow) inline.
- Go, used once to build go-pmtiles
- About 200 GB free disk

## Build

```sh
make            # everything up to a verified work/osm-bright.pmtiles
make card CARD=/Volumes/MESHMAP
```

Steps are resumable. Re-running `make` skips any step whose output exists. The build stops at these gates:

| Gate | What it checks | Output |
|---|---|---|
| `parity` | Local renders vs Oxed's server for 56 sampled tiles. Fails if the median tile has more than 2% differing pixels. | `work/parity/report.txt`, `work/parity/sheet.png` |
| `fit` | The archive payload fits the card | from `build.py report` |
| `verify` | Header, tile count and tier-boundary probes | `work/verify.txt` |
| `card` | SHA-256 of the copy read back from the card, after a remount so the page cache can't answer for it | — |

Other targets:
- `make status`: progress from recorded state (stages done, the running step, renders, conversion, card copy). Safe while a build runs.
- `make publish DEST=host:/path/to/maps`: rsyncs everything that's expensive to reproduce, then has the destination host re-hash the archive and compare. Code is not copied.
  - the archive + its `.sha256`
  - `vector/illinois.mbtiles`
  - build records
- `make bench`: renders a 1% sample, then reports tiles/s and an ETA.
- `make renderer-down`: stops the renderer containers.

`work/build-manifest.json` records the inputs used: extract md5 and date, image digests, font and style checksums.

## Card

Format it by hand, because erasing is destructive. Check the disk ID first:

```sh
diskutil list external
diskutil eraseDisk ExFAT MESHMAP MBR diskN
```

`make card` refuses to copy if `maps/` on the card holds any other map data.

## Device

- **Firmware:** needs firmware ≥ 2.8.1. 2.7.x has no PMTiles support.
- **Where the map opens:** MUI opens on London (its default location) unless the T-Deck has a GPS fix, a saved home position, or nodes with positions. To open on Chicago, get a GPS fix or set a fixed position in the Meshtastic app.

## Credits and licenses

- **Map data:** © OpenStreetMap contributors, [ODbL](https://opendatacommons.org/licenses/odbl/). Vector schema: © [OpenMapTiles](https://openmaptiles.org/). The archive metadata carries the OpenStreetMap attribution.
- **Style:** `render/styles/osm-bright/` holds the OSM Bright GL style and its sprites. It was fetched on 2026-10-10 from Oxed's tile server (`tiles.coalition.space/raster/styles/osm-bright/`), and `SHA256SUMS` records the files.
  - Code is BSD-3-Clause; the design is CC-BY 4.0. © MapTiler.com & OpenMapTiles contributors and © Mapbox: see [`render/styles/osm-bright/LICENSE.md`](render/styles/osm-bright/LICENSE.md).
  - `make` writes the derived `style.json`, changing only `sources`, `glyphs` and `sprite`.
- **Fonts:** Noto Sans from [openmaptiles/fonts v2.0](https://github.com/openmaptiles/fonts), downloaded during the build (SIL OFL). They are not stored in this repo.
- **Pre-built tiles (z0–15):** [Oxed's Map Tile Downloader](https://download.tiles.coalition.space/) bundles, downloaded during the build.
