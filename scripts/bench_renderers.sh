#!/usr/bin/env bash
# Render throughput with N renderer containers in parallel, for comparing build hosts.
#
#   scripts/bench_renderers.sh N [tiles_per_renderer_fraction]
#
# Starts N TileServer GL containers (image pinned in docker-compose.yml) on 127.0.0.1:18080..,
# runs one `build.py fetch` per container on a fixed sample of the plan's deepest render layer
# (0.4% each; 4,975 tiles for this build's chicago-metro-z18; seeds 101..), prints the combined tiles/s,
# then removes the containers and scratch databases.
# Needs: render/, work/render/fonts/, work/vector/openmaptiles.mbtiles, work/plan/plan.json, uv, docker.
set -euo pipefail
cd "$(dirname "$0")/.."

n=${1:?usage: scripts/bench_renderers.sh N [sample]}
sample=${2:-0.004}
image=$(sed -n 's/^ *image: *//p' docker-compose.yml)
names=()

cleanup() {
  for c in "${names[@]}"; do docker rm -f "$c" >/dev/null 2>&1 || true; done
  rm -f work/bench-r*.mbtiles
}
trap cleanup EXIT

for i in $(seq 0 $((n - 1))); do
  name="mm-bench-$i"
  names+=("$name")
  docker run -d --rm --name "$name" -p "127.0.0.1:$((18080 + i)):8080" \
    -v "$PWD/render/config.json:/data/config.json:ro" -v "$PWD/render/styles:/data/styles:ro" \
    -v "$PWD/work/render/fonts:/data/fonts:ro" -v "$PWD/work/vector:/data/vector:ro" \
    "$image" --config /data/config.json --port 8080 >/dev/null
done
for i in $(seq 0 $((n - 1))); do
  until curl -sf -o /dev/null "http://127.0.0.1:$((18080 + i))/styles/osm-bright.json"; do sleep 1; done
done

rm -f work/bench-r*.mbtiles
layer=$(python3 -c 'import json; print([l["name"] for l in json.load(open("work/plan/plan.json"))["layers"] if l["kind"] == "render"][-1])')
pids=()
for i in $(seq 0 $((n - 1))); do
  uv run --quiet build.py fetch --db "work/bench-r$i.mbtiles" --layers "$layer" --sample "$sample" \
    --seed $((101 + i)) --workers 16 \
    --render-url "http://127.0.0.1:$((18080 + i))/styles/osm-bright/{z}/{x}/{y}.png" \
    > "work/bench-r$i.log" 2>&1 &
  pids+=($!)
done
for p in "${pids[@]}"; do wait "$p"; done

# rate = all tiles / the slowest render loop ("... N tiles/s over S s" in each log; excludes start-up)
tiles=0
secs=0
for i in $(seq 0 $((n - 1))); do
  line=$(grep 'done:' "work/bench-r$i.log") || { echo "renderer $i: no result"; cat "work/bench-r$i.log"; exit 1; }
  read -r done failed s <<<"$(sed -E 's/.*done: ([0-9]+) rendered, ([0-9]+) failed.* over ([0-9]+) s.*/\1 \2 \3/' <<<"$line")"
  [ "$failed" = 0 ] || { echo "renderer $i had $failed failures"; exit 1; }
  tiles=$((tiles + done))
  [ "$s" -gt "$secs" ] && secs=$s
done
rm -f work/bench-r*.log
echo "$(hostname -s) renderers=$n tiles=$tiles slowest_loop=${secs}s rate=$((tiles / secs)) tiles/s (cpus $(getconf _NPROCESSORS_ONLN))"
