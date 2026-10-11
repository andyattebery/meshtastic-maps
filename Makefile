# Repeatable build of the T-Deck map card archive from the nested map areas in areas.toml.
#
#   make plan                         what areas.toml derives to: sizes vs the card, what one zoom
#                                     deeper/shallower costs, extract, bounds, probes (no tiles fetched)
#   make bundles [AT=lon,lat]         Oxed's bundles, for a tier's `bundles`
#   make bench                        measure render speed and size from a 1% sample (scratch database)
#   make                              build and verify work/osm-bright.pmtiles
#   make card CARD=/Volumes/MESHMAP   copy it to a mounted exFAT card (full read-back check)
#   make renderer-down                stop the local renderer
#
# Design and measurements: docs/DESIGN.md. Written for macOS's GNU Make 3.81.

PY := uv run --quiet build.py
PMTILES := work/bin/go-pmtiles
# go-pmtiles release binaries and their sha256 (from the GitHub release's asset digests)
GO_PMTILES_VERSION := 1.31.2
GO_PMTILES_SHA256_arm64 := 40528f7f616fcbf91207cd48c8fc023d213f6d86c0cbf1f748732803d1880f3d
GO_PMTILES_SHA256_x86_64 := 1f0dc02eee6c58312dd6c509faee1b5c32f0596568af1bf51f1b034e7a88a65b
ARCH := $(shell uname -m)

PLANETILER_IMAGE := ghcr.io/onthegomap/planetiler:0.10.2@sha256:cf32202dbc001a9ab4bc11534b642b13de3798179817da8558e567a3d13dd403
TILESERVER_IMAGE := $(shell sed -n 's/^ *image: *//p' docker-compose.yml)
FONTS_URL := https://github.com/openmaptiles/fonts/releases/download/v2.0/noto-sans.zip
FONTS_SHA256 := d117316544b43a5dde7ee761b36e17701e9f85574e181d76a74814240fdbaf34
# Planetiler's Java heap; preflight checks Docker has at least this much memory
PLANETILER_HEAP_GB := 8
WORKERS ?= 16
# the two renderers in docker-compose.yml
RENDER_URLS := http://127.0.0.1:8080/styles/osm-bright/{z}/{x}/{y}.png,http://127.0.0.1:8081/styles/osm-bright/{z}/{x}/{y}.png

.DELETE_ON_ERROR:
.PHONY: all plan bundles renderer renderer-down parity bench card status publish

all: work/verify.txt

# ---- preconditions and tools

# Docker running with memory for Planetiler, and disk for this plan's peak; go-pmtiles fetched up front
work/.preflight: work/plan/plan.json work/plan/extract-bytes.txt $(PMTILES)
	$(PY) preflight --heap-gb $(PLANETILER_HEAP_GB)
	touch $@

$(PMTILES):
	mkdir -p work/bin work/cache
	curl -fsSL -o work/cache/go-pmtiles.zip \
	  https://github.com/protomaps/go-pmtiles/releases/download/v$(GO_PMTILES_VERSION)/go-pmtiles-$(GO_PMTILES_VERSION)_Darwin_$(ARCH).zip
	test "$$(shasum -a 256 work/cache/go-pmtiles.zip | cut -d' ' -f1)" = "$(GO_PMTILES_SHA256_$(ARCH))" || \
	  { echo "go-pmtiles download: sha256 mismatch (or no release for $(ARCH))"; exit 1; }
	unzip -qo work/cache/go-pmtiles.zip pmtiles -d work/bin
	mv work/bin/pmtiles $@

bundles:
	@$(PY) bundles $(if $(AT),--at=$(AT))

# ---- the plan: areas.toml -> layers, sizes, extract, Planetiler bounds, probes. `pin` pins bundles
# areas.toml names for the first time. `plan` rewrites a file in work/plan/ only when its contents
# change, so only what depends on that file is rebuilt (a probe edit re-runs `verify`; new bounds re-run
# Planetiler; a new card re-runs the size gates).

work/.plan: areas.toml $(wildcard work/bundles.json)
	$(PY) pin
	$(PY) plan
	touch $@

work/plan/plan.json work/plan/probes.json work/plan/bounds.txt work/plan/osm-url.txt \
  work/plan/budget.txt work/plan/extract-bytes.txt: work/.plan ;

plan:
	$(PY) pin
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
	docker run --rm -e JAVA_TOOL_OPTIONS=-Xmx$(PLANETILER_HEAP_GB)g \
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

bench: renderer work/plan/plan.json work/plan/budget.txt
	$(PY) bench --workers $(WORKERS) --render-url '$(RENDER_URLS)'

# size gate: a 1% render sample into the real database (the fetch skips those tiles) projects the
# archive; fails if it is clearly over the card, before the hours of fetching
work/.size.ok: work/.parity.ok work/plan/plan.json work/plan/budget.txt | renderer
	$(PY) initdb
	$(PY) size-gate --workers $(WORKERS) --render-url '$(RENDER_URLS)'
	touch $@

# bundle download (network-bound) and local rendering (CPU-bound) overlap: ~36 min and ~53 min
# measured separately, instead of ~2 h one after the other. Both write work/tiles.mbtiles (WAL).
work/.tiles.done: work/.size.ok work/plan/plan.json
	$(PY) manifest --planetiler-image $(PLANETILER_IMAGE) --tileserver-image $(TILESERVER_IMAGE) \
	  --fonts-url $(FONTS_URL) --fonts-sha256 $(FONTS_SHA256) --go-pmtiles $(GO_PMTILES_VERSION)
	$(MAKE) -j2 work/.bundles.done work/.renders.done
	touch $@

work/.bundles.done: work/plan/plan.json
	$(PY) fetch --kind bundle
	touch $@

work/.renders.done: work/.parity.ok work/plan/plan.json | renderer
	$(PY) fetch --kind render --workers $(WORKERS) --render-url '$(RENDER_URLS)'
	touch $@

work/.fit.ok: work/.tiles.done work/plan/budget.txt
	$(PY) report --fit
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
	rsync -a --mkpath work/size/report.txt $(DEST)/build/size/
	test ! -f work/bench/report.txt || rsync -a --mkpath work/bench/report.txt $(DEST)/build/bench/
	test ! -f work/card.log || rsync -a work/card.log $(DEST)/build/
	@echo "verifying the copy on $(PUBLISH_HOST) (reads it back there) ..."
	test "$$(ssh $(PUBLISH_HOST) sha256sum $(PUBLISH_PATH)/osm-bright.pmtiles | cut -d' ' -f1)" = \
	  "$$(cut -d' ' -f1 work/osm-bright.pmtiles.sha256)"
	echo "$$(date '+%Y-%m-%d %H:%M:%S %Z')  published OK to $(DEST), sha256 $$(cut -c1-16 work/osm-bright.pmtiles.sha256)…" \
	  | tee -a work/publish.log
