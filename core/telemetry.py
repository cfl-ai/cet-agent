"""
可观测性模块 — OpenTelemetry 集成（带优雅降级）
未装 OTel 或初始化失败时，所有 tracing 退化为 no-op。
"""
import os
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# ---- 统一在顶部尝试导入所有 OTel 相关包 ----
_OTEL_AVAILABLE = False
_ENABLED = False
tracer = None

_OTLP_AVAILABLE = False
_FASTAPI_INSTR_AVAILABLE = False
_HTTPX_INSTR_AVAILABLE = False

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor, ConsoleSpanExporter,
    )
    from opentelemetry.sdk.resources import Resource
    _OTEL_AVAILABLE = True
except ImportError:
    logger.info("[Telemetry] OpenTelemetry 核心未安装，追踪功能禁用")

try:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
    _OTLP_AVAILABLE = True
except ImportError:
    pass

try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    _FASTAPI_INSTR_AVAILABLE = True
except ImportError:
    pass

try:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    _HTTPX_INSTR_AVAILABLE = True
except ImportError:
    pass


class _NoOpSpan:
    """OTel 不可用时的占位 span"""
    def set_attribute(self, *a, **kw): pass
    def record_exception(self, *a, **kw): pass
    def set_status(self, *a, **kw): pass
    def add_event(self, *a, **kw): pass


def setup_telemetry(app=None) -> bool:
    """初始化 OTel，在 FastAPI 启动时调用"""
    global _ENABLED, tracer

    if not _OTEL_AVAILABLE:
        return False

    try:
        resource = Resource.create({
            "service.name": "cet-agent",
            "service.version": "1.0.0",
            "deployment.environment": os.getenv("ENV", "production"),
        })

        provider = TracerProvider(resource=resource)

        otel_endpoint = os.getenv("OTEL_ENDPOINT", "")
        if otel_endpoint and _OTLP_AVAILABLE:
            try:
                exporter = OTLPSpanExporter(endpoint=otel_endpoint, insecure=True)
                provider.add_span_processor(BatchSpanProcessor(exporter))
                logger.info(f"[Telemetry] OTLP 导出到 {otel_endpoint}")
            except Exception as e:
                logger.warning(f"[Telemetry] OTLP 导出失败，回退控制台：{e}")
                provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        else:
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
            if otel_endpoint:
                logger.info("[Telemetry] OTLP exporter 未安装，追踪输出到控制台")
            else:
                logger.info("[Telemetry] 无 OTEL_ENDPOINT，追踪输出到控制台")

        trace.set_tracer_provider(provider)
        tracer = trace.get_tracer("cet-agent")

        # FastAPI 插桩（可选）
        if app is not None and _FASTAPI_INSTR_AVAILABLE:
            try:
                FastAPIInstrumentor.instrument_app(app)
                logger.info("[Telemetry] FastAPI 已插桩")
            except Exception as e:
                logger.warning(f"[Telemetry] FastAPI 插桩失败：{e}")
        elif app is not None:
            logger.info("[Telemetry] FastAPI 插桩库未安装，跳过")

        # httpx 插桩（可选）
        if _HTTPX_INSTR_AVAILABLE:
            try:
                HTTPXClientInstrumentor().instrument()
                logger.info("[Telemetry] HTTPX 已插桩")
            except Exception as e:
                logger.warning(f"[Telemetry] HTTPX 插桩失败：{e}")
        else:
            logger.info("[Telemetry] HTTPX 插桩库未安装，跳过")

        _ENABLED = True
        return True

    except Exception as e:
        logger.error(f"[Telemetry] 初始化失败：{e}")
        _ENABLED = False
        return False


@contextmanager
def trace_span(name: str, attributes: dict = None):
    """
    上下文管理器：OTel 可用则创建 span，否则 no-op。
    用法：
        with trace_span("agent.QuestionAgent", {"task_id": "xxx"}):
            result = await do_work()
    """
    if not _ENABLED or tracer is None:
        yield _NoOpSpan()
        return

    with tracer.start_as_current_span(name) as span:
        if attributes:
            for k, v in attributes.items():
                try:
                    span.set_attribute(k, v)
                except Exception:
                    pass
        yield span


def is_enabled() -> bool:
    return _ENABLED