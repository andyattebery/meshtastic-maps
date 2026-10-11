# meshtastic-maps

Builds one offline map archive for a LILYGO T-Deck running Meshtastic UI (MUI ≥ 2.8.1). It goes on a 64 GB SD card at `maps/osm-bright/osm-bright.pmtiles`.

The map is a detailed area, then zoomed-out areas around it, then the world. The tiers are set in [`areas.toml`](areas.toml); this build's are:

| Area | Zoom levels |
|---|---|
| World | 0–9 |
| Alaska | 0–13 |
| Rest of the US | 0–15 |
| Illinois | 0–16 |
| Chicago metro (CMAP 7 counties) | 0–18 |

Total size is about 58 GB. Sizing, measurements and the reasons for each choice are in [`docs/DESIGN.md`](docs/DESIGN.md).

## Where the tiles come from

- **Zoom levels with a pre-built bundle (here z0–15):** [Oxed's bundles](https://download.tiles.coalition.space/bundles) (OpenStreetMap, osm-bright style), read with HTTP range requests. Bundle URLs are pinned in `work/bundles.json`.
- **Deeper zoom levels (here z16–18):** rendered locally with the same stack as Oxed's tile server:
  - Planetiler 0.10.2 builds OpenMapTiles vector data for the rendered area from the OpenStreetMap extract named in `areas.toml`.
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
| `plan` | The tiers in `areas.toml` nest, each probe is where the derived tiles put it, and the OSM extract covers every rendered tile | `work/plan/` |
| `parity` | Local renders vs Oxed's server: 4 tiles per zoom, area and density (dense = busiest tenth), 48 for this build. Fails if the median tile has more than 2% differing pixels. | `work/parity/report.txt`, `work/parity/sheet.png` |
| `fit` | The archive payload fits the card, and the database holds exactly the planned tiles (row counts per zoom) | from `build.py report` |
| `verify` | Header, tile count, and the probes in the finished archive | `work/verify.txt` |
| `card` | SHA-256 of the copy read back from the card, after a remount so the page cache can't answer for it | — |

Other targets:
- `make plan`: what `areas.toml` derives to. No tiles are fetched.
- `make status`: progress from recorded state (stages done, the running step, renders, conversion, card copy). Safe while a build runs.
- `make publish DEST=host:/path/to/maps`: rsyncs everything that's expensive to reproduce, then has the destination host re-hash the archive and compare. Code is not copied.
  - the archive + its `.sha256`
  - `vector/openmaptiles.mbtiles`
  - build records, including the plan
- `make bench`: renders a 1% sample, then reports tiles/s, an ETA and the projected archive size against the card budget.
- `make renderer-down`: stops the renderer containers.

`work/build-manifest.json` records the inputs used: `areas.toml` hash, extract md5 and date, image digests, font and style checksums.

## Changing the map areas

Edit [`areas.toml`](areas.toml). It lists tiers innermost first; each one is a zoomed-out area around the tier before it:

```toml
[[tier]]
name = "chicago-metro"
max_zoom = 18
bbox = [-88.94, 41.20, -87.52, 42.50]  # W, S, E, N

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

- **Zoom levels:** a tier covers the zooms from the next tier's `max_zoom` + 1 to its own. The last tier starts at z0. Here that gives:
  - world z0–9
  - US z10–15
  - Illinois z16
  - Chicago metro z17–18
- **Area:** each tier has one of these.
  - `bundles`: Oxed bundle slugs or groups. [`metadata.json`](https://bundle.tiles.coalition.space/metadata.json) lists them with their zoom ranges; groups include `us-state`, `eu-country` and `ca-province`.
  - `bbox`: the next tier's base tiles whose centre is inside the box. Base tiles come from the deepest bundles: z15 for states and countries. The box is snapped to them (about 1 km at z15) and clipped to the tier around it.
- **Source:** zooms a tier's bundles reach are read from them. Deeper zooms are rendered from the tier's base tiles.
  - Per zoom, the bundle with the smallest max zoom that still reaches it is used. For `us-state`, that's the all-US bundle for z10–13 (it includes Alaska), then the state bundles for z14–15.
  - Where bundles stop, as of 2026-10:
    - z9: world
    - z13: all-US, all-Europe, southern Canada (Canada has no deeper bundle)
    - z15: US states, European countries, Australia
- **Vector data:** `osm_extract` is the OpenStreetMap extract the renderer draws from, for example one from [Geofabrik](https://download.geofabrik.de/).
  - It must contain the whole of every rendered tile. A tile on a state's border reaches into the neighbour, and renders blank wherever the extract has no data.
  - For a Geofabrik extract, `plan` checks every rendered tile against the extract's `.poly` boundary. It fails if any tile crosses the boundary or lies outside it.
  - The check is strict over open water, which renders from Planetiler's own water data. In a Rhode Island test, 14 of the 1,400 failing tiles were open sea.
  - For other sources, `plan` warns that coverage is unchecked.
  - Planetiler's bounds are derived: the rendered tiles' extent plus 2 km.
- **Probes:** `probes` lists places `verify` checks in the finished archive, each present at `zoom` and missing one zoom deeper. The plan adds one sampled point per tier.

Then:
1. **`make plan`:** prints the derived layers, tile counts, bundle sizes, Planetiler bounds and probes.
   - It fails if a probe disagrees with the derived tiles, if a tier isn't inside the one around it, or if the extract doesn't cover a rendered tile.
   - It warns about small gaps, such as 3 ocean tiles in the Aleutians where the world bundle has no z9 tile above an all-US z10 one.
2. **`make bench`:** projects the rendered layers' size from a 1% sample, and compares the total with the card budget. The budget is `BUDGET_BYTES` in the `Makefile`: the formatted card's free space minus 0.5 GB.
3. **Start a new tile database.** `fetch` refuses one built for different tiers.

   ```sh
   rm -f work/tiles.mbtiles* work/.bundles.done work/.renders.done work/.tiles.done
   make
   ```

   `make` re-runs Planetiler only if the bounds changed, and re-downloads the extract only if `osm_extract` changed.

**Bundle not pinned:** if the plan says a bundle or group isn't pinned, run `uv run build.py pin --force`. It re-pins every bundle from Oxed's current metadata. Then start a new database.

## Card

Format it by hand, because erasing is destructive. Check the disk ID first:

```sh
diskutil list external
diskutil eraseDisk ExFAT MESHMAP MBR diskN
```

`make card` refuses to copy if `maps/` on the card holds any other map data.

## Device

- **Firmware:** needs firmware ≥ 2.8.1. 2.7.x has no PMTiles support.
- **Where the map opens:** MUI opens on London (its default location) unless the T-Deck has a GPS fix, a saved home position, or nodes with positions. To open on your area, get a GPS fix or set a fixed position in the Meshtastic app.

## Credits and licenses

- **Map data:** © OpenStreetMap contributors, [ODbL](https://opendatacommons.org/licenses/odbl/). Vector schema: © [OpenMapTiles](https://openmaptiles.org/). The archive metadata carries the OpenStreetMap attribution.
- **Style:** `render/styles/osm-bright/` holds the OSM Bright GL style and its sprites. It was fetched on 2026-10-10 from Oxed's tile server (`tiles.coalition.space/raster/styles/osm-bright/`), and `SHA256SUMS` records the files.
  - Code is BSD-3-Clause; the design is CC-BY 4.0. © MapTiler.com & OpenMapTiles contributors and © Mapbox: see [`render/styles/osm-bright/LICENSE.md`](render/styles/osm-bright/LICENSE.md).
  - `make` writes the derived `style.json`, changing only `sources`, `glyphs` and `sprite`.
- **Fonts:** Noto Sans from [openmaptiles/fonts v2.0](https://github.com/openmaptiles/fonts), downloaded during the build (SIL OFL). They are not stored in this repo.
- **Pre-built tiles:** [Oxed's Map Tile Downloader](https://download.tiles.coalition.space/) bundles, downloaded during the build.
