from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Generator
from typing import TYPE_CHECKING, Any

import structlog
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

if TYPE_CHECKING:
    from .config import Settings

__all__ = ["configure_logging", "configure_tracing", "get_tracer", "CostCollector", "get_cost_collector", "timed_span"]


def configure_logging(settings: "Settings") -> None:
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.add_logger_name,
    ]
    if settings.log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True, exception_formatter=structlog.dev.plain_traceback))
    log_level_int = getattr(logging, settings.log_level.upper(), logging.INFO)
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level_int),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service="autonomous_company", version="10")


def configure_tracing(settings: "Settings") -> trace.TracerProvider:
    if not settings.otel_enabled:
        from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
        provider = SdkTracerProvider()
        trace.set_tracer_provider(provider)
        return provider
    provider = TracerProvider()
    exporter_kind = getattr(settings, "otel_exporter", "console")
    if exporter_kind == "console":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    elif exporter_kind == "otlp":
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        except ImportError as exc:
            raise ImportError("OTLP span exporter not installed.") from exc
        exporter = OTLPSpanExporter(endpoint=settings.otel_otlp_endpoint, insecure=True)
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    elif exporter_kind == "none":
        pass
    else:
        raise ValueError(f"Unknown otel_exporter={exporter_kind!r}")
    trace.set_tracer_provider(provider)
    return provider


def get_tracer(name: str = "autonomous_company") -> trace.Tracer:
    return trace.get_tracer(name, schema_url="https://opentelemetry.io/schemas/1.24.0")


class CostCollector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, float]] = {}

    def record(self, model: str, tokens_in: int, tokens_out: int, cost_usd: float, latency_ms: float) -> None:
        with self._lock:
            if model not in self._data:
                self._data[model] = {"call_count": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "total_latency_ms": 0.0}
            d = self._data[model]
            d["call_count"] += 1
            d["tokens_in"] += tokens_in
            d["tokens_out"] += tokens_out
            d["cost_usd"] += cost_usd
            d["total_latency_ms"] += latency_ms

    def snapshot(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {model: dict(stats) for model, stats in self._data.items()}

    def total_cost_usd(self) -> float:
        with self._lock:
            return sum(d["cost_usd"] for d in self._data.values())

    def reset(self) -> None:
        with self._lock:
            self._data.clear()


_cost_collector: CostCollector = CostCollector()


def get_cost_collector() -> CostCollector:
    return _cost_collector


@contextlib.contextmanager
def timed_span(span_name: str, *, model: str | None = None, record_cost: bool = False) -> Generator[dict, None, None]:
    tracer = get_tracer()
    meta: dict = {}
    t0 = time.monotonic()
    with tracer.start_as_current_span(span_name) as span:
        try:
            yield meta
        finally:
            latency_ms = (time.monotonic() - t0) * 1000.0
            try:
                span.set_attribute("latency_ms", latency_ms)
                if model:
                    span.set_attribute("llm.model", model)
                for key, value in meta.items():
                    if isinstance(value, (str, bool, int, float)):
                        span.set_attribute(key, value)
                    else:
                        span.set_attribute(key, str(value))
            except Exception:
                pass
            if record_cost and model and meta:
                _cost_collector.record(
                    model=model,
                    tokens_in=int(meta.get("tokens_in", 0) or 0),
                    tokens_out=int(meta.get("tokens_out", 0) or 0),
                    cost_usd=float(meta.get("cost_usd", 0.0) or 0.0),
                    latency_ms=latency_ms,
                )
