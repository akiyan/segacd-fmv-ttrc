#!/usr/bin/env bash
#
# record_movie.sh - record Sega CD playback and create a verification preview.
#
# Wraps tools/run_headless.sh (RetroArch + Genesis Plus GX FFmpeg recorder under
# Xvfb), keeps the Mega-CD startup sequence, records FFV1/FLAC through a fixed
# input Replay at uncapped speed by default, and transcodes it to an H.264 + AAC
# preview for quick verification.
#
# The recording is the emulator's own synchronized A/V output (video + whatever
# audio the build produces) - not an offline re-mux of PROBE.BIN.
#
# Usage:
#   tools/record_movie.sh --config configs/bad-apple-h32.toml --seconds 160
#   tools/record_movie.sh --config configs/bad-apple-h32.toml --seconds 30
#   tools/record_movie.sh --disc out/bad-apple-h32.cue --no-build --seconds 160
#
# Options:
#   --config TOML  profile used by make; also derives out/<toml-stem>.cue
#   --disc CUE     explicit disc image to boot (normally derived from --config)
#   --out MP4      verification preview (default videos/<tag>_preview.mp4)
#   --seconds N    seconds to keep in the bounded native MKV/preview (default 160)
#   --trim SEC     seconds to drop from the front; disables auto-audio-trim
#                  (default: 0, preserving the startup sequence)
#   --tag NAME     work prefix under the capture dir (default: rec_<disc basename>)
#   --display :N   X display for Xvfb (default :236)
#   --preset NAME  ffv1-flac (pixel-lossless default) or realtime (paced 4:2:0 check)
#   --offline-record
#                  explicitly select the default fixed-Replay uncapped mode
#   --realtime-lossless
#                  use the legacy wall-clock-paced FFV1/FLAC path
#   --input-replay FILE
#                  reuse an input replay for an exact-frame offline or realtime run
#   --record-size WxH
#                  native recording surface (H32: 256x224, H40: 320x224)
#   --auto-audio-trim
#                  explicitly choose a movie-only window from the recorded WAV
#   --no-build     do not run `make disc` first
#   --release-build
#                  build with DEBUG=0 instead of the recording default DEBUG=1
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-$ROOT/tools/python.sh}"
export PYTHON

CONFIG=""
DISC=""
OUT=""
REC_SECS=160
TRIM=0
TAG=""
DISPLAY_NUM=":236"
PRESET="ffv1-flac"
RECORD_SIZE=""
BUILD=1
BUILD_DEBUG=1
AUTO_AUDIO_TRIM=0
OFFLINE_RECORD=1
OFFLINE_REQUESTED=0
REALTIME_LOSSLESS_REQUESTED=0
INPUT_REPLAY=""

while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2;;
    --disc) DISC="$2"; shift 2;;
    --out) OUT="$2"; shift 2;;
    --seconds) REC_SECS="$2"; shift 2;;
    --trim) TRIM="$2"; AUTO_AUDIO_TRIM=0; shift 2;;
    --tag) TAG="$2"; shift 2;;
    --display) DISPLAY_NUM="$2"; shift 2;;
    --preset) PRESET="$2"; shift 2;;
    --offline-record) OFFLINE_RECORD=1; OFFLINE_REQUESTED=1; shift;;
    --realtime-lossless) OFFLINE_RECORD=0; REALTIME_LOSSLESS_REQUESTED=1; shift;;
    --input-replay) INPUT_REPLAY="$2"; shift 2;;
    --record-size) RECORD_SIZE="$2"; shift 2;;
    --auto-audio-trim) AUTO_AUDIO_TRIM=1; shift;;
    --no-build) BUILD=0; shift;;
    --release-build) BUILD_DEBUG=0; shift;;
    -h|--help) sed -n '2,/^set -euo pipefail/p' "$0" | sed '$d'; exit 0;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done

if [ "$OFFLINE_REQUESTED" -eq 1 ] && [ "$REALTIME_LOSSLESS_REQUESTED" -eq 1 ]; then
  echo "--offline-record and --realtime-lossless are mutually exclusive" >&2
  exit 2
fi
if [ "$REALTIME_LOSSLESS_REQUESTED" -eq 1 ] && [ "$PRESET" != "ffv1-flac" ]; then
  echo "--realtime-lossless requires --preset ffv1-flac" >&2
  exit 2
fi
if [ "$PRESET" = "realtime" ]; then
  if [ "$OFFLINE_REQUESTED" -eq 1 ]; then
    echo "--offline-record does not support --preset realtime" >&2
    exit 2
  fi
  OFFLINE_RECORD=0
fi

if [ -n "$CONFIG" ]; then
  CONFIG_STEM="$("$PYTHON" tools/encode_config.py "$CONFIG" --print-stem)"
  CONFIG_DISC="out/${CONFIG_STEM}.cue"
  if [ -z "$DISC" ]; then
    DISC="$CONFIG_DISC"
  elif [ "$BUILD" -eq 1 ] && [ "$DISC" != "$CONFIG_DISC" ]; then
    echo "--disc $DISC does not match --config output $CONFIG_DISC" >&2
    exit 2
  fi
fi
if [ "$BUILD" -eq 1 ] && [ -z "$CONFIG" ]; then
  echo "--config is required when building; use --disc CUE --no-build for an existing disc" >&2
  exit 2
fi
if [ -z "$DISC" ]; then
  echo "provide --config TOML, or --disc CUE together with --no-build" >&2
  exit 2
fi
if [[ ! "$REC_SECS" =~ ^[1-9][0-9]*$ ]]; then
  echo "--seconds must be a positive integer: $REC_SECS" >&2
  exit 2
fi
if [[ ! "$TRIM" =~ ^(0|[1-9][0-9]*)$ ]]; then
  echo "--trim must be a non-negative integer: $TRIM" >&2
  exit 2
fi
if [ "$OFFLINE_RECORD" -eq 1 ] && [ "$PRESET" != "ffv1-flac" ]; then
  echo "offline recording only supports --preset ffv1-flac" >&2
  exit 2
fi
if [ -n "$INPUT_REPLAY" ] && [ ! -f "$INPUT_REPLAY" ]; then
  echo "input replay not found: $INPUT_REPLAY" >&2
  exit 1
fi

[ -z "$TAG" ] && TAG="rec_$(basename "${DISC%.*}")"
[ -z "$OUT" ] && OUT="videos/${TAG}_preview.mp4"
CAPTURE_DIR="${OUTDIR:-$ROOT/videos}"
mkdir -p "$CAPTURE_DIR" "$(dirname "$OUT")"
command -v ffmpeg >/dev/null 2>&1 || { echo "missing ffmpeg" >&2; exit 1; }
command -v ffprobe >/dev/null 2>&1 || { echo "missing ffprobe" >&2; exit 1; }

if [ "$BUILD" -eq 1 ]; then
  if [ "$BUILD_DEBUG" -eq 1 ]; then
    echo ">> make disc CONFIG=$CONFIG DEBUG=1 (recording default)"
  else
    echo ">> make disc CONFIG=$CONFIG DEBUG=0 (explicit release build)"
  fi
  make disc CONFIG="$CONFIG" DEBUG="$BUILD_DEBUG" PYTHON="$PYTHON" >/dev/null
fi
[ -f "$DISC" ] || { echo "disc not found: $DISC (drop --no-build or build it)" >&2; exit 1; }

# run_headless records launch->Escape. The capture loop (shots*interval) is the
# window during which paced playback is recorded; size it to cover trim plus the
# requested seconds and a small margin. Offline is the default and uses the same
# duration to derive an exact emulator-frame limit. Auto trim is an explicit
# movie-only mode, so give its window enough extra material to search.
INTERVAL=2
CAPTURE_LEAD="$TRIM"
[ "$AUTO_AUDIO_TRIM" -eq 1 ] && CAPTURE_LEAD=30
SHOTS=$(( (CAPTURE_LEAD + REC_SECS + 10 + INTERVAL - 1) / INTERVAL ))
REPLAY_FILE="$INPUT_REPLAY"
MAX_FRAMES=""
PIPELINE_WALL_START_NS=""
if [ "$OFFLINE_RECORD" -eq 1 ] || [ -n "$INPUT_REPLAY" ]; then
  RAW_EMULATED_SECONDS=$((CAPTURE_LEAD + REC_SECS + 10))
  MAX_FRAMES=$((RAW_EMULATED_SECONDS * 60))
  if [ "$RAW_EMULATED_SECONDS" -le 0 ] || [ "$MAX_FRAMES" -le 0 ]; then
    echo "capture duration is outside the supported integer range" >&2
    exit 2
  fi
  PIPELINE_WALL_START_NS="$(date +%s%N)"
fi

if [ "$OFFLINE_RECORD" -eq 1 ] && [ -z "$REPLAY_FILE" ]; then
  DISC_STEM="$(basename "${DISC%.*}")"
  REPLAY_DIR="$ROOT/tmp/$DISC_STEM/record"
  REPLAY_FILE="$REPLAY_DIR/${TAG}_input.replay"
  REPLAY_MAX_FRAMES=$((MAX_FRAMES + 120))
  echo ">> generating input replay ($REPLAY_MAX_FRAMES frames) -> $REPLAY_FILE"
  OUTDIR="$REPLAY_DIR" tools/run_headless.sh "$DISC" --tag "${TAG}_replay" \
    --record-replay "$REPLAY_FILE" --max-frames "$REPLAY_MAX_FRAMES" \
    --boot-wait 1 --presses 20 --press-gap 1 --display "$DISPLAY_NUM"
  [ -s "$REPLAY_FILE" ] || { echo "input replay not produced: $REPLAY_FILE" >&2; exit 1; }
fi

RECORD_MODE="offline"
[ "$OFFLINE_RECORD" -eq 0 ] && RECORD_MODE="realtime"
echo ">> recording ${REC_SECS}s of $DISC (preset $PRESET, mode $RECORD_MODE) ..."
RECORD_SIZE_ARGS=()
[ -n "$RECORD_SIZE" ] && RECORD_SIZE_ARGS=(--record-size "$RECORD_SIZE")
if [ "$OFFLINE_RECORD" -eq 1 ]; then
  OUTDIR="$CAPTURE_DIR" tools/run_headless.sh "$DISC" --tag "$TAG" \
    --record-offline --max-frames "$MAX_FRAMES" --play-replay "$REPLAY_FILE" \
    --display "$DISPLAY_NUM" \
    "${RECORD_SIZE_ARGS[@]}"
elif [ -n "$INPUT_REPLAY" ] && [ "$PRESET" = "realtime" ]; then
  OUTDIR="$CAPTURE_DIR" tools/run_headless.sh "$DISC" --tag "$TAG" \
    --record-realtime \
    --max-frames "$MAX_FRAMES" --play-replay "$REPLAY_FILE" \
    --shots "$SHOTS" --interval "$INTERVAL" --display "$DISPLAY_NUM" \
    "${RECORD_SIZE_ARGS[@]}"
elif [ -n "$INPUT_REPLAY" ]; then
  OUTDIR="$CAPTURE_DIR" tools/run_headless.sh "$DISC" --tag "$TAG" \
    --record --record-preset "$PRESET" \
    --max-frames "$MAX_FRAMES" --play-replay "$REPLAY_FILE" \
    --shots "$SHOTS" --interval "$INTERVAL" --display "$DISPLAY_NUM" \
    "${RECORD_SIZE_ARGS[@]}"
elif [ "$PRESET" = "realtime" ]; then
  OUTDIR="$CAPTURE_DIR" tools/run_headless.sh "$DISC" --tag "$TAG" \
    --record-realtime \
    --shots "$SHOTS" --interval "$INTERVAL" --display "$DISPLAY_NUM" \
    "${RECORD_SIZE_ARGS[@]}"
else
  OUTDIR="$CAPTURE_DIR" tools/run_headless.sh "$DISC" --tag "$TAG" \
    --record --record-preset "$PRESET" \
    --shots "$SHOTS" --interval "$INTERVAL" --display "$DISPLAY_NUM" \
    "${RECORD_SIZE_ARGS[@]}"
fi

RAW_MKV="$CAPTURE_DIR/${TAG}.mkv"
if [ "$PRESET" = "ffv1-flac" ]; then
  BOUNDED_MKV="$CAPTURE_DIR/${TAG}_lossless.mkv"
  BOUNDED_KEY="LOSSLESS"
  BOUNDED_LABEL="lossless"
else
  # flac-fast/realtime converts chroma to yuv420p before lossless H.264 coding,
  # so it is useful for synchronized checks but must not be labelled lossless.
  BOUNDED_MKV="$CAPTURE_DIR/${TAG}_native.mkv"
  BOUNDED_KEY="CAPTURE"
  BOUNDED_LABEL="native 4:2:0"
fi
[ -f "$RAW_MKV" ] || { echo "recording not produced: $RAW_MKV (see $CAPTURE_DIR/retroarch_${TAG}.log)" >&2; exit 1; }

AUTO_TRIM_WAV=""
if [ "$AUTO_AUDIO_TRIM" -eq 1 ]; then
  AUTO_TRIM_WAV="$CAPTURE_DIR/${TAG}_auto_trim.wav"
  ffmpeg -y -hide_banner -loglevel error -i "$RAW_MKV" -vn -ar 44100 "$AUTO_TRIM_WAV"
  [ -s "$AUTO_TRIM_WAV" ] || { echo "auto audio trim extraction failed: $AUTO_TRIM_WAV" >&2; exit 1; }
  TRIM="$("$PYTHON" - "$AUTO_TRIM_WAV" "$REC_SECS" <<'PY'
import math
import struct
import sys
import wave

path, seconds_s = sys.argv[1:]
seconds = int(seconds_s)

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

if best_rms < 1:
    raise SystemExit(f"no non-silent audio window found; best_rms={best_rms:.4f}")

print(best_start // step_samples)
print(f"auto audio trim: start={best_start // step_samples}s rms={best_rms:.4f}", file=sys.stderr)
PY
)"
  echo ">> auto audio trim selected ${TRIM}s"
fi

if [ "$TRIM" = "0" ]; then
  echo ">> keeping Mega-CD startup in bounded $BOUNDED_LABEL capture -> $BOUNDED_MKV"
else
  echo ">> explicitly trimming ${TRIM}s from the bounded $BOUNDED_LABEL capture -> $BOUNDED_MKV"
fi
ffmpeg -y -hide_banner -loglevel error -ss "$TRIM" -i "$RAW_MKV" \
  -t "$REC_SECS" -map 0:v:0 -map '0:a:0?' -c copy "$BOUNDED_MKV"

echo ">> transcoding verification preview -> $OUT"
OUT_ABS="$(realpath -m "$OUT")"
if [[ "$OUT_ABS" == "$ROOT/videos/"* ]]; then
  "$PYTHON" tools/tmpfs_workspace.py run-file \
    --output "$OUT" --kind record-preview-mp4 --required-gb 1 -- \
    ffmpeg -y -hide_banner -loglevel error -i "$BOUNDED_MKV" \
      -c:v libx264 -crf 18 -pix_fmt yuv420p \
      -c:a aac -b:a 128k -movflags +faststart '{output}'
else
  ffmpeg -y -hide_banner -loglevel error -i "$BOUNDED_MKV" \
    -c:v libx264 -crf 18 -pix_fmt yuv420p \
    -c:a aac -b:a 128k -movflags +faststart "$OUT"
fi

if [ -n "$PIPELINE_WALL_START_NS" ]; then
  PIPELINE_WALL_END_NS="$(date +%s%N)"
  PIPELINE_WALL_SECONDS="$(awk -v start="$PIPELINE_WALL_START_NS" -v end="$PIPELINE_WALL_END_NS" \
    'BEGIN { printf "%.3f", (end - start) / 1000000000 }')"
fi

rm -f "$RAW_MKV"
[ -z "$AUTO_TRIM_WAV" ] || rm -f "$AUTO_TRIM_WAV"
[ -n "$REPLAY_FILE" ] && echo "REPLAY=$REPLAY_FILE"
[ -n "$PIPELINE_WALL_START_NS" ] && echo "PIPELINE_WALL_SECONDS=$PIPELINE_WALL_SECONDS"
echo "$BOUNDED_KEY=$BOUNDED_MKV"
echo "OUT=$OUT"
for artifact in "$BOUNDED_MKV" "$OUT"; do
  AUDIO_INFO="$(ffprobe -v error -count_packets -select_streams a:0 \
    -show_entries stream=codec_name,sample_rate,channels,nb_read_packets \
    -of csv=p=0 "$artifact")"
  IFS=, read -r AUDIO_CODEC AUDIO_RATE AUDIO_CHANNELS AUDIO_PACKETS <<< "$AUDIO_INFO"
  if [ -z "$AUDIO_CODEC" ] || [ -z "$AUDIO_RATE" ] || [ -z "$AUDIO_CHANNELS" ] ||
     ! [[ "$AUDIO_PACKETS" =~ ^[1-9][0-9]*$ ]]; then
    echo "artifact has no usable audio stream: $artifact" >&2
    exit 1
  fi
  echo "AUDIO=$artifact codec=$AUDIO_CODEC rate=$AUDIO_RATE channels=$AUDIO_CHANNELS packets=$AUDIO_PACKETS"
  ffprobe -hide_banner "$artifact" 2>&1 | grep -E 'Input|Duration|Stream' || true
done
