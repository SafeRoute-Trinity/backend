#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
GH_DIR="${ROOT_DIR}/graphhopper"
CONFIG_FILE="${GH_DIR}/config.yml"
PBF_FILE="${GH_DIR}/data/map.osm.pbf"
JAR_FILE=""
JAVA_HEAP="${JAVA_HEAP:-4g}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --jar)
      JAR_FILE="$2"
      shift 2
      ;;
    --pbf)
      PBF_FILE="$2"
      shift 2
      ;;
    --config)
      CONFIG_FILE="$2"
      shift 2
      ;;
    --heap)
      JAVA_HEAP="$2"
      shift 2
      ;;
    *)
      echo "Unknown arg: $1"
      exit 1
      ;;
  esac
done

if [[ -z "${JAR_FILE}" ]]; then
  echo "Usage: $0 --jar /path/to/graphhopper-web-<version>.jar [--pbf /path/to/map.osm.pbf] [--config /path/to/config.yml] [--heap 4g]"
  exit 1
fi

if [[ ! -f "${JAR_FILE}" ]]; then
  echo "GraphHopper jar not found: ${JAR_FILE}"
  exit 1
fi

if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "Config not found: ${CONFIG_FILE}"
  exit 1
fi

if [[ ! -d "${GH_DIR}/cache" ]]; then
  echo "graph-cache not found at ${GH_DIR}/cache"
  echo "Run ./scripts/graphhopper_build_cache.sh first."
  exit 1
fi

echo "[graphhopper] Starting server on 8989..."
echo "  jar:    ${JAR_FILE}"
echo "  pbf:    ${PBF_FILE}"
echo "  config: ${CONFIG_FILE}"
echo "  cache:  ${GH_DIR}/cache"

java -Xmx"${JAVA_HEAP}" \
  -D"dw.graphhopper.datareader.file=${PBF_FILE}" \
  -D"dw.graphhopper.graph.location=${GH_DIR}/cache" \
  -jar "${JAR_FILE}" \
  server "${CONFIG_FILE}"
