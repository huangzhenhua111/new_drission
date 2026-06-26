#!/usr/bin/env bash
set -euo pipefail

# Defaults run the bundled 123Apps video-editing demo.
# Override TASK / ENTRY_URL / RESOURCE / OUTPUT_DIR in one shell line to run your own job.
ENTRY_URL="${ENTRY_URL:-https://123apps.com/}"
RESOURCE="${RESOURCE:-manual_files/sample.mp4}"
OUTPUT_DIR="${OUTPUT_DIR:-outputs/demo_123apps}"
MAX_STEPS="${MAX_STEPS:-35}"
TASK_TEMPLATE="${TASK_TEMPLATE:-examples/tasks/123apps_video_edit.txt}"

if [[ -z "${TASK:-}" ]]; then
  if [[ ! -f "$TASK_TEMPLATE" ]]; then
    echo "Task template not found: $TASK_TEMPLATE" >&2
    exit 1
  fi
  TASK="$(python - "$RESOURCE" "$TASK_TEMPLATE" <<'PY'
from pathlib import Path
import sys

resource = str(Path(sys.argv[1]).resolve())
template = Path(sys.argv[2]).read_text(encoding="utf-8")
print(template.replace("{VIDEO_FILE}", resource).replace("{RESOURCE}", resource))
PY
)"
else
  TASK="$(python - "$RESOURCE" "$TASK" <<'PY'
from pathlib import Path
import sys

resource = str(Path(sys.argv[1]).resolve())
task = sys.argv[2]
print(task.replace("{VIDEO_FILE}", resource).replace("{RESOURCE}", resource))
PY
)"
fi

if [[ -n "$RESOURCE" && ! -f "$RESOURCE" ]]; then
  echo "Resource file not found: $RESOURCE" >&2
  echo "Set RESOURCE=/path/to/file or put a demo video at manual_files/sample.mp4" >&2
  exit 1
fi

python -m app.cli run-task \
  --init-no-llm \
  --request "$TASK" \
  --entry-url "$ENTRY_URL" \
  --resource "$RESOURCE" \
  --output-dir "$OUTPUT_DIR" \
  --max-steps "$MAX_STEPS"

echo
echo "Final standalone script:"
echo "  $OUTPUT_DIR/generated_script.py"
