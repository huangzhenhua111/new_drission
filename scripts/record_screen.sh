#!/usr/bin/env bash
set -euo pipefail

OUT="${1:-outputs/demo_recording.mp4}"
DISPLAY_ID="${DISPLAY:-:0}"
FPS="${FPS:-15}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is not installed." >&2
  echo "Install it first, for example: sudo apt-get update && sudo apt-get install -y ffmpeg" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUT")"

SIZE="$(xdpyinfo -display "$DISPLAY_ID" 2>/dev/null | awk '/dimensions:/{print $2; exit}')"
if [[ -z "$SIZE" ]]; then
  SIZE="$(xrandr --current 2>/dev/null | awk '/\\*/{print $1; exit}')"
fi
if [[ -z "$SIZE" ]]; then
  echo "Cannot detect screen size. Set DISPLAY and make sure x11 tools are available." >&2
  exit 1
fi

echo "Recording display $DISPLAY_ID at $SIZE, fps=$FPS"
echo "Output: $OUT"
echo "Press q in this terminal to stop recording."

ffmpeg -y \
  -video_size "$SIZE" \
  -framerate "$FPS" \
  -f x11grab \
  -i "$DISPLAY_ID.0" \
  -codec:v libx264 \
  -preset veryfast \
  -pix_fmt yuv420p \
  "$OUT"
