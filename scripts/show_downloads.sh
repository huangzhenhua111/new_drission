#!/usr/bin/env bash
set -euo pipefail

DOWNLOAD_DIR="${DOWNLOAD_DIR:-$HOME/Downloads}"

echo "Chrome download directory:"
echo "  $DOWNLOAD_DIR"
echo
echo "Recent mp4 files:"
find "$DOWNLOAD_DIR" -maxdepth 1 -type f -iname "*.mp4" -printf "%TY-%Tm-%Td %TH:%TM  %s bytes  %p\n" 2>/dev/null \
  | sort -r \
  | head -10
echo
echo "If you are in WSL and want to open it in Windows Explorer, run from WSL:"
echo "  explorer.exe \"$(wslpath -w "$DOWNLOAD_DIR" 2>/dev/null || echo "$DOWNLOAD_DIR")\""
