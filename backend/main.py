from __future__ import annotations

import logging
import os
import re
import time
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, RootModel, field_validator
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


@asynccontextmanager
async def lifespan(_: FastAPI) -> Any:
    yield
    global _mem0_client
    with _mem0_lock:
        with suppress(Exception):
            close_method = getattr(_mem0_client, "close", None)
            if callable(close_method):
                close_method()
        _mem0_client = None


app = FastAPI(title=APP_TITLE, version="1.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)
router = APIRouter(prefix=API_PREFIX)

_transcript: list[TranscriptMessage] = []
_transcript_lock = Lock()

_mem0_client: Any | None = None
_mem0_lock = Lock()

_call_reports: list[CallReportItem] = []
_reports_lock = Lock()

try:
    from mem0 import MemoryClient
except Exception:  # pragma: no cover
    MemoryClient = None  # type: ignore[assignment]


def _log_event(event: str, **fields: Any) -> None:
    payload = " ".join(f"{key}={fields[key]}" for key in sorted(fields))
    logger.info("event=%s %s", event, payload)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next: Any) -> Any:
    request_id = uuid4().hex
    start = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        latency_ms = int((time.perf_counter() - start) * 1000)
        _log_event(
            "request_failed",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            latency_ms=latency_ms,
        )
        raise

    latency_ms = int((time.perf_counter() - start) * 1000)
    response.headers["X-Request-Id"] = request_id
    _log_event(
        "request_completed",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        latency_ms=latency_ms,
    )
    return response


async def _verify_webhook_token(
    x_webhook_token: str | None = Header(default=None, alias="X-Webhook-Token"),
) -> None:
    if settings.webhook_token and x_webhook_token != settings.webhook_token:
        raise HTTPException(status_code=401, detail="Unauthorized webhook request")


class TranscriptMessage(BaseModel):
    role: str = Field(pattern=r"^(user|assistant)$")
    text: str


class TranscriptResponse(BaseModel):
    status: str
    messages: list[TranscriptMessage]


class MemoryItem(BaseModel):
    id: str
    text: str
    category: str
    timestamp: str


class MemoriesResponse(BaseModel):
    status: str
    data: list[MemoryItem]


class ContextResponse(BaseModel):
    status: str
    injected_prompt: str


class ToolModel(BaseModel):
    name: str | None = None


class ToolCallArguments(BaseModel):
    fact_to_remember: str | None = None
    category: str | None = None


class ToolCallModel(BaseModel):
    id: str | None = None
    arguments: ToolCallArguments | dict[str, Any] | None = None


class ToolWithToolCallModel(BaseModel):
    tool: ToolModel | None = None
    toolCall: ToolCallModel | None = None


class WebhookMessageModel(BaseModel):
    type: str | None = None
    toolWithToolCallList: list[ToolWithToolCallModel] = Field(default_factory=list)


class WebhookPayload(BaseModel):
    message: WebhookMessageModel | None = None


class ToolResultModel(BaseModel):
    toolCallId: str
    result: str


class WebhookResponse(BaseModel):
    results: list[ToolResultModel]


class CallEndedPayload(RootModel[dict[str, Any]]):
    pass


class CallReportItem(BaseModel):
    timestamp: str
    summary: str
    transcript_excerpt: str | None = None


class CallEndedResponse(BaseModel):
    status: str
    report: str


class ReportsResponse(BaseModel):
    status: str
    reports: list[CallReportItem]


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _sanitize_preview(text: str, limit: int = MAX_PREVIEW_LEN) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned.encode("ascii", errors="ignore").decode("ascii")
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3].rstrip()}..."


def _append_transcript(role: str, text: str) -> None:
    with _transcript_lock:
        _transcript.append(TranscriptMessage(role=role, text=text))


def _extract_first_valid_tool_call(
    payload: WebhookPayload,
) -> tuple[str | None, str | None, str]:
    """Return first valid (tool_call_id, fact, category)."""
    if payload.message is None:
        return None, None, "general"

    tool_calls = payload.message.toolWithToolCallList

    # This parser is intentionally defensive because webhook payloads can vary.
    # The first valid memory tool call is processed to keep the endpoint responsive.
    for item in tool_calls:
        if item.tool is None or item.tool.name != MEMORY_TOOL_NAME:
            continue
        if item.toolCall is None:
            continue

        tool_call_id = item.toolCall.id
        args = item.toolCall.arguments

        fact: str | None = None
        category = "general"

        if isinstance(args, ToolCallArguments):
            fact = args.fact_to_remember
            if args.category:
                category = args.category
        elif isinstance(args, dict):
            fact_value = args.get("fact_to_remember")
            category_value = args.get("category")
            if isinstance(fact_value, str):
                fact = fact_value
            if isinstance(category_value, str) and category_value.strip():
                category = category_value

        if fact and fact.strip():
            return tool_call_id, fact.strip(), category.strip() or "general"

    return None, None, "general"


def _extract_tool_call_id(payload: WebhookPayload, tool_name: str) -> str | None:
    if payload.message is None:
        return None

    tool_calls = payload.message.toolWithToolCallList

    for item in tool_calls:
        if item.tool and item.tool.name == tool_name and item.toolCall and item.toolCall.id:
            return item.toolCall.id

    for item in tool_calls:
        if item.toolCall and item.toolCall.id:
            return item.toolCall.id

    return None


def _extract_memory_text(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("text", "memory", "content"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _extract_memory_id(item: Any, fallback_index: int) -> str:
    if isinstance(item, dict):
        for key in ("id", "memory_id", "uuid"):
            value = item.get(key)
            if value is not None:
                return str(value)
    return f"memory-{fallback_index}"


def _extract_memory_category(item: Any) -> str:
    if isinstance(item, dict):
        direct = item.get("category")
        if isinstance(direct, str) and direct.strip():
            return direct

        metadata = item.get("metadata")
        if isinstance(metadata, dict):
            metadata_category = metadata.get("category")
            if isinstance(metadata_category, str) and metadata_category.strip():
                return metadata_category
    return "general"


def _extract_memory_timestamp(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("timestamp", "created_at", "updated_at"):
            value = item.get(key)
            if value is not None:
                return str(value)
    return _utc_now_iso()


def _normalize_memories(raw_memories: Any) -> list[MemoryItem]:
    memories_payload = raw_memories
    if isinstance(raw_memories, dict):
        if isinstance(raw_memories.get("results"), list):
            memories_payload = raw_memories["results"]
        elif isinstance(raw_memories.get("data"), list):
            memories_payload = raw_memories["data"]

    if not isinstance(memories_payload, list):
        return []

    normalized: list[MemoryItem] = []
    for index, item in enumerate(memories_payload, start=1):
        text = _extract_memory_text(item)
        if not text:
            continue

        normalized.append(
            MemoryItem(
                id=_extract_memory_id(item, index),
                text=text,
                category=_extract_memory_category(item),
                timestamp=_extract_memory_timestamp(item),
            )
        )

    return normalized


def _extract_transcript_excerpt(transcript: Any, limit: int = 220) -> str | None:
    if isinstance(transcript, str):
        preview = _sanitize_preview(transcript, limit)
        return preview or None

    if isinstance(transcript, list):
        parts: list[str] = []
        for entry in transcript:
            if isinstance(entry, str):
                parts.append(entry)
            elif isinstance(entry, dict):
                text = entry.get("text") or entry.get("content")
                if isinstance(text, str):
                    parts.append(text)
        joined = " ".join(parts)
        preview = _sanitize_preview(joined, limit)
        return preview or None

    if isinstance(transcript, dict):
        with suppress(Exception):
            serialized = str(transcript)
            preview = _sanitize_preview(serialized, limit)
            return preview or None

    return None


def _build_mem0_client() -> Any:
    api_key = (settings.mem0_api_key or os.getenv("MEM0_API_KEY", "")).strip()
    if not api_key:
        raise HTTPException(status_code=500, detail="MEM0_API_KEY is not configured")

    if MemoryClient is None:
        raise HTTPException(status_code=500, detail="mem0ai package is unavailable")

    try:
        return MemoryClient(api_key=api_key)
    except TypeError:
        # Some SDK versions read MEM0_API_KEY directly from environment.
        return MemoryClient()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to initialize Mem0 client: {exc}")


def _get_mem0_client() -> Any:
    global _mem0_client
    with _mem0_lock:
        if _mem0_client is None:
            _mem0_client = _build_mem0_client()
        return _mem0_client


def _save_memory_in_background(fact: str, category: str) -> None:
    try:
        client = _get_mem0_client()
        messages = [{"role": "user", "content": fact}]

        try:
            client.add(messages, user_id=DEMO_USER_ID, metadata={"category": category})
        except TypeError:
            client.add(messages, user_id=DEMO_USER_ID)

        _log_event("memory_saved_background", category=category)
    except Exception as exc:
        _log_event("memory_save_background_failed", error=str(exc))


@router.get("/transcript", response_model=TranscriptResponse)
async def get_transcript() -> TranscriptResponse:
    try:
        with _transcript_lock:
            messages = list(_transcript)
        return TranscriptResponse(status="success", messages=messages)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch transcript: {exc}")


@router.get("/memories", response_model=MemoriesResponse)
async def get_memories() -> MemoriesResponse:
    try:
        client = _get_mem0_client()
        # Mem0 Platform get_all requires filters; fallback keeps compatibility with other SDK variants.
        try:
            raw_memories = await run_in_threadpool(
                client.get_all,
                filters={"user_id": DEMO_USER_ID},
            )
        except Exception:
            raw_memories = await run_in_threadpool(client.get_all, user_id=DEMO_USER_ID)
        normalized = _normalize_memories(raw_memories)
        return MemoriesResponse(status="success", data=normalized)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch memories: {exc}")


@router.get("/user/context", response_model=ContextResponse)
async def get_user_context() -> ContextResponse:
    fallback_prompt = (
        "You are assisting a user with the following known context: none. "
        "Be proactive about these topics."
    )

    try:
        client = _get_mem0_client()
        try:
            raw_memories = await run_in_threadpool(
                client.get_all,
                filters={"user_id": DEMO_USER_ID},
            )
        except Exception:
            raw_memories = await run_in_threadpool(client.get_all, user_id=DEMO_USER_ID)

        normalized = _normalize_memories(raw_memories)
        memory_texts = [f"[{_sanitize_preview(item.text, 120)}]" for item in normalized if item.text]

        if not memory_texts:
            return ContextResponse(status="success", injected_prompt=fallback_prompt)

        injected_prompt = (
            "You are assisting a user with the following known context: "
            f"{', '.join(memory_texts)}. "
            "Be proactive about these topics."
        )
        return ContextResponse(status="success", injected_prompt=injected_prompt)
    except Exception:
        return ContextResponse(status="success", injected_prompt=fallback_prompt)


@router.post(
    "/webhook/save_memory",
    response_model=WebhookResponse,
    dependencies=[Depends(_verify_webhook_token)],
)
async def save_memory_webhook(
    payload: WebhookPayload,
    background_tasks: BackgroundTasks,
) -> WebhookResponse:
    try:
        tool_call_id, fact, category = _extract_first_valid_tool_call(payload)
        if not tool_call_id or not fact:
            return WebhookResponse(results=[])

        preview = _sanitize_preview(fact)
        _append_transcript("assistant", f"[Memory Saved: {preview}]")

        # Network write is offloaded to keep webhook latency below strict limits.
        background_tasks.add_task(_save_memory_in_background, fact, category)

        return WebhookResponse(
            results=[
                ToolResultModel(
                    toolCallId=tool_call_id,
                    result="Memory successfully saved.",
                )
            ]
        )
    except Exception:
        return WebhookResponse(results=[])


@router.post(
    "/webhook/check_calendar",
    response_model=WebhookResponse,
    dependencies=[Depends(_verify_webhook_token)],
)
async def check_calendar_webhook(payload: WebhookPayload) -> WebhookResponse:
    try:
        tool_call_id = _extract_tool_call_id(payload, CALENDAR_TOOL_NAME)
        if not tool_call_id:
            return WebhookResponse(results=[])

        result = "Agenda for today: 14:00 - Board Meeting, 16:00 - Dentist Appointment."
        _append_transcript("assistant", "[Calendar Checked: 2 events found]")

        return WebhookResponse(
            results=[
                ToolResultModel(
                    toolCallId=tool_call_id,
                    result=result,
                )
            ]
        )
    except Exception:
        return WebhookResponse(results=[])


@router.post(
    "/webhook/call_ended",
    response_model=CallEndedResponse,
    dependencies=[Depends(_verify_webhook_token)],
)
async def call_ended_webhook(payload: CallEndedPayload) -> CallEndedResponse:
    try:
        data = payload.root if isinstance(payload.root, dict) else {}
        transcript_excerpt = _extract_transcript_excerpt(data.get("transcript"))
        summary = "Call completed. 3 action items identified."

        report = CallReportItem(
            timestamp=_utc_now_iso(),
            summary=summary,
            transcript_excerpt=transcript_excerpt,
        )
        with _reports_lock:
            _call_reports.insert(0, report)

        return CallEndedResponse(status="success", report=summary)
    except Exception:
        fallback = "Call completed. Report generation deferred."
        report = CallReportItem(
            timestamp=_utc_now_iso(),
            summary=fallback,
            transcript_excerpt=None,
        )
        with _reports_lock:
            _call_reports.insert(0, report)
        return CallEndedResponse(status="success", report=fallback)


@router.get("/reports", response_model=ReportsResponse)
async def get_reports() -> ReportsResponse:
    with _reports_lock:
        reports = list(_call_reports)
    return ReportsResponse(status="success", reports=reports)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(router)
