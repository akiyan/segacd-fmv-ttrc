#!/usr/bin/env bash
#
# run_headless.sh - headless Sega CD smoke/capture harness.
#
# Boots a built disc image in RetroArch + Genesis Plus GX under Xvfb, presses
# START a few times to get past the Sega CD BIOS / CD player, then captures the
# screen at a fixed interval and tiles the frames into a contact sheet. Used to
# verify boot / video progression / on-screen counters without a display.
#
# Usage:
#   tools/run_headless.sh out/SCFMV_MCD.cue
#   tools/run_headless.sh out/CDCBENCH.cue --tag cdc --shots 30 --interval 2
#   tools/run_headless.sh out/TEST1M.cue --tag t1m --shots 4 --interval 1
#
# Options (all optional except the disc):
#   --tag NAME        output prefix under the work dir (default: disc basename)
#   --shots N         number of screenshots (default 24)
#   --interval SEC    seconds between screenshots (default 2)
#   --boot-wait SEC   seconds to wait before the first START (default 4)
#   --presses N       number of START presses (default 2)
#   --press-gap SEC   seconds between/after START presses (default 2)
#   --display :N      X display for Xvfb (default :231)
#   --record [FILE]   record video+audio with RetroArch's FFmpeg recorder.
#                     If FILE is omitted, writes $OUTDIR/<tag>.mkv and also
#                     extracts $OUTDIR/<tag>.wav for quick audio checks. Normal
#                     recording is audio-synchronised and never runs uncapped.
#   --record-offline  explicit uncapped FFV1/FLAC test mode. Implies --record
#                     and requires --max-frames plus --play-replay.
#   --max-frames N    stop normally after exactly N emulator video frames.
#   --play-replay FILE
#                     play a RetroArch input replay instead of sending START.
#   --record-replay FILE
#                     record RetroArch input to FILE (mutually exclusive with
#                     --play-replay).
#   --recordconfig FILE
#                     pass an explicit RetroArch FFmpeg recording config.
#   --record-preset NAME
#                     generate a recording config. Supported: flac-fast,
#                     ffv1-flac.
#   --record-size WxH pass RetroArch --size for recording output geometry.
#                     H32 uses native 256x224 and H40 uses native 320x224;
#                     both contain the same fixed 32-cell Window HUD.
#   --record-realtime
#                     shorthand for synced emulator audio recording:
#                     --record --record-preset flac-fast
#                     --audio-driver sdl2 --sdl-audio-driver dummy
#   --audio-driver NAME
#                     RetroArch audio driver (default: null).
#   --audio-device NAME
#                     audio_device value for RetroArch (driver-specific).
#   --sdl-audio-driver NAME
#                     SDL_AUDIODRIVER when using SDL audio (default: unset).
#   --audio-jump-threshold N
#                     fail recording if extracted WAV has a sample jump >= N
#                     (default: 12000; use 6000 for stricter local checks).
#   --no-audio-check
#                     skip post-recording click/clip detection.
#   --audio-min-rms N fail recording if extracted WAV RMS is below N
#                     (default: 0, disabled).
#
# Env overrides:
#   CORE         libretro core .so (default: system genesis_plus_gx)
#   SYSTEM_DIR   RetroArch system dir holding bios_CD_J.bin
#   OUTDIR       capture output dir (default: <repo>/tmp/<disc-stem>/record)
#
# Outputs: $OUTDIR/<tag>_NN.png, $OUTDIR/<tag>_sheet.jpg, plus retroarch/xvfb logs.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DISC=""
TAG=""
SHOTS=24
INTERVAL=2
BOOT_WAIT=5
PRESSES=14
PRESS_GAP=1.0
DISPLAY_NUM=":231"
RECORD=0
RECORD_PATH=""
RECORD_CONFIG=""
RECORD_PRESET=""
RECORD_SIZE=""
AUDIO_DRIVER="null"
AUDIO_DEVICE=""
SDL_AUDIO_DRIVER=""
REALTIME_RECORD=0
OFFLINE_RECORD=0
MAX_FRAMES=""
PLAY_REPLAY=""
RECORD_REPLAY=""
AUDIO_CHECK=1
AUDIO_JUMP_THRESHOLD=12000
AUDIO_MIN_RMS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="$2"; shift 2;;
    --shots) SHOTS="$2"; shift 2;;
    --interval) INTERVAL="$2"; shift 2;;
    --boot-wait) BOOT_WAIT="$2"; shift 2;;
    --presses) PRESSES="$2"; shift 2;;
    --press-gap) PRESS_GAP="$2"; shift 2;;
    --display) DISPLAY_NUM="$2"; shift 2;;
    --record)
      RECORD=1
      if [ $# -ge 2 ] && [[ "$2" != -* ]]; then
        RECORD_PATH="$2"
        shift 2
      else
        shift
      fi
      ;;
    --recordconfig) RECORD_CONFIG="$2"; shift 2;;
    --record-preset) RECORD_PRESET="$2"; shift 2;;
    --record-size) RECORD_SIZE="$2"; shift 2;;
    --record-realtime) RECORD=1; REALTIME_RECORD=1; shift;;
    --record-offline) RECORD=1; OFFLINE_RECORD=1; shift;;
    --max-frames) MAX_FRAMES="$2"; shift 2;;
    --play-replay) PLAY_REPLAY="$2"; shift 2;;
    --record-replay) RECORD_REPLAY="$2"; shift 2;;
    --audio-driver) AUDIO_DRIVER="$2"; shift 2;;
    --audio-device) AUDIO_DEVICE="$2"; shift 2;;
    --sdl-audio-driver) SDL_AUDIO_DRIVER="$2"; shift 2;;
    --audio-jump-threshold) AUDIO_JUMP_THRESHOLD="$2"; shift 2;;
    --audio-min-rms) AUDIO_MIN_RMS="$2"; shift 2;;
    --no-audio-check) AUDIO_CHECK=0; shift;;
    -h|--help) sed -n '2,/^set -euo pipefail/p' "$0" | sed '$d'; exit 0;;
    -*) echo "unknown option: $1" >&2; exit 2;;
    *) DISC="$1"; shift;;
  esac
done

[ -n "$DISC" ] || { echo "usage: $0 <disc.cue> [options]" >&2; exit 2; }
[ -f "$DISC" ] || { echo "disc not found: $DISC" >&2; exit 1; }
if [ -n "$MAX_FRAMES" ] && [[ ! "$MAX_FRAMES" =~ ^[1-9][0-9]*$ ]]; then
  echo "--max-frames must be a positive integer: $MAX_FRAMES" >&2
  exit 2
fi
if [ -n "$PLAY_REPLAY" ] && [ -n "$RECORD_REPLAY" ]; then
  echo "--play-replay and --record-replay are mutually exclusive" >&2
  exit 2
fi
if [ -n "$PLAY_REPLAY" ] && [ ! -f "$PLAY_REPLAY" ]; then
  echo "replay not found: $PLAY_REPLAY" >&2
  exit 1
fi
if [ "$OFFLINE_RECORD" -eq 1 ]; then
  [ "$REALTIME_RECORD" -eq 0 ] || {
    echo "--record-offline and --record-realtime are mutually exclusive" >&2
    exit 2
  }
  [ -z "$RECORD_CONFIG" ] || {
    echo "--record-offline does not accept --recordconfig; use its fixed FFV1/FLAC preset" >&2
    exit 2
  }
  if [ -n "$RECORD_PRESET" ] && [ "$RECORD_PRESET" != "ffv1-flac" ]; then
    echo "--record-offline only supports --record-preset ffv1-flac" >&2
    exit 2
  fi
  [ -n "$MAX_FRAMES" ] || {
    echo "--record-offline requires --max-frames N" >&2
    exit 2
  }
  [ -n "$PLAY_REPLAY" ] || {
    echo "--record-offline requires --play-replay FILE" >&2
    exit 2
  }
fi
DISC_STEM="$(basename "${DISC%.*}")"
[ -z "$TAG" ] && TAG="$DISC_STEM"
DISPLAY_ID="${DISPLAY_NUM#:}"
DISPLAY_ID="${DISPLAY_ID%%.*}"
if [[ ! "$DISPLAY_ID" =~ ^[0-9]+$ ]]; then
  DISPLAY_ID=231
fi
COMMAND_PORT=$((55355 + DISPLAY_ID % 1000))
if [ "$REALTIME_RECORD" -eq 1 ]; then
  [ -z "$RECORD_PRESET" ] && RECORD_PRESET="flac-fast"
fi
if [ "$OFFLINE_RECORD" -eq 1 ]; then
  [ -z "$RECORD_PRESET" ] && [ -z "$RECORD_CONFIG" ] && RECORD_PRESET="ffv1-flac"
fi
# Both recording modes use SDL2's dummy sink so RetroArch initializes its audio
# path normally. Realtime mode is paced by audio_sync below; offline mode keeps
# the same sink but disables audio sync and rate control, so it remains uncapped.
if [ "$RECORD" -eq 1 ]; then
  [ "$AUDIO_DRIVER" = "null" ] && AUDIO_DRIVER="sdl2"
  [ -z "$SDL_AUDIO_DRIVER" ] && SDL_AUDIO_DRIVER="dummy"
fi

CORE="${CORE:-/usr/lib/x86_64-linux-gnu/libretro/genesis_plus_gx_libretro.so}"
SYSTEM_DIR="${SYSTEM_DIR:-$HOME/.config/retroarch/system}"
OUTDIR="${OUTDIR:-$ROOT/tmp/$DISC_STEM/record}"
mkdir -p "$OUTDIR"

for tool in Xvfb retroarch xdotool import montage; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing required tool: $tool" >&2; exit 1; }
done
if [ "$RECORD" -eq 1 ]; then
  command -v ffmpeg >/dev/null 2>&1 || { echo "missing required tool for --record: ffmpeg" >&2; exit 1; }
  if [ "$AUDIO_CHECK" -eq 1 ]; then
    command -v python3 >/dev/null 2>&1 || { echo "missing required tool for audio check: python3" >&2; exit 1; }
    [ -f "$ROOT/tools/analyze_recorded_audio.py" ] || { echo "missing audio check tool: $ROOT/tools/analyze_recorded_audio.py" >&2; exit 1; }
  fi
fi
[ -f "$CORE" ] || { echo "core not found: $CORE (set CORE=)" >&2; exit 1; }
ls "$SYSTEM_DIR"/bios_CD_*.bin >/dev/null 2>&1 || \
  echo "warning: no bios_CD_*.bin in $SYSTEM_DIR" >&2

# Portable RetroArch config generated at run time (no absolute paths committed).
CFG="$OUTDIR/retroarch_${TAG}.cfg"
AUDIO_ENABLE=false
AUDIO_SYNC=true
AUDIO_RATE_CONTROL=true
VIDEO_VSYNC=true
if [ "$RECORD" -eq 1 ]; then
  AUDIO_ENABLE=true
fi
if [ "$OFFLINE_RECORD" -eq 1 ]; then
  AUDIO_SYNC=false
  AUDIO_RATE_CONTROL=false
  VIDEO_VSYNC=false
fi
cat > "$CFG" <<EOF
video_driver = "gl"
video_context_driver = "x"
input_driver = "x"
joystick_driver = "null"
audio_driver = "$AUDIO_DRIVER"
audio_device = "$AUDIO_DEVICE"
audio_enable = "$AUDIO_ENABLE"
audio_sync = "$AUDIO_SYNC"
audio_latency = "64"
audio_rate_control = "$AUDIO_RATE_CONTROL"
audio_rate_control_delta = "0.005"
audio_max_timing_skew = "0.05"
quit_press_twice = "false"
input_exit_emulator = "escape"
menu_driver = "rgui"
config_save_on_exit = "false"
network_cmd_enable = "true"
network_cmd_port = "$COMMAND_PORT"
system_directory = "$SYSTEM_DIR"
screenshot_directory = "$OUTDIR"
savestate_directory = "$OUTDIR"
video_fullscreen = "false"
video_vsync = "$VIDEO_VSYNC"
video_scale = "2"
video_smooth = "false"
input_player1_start = "enter"
genesis_plus_gx_region_detect = "ntsc-j"
genesis_plus_gx_bios = "enabled"
EOF

RA_PID="$OUTDIR/${TAG}_ra.pid"
XVFB_PID="$OUTDIR/${TAG}_xvfb.pid"
if [ "$RECORD" -eq 1 ] && [ -z "$RECORD_PATH" ]; then
  RECORD_PATH="$OUTDIR/${TAG}.mkv"
fi
rm -f "$OUTDIR/${TAG}"_*.png "$OUTDIR/${TAG}_sheet.jpg" \
      "$OUTDIR/retroarch_${TAG}.log" "$OUTDIR/xvfb_${TAG}.log" "$RA_PID" "$XVFB_PID"
if [ "$RECORD" -eq 1 ]; then
  rm -f "$RECORD_PATH" "${RECORD_PATH%.*}.wav"
  rm -f "$OUTDIR/${TAG}_audio.json"
fi
if [ -n "$RECORD_REPLAY" ]; then
  mkdir -p "$(dirname "$RECORD_REPLAY")"
  rm -f "$RECORD_REPLAY"
fi
if [ -n "$RECORD_CONFIG" ] && [ -n "$RECORD_PRESET" ]; then
  echo "--recordconfig and --record-preset are mutually exclusive" >&2
  exit 2
fi
if [ -n "$RECORD_CONFIG" ] && [ ! -f "$RECORD_CONFIG" ]; then
  echo "record config not found: $RECORD_CONFIG" >&2
  exit 1
fi
if [ -n "$RECORD_PRESET" ]; then
  RECORD_CONFIG="$OUTDIR/record_${TAG}_${RECORD_PRESET}.cfg"
  case "$RECORD_PRESET" in
    flac-fast)
      cat > "$RECORD_CONFIG" <<EOF
format = matroska
vcodec = libx264
acodec = flac
pix_fmt = yuv420p
sample_rate = 44100
threads = 2
frame_drop_ratio = 1
video_crf = 0
video_preset = ultrafast
video_tune = zerolatency
EOF
      ;;
    ffv1-flac)
      cat > "$RECORD_CONFIG" <<EOF
format = matroska
vcodec = ffv1
acodec = flac
pix_fmt = bgr0
sample_rate = 44100
threads = 2
frame_drop_ratio = 1
EOF
      ;;
    *)
      echo "unknown record preset: $RECORD_PRESET (supported: flac-fast, ffv1-flac)" >&2
      exit 2
      ;;
  esac
fi

cleanup() { kill "$(cat "$RA_PID" 2>/dev/null)" "$(cat "$XVFB_PID" 2>/dev/null)" 2>/dev/null || true; }
retroarch_window() {
  DISPLAY="$DISPLAY_NUM" xdotool search --onlyvisible --class retroarch 2>/dev/null | head -n1 || true
}
stop_retroarch() {
  local window
  retroarch -c "$CFG" --command QUIT >/dev/null 2>&1 || true
  sleep 0.5
  if ! kill -0 "$(cat "$RA_PID" 2>/dev/null)" 2>/dev/null; then
    return
  fi
  window="$(retroarch_window)"
  if [ -n "$window" ]; then
    DISPLAY="$DISPLAY_NUM" xdotool windowactivate "$window" 2>/dev/null || true
    DISPLAY="$DISPLAY_NUM" xdotool key --window "$window" Escape 2>/dev/null || true
  else
    DISPLAY="$DISPLAY_NUM" xdotool key Escape 2>/dev/null || true
  fi
  for _ in $(seq 1 60); do
    if ! kill -0 "$(cat "$RA_PID" 2>/dev/null)" 2>/dev/null; then
      return
    fi
    sleep 0.25
  done
  kill "$(cat "$RA_PID" 2>/dev/null)" 2>/dev/null || true
}
trap cleanup EXIT

Xvfb "$DISPLAY_NUM" -screen 0 800x600x24 >"$OUTDIR/xvfb_${TAG}.log" 2>&1 &
echo $! > "$XVFB_PID"
sleep 0.5

RA_RECORD_ARGS=()
if [ "$RECORD" -eq 1 ]; then
  RA_RECORD_ARGS=(--record "$RECORD_PATH")
  if [ -n "$RECORD_CONFIG" ]; then
    RA_RECORD_ARGS+=(--recordconfig "$RECORD_CONFIG")
  fi
  if [ -n "$RECORD_SIZE" ]; then
    RA_RECORD_ARGS+=(--size "$RECORD_SIZE")
  fi
fi
RA_RUN_ARGS=()
if [ -n "$MAX_FRAMES" ]; then
  RA_RUN_ARGS+=(--max-frames "$MAX_FRAMES")
fi
if [ -n "$PLAY_REPLAY" ]; then
  RA_RUN_ARGS+=(--play-replay "$PLAY_REPLAY")
elif [ -n "$RECORD_REPLAY" ]; then
  RA_RUN_ARGS+=(--record-replay "$RECORD_REPLAY")
fi
RA_ENV=(DISPLAY="$DISPLAY_NUM" LIBGL_ALWAYS_SOFTWARE=1)
if [ -n "$SDL_AUDIO_DRIVER" ]; then
  RA_ENV+=(SDL_AUDIODRIVER="$SDL_AUDIO_DRIVER")
fi

RA_WALL_START_NS="$(date +%s%N)"
env "${RA_ENV[@]}" retroarch --verbose \
  -c "$CFG" -L "$CORE" "${RA_RECORD_ARGS[@]}" "${RA_RUN_ARGS[@]}" "$DISC" >"$OUTDIR/retroarch_${TAG}.log" 2>&1 &
echo $! > "$RA_PID"

if [ -z "$PLAY_REPLAY" ]; then
  sleep "$BOOT_WAIT"
  for _ in $(seq 1 "$PRESSES"); do
    W="$(retroarch_window)"
    [ -n "$W" ] && DISPLAY="$DISPLAY_NUM" xdotool key --window "$W" Return || true
    sleep "$PRESS_GAP"
  done
fi

# Capture the RetroArch window itself, not the whole Xvfb desktop: with no window
# manager the window sits at the desktop top-left, so a root grab pads the frame
# with empty desktop (black right/bottom) and makes centred content look mis-placed.
# Re-resolve the window each shot (id can change once the core loads); fall back to
# root only if it can't be found.
RA_STATUS=0
MAX_FRAMES_TIMED_OUT=0
if [ -n "$MAX_FRAMES" ]; then
  RA_PROCESS_PID="$(cat "$RA_PID")"
  # Allow three times the nominal emulated duration plus two minutes for slow
  # hosts and FFmpeg trailer flushing. A timeout still goes through the normal
  # QUIT path before the exact-frame gate rejects the capture.
  MAX_FRAMES_WATCHDOG=$(( (MAX_FRAMES + 59) / 60 * 3 + 120 ))
  MAX_FRAMES_WAIT_START=$SECONDS
  while kill -0 "$RA_PROCESS_PID" 2>/dev/null; do
    RA_PROCESS_STATE="$(ps -o stat= -p "$RA_PROCESS_PID" 2>/dev/null || true)"
    if [ -z "$RA_PROCESS_STATE" ] || [[ "$RA_PROCESS_STATE" = Z* ]]; then
      break
    fi
    if [ $((SECONDS - MAX_FRAMES_WAIT_START)) -ge "$MAX_FRAMES_WATCHDOG" ]; then
      echo "max-frames watchdog expired after ${MAX_FRAMES_WATCHDOG}s; stopping RetroArch gracefully" >&2
      MAX_FRAMES_TIMED_OUT=1
      stop_retroarch
      break
    fi
    sleep 1
  done
  if wait "$RA_PROCESS_PID"; then
    RA_STATUS=0
  else
    RA_STATUS=$?
  fi
else
  for i in $(seq 0 $((SHOTS - 1))); do
    W="$(retroarch_window)"
    CAP_TARGET="${W:-root}"
    DISPLAY="$DISPLAY_NUM" import -window "$CAP_TARGET" "$OUTDIR/${TAG}_$(printf '%02d' "$i").png" \
      || DISPLAY="$DISPLAY_NUM" import -window root "$OUTDIR/${TAG}_$(printf '%02d' "$i").png" || true
    sleep "$INTERVAL"
  done

  stop_retroarch
  wait "$(cat "$RA_PID" 2>/dev/null)" 2>/dev/null || true
fi
RA_WALL_END_NS="$(date +%s%N)"
RA_WALL_SECONDS="$(awk -v start="$RA_WALL_START_NS" -v end="$RA_WALL_END_NS" \
  'BEGIN { printf "%.3f", (end - start) / 1000000000 }')"
kill "$(cat "$XVFB_PID" 2>/dev/null)" 2>/dev/null || true
trap - EXIT

if [ "$MAX_FRAMES_TIMED_OUT" -eq 1 ]; then
  exit 1
fi
if [ -n "$MAX_FRAMES" ] && [ "$RA_STATUS" -ne 0 ]; then
  echo "RetroArch exited with status $RA_STATUS before completing --max-frames $MAX_FRAMES" >&2
  exit 1
fi
if [ -n "$MAX_FRAMES" ]; then
  if ! grep -q '\[Runtime\].*Content ran for a total of' "$OUTDIR/retroarch_${TAG}.log" || \
     ! grep -q '\[Core\].*Unloading core' "$OUTDIR/retroarch_${TAG}.log"; then
    echo "RetroArch log does not show a normal runtime/core shutdown" >&2
    exit 1
  fi
fi
if [ -n "$PLAY_REPLAY" ] && [ -n "$MAX_FRAMES" ] && \
   grep -q '\[Replay\].*EOF' "$OUTDIR/retroarch_${TAG}.log"; then
  echo "input replay reached EOF before the fixed-frame run ended: $PLAY_REPLAY" >&2
  echo "record a replay longer than --max-frames (the high-level harness adds 120 frames)" >&2
  exit 1
fi
if [ -n "$RECORD_REPLAY" ] && [ ! -s "$RECORD_REPLAY" ]; then
  echo "input replay was not produced: $RECORD_REPLAY" >&2
  exit 1
fi

if [ -z "$MAX_FRAMES" ]; then
  montage "$OUTDIR/${TAG}"_*.png -tile 6x -geometry 260x195+2+2 "$OUTDIR/${TAG}_sheet.jpg" 2>/dev/null || true
  echo "done: $OUTDIR/${TAG}_sheet.jpg ($(ls "$OUTDIR/${TAG}"_*.png 2>/dev/null | wc -l) frames)"
else
  echo "done: RetroArch completed $MAX_FRAMES frames without wall-clock screenshots"
fi
echo "log:  $OUTDIR/retroarch_${TAG}.log"
if [ "$RECORD" -eq 1 ]; then
  WAV_PATH="${RECORD_PATH%.*}.wav"
  AUDIO_REPORT="$OUTDIR/${TAG}_audio.json"
  [ -s "$RECORD_PATH" ] || { echo "recording not produced: $RECORD_PATH" >&2; exit 1; }
  RECORD_DURATION="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$RECORD_PATH" 2>/dev/null || true)"
  if [ -z "$RECORD_DURATION" ] || [ "$RECORD_DURATION" = "N/A" ]; then
    echo "recording has no valid duration: $RECORD_PATH" >&2
    exit 1
  fi
  if [ -n "$MAX_FRAMES" ]; then
    VIDEO_PACKET_COUNT="$(ffprobe -v error -count_packets -select_streams v:0 \
      -show_entries stream=nb_read_packets -of default=nw=1:nk=1 "$RECORD_PATH")"
    VIDEO_FRAME_COUNT="$(ffprobe -v error -count_frames -select_streams v:0 \
      -show_entries stream=nb_read_frames -of default=nw=1:nk=1 "$RECORD_PATH")"
    if [ "$VIDEO_PACKET_COUNT" != "$MAX_FRAMES" ] || [ "$VIDEO_FRAME_COUNT" != "$MAX_FRAMES" ]; then
      echo "recording frame count invalid: packets=$VIDEO_PACKET_COUNT frames=$VIDEO_FRAME_COUNT expected=$MAX_FRAMES" >&2
      exit 1
    fi
    VIDEO_RATE="$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate \
      -of default=nw=1:nk=1 "$RECORD_PATH")"
    VIDEO_DURATION="$(awk -v frames="$VIDEO_FRAME_COUNT" -v rate="$VIDEO_RATE" \
      'BEGIN { split(rate, r, "/"); if (r[1] > 0 && r[2] > 0) printf "%.3f", frames * r[2] / r[1]; else exit 1 }')"
    RECORD_SPEED="$(awk -v media="$VIDEO_DURATION" -v wall="$RA_WALL_SECONDS" \
      'BEGIN { if (wall > 0) printf "%.2f", media / wall; else print "inf" }')"
    echo "record timing: video=${VIDEO_DURATION}s container=${RECORD_DURATION}s wall=${RA_WALL_SECONDS}s speed=${RECORD_SPEED}x frames=$VIDEO_FRAME_COUNT packets=$VIDEO_PACKET_COUNT"
  else
    EXPECTED_DURATION="$(awk -v boot="$BOOT_WAIT" -v presses="$PRESSES" -v gap="$PRESS_GAP" \
      -v shots="$SHOTS" -v interval="$INTERVAL" \
      'BEGIN { printf "%.3f", boot + presses * gap + shots * interval }')"
    if ! awk -v got="$RECORD_DURATION" -v expected="$EXPECTED_DURATION" \
      'BEGIN { exit !(got >= expected * 0.60 && got <= expected * 1.50) }'; then
      echo "recording timing invalid: duration=${RECORD_DURATION}s expected about ${EXPECTED_DURATION}s" >&2
      echo "check audio synchronisation before using this capture" >&2
      exit 1
    fi
    echo "record timing: ${RECORD_DURATION}s (expected about ${EXPECTED_DURATION}s)"
  fi
  ffmpeg -y -hide_banner -loglevel error -i "$RECORD_PATH" -vn -ar 44100 "$WAV_PATH"
  echo "record: $RECORD_PATH"
  [ -n "$RECORD_CONFIG" ] && echo "record config: $RECORD_CONFIG"
  [ -s "$WAV_PATH" ] || { echo "audio extraction failed: $WAV_PATH" >&2; exit 1; }
  echo "audio:  $WAV_PATH"
  if [ "$AUDIO_CHECK" -eq 1 ]; then
    python3 "$ROOT/tools/analyze_recorded_audio.py" "$RECORD_PATH" \
      --wav "$WAV_PATH" \
      --seconds 12 \
      --jump-threshold "$AUDIO_JUMP_THRESHOLD" \
      --min-rms "$AUDIO_MIN_RMS" \
      --fail-on-clicks > "$AUDIO_REPORT"
    echo "audio check: $AUDIO_REPORT (jump_threshold=$AUDIO_JUMP_THRESHOLD min_rms=$AUDIO_MIN_RMS)"
  fi
fi
