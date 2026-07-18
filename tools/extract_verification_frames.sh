#!/usr/bin/env bash
# Extract named verification stills into a source-specific, never-reused
# directory. The printed CHECK_DIR contains only this invocation's stills, an
# explicit manifest, and a montage assembled from the in-memory file list.
# Existing PNGs under BASE_DIR are never globbed into the result.
usage() {
  echo "usage: $0 INPUT BASE_DIR LABEL=SECONDS [LABEL=SECONDS ...]" >&2
}
set -euo pipefail

if [ "$#" -lt 3 ]; then
  usage
  exit 2
fi

INPUT="$1"
BASE_DIR="$2"
shift 2

if [ ! -f "$INPUT" ]; then
  echo "input video not found: $INPUT" >&2
  exit 1
fi
command -v ffmpeg >/dev/null 2>&1 || { echo "missing ffmpeg" >&2; exit 1; }
command -v montage >/dev/null 2>&1 || { echo "missing montage" >&2; exit 1; }
command -v sha256sum >/dev/null 2>&1 || { echo "missing sha256sum" >&2; exit 1; }

# Reject malformed or duplicate requests before creating a check directory.
declare -A REQUESTED_LABELS=()
for SPEC in "$@"; do
  if [[ "$SPEC" != *=* ]]; then
    echo "frame must be LABEL=SECONDS: $SPEC" >&2
    exit 2
  fi
  LABEL="${SPEC%%=*}"
  AT_SECONDS="${SPEC#*=}"
  if [[ ! "$LABEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "unsafe frame label: $LABEL" >&2
    exit 2
  fi
  if [[ ! "$AT_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "invalid non-negative timestamp: $AT_SECONDS" >&2
    exit 2
  fi
  if [[ -n "${REQUESTED_LABELS[$LABEL]+present}" ]]; then
    echo "duplicate frame label: $LABEL" >&2
    exit 2
  fi
  REQUESTED_LABELS[$LABEL]=1
done

INPUT_ABS="$(realpath "$INPUT")"
SOURCE_NAME="$(basename "$INPUT_ABS")"
SOURCE_STEM="${SOURCE_NAME%.*}"
RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BASE_DIR"
RUN_DIR="$(mktemp -d "$BASE_DIR/${SOURCE_STEM}_${RUN_STAMP}_XXXXXX")"
MANIFEST="$RUN_DIR/manifest.tsv"
INCOMPLETE="$RUN_DIR/.incomplete"
: > "$INCOMPLETE"

SOURCE_SHA_LINE="$(sha256sum "$INPUT_ABS")"
SOURCE_SHA="${SOURCE_SHA_LINE%% *}"
{
  printf 'source\t%s\n' "$INPUT_ABS"
  printf 'source_size\t%s\n' "$(stat -c '%s' "$INPUT_ABS")"
  printf 'source_mtime\t%s\n' "$(stat -c '%y' "$INPUT_ABS")"
  printf 'source_sha256\t%s\n' "$SOURCE_SHA"
  printf 'label\tseconds\tfile\tsha256\n'
} > "$MANIFEST"

FRAMES=()
for SPEC in "$@"; do
  LABEL="${SPEC%%=*}"
  AT_SECONDS="${SPEC#*=}"
  OUT="$RUN_DIR/${LABEL}.png"
  ffmpeg -y -hide_banner -loglevel error -ss "$AT_SECONDS" -i "$INPUT_ABS" \
    -frames:v 1 "$OUT"
  if [ ! -s "$OUT" ]; then
    echo "frame extraction produced no image: $LABEL=$AT_SECONDS" >&2
    exit 1
  fi
  FRAME_SHA_LINE="$(sha256sum "$OUT")"
  FRAME_SHA="${FRAME_SHA_LINE%% *}"
  printf '%s\t%s\t%s\t%s\n' "$LABEL" "$AT_SECONDS" "$(basename "$OUT")" "$FRAME_SHA" \
    >> "$MANIFEST"
  FRAMES+=("$OUT")
done

MONTAGE="$RUN_DIR/montage.png"
montage "${FRAMES[@]}" -filter point -tile 4x -geometry '640x448+4+24' \
  -set label '%t' "$MONTAGE"
if [ ! -s "$MONTAGE" ]; then
  echo "verification montage was not produced" >&2
  exit 1
fi

rm -f "$INCOMPLETE"
printf 'CHECK_DIR=%s\n' "$RUN_DIR"
printf 'MANIFEST=%s\n' "$MANIFEST"
printf 'MONTAGE=%s\n' "$MONTAGE"
