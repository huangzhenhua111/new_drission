#!/usr/bin/env bash
set -euo pipefail

RESOURCE="${1:-manual_files/sample.mp4}" \
OUTPUT_DIR="${2:-outputs/demo_clideo}" \
ENTRY_URL="https://clideo.com/editor/" \
TASK_TEMPLATE="examples/tasks/clideo_video_edit.txt" \
MAX_STEPS="${MAX_STEPS:-28}" \
bash scripts/run_agent.sh
