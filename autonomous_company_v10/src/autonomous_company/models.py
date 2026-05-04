from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


def new_id() -> str:
    return uuid.uuid4().hex


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class BudgetExceeded(Exception):
    def __init__(self, spent: float, budget: float) -> None:
        super().__init__(f"Budget exhausted: ${spent:.4f} >= ${budget:.4f}")
        self.spent = spent
        self.budget = budget


class StepFailed(Exception):
    def __init__(self, step_index: int, attempts: int, cause: Exception) -> None:
        super().__init__(f"Step {step_index} failed after {attempts} attempt(s): {cause}")
        self.step_index = step_index
        self.attempts = attempts
        self.cause = cause


class Company(BaseModel):
    id: str
    name: str
    mission: str
    total_budget_usd: float
    spent_usd: float = 0.0
    status: str = "pending"
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(use_enum_values=True)


class Agent(BaseModel):
    id: str
    company_id: str
    role: str
    system_prompt: str
    model: str
    budget_cap_usd: float
    spent_usd: float = 0.0
    manager_id: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    max_turns: int | None = None
    status: str = "idle"
    created_at: datetime


class Task(BaseModel):
    id: str
    company_id: str
    agent_id: str
    step_index: int
    instruction: str
    status: StepStatus = StepStatus.PENDING
    output: str | None = None
    error: str | None = None
    attempt: int = 0
    cost_usd: float = 0.0
    started_at: datetime | None = None
    completed_at: datetime | None = None


class Message(BaseModel):
    id: str
    company_id: str
    from_agent_id: str
    to_agent_id: str
    content: str
    kind: str = "legacy"
    severity: str = "info"
    status: str = "pending"
    parent_id: str | None = None
    hop_count: int = 0
    expires_at: datetime | None = None
    created_at: datetime


class AuditEntry(BaseModel):
    id: str
    company_id: str
    agent_id: str | None
    action: str
    details: dict = Field(default_factory=dict)
    created_at: datetime


class StepCheckpoint(BaseModel):
    company_id: str
    step_index: int
    status: StepStatus = StepStatus.PENDING
    output: str | None = None
    attempt: int = 0
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cost_usd: float = 0.0
    model_config = ConfigDict(use_enum_values=True)


class HITLRequest(BaseModel):
    id: str
    company_id: str
    agent_id: str
    prompt: str
    status: str = "pending"
    response: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class Event(BaseModel):
    id: str
    company_id: str
    type: str
    payload: dict = Field(default_factory=dict)
    created_at: datetime
