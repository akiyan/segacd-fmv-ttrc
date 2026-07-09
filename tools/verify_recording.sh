#!/usr/bin/env bash
#
# verify_recording.sh - post-check a recorded MP4/MKV audio stream.
#
# Extracts synchronized emulator audio from an existing recording, then runs the
# same click/clip/silence detector used by the headless recording harness.
#
# Usage:
#   tools/verify_recording.sh tmp/rec_final_160s_movie.mp4
#   tools/verify_recording.sh tmp/rec_final_160s_movie.mp4 --out-prefix tmp/check
#
# Options:
#   --out-prefix NAME     output prefix for NAME_verify.wav/json
#   --jump-threshold N   per-channel sample jump threshold (default: 12000)
#   --min-rms N          fail if extracted WAV RMS is below N (default: 1)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RECORDING=""
OUT_PREFIX=""
JUMP_THRESHOLD=12000
MIN_RMS=1

while [ $# -gt 0 ]; do
  case "$1" in
    --out-prefix) OUT_PREFIX="$2"; shift 2;;
    --jump-threshold) JUMP_THRESHOLD="$2"; shift 2;;
    --min-rms) MIN_RMS="$2"; shift 2;;
    -h|--help) sed -n '2,15p' "$0"; exit 0;;
    -*) echo "unknown option: $1" >&2; exit 2;;
    *) RECORDING="$1"; shift;;
  esac
done

[ -n "$RECORDING" ] || { echo "usage: $0 <recording.mp4|recording.mkv> [options]" >&2; exit 2; }
[ -s "$RECORDING" ] || { echo "recording not found: $RECORDING" >&2; exit 1; }
command -v ffmpeg >/dev/null 2>&1 || { echo "missing ffmpeg" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "missing python3" >&2; exit 1; }

if [ -z "$OUT_PREFIX" ]; then
  OUT_PREFIX="${RECORDING%.*}"
fi
WAV="${OUT_PREFIX}_verify.wav"
JSON="${OUT_PREFIX}_verify.json"

ffmpeg -y -hide_banner -loglevel error -i "$RECORDING" -vn -ar 44100 "$WAV"
python3 tools/analyze_recorded_audio.py "$RECORDING" \
  --wav "$WAV" \
  --jump-threshold "$JUMP_THRESHOLD" \
  --min-rms "$MIN_RMS" \
  --fail-on-clicks > "$JSON"

echo "audio:  $WAV"
echo "report: $JSON"
