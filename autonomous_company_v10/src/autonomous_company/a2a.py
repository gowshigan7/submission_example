"""Agent-to-Agent (A2A) protocol types for autonomous_company_v10."""

from __future__ import annotations

from enum import StrEnum
from typing import Any  # noqa: F401

from pydantic import BaseModel, Field


DEFAULT_TTL_SEC: int = 120
MAX_HOP_COUNT: int = 5


class Severity(StrEnum):
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    GRAVE = "grave"
    BLOCKER = "blocker"


class MessageKind(StrEnum):
    LEGACY = "legacy"
    REQUEST = "request"
    RESPONSE = "response"
    ESCALATION = "escalation"
    HITL_REQUEST = "hitl_request"
    HITL_REPLY = "hitl_reply"
    BROADCAST = "broadcast"


class MessageStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    READ = "read"
    ANSWERED = "answered"
    EXPIRED = "expired"
    ESCALATED = "escalated"


class OutboundMessage(BaseModel):
    to: str = Field(..., description="Recipient role_name, 'ceo', or 'broadcast'")
    kind: MessageKind = MessageKind.REQUEST
    severity: Severity = Severity.INFO
    content: str
    parent_id: str | None = None


class WorkerReply(BaseModel):
    content: str = Field(..., description="The main deliverable text")
    outbound: list[OutboundMessage] = Field(default_factory=list)
    escalate_to_ceo: str | None = Field(default=None)
    ask_human: str | None = Field(default=None)


class CEODecision(StrEnum):
    CONTINUE = "continue"
    ABORT = "abort"
    MODIFY_NEXT = "modify_next"
    ASK_HUMAN = "ask_human"


class CEOReaction(BaseModel):
    decision: CEODecision
    rationale: str = Field(...)
    new_instruction: str | None = Field(default=None)
    hitl_prompt: str | None = Field(default=None)
