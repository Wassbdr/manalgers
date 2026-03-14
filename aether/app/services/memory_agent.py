from __future__ import annotations

import json
import os
import re
from contextlib import suppress
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from fastapi import HTTPException

from app.core.config import DEMO_USER_ID, MAX_PREVIEW_LEN, MEMORY_TOOL_NAME, _log_event, settings
from app.models.schemas import (
    CallReportItem,
    MemoryItem,
    ToolCallArguments,
    TranscriptMessage,
    WebhookPayload,
)

try:
    from mem0 import MemoryClient
except Exception:  # pragma: no cover
    MemoryClient = None  # type: ignore[assignment]

_transcript: list[TranscriptMessage] = []
_transcript_lock = Lock()

_mem0_client: Any | None = None
_mem0_lock = Lock()

_call_reports: list[CallReportItem] = []
_reports_lock = Lock()


class _InMemoryMem0Client:
    """Minimal Mem0-compatible client for local/demo mode."""

    def __init__(self) -> None:
        self._memories: list[dict[str, Any]] = []
        self._lock = Lock()

    def add(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        text = ""
        if messages and isinstance(messages[0], dict):
            content = messages[0].get("content")
            if isinstance(content, str):
                text = content.strip()

        metadata = kwargs.get("metadata") if isinstance(kwargs, dict) else None
        category = "general"
        if isinstance(metadata, dict):
            metadata_category = metadata.get("category")
            if isinstance(metadata_category, str) and metadata_category.strip():
                category = metadata_category.strip()

        with self._lock:
            self._memories.insert(
                0,
                {
                    "id": f"mem-{len(self._memories) + 1}",
                    "text": text,
                    "category": category,
                    "timestamp": _utc_now_iso(),
                    "user_id": kwargs.get("user_id", DEMO_USER_ID),
                },
            )
        return {"id": "ok"}

    def get_all(self, **kwargs: Any) -> dict[str, Any]:
        user_id = kwargs.get("user_id")
        filters = kwargs.get("filters")
        if isinstance(filters, dict):
            user_id = filters.get("user_id", user_id)

        with self._lock:
            if user_id:
                result = [m for m in self._memories if m.get("user_id") == user_id]
            else:
                result = list(self._memories)
        return {"results": result}

    def delete_all(self, **kwargs: Any) -> dict[str, Any]:
        user_id = kwargs.get("user_id")
        filters = kwargs.get("filters")
        if isinstance(filters, dict):
            user_id = filters.get("user_id", user_id)

        with self._lock:
            if user_id:
                self._memories = [m for m in self._memories if m.get("user_id") != user_id]
            else:
                self._memories.clear()
        return {"status": "ok"}

    def delete(self, **kwargs: Any) -> dict[str, Any]:
        return self.delete_all(**kwargs)


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
        elif isinstance(args, str):
            with suppress(Exception):
                parsed = json.loads(args)
                if isinstance(parsed, dict):
                    fact_value = parsed.get("fact_to_remember")
                    category_value = parsed.get("category")
                    if isinstance(fact_value, str):
                        fact = fact_value
                    if isinstance(category_value, str) and category_value.strip():
                        category = category_value

        if fact and fact.strip():
            return tool_call_id, fact.strip(), category.strip() or "general"

    return None, None, "general"


def _select_meeting_memory(attendee_name: str, memories: list[MemoryItem], fallback: str) -> str:
    attendee_lower = attendee_name.lower()
    for item in memories:
        if attendee_lower in item.text.lower():
            return item.text
    return fallback


def _build_meeting_whisper(attendee_name: str, selected_memory: str) -> str:
    return f"Meeting starting with {attendee_name}. Remember: {selected_memory}"


def _format_proactive_whisper_transcript(whisper_generated: str) -> str:
    return f'[PROACTIVE WHISPER INJECTED: "{whisper_generated}"]'


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
    mode = settings.external_services_mode

    if mode == "mock":
        _log_event("mem0_mode_selected", mode="mock", reason="external_services_mode")
        return _InMemoryMem0Client()

    api_key = (settings.mem0_api_key or os.getenv("MEM0_API_KEY", "")).strip()
    if not api_key:
        if mode == "real":
            raise HTTPException(status_code=500, detail="MEM0_API_KEY is not configured")
        _log_event("mem0_mode_selected", mode="mock", reason="missing_api_key")
        return _InMemoryMem0Client()

    if MemoryClient is None:
        if mode == "real":
            raise HTTPException(status_code=500, detail="mem0ai package is unavailable")
        _log_event("mem0_mode_selected", mode="mock", reason="mem0_package_unavailable")
        return _InMemoryMem0Client()

    try:
        _log_event("mem0_mode_selected", mode="real")
        return MemoryClient(api_key=api_key)
    except TypeError:
        # Some SDK versions read MEM0_API_KEY directly from environment.
        _log_event("mem0_mode_selected", mode="real", variant="env_only")
        return MemoryClient()
    except Exception as exc:
        if mode == "real":
            raise HTTPException(status_code=500, detail=f"Failed to initialize Mem0 client: {exc}")
        _log_event("mem0_mode_selected", mode="mock", reason="real_init_failed", error=str(exc))
        return _InMemoryMem0Client()


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
