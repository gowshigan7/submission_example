from __future__ import annotations
import pytest
from autonomous_company.dag import StepGraph, StepNode, CycleError
from dataclasses import dataclass, field as dc_field
from typing import Any

@dataclass
class FakeStep:
    to_role: str = "worker"
    instruction: str = "do it"
    include_prior_outputs: list[int] = dc_field(default_factory=list)
    output_schema: Any = None
    condition: Any = None
    deps: list[int] = dc_field(default_factory=list)

def make_steps(deps_list):
    return [FakeStep(deps=deps) for deps in deps_list]

class TestStepGraphBasics:
    def test_empty_steps(self):
        graph = StepGraph([])
        groups = graph.parallel_groups()
        assert groups == [[]] or groups == []

    def test_single_step_no_deps(self):
        graph = StepGraph(make_steps([[]]))
        groups = graph.parallel_groups()
        assert len(groups) == 1 and len(groups[0]) == 1

    def test_linear_chain(self):
        graph = StepGraph(make_steps([[], [0], [1]]))
        groups = graph.parallel_groups()
        assert len(groups) == 3
        assert [g[0].index for g in groups] == [0, 1, 2]

    def test_diamond_pattern(self):
        graph = StepGraph(make_steps([[], [0], [0], [1, 2]]))
        groups = graph.parallel_groups()
        assert len(groups) == 3
        assert len(groups[1]) == 2
        assert {n.index for n in groups[1]} == {1, 2}

    def test_all_parallel(self):
        graph = StepGraph(make_steps([[], [], [], []]))
        groups = graph.parallel_groups()
        assert len(groups) == 1 and len(groups[0]) == 4

    def test_len(self):
        assert len(StepGraph(make_steps([[], [0], [0]]))) == 3

    def test_nodes_by_index(self):
        by_idx = StepGraph(make_steps([[], [0]])).nodes_by_index()
        assert set(by_idx.keys()) == {0, 1}
        assert by_idx[1].deps == [0]

class TestStepGraphValidation:
    def test_self_dep_raises_value_error(self):
        with pytest.raises(ValueError, match="itself"):
            StepGraph(make_steps([[0]]))

    def test_out_of_range_dep_raises(self):
        with pytest.raises(ValueError):
            StepGraph(make_steps([[], [5]]))

    def test_negative_dep_raises(self):
        with pytest.raises(ValueError):
            StepGraph(make_steps([[], [-1]]))

    def test_simple_cycle_raises(self):
        with pytest.raises(CycleError) as exc_info:
            StepGraph(make_steps([[1], [0]]))
        assert len(exc_info.value.cycle_path) >= 2

    def test_three_node_cycle(self):
        with pytest.raises(CycleError):
            StepGraph(make_steps([[2], [0], [1]]))

    def test_valid_dag_no_cycle(self):
        graph = StepGraph(make_steps([[], [], [0, 1], [2], [2], [3, 4]]))
        assert len(graph.parallel_groups()) == 4

class TestCycleError:
    def test_cycle_error_message(self):
        err = CycleError([0, 1, 2, 0])
        assert "0" in str(err) and "1" in str(err)

    def test_cycle_path_stored(self):
        assert CycleError([3, 4, 3]).cycle_path == [3, 4, 3]

class TestParallelGroups:
    def test_groups_cover_all_nodes(self):
        graph = StepGraph(make_steps([[], [0], [0], [1, 2]]))
        all_indices = {n.index for g in graph.parallel_groups() for n in g}
        assert all_indices == {0, 1, 2, 3}

    def test_no_group_has_internal_deps(self):
        graph = StepGraph(make_steps([[], [0], [0], [1, 2], [3]]))
        for group in graph.parallel_groups():
            group_indices = {n.index for n in group}
            for node in group:
                for dep in node.deps:
                    assert dep not in group_indices

    def test_complex_dag_ordering(self):
        graph = StepGraph(make_steps([[], [], [0], [1], [2, 3]]))
        groups = graph.parallel_groups()
        assert len(groups) == 3
        assert len(groups[0]) == 2
        assert len(groups[1]) == 2
        assert len(groups[2]) == 1
