from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .decisions import StepSpec


class CycleError(ValueError):
    def __init__(self, cycle_path: list[int]) -> None:
        path_str = " -> ".join(str(i) for i in cycle_path)
        super().__init__(f"Dependency cycle detected: {path_str}")
        self.cycle_path = cycle_path


@dataclass
class StepNode:
    index: int
    spec: "StepSpec"
    deps: list[int]
    dependents: list[int] = field(default_factory=list)


class StepGraph:
    def __init__(self, steps: list["StepSpec"]) -> None:
        self._nodes: list[StepNode] = []
        self._build(steps)
        self._validate_acyclic()

    def _build(self, steps: list["StepSpec"]) -> None:
        for i, spec in enumerate(steps):
            self._nodes.append(StepNode(index=i, spec=spec, deps=list(spec.deps)))
        n = len(steps)
        for node in self._nodes:
            for dep_idx in node.deps:
                if dep_idx < 0 or dep_idx >= n:
                    raise ValueError(
                        f"Step {node.index} has invalid dep index {dep_idx} "
                        f"(must be 0 <= dep < {n})"
                    )
                if dep_idx == node.index:
                    raise ValueError(f"Step {node.index} cannot depend on itself")
                self._nodes[dep_idx].dependents.append(node.index)

    def _validate_acyclic(self) -> None:
        in_degree = [len(node.deps) for node in self._nodes]
        queue = [i for i, d in enumerate(in_degree) if d == 0]
        processed = 0
        while queue:
            idx = queue.pop(0)
            processed += 1
            for dep_idx in self._nodes[idx].dependents:
                in_degree[dep_idx] -= 1
                if in_degree[dep_idx] == 0:
                    queue.append(dep_idx)
        if processed < len(self._nodes):
            cycle = self._find_cycle()
            raise CycleError(cycle)

    def _find_cycle(self) -> list[int]:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = [WHITE] * len(self._nodes)
        path: list[int] = []
        cycle: list[int] = []

        def dfs(u: int) -> bool:
            nonlocal cycle
            color[u] = GRAY
            path.append(u)
            for v in self._nodes[u].dependents:
                if color[v] == GRAY:
                    # v is already in path — extract the cycle inline (fix C-8).
                    idx = path.index(v)
                    cycle = path[idx:] + [v]
                    return True
                if color[v] == WHITE and dfs(v):
                    return True
            color[u] = BLACK
            path.pop()
            return False

        for i in range(len(self._nodes)):
            if color[i] == WHITE:
                if dfs(i):
                    return cycle
        return []

    def parallel_groups(self) -> list[list[StepNode]]:
        if not self._nodes:
            return [[]]
        level = [0] * len(self._nodes)
        in_degree = [len(node.deps) for node in self._nodes]
        queue = [i for i, d in enumerate(in_degree) if d == 0]
        while queue:
            idx = queue.pop(0)
            for dep_idx in self._nodes[idx].dependents:
                level[dep_idx] = max(level[dep_idx], level[idx] + 1)
                in_degree[dep_idx] -= 1
                if in_degree[dep_idx] == 0:
                    queue.append(dep_idx)
        if not level:
            return [[]]
        max_level = max(level)
        groups: list[list[StepNode]] = [[] for _ in range(max_level + 1)]
        for i, node in enumerate(self._nodes):
            groups[level[i]].append(node)
        return groups

    def nodes_by_index(self) -> dict[int, StepNode]:
        return {node.index: node for node in self._nodes}

    def __len__(self) -> int:
        return len(self._nodes)

    def __repr__(self) -> str:
        groups = self.parallel_groups()
        return f"StepGraph({len(self._nodes)} nodes, {len(groups)} waves)"
