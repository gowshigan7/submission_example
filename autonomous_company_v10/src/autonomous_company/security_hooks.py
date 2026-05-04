from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .config import Settings


# Shell command-chaining operators that can bypass allow-list pattern matching.
# fnmatch '*' matches '&&' and '||', so a pattern like 'curl *' would allow
# 'curl http://x.com && rm -rf /' without this secondary check.
_SHELL_CHAINING = re.compile(r'&&|\|\|')


class ToolCallBlocked(Exception):
    def __init__(self, command: str, reason: str, role: str | None = None) -> None:
        super().__init__(f"Blocked [{role or 'default'}]: {reason!r} — command: {command!r}")
        self.command = command
        self.reason = reason
        self.role = role


@dataclass
class AllowlistPolicy:
    default_sdk_tools: set[str] = field(default_factory=set)
    default_bash_patterns: list[str] = field(default_factory=list)
    role_overrides: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    blocklist_patterns: list[str] = field(default_factory=list)


class SecurityHooks:
    def __init__(self, settings: "Settings") -> None:
        path = settings.tools_allowlist_path
        if not path.exists():
            raise FileNotFoundError(f"Tools allowlist not found: {path}.")
        self._policy = self._load_policy(path)
        self._logger = logging.getLogger(__name__)

    @classmethod
    def from_yaml(cls, path: Path, settings: "Settings") -> "SecurityHooks":
        instance = object.__new__(cls)
        instance._policy = instance._load_policy(path)
        instance._logger = logging.getLogger(__name__)
        return instance

    def _load_policy(self, path: Path) -> AllowlistPolicy:
        with path.open() as f:
            data = yaml.safe_load(f)
        default = data.get("default", {})
        blocklist = data.get("blocklist", {})
        role_overrides_raw = data.get("role_overrides", {})
        role_overrides: dict[str, dict[str, list[str]]] = {}
        for role, overrides in role_overrides_raw.items():
            role_overrides[role] = {
                "sdk_tools": list(overrides.get("sdk_tools", [])),
                "bash_patterns": list(overrides.get("bash_patterns", [])),
            }
        return AllowlistPolicy(
            default_sdk_tools=set(default.get("sdk_tools", [])),
            default_bash_patterns=list(default.get("bash_patterns", [])),
            role_overrides=role_overrides,
            blocklist_patterns=list(blocklist.get("bash_patterns", [])),
        )

    def _is_blocked(self, command: str) -> bool:
        cmd_lower = command.strip().lower()
        for pattern in self._policy.blocklist_patterns:
            if fnmatch.fnmatch(cmd_lower, pattern.lower()):
                return True
        return False

    def _is_allowed_bash(self, command: str, role: str | None) -> bool:
        cmd_lower = command.strip().lower()
        patterns = list(self._policy.default_bash_patterns)
        if role and role in self._policy.role_overrides:
            patterns.extend(self._policy.role_overrides[role].get("bash_patterns", []))
        for pattern in patterns:
            if fnmatch.fnmatch(cmd_lower, pattern.lower()):
                return True
        return False

    def pre_bash_guard(self, command: str, role: str | None = None) -> None:
        command = command.strip()
        if not command:
            return
        if self._is_blocked(command):
            raise ToolCallBlocked(command=command, reason="matches blocklist pattern", role=role)
        if not self._is_allowed_bash(command, role):
            raise ToolCallBlocked(command=command, reason="no matching allowlist pattern", role=role)
        # Secondary check: fnmatch '*' matches '&&' and '||', so a command like
        # 'curl http://x.com && rm -rf /' can pass the allow-list pattern 'curl *'.
        # Block chaining operators explicitly (fix S-2).
        if _SHELL_CHAINING.search(command):
            raise ToolCallBlocked(
                command=command,
                reason="shell chaining operators (&& or ||) not permitted; split into separate commands",
                role=role,
            )

    def pre_tool_guard(self, tool_name: str, role: str | None = None) -> None:
        allowed = set(self._policy.default_sdk_tools)
        if role and role in self._policy.role_overrides:
            allowed.update(self._policy.role_overrides[role].get("sdk_tools", []))
        if tool_name not in allowed:
            raise ToolCallBlocked(command=tool_name, reason=f"tool '{tool_name}' not in allowed tools", role=role)

    def get_effective_bash_patterns(self, role: str | None = None) -> list[str]:
        patterns = list(self._policy.default_bash_patterns)
        if role and role in self._policy.role_overrides:
            patterns.extend(self._policy.role_overrides[role].get("bash_patterns", []))
        return patterns

    def get_effective_tools(self, role: str | None = None) -> set[str]:
        tools = set(self._policy.default_sdk_tools)
        if role and role in self._policy.role_overrides:
            tools.update(self._policy.role_overrides[role].get("sdk_tools", []))
        return tools

    def is_dangerous_bash_command(self, cmd: str) -> bool:
        return self._is_blocked(cmd)

    def build_pre_tool_hook(self, role: str | None = None):
        def hook(tool_name: str, tool_input: dict) -> None:
            if tool_name == "Bash":
                cmd = tool_input.get("command", "")
                self.pre_bash_guard(cmd, role=role)
            else:
                self.pre_tool_guard(tool_name, role=role)
        return hook


def build_default_hooks(*, agent_id: str, audit_log_path: Path, emit, role: str | None = None, settings: "Settings | None" = None) -> dict:
    hooks: dict = {"pre_tool_use": [], "post_tool_use": []}
    if settings is not None:
        security = SecurityHooks(settings)
        pre_hook = security.build_pre_tool_hook(role=role)
        hooks["pre_tool_use"].append(pre_hook)
    def post_audit(tool_name: str, tool_input: dict, tool_output) -> None:
        entry = {"agent_id": agent_id, "tool": tool_name, "input": tool_input, "output_preview": str(tool_output)[:500] if tool_output else None}
        try:
            with audit_log_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass
    hooks["post_tool_use"].append(post_audit)
    return hooks
