from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
OLD_PROJECT_ENV = WORKSPACE_ROOT / "drission_agent" / ".env"


def load_environment() -> None:
    """Load new project .env first, then old project .env as fallback.

    `override=False` keeps values from the new project / shell if both exist.
    """

    _load_dotenv(PROJECT_ROOT / ".env")
    _load_dotenv(OLD_PROJECT_ENV)


@dataclass(frozen=True)
class ModelConfig:
    model: str
    api_key: str
    base_url: str


@dataclass(frozen=True)
class BrowserConfig:
    headless: bool
    user_data_dir: Path


@dataclass(frozen=True)
class Settings:
    text: ModelConfig
    multimodal: ModelConfig
    browser: BrowserConfig


def get_settings() -> Settings:
    load_environment()
    multimodal = ModelConfig(
        # The new project intentionally does not inherit the old project's
        # VISION_LLM_MODEL. We only reuse its key/base_url. Model default is
        # the user-selected qwen3.7-plus unless MULTIMODAL_MODEL is set here.
        model=_env_first("MULTIMODAL_MODEL", default="qwen3.7-plus"),
        api_key=_env_first("MULTIMODAL_API_KEY", "VISION_LLM_API_KEY", "DASHSCOPE_API_KEY", default=""),
        base_url=_env_first(
            "MULTIMODAL_BASE_URL",
            "VISION_LLM_BASE_URL",
            default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        ).rstrip("/"),
    )
    text = ModelConfig(
        model=_env_first("TEXT_MODEL", "TEXT_LLM_MODEL", default=multimodal.model),
        api_key=_env_first("TEXT_API_KEY", "TEXT_LLM_API_KEY", default=multimodal.api_key),
        base_url=_env_first("TEXT_BASE_URL", "TEXT_LLM_BASE_URL", default=multimodal.base_url).rstrip("/"),
    )
    browser = BrowserConfig(
        headless=_env_first("BROWSER_HEADLESS", default="false").lower() in {"1", "true", "yes"},
        user_data_dir=PROJECT_ROOT / _env_first("BROWSER_USER_DATA_DIR", default=".browser_profile"),
    )
    return Settings(text=text, multimodal=multimodal, browser=browser)


def _env_first(*names: str, default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
