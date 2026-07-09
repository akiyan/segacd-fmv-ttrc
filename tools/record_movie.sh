#!/usr/bin/env bash
#
# record_movie.sh - record the Mega-CD disc playback to a shareable mp4.
#
# Wraps tools/run_headless.sh --record-realtime (RetroArch + Genesis Plus GX
# FFmpeg recorder under Xvfb), selects an audible window by default, and
# transcodes it to an H.264 + AAC mp4 for sharing.
#
# The recording is the emulator's own synchronized A/V output (video + whatever
# audio the build produces) - not an offline re-mux of PROBE.BIN.
#
# Usage:
#   tools/record_movie.sh                      # record out/SCFMV_MCD.cue, ~160s
#   tools/record_movie.sh --seconds 30         # short clip
#   tools/record_movie.sh --disc out/SCFMV_MCD.cue --out tmp/op.mp4 --seconds 160
#
# Options:
#   --disc CUE     disc image to boot (default out/SCFMV_MCD.cue)
#   --out MP4      output mp4 (default tmp/<tag>_movie.mp4)
#   --seconds N    seconds of playback to include in the MP4 (default 160)
#   --trim SEC     seconds to drop from the front; disables auto-audio-trim
#                  (default: auto-audio-trim)
#   --tag NAME     work prefix under tmp/ (default: rec_<disc basename>)
#   --display :N   X display for Xvfb (default :236)
#   --preset NAME  run_headless record preset, or realtime (default realtime)
#   --audio-jump-threshold N
#                  pass through to run_headless (default: run_headless default)
#   --audio-min-rms N
#                  fail pre/post-transcode checks if RMS is below N
#   --auto-audio-trim
#                  choose the trim position from the loudest recorded WAV window
#   --no-audio-check
#                  pass through to run_headless
#   --no-build     do not run `make disc` first
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DISC="out/SCFMV_MCD.cue"
OUT=""
REC_SECS=160
TRIM=8
TAG=""
DISPLAY_NUM=":236"
PRESET="realtime"
BUILD=1
AUDIO_CHECK_ARGS=()
AUTO_AUDIO_TRIM=1

while [ $# -gt 0 ]; do
  case "$1" in
    --disc) DISC="$2"; shift 2;;
    --out) OUT="$2"; shift 2;;
    --seconds) REC_SECS="$2"; shift 2;;
    --trim) TRIM="$2"; AUTO_AUDIO_TRIM=0; shift 2;;
    --tag) TAG="$2"; shift 2;;
    --display) DISPLAY_NUM="$2"; shift 2;;
    --preset) PRESET="$2"; shift 2;;
    --audio-jump-threshold) AUDIO_CHECK_ARGS+=(--audio-jump-threshold "$2"); shift 2;;
    --audio-min-rms) AUDIO_CHECK_ARGS+=(--audio-min-rms "$2"); shift 2;;
    --auto-audio-trim) AUTO_AUDIO_TRIM=1; shift;;
    --no-audio-check) AUDIO_CHECK_ARGS+=(--no-audio-check); shift;;
    --no-build) BUILD=0; shift;;
    -h|--help) sed -n '2,34p' "$0"; exit 0;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done

[ -z "$TAG" ] && TAG="rec_$(basename "${DISC%.*}")"
[ -z "$OUT" ] && OUT="tmp/${TAG}_movie.mp4"
command -v ffmpeg >/dev/null 2>&1 || { echo "missing ffmpeg" >&2; exit 1; }

if [ "$BUILD" -eq 1 ]; then
  echo ">> make disc"
  make disc >/dev/null
fi
[ -f "$DISC" ] || { echo "disc not found: $DISC (drop --no-build or build it)" >&2; exit 1; }

# run_headless records launch->Escape. The capture loop (shots*interval) is the
# window during which playback is recorded; size it to cover trim + requested
# seconds plus a small margin.
INTERVAL=2
SHOTS=$(( (TRIM + REC_SECS + 10 + INTERVAL - 1) / INTERVAL ))

echo ">> recording ${REC_SECS}s of $DISC (preset $PRESET) ..."
if [ "$PRESET" = "realtime" ]; then
  tools/run_headless.sh "$DISC" --tag "$TAG" \
    --record-realtime \
    --shots "$SHOTS" --interval "$INTERVAL" --display "$DISPLAY_NUM" \
    "${AUDIO_CHECK_ARGS[@]}"
else
  tools/run_headless.sh "$DISC" --tag "$TAG" \
    --record --record-preset "$PRESET" \
    --shots "$SHOTS" --interval "$INTERVAL" --display "$DISPLAY_NUM" \
    "${AUDIO_CHECK_ARGS[@]}"
fi

MKV="tmp/${TAG}.mkv"
[ -f "$MKV" ] || { echo "recording not produced: $MKV (see tmp/retroarch_${TAG}.log)" >&2; exit 1; }
if [ "${#AUDIO_CHECK_ARGS[@]}" -eq 0 ] || [[ " ${AUDIO_CHECK_ARGS[*]} " != *" --no-audio-check "* ]]; then
  [ -s "tmp/${TAG}_audio.json" ] || { echo "audio check report not produced: tmp/${TAG}_audio.json" >&2; exit 1; }
fi

MIN_RMS=0
for ((i = 0; i < ${#AUDIO_CHECK_ARGS[@]}; i++)); do
  if [ "${AUDIO_CHECK_ARGS[$i]}" = "--audio-min-rms" ]; then
    MIN_RMS="${AUDIO_CHECK_ARGS[$((i + 1))]}"
  fi
done
if [ "$AUTO_AUDIO_TRIM" -eq 1 ]; then
  [ -s "tmp/${TAG}.wav" ] || { echo "auto audio trim requires tmp/${TAG}.wav" >&2; exit 1; }
  [ "$MIN_RMS" = "0" ] && MIN_RMS=1
  TRIM="$(python3 - "tmp/${TAG}.wav" "$REC_SECS" "$MIN_RMS" <<'PY'
import math
import struct
import sys
import wave

path, seconds_s, min_rms_s = sys.argv[1:]
seconds = int(seconds_s)
min_rms = float(min_rms_s)

with wave.open(path, "rb") as wav:
    rate = wav.getframerate()
    channels = wav.getnchannels()
    frames = wav.getnframes()
    data = wav.readframes(frames)

samples = struct.unpack("<%dh" % (len(data) // 2), data)
window_samples = seconds * rate * channels
step_samples = rate * channels

best_start = 0
best_rms = -1.0
last = max(0, len(samples) - window_samples)
for start in range(0, last + 1, step_samples):
    seg = samples[start : start + window_samples]
    if not seg:
        continue
    rms = math.sqrt(sum(sample * sample for sample in seg) / len(seg))
    if rms > best_rms:
        best_rms = rms
        best_start = start

if best_rms < min_rms:
    raise SystemExit(f"no audio window reached min_rms={min_rms}; best_rms={best_rms:.4f}")

print(best_start // step_samples)
print(f"auto audio trim: start={best_start // step_samples}s rms={best_rms:.4f}", file=sys.stderr)
PY
)"
  echo ">> auto audio trim selected ${TRIM}s (min_rms=$MIN_RMS)"
fi

echo ">> trimming ${TRIM}s boot and transcoding -> $OUT"
ffmpeg -y -hide_banner -loglevel error -ss "$TRIM" -i "$MKV" \
  -t "$REC_SECS" \
  -c:v libx264 -crf 18 -pix_fmt yuv420p \
  -c:a aac -b:a 128k -movflags +faststart "$OUT"

if [ "${#AUDIO_CHECK_ARGS[@]}" -eq 0 ] || [[ " ${AUDIO_CHECK_ARGS[*]} " != *" --no-audio-check "* ]]; then
  OUT_WAV="${OUT%.*}_audio.wav"
  OUT_JSON="${OUT%.*}_audio.json"
  THRESHOLD=12000
  for ((i = 0; i < ${#AUDIO_CHECK_ARGS[@]}; i++)); do
    if [ "${AUDIO_CHECK_ARGS[$i]}" = "--audio-jump-threshold" ]; then
      THRESHOLD="${AUDIO_CHECK_ARGS[$((i + 1))]}"
    elif [ "${AUDIO_CHECK_ARGS[$i]}" = "--audio-min-rms" ]; then
      MIN_RMS="${AUDIO_CHECK_ARGS[$((i + 1))]}"
    fi
  done
  ffmpeg -y -hide_banner -loglevel error -i "$OUT" -vn -ar 44100 "$OUT_WAV"
  python3 tools/analyze_recorded_audio.py "$OUT" \
    --wav "$OUT_WAV" \
    --seconds 12 \
    --jump-threshold "$THRESHOLD" \
    --min-rms "$MIN_RMS" \
    --fail-on-clicks > "$OUT_JSON"
  echo "mp4 audio check: $OUT_JSON (jump_threshold=$THRESHOLD min_rms=$MIN_RMS)"
fi

echo "OUT=$OUT"
ffprobe -hide_banner "$OUT" 2>&1 | grep -E 'Duration|Stream' || true
