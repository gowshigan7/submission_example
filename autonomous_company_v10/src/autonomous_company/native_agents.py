from __future__ import annotations
from typing import Any

NATIVE_AGENTS: dict[str, dict[str, Any]] = {
    "Explorer": {
        "model": "claude-haiku-4-5-20251001",
        "tools": ["Read", "Glob", "Grep"],
        "system_prompt": "You are Explorer, a concise read-only codebase exploration agent. Investigate and return findings with precise citations. Access only read-only tools.",
    },
    "Reviewer": {
        "model": "claude-sonnet-4-6",
        "tools": ["Read", "Bash", "Glob", "Grep"],
        "system_prompt": "You are Reviewer, a deliverable verification agent. Verify deliverables against acceptance criteria by reading files and running checks. Return a clear verdict.",
    },
    "Researcher": {
        "model": "claude-sonnet-4-6",
        "tools": ["WebSearch", "WebFetch", "Read"],
        "system_prompt": "You are Researcher, an external research agent. Investigate questions requiring information outside the local codebase. Return a tight, well-cited summary.",
    },
}


def merge_with_runtime_agents(runtime_agents: dict[str, Any] | None = None) -> dict[str, Any]:
    merged: dict[str, Any] = dict(NATIVE_AGENTS)
    if runtime_agents:
        merged.update(runtime_agents)
    return merged


def get_native_agent(name: str) -> dict[str, Any] | None:
    agent = NATIVE_AGENTS.get(name)
    if agent is None:
        return None
    return dict(agent)


__all__ = ["NATIVE_AGENTS", "merge_with_runtime_agents", "get_native_agent"]
