# Repeatable build of the T-Deck map card archive from the nested map areas in areas.toml.
#
#   make                              build and verify work/osm-bright.pmtiles
#   make plan                         what areas.toml derives to: layers, tile counts, bounds, probes
#   make card CARD=/Volumes/MESHMAP   copy it to a mounted exFAT card (full read-back check)
#   make parity | make bench          renderer checks (parity must pass before tiles are fetched)
#   make renderer-down                stop the local renderer
#
# Design and measurements: docs/DESIGN.md. Written for macOS's GNU Make 3.81.

PY := uv run --quiet build.py
PMTILES := work/bin/go-pmtiles
GO_PMTILES_VERSION := v1.31.2

PLANETILER_IMAGE := ghcr.io/onthegomap/planetiler:0.10.2@sha256:cf32202dbc001a9ab4bc11534b642b13de3798179817da8558e567a3d13dd403
TILESERVER_IMAGE := $(shell sed -n 's/^ *image: *//p' docker-compose.yml)
FONTS_URL := https://github.com/openmaptiles/fonts/releases/download/v2.0/noto-sans.zip
FONTS_SHA256 := d117316544b43a5dde7ee761b36e17701e9f85574e181d76a74814240fdbaf34
# the formatted 64 GB card's free space (63,983,058,944 B on 2026-10-10) minus a 0.5 GB margin
BUDGET_BYTES := 63483058944
# peak use was ~190 GB for a 57 GB archive (extract, MBTiles, convert temp and master side by side)
MIN_FREE_KB := 195312500
WORKERS ?= 16
# the two renderers in docker-compose.yml
RENDER_URLS := http://127.0.0.1:8080/styles/osm-bright/{z}/{x}/{y}.png,http://127.0.0.1:8081/styles/osm-bright/{z}/{x}/{y}.png

.DELETE_ON_ERROR:
.PHONY: all plan renderer renderer-down parity bench card status publish

all: work/verify.txt

# ---- preconditions and tools

work/.preflight:
	@docker info >/dev/null 2>&1 || { echo "Docker is not running"; exit 1; }
	@free=$$(df -k . | awk 'NR==2 {print $$4}'); test "$$free" -ge $(MIN_FREE_KB) || \
	  { echo "need ~200 GB free on this disk, have $$((free / 1000000)) GB"; exit 1; }
	@mkdir -p work && touch $@

$(PMTILES):
	GOBIN=$(CURDIR)/work/bin go install github.com/protomaps/go-pmtiles@$(GO_PMTILES_VERSION)

work/bundles.json:
	$(PY) pin

# ---- the plan: areas.toml -> layers, tile counts, Planetiler bounds, probes. `plan` rewrites a file in
# work/plan/ only when its contents change, so only what depends on that file is rebuilt (a probe
# edit re-runs `verify`; new bounds re-run Planetiler).

work/.plan: areas.toml work/bundles.json
	$(PY) plan
	touch $@

work/plan/plan.json work/plan/probes.json work/plan/bounds.txt work/plan/osm-url.txt: work/.plan ;

plan: work/bundles.json
	$(PY) plan
	touch work/.plan

# ---- vector tiles for the local renderer: the extract and bounds come from the plan

work/osm/extract.osm.pbf: work/plan/osm-url.txt | work/.preflight
	mkdir -p work/osm
	curl -fsSL -o $@.md5 "$$(cat work/plan/osm-url.txt).md5"
	curl -fsSIL "$$(cat work/plan/osm-url.txt)" | awk 'tolower($$1) == "last-modified:" {sub(/^[^:]*: */, ""); v = $$0} END {print v}' \
	  > $@.last-modified
	curl -fL -C - -o $@.part "$$(cat work/plan/osm-url.txt)"
	test "$$(md5 -q $@.part)" = "$$(awk '{print $$1}' $@.md5)" || \
	  { echo "md5 mismatch (Geofabrik published a newer extract, or osm_extract changed?): delete $@.part and re-run"; exit 1; }
	mv $@.part $@

work/vector/openmaptiles.mbtiles: work/osm/extract.osm.pbf work/plan/bounds.txt
	mkdir -p work/planetiler work/vector
	docker run --rm -e JAVA_TOOL_OPTIONS=-Xmx8g \
	  -v "$(CURDIR)/work/planetiler:/data" -v "$(CURDIR)/work/osm:/osm:ro" \
	  $(PLANETILER_IMAGE) --osm-path=/osm/extract.osm.pbf --download --maxzoom=15 --bounds=$$(cat work/plan/bounds.txt) \
	  --output=/data/openmaptiles.mbtiles --force
	mv work/planetiler/openmaptiles.mbtiles $@

# ---- renderer inputs: fonts, style

work/cache/noto-sans.zip:
	mkdir -p work/cache
	curl -fsSL -o $@ $(FONTS_URL)
	test "$$(shasum -a 256 $@ | cut -d' ' -f1)" = "$(FONTS_SHA256)" || { echo "fonts sha256 mismatch"; exit 1; }

work/render/fonts/.done: work/cache/noto-sans.zip
	mkdir -p work/render/fonts
	unzip -qo $< -d work/render/fonts
	test -d "work/render/fonts/Noto Sans Regular" -a -d "work/render/fonts/Noto Sans Bold" \
	  -a -d "work/render/fonts/Noto Sans Italic"
	touch $@

render/styles/osm-bright/style.json: render/styles/osm-bright/style.upstream.json render/config.json
	cd render/styles/osm-bright && shasum -a 256 -c SHA256SUMS
	$(PY) style

# ---- renderer: recreated whenever its inputs change; `renderer` only makes sure it is up

work/.renderer: work/vector/openmaptiles.mbtiles work/render/fonts/.done render/styles/osm-bright/style.json \
                render/config.json docker-compose.yml
	docker compose up -d --wait --force-recreate
	touch $@

renderer: work/.renderer
	docker compose up -d --wait

renderer-down:
	docker compose down

# progress from recorded state (stamps, tile database, fetch.log); safe while a build runs
status:
	@$(PY) status

# ---- gates and tiles

work/.parity.ok: work/.renderer work/plan/plan.json
	$(PY) parity
	touch $@

parity: work/.parity.ok

bench: renderer work/plan/plan.json
	$(PY) bench --workers $(WORKERS) --budget-bytes $(BUDGET_BYTES)

# bundle download (network-bound) and local rendering (CPU-bound) overlap: ~36 min and ~53 min
# measured separately, instead of ~2 h one after the other. Both write work/tiles.mbtiles (WAL).
work/.tiles.done: work/.parity.ok work/plan/plan.json
	$(PY) manifest --planetiler-image $(PLANETILER_IMAGE) --tileserver-image $(TILESERVER_IMAGE) \
	  --fonts-url $(FONTS_URL) --fonts-sha256 $(FONTS_SHA256) --go-pmtiles $(GO_PMTILES_VERSION)
	$(PY) initdb
	$(MAKE) -j2 work/.bundles.done work/.renders.done
	touch $@

work/.bundles.done: work/plan/plan.json
	$(PY) fetch --kind bundle
	touch $@

work/.renders.done: work/.parity.ok work/plan/plan.json | renderer
	$(PY) fetch --kind render --workers $(WORKERS) --render-url '$(RENDER_URLS)'
	touch $@

work/.fit.ok: work/.tiles.done
	$(PY) report --budget-bytes $(BUDGET_BYTES)
	touch $@

# ---- archive

work/osm-bright.pmtiles: work/.fit.ok $(PMTILES)
	$(PY) seal
	rm -rf work/tmp && mkdir -p work/tmp
	$(PMTILES) convert work/tiles.mbtiles work/osm-bright.tmp.pmtiles --tmpdir work/tmp
	$(PMTILES) verify work/osm-bright.tmp.pmtiles
	rm -rf work/tmp
	mv work/osm-bright.tmp.pmtiles $@

work/verify.txt: work/osm-bright.pmtiles $(PMTILES) work/plan/probes.json
	$(PY) verify

card: work/verify.txt
	@test -n "$(CARD)" || { echo "usage: make card CARD=/Volumes/<card name>"; exit 1; }
	$(PY) card --mount "$(CARD)" --eject

# ---- publish: the finished archive and what is not cheap to reproduce (not code, not intermediates)

work/osm-bright.pmtiles.sha256: work/osm-bright.pmtiles
	cd work && shasum -a 256 osm-bright.pmtiles > osm-bright.pmtiles.sha256

PUBLISH_HOST = $(firstword $(subst :, ,$(DEST)))
PUBLISH_PATH = $(word 2,$(subst :, ,$(DEST)))

publish: work/verify.txt work/osm-bright.pmtiles.sha256
	@test -n "$(PUBLISH_PATH)" || { echo "usage: make publish DEST=host:/path"; exit 1; }
	set -o pipefail; rsync -a --mkpath --info=progress2 work/osm-bright.pmtiles work/osm-bright.pmtiles.sha256 \
	  $(DEST)/ | tee work/publish-progress.log
	rsync -a --mkpath work/vector/openmaptiles.mbtiles $(DEST)/vector/
	rsync -a --mkpath work/build-manifest.json work/verify.txt work/bundles.json work/fetch.log \
	  work/plan/plan.json work/plan/probes.json $(DEST)/build/
	rsync -a --mkpath work/parity/report.txt work/parity/sheet.png $(DEST)/build/parity/
	rsync -a --mkpath work/bench/report.txt $(DEST)/build/bench/
	test ! -f work/card.log || rsync -a work/card.log $(DEST)/build/
	@echo "verifying the copy on $(PUBLISH_HOST) (reads it back there) ..."
	test "$$(ssh $(PUBLISH_HOST) sha256sum $(PUBLISH_PATH)/osm-bright.pmtiles | cut -d' ' -f1)" = \
	  "$$(cut -d' ' -f1 work/osm-bright.pmtiles.sha256)"
	echo "$$(date '+%Y-%m-%d %H:%M:%S %Z')  published OK to $(DEST), sha256 $$(cut -c1-16 work/osm-bright.pmtiles.sha256)…" \
	  | tee -a work/publish.log
