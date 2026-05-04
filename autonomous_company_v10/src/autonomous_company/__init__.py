"""
autonomous_company_v10 — Autonomous multi-agent mission platform.
"""
from __future__ import annotations

from .config import Settings, get_settings, init_settings
from .models import (
    Company, Agent, Task, Message, AuditEntry,
    StepCheckpoint, StepStatus, BudgetExceeded, StepFailed,
)
from .dag import StepGraph, StepNode, CycleError
from .decisions import TeamPlan, RoleSpec, StepSpec
from .interfaces import IStorage, ILLM, IWorker, ICEO, ISecurityHooks
from .factory import build_mission_stack, mission_context
from .telemetry import configure_logging, configure_tracing, get_cost_collector
from .events import EventBus, make_emit_fn
from .security_hooks import SecurityHooks, ToolCallBlocked

__version__ = "10.0.0"
__all__ = [
    "Settings", "get_settings", "init_settings",
    "Company", "Agent", "Task", "Message", "AuditEntry",
    "StepCheckpoint", "StepStatus", "BudgetExceeded", "StepFailed",
    "StepGraph", "StepNode", "CycleError",
    "TeamPlan", "RoleSpec", "StepSpec",
    "IStorage", "ILLM", "IWorker", "ICEO", "ISecurityHooks",
    "build_mission_stack", "mission_context",
    "configure_logging", "configure_tracing", "get_cost_collector",
    "EventBus", "make_emit_fn",
    "SecurityHooks", "ToolCallBlocked",
    "__version__",
]
