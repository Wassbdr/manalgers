from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, RootModel


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
