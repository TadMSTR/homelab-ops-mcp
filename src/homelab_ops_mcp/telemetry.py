"""Optional telemetry — OTLP spans+metrics plus InfluxDB3 and NATS sinks.

Everything here is **off by default** and degrades to a no-op when either the
relevant environment variable is unset *or* the optional dependency is not
installed. The base install (and CI's ``[dev]`` extra) carry none of these
libraries, so importing this module must never fail — every backend import is
lazy and guarded.

Enable a backend by setting its endpoint env var. Credentials come from the
environment only, never from a config file or a tool argument:

- **OTLP traces + metrics** — ``OTEL_EXPORTER_OTLP_ENDPOINT``. Service name from
  ``OTEL_SERVICE_NAME`` (default ``homelab-ops-mcp``). Needs the ``[telemetry]``
  extra.
- **InfluxDB 3** — ``SYSTEM_OPS_INFLUXDB3_URL`` (+ ``SYSTEM_OPS_INFLUXDB3_TOKEN``,
  ``SYSTEM_OPS_INFLUXDB3_DATABASE``). Deliberately its own variable rather than a
  bare ``INFLUXDB_URL``: a shared name tends to hold a value meant for a
  different network, and a client that only connects on first write will not
  tell you it is wrong until a metric is dropped.
- **NATS** — ``SYSTEM_OPS_NATS_URL`` (+ optional ``SYSTEM_OPS_NATS_SUBJECT``,
  default ``system.ops.mcp.metrics``).

Per-tool signal: call count, error count, and latency in seconds.

**This server's tools are synchronous**, and FastMCP runs them on worker
threads, so ``record_tool_call`` is a plain context manager rather than an async
one and there is no event loop to schedule on. The two network sinks therefore
run on a dedicated background loop owned by this module, started lazily and only
if one of them is actually configured. With both unset — the default, and the
forge deployment, which enables OTLP alone — no thread is created at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
from collections.abc import Iterator
from typing import Any

import structlog

log = structlog.get_logger("homelab_ops.telemetry")

_DEFAULT_SERVICE_NAME = "homelab-ops-mcp"
_DEFAULT_NATS_SUBJECT = "system.ops.mcp.metrics"


# ---------------------------------------------------------------------------
# OTLP (traces + metrics) — gated on OTEL_EXPORTER_OTLP_ENDPOINT
# ---------------------------------------------------------------------------

_tracer: Any = None
_calls_counter: Any = None
_errors_counter: Any = None
_latency_hist: Any = None


def _service_name() -> str:
    return os.getenv("OTEL_SERVICE_NAME", "").strip() or _DEFAULT_SERVICE_NAME


def _init_otlp() -> None:
    """Wire OTLP tracing + metrics if an endpoint is set and the SDK is importable."""
    global _tracer, _calls_counter, _errors_counter, _latency_hist
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("otlp_import_failed", hint="pip install 'homelab-ops-mcp[telemetry]'")
        return

    try:
        service = _service_name()
        resource = Resource.create({"service.name": service})

        tp = TracerProvider(resource=resource)
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(tp)
        _tracer = trace.get_tracer(service)

        reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint))
        mp = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(mp)
        meter = metrics.get_meter(service)
        _calls_counter = meter.create_counter("homelab_ops_mcp.tool.calls", unit="1")
        _errors_counter = meter.create_counter("homelab_ops_mcp.tool.errors", unit="1")
        _latency_hist = meter.create_histogram("homelab_ops_mcp.tool.latency", unit="s")
        log.info("otlp_enabled", endpoint=endpoint, service=service)
    except Exception as exc:
        log.warning("otlp_init_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Background loop for the two network sinks
# ---------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_lock = threading.Lock()


def _sinks_configured() -> bool:
    return bool(
        os.getenv("SYSTEM_OPS_INFLUXDB3_URL", "").strip()
        or os.getenv("SYSTEM_OPS_NATS_URL", "").strip()
    )


def _ensure_loop() -> asyncio.AbstractEventLoop | None:
    """Start the sink loop on first use, or return the running one.

    Started lazily so a deployment with no sink configured never creates the
    thread. Daemon, because a metrics sink must not hold the process open.
    """
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is not None:
            return _loop
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=loop.run_forever, name="homelab-ops-telemetry", daemon=True
        )
        thread.start()
        _loop, _loop_thread = loop, thread
        return loop


def _schedule(coro: Any) -> None:
    """Fire-and-forget a coroutine on the sink loop. Never raises."""
    loop = _ensure_loop()
    if loop is None:  # pragma: no cover — _ensure_loop always returns a loop
        coro.close()
        return
    try:
        asyncio.run_coroutine_threadsafe(coro, loop)
    except RuntimeError:  # loop already closed, e.g. during shutdown
        coro.close()


# ---------------------------------------------------------------------------
# InfluxDB 3 sink — gated on SYSTEM_OPS_INFLUXDB3_URL
# ---------------------------------------------------------------------------

_influx_client: Any = None


def _init_influx() -> None:
    global _influx_client
    url = os.getenv("SYSTEM_OPS_INFLUXDB3_URL", "").strip()
    if not url:
        return
    try:
        from influxdb_client_3 import InfluxDBClient3
    except ImportError:
        log.warning("influxdb3_import_failed", hint="pip install 'homelab-ops-mcp[telemetry]'")
        return
    try:
        _influx_client = InfluxDBClient3(
            host=url,
            token=os.getenv("SYSTEM_OPS_INFLUXDB3_TOKEN", ""),
            database=os.getenv("SYSTEM_OPS_INFLUXDB3_DATABASE", "homelab_ops_mcp"),
        )
        # Constructing the client contacts nothing, so this line says the client
        # exists — not that the host is reachable. A bad URL surfaces as a write
        # failure below, not here.
        log.info("influxdb3_enabled", host=url)
    except Exception as exc:
        log.warning("influxdb3_init_failed", error=str(exc))


def _influx_write(tool: str, duration: float, error: str | None) -> None:
    if _influx_client is None:
        return
    try:
        from influxdb_client_3 import Point

        point = (
            Point("tool_call")
            .tag("tool", tool)
            .tag("status", "error" if error else "ok")
            .field("latency_s", float(duration))
            .field("count", 1)
        )
        if error:
            point = point.tag("error", error)
        _influx_client.write(point)
    except Exception as exc:
        log.warning("influxdb3_write_failed", tool=tool, error=str(exc))


# ---------------------------------------------------------------------------
# NATS sink — gated on SYSTEM_OPS_NATS_URL
# ---------------------------------------------------------------------------

_nats_conn: Any = None
_nats_lock: asyncio.Lock | None = None


async def _nats_publish(tool: str, duration: float, error: str | None) -> None:
    global _nats_conn, _nats_lock
    url = os.getenv("SYSTEM_OPS_NATS_URL", "").strip()
    if not url:
        return
    try:
        import nats
    except ImportError:
        log.warning("nats_import_failed", hint="pip install 'homelab-ops-mcp[telemetry]'")
        return
    subject = os.getenv("SYSTEM_OPS_NATS_SUBJECT", _DEFAULT_NATS_SUBJECT)
    if _nats_lock is None:
        _nats_lock = asyncio.Lock()
    try:
        async with _nats_lock:
            if _nats_conn is None or not _nats_conn.is_connected:
                # Bounded and quiet on purpose. nats-py's defaults retry a
                # dead endpoint for roughly two minutes per connect and log an
                # ERROR per attempt, which turns an unreachable metrics sink
                # into the loudest thing in the log.
                _nats_conn = await nats.connect(
                    url,
                    connect_timeout=2,
                    max_reconnect_attempts=3,
                    allow_reconnect=True,
                )
        payload = {
            "tool": tool,
            "latency_s": round(duration, 6),
            "status": "error" if error else "ok",
            "error": error,
        }
        await _nats_conn.publish(subject, json.dumps(payload).encode())
    except Exception as exc:
        log.warning("nats_publish_failed", tool=tool, error=str(exc))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_initialized = False


def init() -> None:
    """Initialise every enabled backend. Idempotent; safe to call at import time."""
    global _initialized
    if _initialized:
        return
    _initialized = True
    _init_otlp()
    _init_influx()


def enabled() -> bool:
    """Whether any backend is actually wired up."""
    return _tracer is not None or _calls_counter is not None or _sinks_configured()


def _emit(tool: str, duration: float, error: str | None) -> None:
    """Fan a recorded call out to every enabled sink. Never raises."""
    attrs = {"tool": tool, "status": "error" if error else "ok"}
    try:
        if _calls_counter is not None:
            _calls_counter.add(1, attrs)
        if _latency_hist is not None:
            _latency_hist.record(duration, attrs)
        if error and _errors_counter is not None:
            _errors_counter.add(1, {"tool": tool, "error": error})
    except Exception as exc:  # a metrics backend must never break a tool call
        log.warning("otlp_emit_failed", tool=tool, error=str(exc))

    # InfluxDB's write() is synchronous and blocking, so it goes to a thread:
    # a hung endpoint must not hold up the tool call that produced the metric.
    if _influx_client is not None:
        _schedule(asyncio.to_thread(_influx_write, tool, duration, error))

    if os.getenv("SYSTEM_OPS_NATS_URL", "").strip():
        _schedule(_nats_publish(tool, duration, error))


@contextlib.contextmanager
def record_tool_call(tool: str) -> Iterator[dict]:
    """Time a tool call, open an OTLP span, and emit metrics on exit.

    Yields a small dict; setting ``result["error"] = "<name>"`` inside the block
    marks the call as failed. That matters here because these tools report
    failure by *returning* ``{"error": ...}`` rather than raising, so counting
    only exceptions would report a 0% error rate on a tool failing every call.

    An exception is also recorded, and re-raised. With no backend enabled this
    is a timer with no observable effect.
    """
    start = time.perf_counter()
    outcome: dict = {"error": None}
    span_cm = (
        _tracer.start_as_current_span(f"tool.{tool}")
        if _tracer is not None
        else contextlib.nullcontext()
    )
    with span_cm as span:
        try:
            yield outcome
        except Exception as exc:
            outcome["error"] = type(exc).__name__
            if span is not None and hasattr(span, "record_exception"):
                span.record_exception(exc)
            raise
        finally:
            _emit(tool, time.perf_counter() - start, outcome["error"])


_DRAIN_TIMEOUT = 3.0


async def _drain_pending(timeout: float = _DRAIN_TIMEOUT) -> None:
    """Let queued sink tasks finish, then cancel any that are still running.

    Draining rather than cancelling outright: these tasks are the metrics
    themselves, and a shutdown that discards whatever happened to be queued
    loses exactly the records from the interval you most want to look at.
    Cancelling first is also what would make a just-scheduled coroutine get
    collected un-awaited.
    """
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current]
    if not pending:
        return
    _, still_running = await asyncio.wait(pending, timeout=timeout)
    for task in still_running:
        task.cancel()
    if still_running:
        await asyncio.gather(*still_running, return_exceptions=True)


def shutdown() -> None:
    """Flush and close backends, then stop the sink loop.

    Order matters. Draining comes first: a queued write runs against
    ``_influx_client``, so closing the client before the drain would let every
    still-queued metric run, find ``None``, and return without writing —
    silently losing exactly the records from the interval before shutdown.
    """
    global _nats_conn, _influx_client, _initialized, _loop, _loop_thread

    if _loop is not None:
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_drain_pending(), _loop).result(
                timeout=_DRAIN_TIMEOUT + 2
            )

    if _nats_conn is not None and _loop is not None:
        conn, _nats_conn = _nats_conn, None
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(conn.drain(), _loop).result(timeout=5)
    _nats_conn = None

    if _influx_client is not None:
        with contextlib.suppress(Exception):
            _influx_client.close()
        _influx_client = None

    if _loop is not None:
        _loop.call_soon_threadsafe(_loop.stop)
        if _loop_thread is not None:
            _loop_thread.join(timeout=5)
        with contextlib.suppress(Exception):
            _loop.close()
        _loop, _loop_thread = None, None

    _initialized = False


def reset_for_tests() -> None:
    """Drop all cached provider/sink state so a test can re-init from a clean slate."""
    global _tracer, _calls_counter, _errors_counter, _latency_hist
    global _influx_client, _nats_conn, _nats_lock, _initialized
    shutdown()
    _tracer = _calls_counter = _errors_counter = _latency_hist = None
    _influx_client = None
    _nats_conn = None
    _nats_lock = None
    _initialized = False
