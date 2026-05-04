from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator, field_validator


class RoleSpec(BaseModel):
    role_name: str = Field(...)
    system_prompt: str = Field(...)
    model: str = Field(default="claude-sonnet-4-6")
    budget_cap_usd: float = Field(default=0.50, ge=0.01, le=10.0)
    allowed_tools: list[str] = Field(default_factory=list)
    max_turns: int | None = Field(default=None, ge=1)
    thinking: bool = False
    effort: Literal["low", "medium", "high"] | None = None
    skills: list[str] = Field(default_factory=list)


class StepSpec(BaseModel):
    to_role: str = Field(...)
    instruction: str = Field(...)
    include_prior_outputs: list[int] = Field(default_factory=list)
    output_schema: dict[str, Any] | None = Field(default=None)
    condition: str | None = Field(default=None)
    deps: list[int] = Field(default_factory=list)


class TeamPlan(BaseModel):
    reasoning: str = Field(...)
    roles: list[RoleSpec] = Field(..., min_length=1)
    steps: list[StepSpec] = Field(..., min_length=1)
    final_output_step: int = Field(...)
    ask_human: str | None = Field(default=None)

    @field_validator("roles")
    @classmethod
    def _validate_role_identifiers(cls, roles: list[RoleSpec]) -> list[RoleSpec]:
        for role in roles:
            if not role.role_name:
                raise ValueError("role_name cannot be empty")
            if not role.role_name.isidentifier():
                raise ValueError(f"role_name {role.role_name!r} is not a valid identifier")
        return roles

    @model_validator(mode="after")
    def _validate_plan_consistency(self) -> "TeamPlan":
        seen: set[str] = set()
        for role in self.roles:
            if role.role_name in seen:
                raise ValueError(f"Duplicate role_name: {role.role_name!r}")
            seen.add(role.role_name)
        role_names = seen
        n_steps = len(self.steps)
        for i, step in enumerate(self.steps):
            if step.to_role not in role_names:
                raise ValueError(f"Step {i} references unknown role {step.to_role!r}")
            for prior_idx in step.include_prior_outputs:
                if prior_idx < 0:
                    raise ValueError(f"Step {i} include_prior_outputs has negative index {prior_idx}")
                if prior_idx >= i:
                    raise ValueError(f"Step {i} include_prior_outputs references step {prior_idx} which is not prior")
        if self.final_output_step < 0 or self.final_output_step >= n_steps:
            raise ValueError(f"final_output_step {self.final_output_step} out of range")
        from .dag import StepGraph, CycleError
        try:
            StepGraph(self.steps)
        except CycleError as e:
            raise ValueError(str(e)) from e
        return self
