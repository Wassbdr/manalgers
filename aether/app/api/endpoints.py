from __future__ import annotations

import hmac

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.params import Query

from app.core.config import API_PREFIX, CALENDAR_TOOL_NAME, DEMO_USER_ID, logger, settings
from app.models.schemas import (
    CallEndedPayload,
    CallEndedResponse,
    CallReportItem,
    ContextResponse,
    MeetingStartPayload,
    MeetingTriggerResponse,
    MemoriesResponse,
    ReportsResponse,
    StatusMessageResponse,
    ToolResultModel,
    TranscriptResponse,
    VapiCallItem,
    VapiCallsResponse,
    VapiInjectPayload,
    VapiInjectResponse,
    VapiProvisionPayload,
    VapiProvisionResponse,
    VisionCapturePayload,
    WebhookPayload,
    WebhookResponse,
)
from app.services import memory_agent, vapi_client

router = APIRouter(prefix=API_PREFIX)

_PROACTIVE_TRIGGER_CATEGORIES = {"task", "meeting", "commitment"}


def _build_proactive_system_prompt(memory_texts: list[str]) -> str:
    memory_context = ", ".join(memory_texts) if memory_texts else "none"
    return (
        "You are Aether, an ambient proactive copilot with persistent memory. "
        "You must not behave like a passive question-answer engine. "
        "Do not wait for direct questions before helping. "
        "If the user mentions any topic, instantly cross-reference memory and proactively interject with relevant context. "
        "If a deadline, commitment, risk, or follow-up is detected, warn the user immediately and suggest the next concrete action. "
        "When you trigger a proactive alert, switch to a slightly urgent, authoritative, and direct tone. "
        "For proactive alerts, always preface spoken output with the exact phrase 'System Alert:'. "
        "Be proactive about these topics. "
        "known context: "
        f"{memory_context}."
    )


def _build_memory_saved_result(category: str) -> str:
    normalized = (category or "").strip().lower()
    if normalized not in _PROACTIVE_TRIGGER_CATEGORIES:
        return "Memory successfully saved."

    return (
        "Memory successfully saved. "
        "Aether internal directive: Proactively ask the user if they want to schedule a follow-up for this commitment right now. "
        "Aether Internal Protocol: You are initiating a proactive alert. "
        "You MUST shift your tone to be slightly more urgent, authoritative, and direct. "
        "Preface your spoken response strictly with the phrase 'System Alert:'."
    )


async def _verify_webhook_token(
    x_webhook_token: str | None = Header(default=None, alias="X-Webhook-Token"),
) -> None:
    configured = (settings.webhook_token or "").strip()
    if not configured:
        raise HTTPException(
            status_code=503,
            detail="Webhook auth is not configured on this server.",
        )

    token = (x_webhook_token or "").strip()
    if not token or not hmac.compare_digest(token, configured):
        raise HTTPException(status_code=401, detail="Unauthorized webhook request")


def _internal_error(detail: str, exc: Exception) -> None:
    logger.exception(detail, exc_info=exc)
    raise HTTPException(status_code=500, detail=detail)


@router.get("/transcript", response_model=TranscriptResponse)
async def get_transcript() -> TranscriptResponse:
    try:
        with memory_agent._transcript_lock:
            messages = list(memory_agent._transcript)
        return TranscriptResponse(status="success", messages=messages)
    except Exception as exc:
        _internal_error("Failed to fetch transcript", exc)


@router.delete("/transcript", response_model=StatusMessageResponse)
async def clear_transcript() -> StatusMessageResponse:
    try:
        with memory_agent._transcript_lock:
            memory_agent._transcript.clear()
        return StatusMessageResponse(status="success", message="Neural stream cleared.")
    except Exception as exc:
        _internal_error("Failed to clear transcript", exc)


@router.get("/memories", response_model=MemoriesResponse)
async def get_memories() -> MemoriesResponse:
    try:
        client = memory_agent._get_mem0_client()
        try:
            raw_memories = await run_in_threadpool(
                client.get_all,
                filters={"user_id": DEMO_USER_ID},
            )
        except Exception:
            raw_memories = await run_in_threadpool(client.get_all, user_id=DEMO_USER_ID)
        normalized = memory_agent._normalize_memories(raw_memories)
        return MemoriesResponse(status="success", data=normalized)
    except HTTPException:
        raise
    except Exception as exc:
        _internal_error("Failed to fetch memories", exc)


@router.get("/user/context", response_model=ContextResponse)
async def get_user_context() -> ContextResponse:
    fallback_prompt = _build_proactive_system_prompt(memory_texts=[])

    try:
        client = memory_agent._get_mem0_client()
        try:
            raw_memories = await run_in_threadpool(
                client.get_all,
                filters={"user_id": DEMO_USER_ID},
            )
        except Exception:
            raw_memories = await run_in_threadpool(client.get_all, user_id=DEMO_USER_ID)

        normalized = memory_agent._normalize_memories(raw_memories)
        memory_texts = [
            f"[{memory_agent._sanitize_preview(item.text, 120)}]"
            for item in normalized
            if item.text
        ]

        if not memory_texts:
            return ContextResponse(status="success", injected_prompt=fallback_prompt)

        injected_prompt = _build_proactive_system_prompt(memory_texts=memory_texts)
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
        tool_call_id, fact, category = memory_agent._extract_first_valid_tool_call(payload)
        if not tool_call_id or not fact:
            return WebhookResponse(results=[])

        preview = memory_agent._sanitize_preview(fact)
        memory_agent._append_transcript("assistant", f"[Memory Saved: {preview}]")

        # Network write is offloaded to keep webhook latency below strict limits.
        background_tasks.add_task(memory_agent._save_memory_in_background, fact, category)

        if (category or "").strip().lower() in _PROACTIVE_TRIGGER_CATEGORIES:
            memory_agent._append_transcript(
                "assistant",
                "[AETHER PROACTIVE ACTION: Commitment detected. Prompt user to schedule follow-up now.]",
            )

        return WebhookResponse(
            results=[
                ToolResultModel(
                    toolCallId=tool_call_id,
                    result=_build_memory_saved_result(category=category),
                )
            ]
        )
    except HTTPException:
        raise
    except Exception as exc:
        _internal_error("Failed to persist memory", exc)


@router.post(
    "/webhook/check_calendar",
    response_model=WebhookResponse,
    dependencies=[Depends(_verify_webhook_token)],
)
async def check_calendar_webhook(payload: WebhookPayload) -> WebhookResponse:
    try:
        tool_call_id = memory_agent._extract_tool_call_id(payload, CALENDAR_TOOL_NAME)
        if not tool_call_id:
            return WebhookResponse(results=[])

        result = "Agenda for today: 14:00 - Board Meeting, 16:00 - Dentist Appointment."
        memory_agent._append_transcript("assistant", "[Calendar Checked: 2 events found]")

        return WebhookResponse(
            results=[
                ToolResultModel(
                    toolCallId=tool_call_id,
                    result=result,
                )
            ]
        )
    except HTTPException:
        raise
    except Exception as exc:
        _internal_error("Failed to read calendar", exc)


@router.post(
    "/webhook/call_ended",
    response_model=CallEndedResponse,
    dependencies=[Depends(_verify_webhook_token)],
)
async def call_ended_webhook(payload: CallEndedPayload) -> CallEndedResponse:
    try:
        data = payload.root if isinstance(payload.root, dict) else {}
        transcript_excerpt = memory_agent._extract_transcript_excerpt(data.get("transcript"))
        summary = "Call completed. 3 action items identified."

        report = CallReportItem(
            timestamp=memory_agent._utc_now_iso(),
            summary=summary,
            transcript_excerpt=transcript_excerpt,
        )
        with memory_agent._reports_lock:
            memory_agent._call_reports.insert(0, report)

        return CallEndedResponse(status="success", report=summary)
    except Exception:
        fallback = "Call completed. Report generation deferred."
        report = CallReportItem(
            timestamp=memory_agent._utc_now_iso(),
            summary=fallback,
            transcript_excerpt=None,
        )
        with memory_agent._reports_lock:
            memory_agent._call_reports.insert(0, report)
        return CallEndedResponse(status="success", report=fallback)


@router.get("/reports", response_model=ReportsResponse)
async def get_reports() -> ReportsResponse:
    with memory_agent._reports_lock:
        reports = list(memory_agent._call_reports)
    return ReportsResponse(status="success", reports=reports)


@router.delete("/memories/forget", response_model=StatusMessageResponse)
async def forget_memories() -> StatusMessageResponse:
    try:
        client = memory_agent._get_mem0_client()

        try:
            await run_in_threadpool(client.delete_all, filters={"user_id": DEMO_USER_ID})
        except Exception:
            try:
                await run_in_threadpool(client.delete_all, user_id=DEMO_USER_ID)
            except Exception:
                await run_in_threadpool(client.delete, user_id=DEMO_USER_ID)

        return StatusMessageResponse(
            status="success",
            message="All memories permanently deleted. Zero-knowledge state restored.",
        )
    except HTTPException:
        raise
    except Exception as exc:
        _internal_error("Failed to forget memories", exc)


@router.post("/vision/capture", response_model=StatusMessageResponse)
async def capture_vision_context(
    payload: VisionCapturePayload,
    background_tasks: BackgroundTasks,
) -> StatusMessageResponse:
    try:
        description = payload.image_description.strip()
        if not description:
            raise HTTPException(status_code=400, detail="image_description cannot be empty")

        fact = f"Visual context captured: {description}"
        preview = memory_agent._sanitize_preview(description)
        memory_agent._append_transcript("assistant", f"[Vision Context Saved: {preview}]")

        background_tasks.add_task(memory_agent._save_memory_in_background, fact, "vision")

        return StatusMessageResponse(
            status="success",
            message="Visual context extracted and saved.",
        )
    except HTTPException:
        raise
    except Exception as exc:
        _internal_error("Failed to capture vision context", exc)


@router.post("/trigger/meeting_start", response_model=MeetingTriggerResponse)
async def trigger_meeting_start(payload: MeetingStartPayload) -> MeetingTriggerResponse:
    fallback_memory = "They asked for the survey results 4 hours ago."

    try:
        attendee_name = payload.attendee_name.strip()
        if not attendee_name:
            raise HTTPException(status_code=400, detail="attendee_name cannot be empty")

        selected_memory = fallback_memory

        try:
            client = memory_agent._get_mem0_client()
            try:
                raw_memories = await run_in_threadpool(
                    client.get_all,
                    filters={"user_id": DEMO_USER_ID},
                )
            except Exception:
                raw_memories = await run_in_threadpool(client.get_all, user_id=DEMO_USER_ID)

            normalized = memory_agent._normalize_memories(raw_memories)
            selected_memory = memory_agent._select_meeting_memory(
                attendee_name=attendee_name,
                memories=normalized,
                fallback=fallback_memory,
            )
        except Exception:
            selected_memory = fallback_memory

        whisper_generated = memory_agent._build_meeting_whisper(
            attendee_name=attendee_name,
            selected_memory=selected_memory,
        )
        memory_agent._append_transcript(
            "assistant",
            memory_agent._format_proactive_whisper_transcript(whisper_generated),
        )

        return MeetingTriggerResponse(status="success", whisper_generated=whisper_generated)
    except HTTPException:
        raise
    except Exception as exc:
        _internal_error("Failed to trigger meeting whisper", exc)


# ---------------------------------------------------------------------------
# Vapi management endpoints
# ---------------------------------------------------------------------------


@router.get("/vapi/calls", response_model=VapiCallsResponse)
async def get_vapi_calls(limit: int = Query(default=10, ge=1, le=100)) -> VapiCallsResponse:
    """Return the most-recent *limit* calls from the Vapi dashboard."""
    raw = await vapi_client.list_calls(limit=limit)
    calls = [
        VapiCallItem(
            id=c.get("id", ""),
            status=c.get("status"),
            createdAt=c.get("createdAt"),
            endedAt=c.get("endedAt"),
        )
        for c in raw
        if c.get("id")
    ]
    return VapiCallsResponse(status="success", calls=calls)


@router.post("/vapi/provision", response_model=VapiProvisionResponse)
async def provision_vapi_assistant(payload: VapiProvisionPayload) -> VapiProvisionResponse:
    """Create (or re-configure) the Aether assistant on Vapi.

    The assistant is provisioned with:
    * Both custom tools pointing back to this backend's webhook endpoints.
    * The caller-supplied *system_prompt* as the model system message.
    * ``vapi_server_url`` from settings as the server URL for end-of-call events.
    """
    if not payload.system_prompt.strip():
        raise HTTPException(status_code=400, detail="system_prompt cannot be empty")

    name = (payload.assistant_name or "Aether").strip() or "Aether"
    data = await vapi_client.create_assistant(
        system_prompt=payload.system_prompt.strip(),
        name=name,
    )
    assistant_id = data.get("id", "")
    return VapiProvisionResponse(
        status="success",
        assistant_id=assistant_id,
        message=f"Assistant '{name}' provisioned with id={assistant_id}.",
    )


@router.post("/vapi/inject", response_model=VapiInjectResponse)
async def inject_vapi_message(payload: VapiInjectPayload) -> VapiInjectResponse:
    """Inject a spoken message into an active Vapi call.

    This route uses the live call ``controlUrl`` returned by Vapi for active
    calls. Ended calls are rejected with a clear 409 instead of failing with an
    opaque upstream 404.
    """
    call_id = payload.call_id.strip()
    message = payload.message.strip()
    if not call_id:
        raise HTTPException(status_code=400, detail="call_id cannot be empty")
    if not message:
        raise HTTPException(status_code=400, detail="message cannot be empty")

    await vapi_client.say_in_call(
        call_id=call_id,
        message=message,
        end_call_after=payload.end_call_after,
    )
    memory_agent._append_transcript(
        "assistant",
        f'[VAPI INJECT → call={call_id}: "{memory_agent._sanitize_preview(message)}"]',
    )
    return VapiInjectResponse(
        status="success",
        message=f"Message injected into call {call_id}.",
    )
