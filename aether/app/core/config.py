from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_environment() -> None:
    env_path = Path.cwd() / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)
        return

    # Fallback for uncommon file variants (for example, ".env "),
    # while explicitly skipping template files.
    for env_file in Path.cwd().glob(".env*"):
        if env_file.name in {".env", ".env.example"}:
            continue
        if env_file.is_file():
            load_dotenv(dotenv_path=env_file, override=False)
            break


_load_environment()


class Settings(BaseSettings):
    mem0_api_key: str | None = None
    webhook_token: str | None = None
    cors_origins: list[str] = ["*"]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> list[str]:
        if value is None or value == "":
            return ["*"]
        if isinstance(value, str):
            parts = [item.strip() for item in value.split(",") if item.strip()]
            return parts or ["*"]
        if isinstance(value, list):
            parsed = [str(item).strip() for item in value if str(item).strip()]
            return parsed or ["*"]
        return ["*"]


settings = Settings()

APP_TITLE = "Voice AI Memory Hub"
API_PREFIX = "/api/v1"
DEMO_USER_ID = "hackathon_demo_user"
MEMORY_TOOL_NAME = "save_user_memory"
CALENDAR_TOOL_NAME = "check_calendar"
MAX_PREVIEW_LEN = 80

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("voice_ai_backend")


def _log_event(event: str, **fields: Any) -> None:
    payload = " ".join(f"{key}={fields[key]}" for key in sorted(fields))
    logger.info("event=%s %s", event, payload)
