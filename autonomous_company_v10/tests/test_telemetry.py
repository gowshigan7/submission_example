from __future__ import annotations
import asyncio
import pytest
from autonomous_company.telemetry import CostCollector, configure_logging, configure_tracing, get_tracer, get_cost_collector, timed_span
from autonomous_company.config import Settings


@pytest.fixture
def settings():
    return Settings()


class TestCostCollector:
    def test_record_and_snapshot(self):
        c = CostCollector()
        c.record("claude-opus-4-7", tokens_in=100, tokens_out=50, cost_usd=0.005, latency_ms=1200.0)
        snap = c.snapshot()
        assert "claude-opus-4-7" in snap
        assert snap["claude-opus-4-7"]["call_count"] == 1
        assert snap["claude-opus-4-7"]["tokens_in"] == 100
        assert snap["claude-opus-4-7"]["cost_usd"] == pytest.approx(0.005)

    def test_accumulates_multiple_calls(self):
        c = CostCollector()
        c.record("sonnet", tokens_in=100, tokens_out=50, cost_usd=0.001, latency_ms=500.0)
        c.record("sonnet", tokens_in=200, tokens_out=100, cost_usd=0.002, latency_ms=600.0)
        snap = c.snapshot()
        assert snap["sonnet"]["call_count"] == 2
        assert snap["sonnet"]["cost_usd"] == pytest.approx(0.003)

    def test_total_cost_across_models(self):
        c = CostCollector()
        c.record("opus", tokens_in=100, tokens_out=50, cost_usd=0.01, latency_ms=1000.0)
        c.record("sonnet", tokens_in=50, tokens_out=25, cost_usd=0.002, latency_ms=500.0)
        assert c.total_cost_usd() == pytest.approx(0.012)

    def test_snapshot_is_copy(self):
        c = CostCollector()
        c.record("opus", 100, 50, 0.01, 1000.0)
        snap = c.snapshot()
        snap["opus"]["cost_usd"] = 999.0
        assert c.total_cost_usd() == pytest.approx(0.01)

    def test_reset(self):
        c = CostCollector()
        c.record("opus", 100, 50, 0.01, 1000.0)
        c.reset()
        assert c.total_cost_usd() == 0.0

    def test_thread_safety(self):
        import threading
        c = CostCollector()
        N = 100
        def record_many():
            for _ in range(N):
                c.record("model", 1, 1, 0.001, 10.0)
        threads = [threading.Thread(target=record_many) for _ in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert c.snapshot()["model"]["call_count"] == N * 10

    @pytest.mark.asyncio
    async def test_concurrent_async_records(self):
        c = CostCollector()
        N = 50
        async def record_async():
            for _ in range(N):
                c.record("async_model", 1, 1, 0.001, 5.0)
                await asyncio.sleep(0)
        await asyncio.gather(*[record_async() for _ in range(5)])
        assert c.snapshot()["async_model"]["call_count"] == N * 5


class TestConfigureLogging:
    def test_configure_console_mode(self, settings):
        configure_logging(Settings(log_format="console", log_level="INFO"))

    def test_configure_json_mode(self, settings):
        configure_logging(Settings(log_format="json", log_level="DEBUG"))

    def test_idempotent(self, settings):
        configure_logging(settings)
        configure_logging(settings)


class TestConfigureTracing:
    def test_noop_when_disabled(self, settings):
        provider = configure_tracing(Settings(otel_enabled=False))
        assert provider is not None
        with get_tracer().start_as_current_span("test.span") as span:
            span.set_attribute("test", "value")

    def test_console_exporter(self):
        assert configure_tracing(Settings(otel_enabled=True, otel_exporter="console")) is not None

    def test_none_exporter(self):
        assert configure_tracing(Settings(otel_enabled=True, otel_exporter="none")) is not None


class TestTimedSpan:
    def test_basic_usage(self):
        with timed_span("test.op") as meta:
            meta["tokens_in"] = 100

    def test_record_cost_true(self):
        collector = get_cost_collector()
        collector.reset()
        with timed_span("test.llm", model="test-model", record_cost=True) as meta:
            meta["tokens_in"] = 10
            meta["tokens_out"] = 5
            meta["cost_usd"] = 0.001
        assert "test-model" in collector.snapshot()

    def test_exception_does_not_crash(self):
        try:
            with timed_span("test.fail") as meta:
                raise ValueError("test error")
        except ValueError:
            pass
