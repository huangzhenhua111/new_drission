#!/usr/bin/env bash
set -euo pipefail

RESOURCE="${1:-manual_files/sample.mp4}" \
OUTPUT_DIR="${2:-outputs/demo_123apps}" \
ENTRY_URL="https://123apps.com/" \
TASK_TEMPLATE="examples/tasks/123apps_video_edit.txt" \
bash scripts/run_agent.sh
