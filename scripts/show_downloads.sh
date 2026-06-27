#!/usr/bin/env bash
set -euo pipefail

DOWNLOAD_DIR="${DOWNLOAD_DIR:-$HOME/Downloads}"
OPEN_LATEST="${OPEN_LATEST:-1}"

echo "Chrome download directory:"
echo "  $DOWNLOAD_DIR"
echo
echo "Recent mp4 files:"
find "$DOWNLOAD_DIR" -maxdepth 1 -type f -iname "*.mp4" -printf "%TY-%Tm-%Td %TH:%TM  %s bytes  %p\n" 2>/dev/null \
  | sort -r \
  | head -10
echo

latest_file="$(
  find "$DOWNLOAD_DIR" -maxdepth 1 -type f -iname "*.mp4" -printf "%T@ %p\n" 2>/dev/null \
    | sort -nr \
    | head -1 \
    | cut -d' ' -f2-
)"

if [[ -z "$latest_file" ]]; then
  echo "No mp4 file found in $DOWNLOAD_DIR yet."
  exit 0
fi

echo "Latest mp4:"
echo "  $latest_file"
echo

if [[ "$OPEN_LATEST" == "1" ]]; then
  echo "Opening latest mp4..."
  if command -v ffplay >/dev/null 2>&1; then
    nohup ffplay -autoexit "$latest_file" >/tmp/new_drission_ffplay.log 2>&1 &
  elif command -v xdg-open >/dev/null 2>&1; then
    nohup xdg-open "$latest_file" >/tmp/new_drission_xdg_open.log 2>&1 &
  elif command -v explorer.exe >/dev/null 2>&1 && command -v wslpath >/dev/null 2>&1; then
    explorer.exe "$(wslpath -w "$latest_file")" >/dev/null 2>&1 &
  else
    echo "No video opener found. Install ffmpeg/ffplay or open the file manually."
  fi
  echo
fi

echo "If you are in WSL and want to open it in Windows Explorer, run from WSL:"
echo "  explorer.exe \"$(wslpath -w "$DOWNLOAD_DIR" 2>/dev/null || echo "$DOWNLOAD_DIR")\""
echo
echo "Tips:"
echo "  OPEN_LATEST=0 bash scripts/show_downloads.sh   # only list files"
echo "  DOWNLOAD_DIR=/path/to/downloads bash scripts/show_downloads.sh"
