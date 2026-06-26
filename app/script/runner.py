from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter


@dataclass(frozen=True)
class ScriptRunResult:
    script_path: Path
    output_dir: Path
    exit_code: int
    stdout: str
    stderr: str
    elapsed_seconds: float
    run_json: Path
    success: bool

    def to_dict(self) -> dict:
        data = asdict(self)
        data["script_path"] = str(self.script_path)
        data["output_dir"] = str(self.output_dir)
        data["run_json"] = str(self.run_json)
        return data


def run_script(
    *,
    script_path: Path,
    output_dir: Path,
    timeout_seconds: int = 1800,
    python_executable: str | None = None,
    run_name: str = "script_run",
) -> ScriptRunResult:
    script_path = script_path.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["REPLAY_OUTPUT_DIR"] = str(output_dir / "browser")
    started = perf_counter()
    completed = subprocess.run(
        [python_executable or sys.executable, str(script_path)],
        cwd=str(script_path.parent),
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
    )
    elapsed = round(perf_counter() - started, 3)
    result = ScriptRunResult(
        script_path=script_path,
        output_dir=output_dir,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        elapsed_seconds=elapsed,
        run_json=output_dir / f"{run_name}.json",
        success=completed.returncode == 0,
    )
    result.run_json.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / f"{run_name}.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (output_dir / f"{run_name}.stderr.log").write_text(completed.stderr, encoding="utf-8")
    return result
