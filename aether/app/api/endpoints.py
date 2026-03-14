from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from fastapi.concurrency import run_in_threadpool

from app.core.config import API_PREFIX, CALENDAR_TOOL_NAME, DEMO_USER_ID, settings
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
    VisionCapturePayload,
    WebhookPayload,
    WebhookResponse,
)
from app.services import memory_agent

router = APIRouter(prefix=API_PREFIX)


async def _verify_webhook_token(
    x_webhook_token: str | None = Header(default=None, alias="X-Webhook-Token"),
) -> None:
    if settings.webhook_token and x_webhook_token != settings.webhook_token:
        raise HTTPException(status_code=401, detail="Unauthorized webhook request")


@router.get("/transcript", response_model=TranscriptResponse)
async def get_transcript() -> TranscriptResponse:
    try:
        with memory_agent._transcript_lock:
            messages = list(memory_agent._transcript)
        return TranscriptResponse(status="success", messages=messages)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch transcript: {exc}")


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
        raise HTTPException(status_code=500, detail=f"Failed to fetch memories: {exc}")


@router.get("/user/context", response_model=ContextResponse)
async def get_user_context() -> ContextResponse:
    fallback_prompt = (
        "You are assisting a user with the following known context: none. "
        "Be proactive about these topics."
    )

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
        tool_call_id, fact, category = memory_agent._extract_first_valid_tool_call(payload)
        if not tool_call_id or not fact:
            return WebhookResponse(results=[])

        preview = memory_agent._sanitize_preview(fact)
        memory_agent._append_transcript("assistant", f"[Memory Saved: {preview}]")

        # Network write is offloaded to keep webhook latency below strict limits.
        background_tasks.add_task(memory_agent._save_memory_in_background, fact, category)

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
        raise HTTPException(status_code=500, detail=f"Failed to forget memories: {exc}")


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
        raise HTTPException(status_code=500, detail=f"Failed to capture vision context: {exc}")


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
            attendee_lower = attendee_name.lower()
            for item in normalized:
                if attendee_lower in item.text.lower():
                    selected_memory = item.text
                    break
        except Exception:
            selected_memory = fallback_memory

        whisper_generated = (
            f"Meeting starting with {attendee_name}. Remember: {selected_memory}"
        )
        memory_agent._append_transcript(
            "assistant",
            f'[PROACTIVE WHISPER INJECTED: "{whisper_generated}"]',
        )

        return MeetingTriggerResponse(status="success", whisper_generated=whisper_generated)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to trigger meeting whisper: {exc}")
