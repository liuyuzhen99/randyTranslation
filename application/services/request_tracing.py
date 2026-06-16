from __future__ import annotations

from typing import Any


def request_span(name: str, attributes: dict[str, Any] | None = None):
    try:
        from opentelemetry import trace
    except ImportError:
        return _NoopSpanContext()

    tracer = trace.get_tracer("randyTranslation.request_tracing")
    span = tracer.start_as_current_span(name)
    return _SpanContext(span, attributes or {})


class _SpanContext:
    def __init__(self, span_context, attributes: dict[str, Any]) -> None:
        self.span_context = span_context
        self.attributes = attributes

    def __enter__(self):
        span = self.span_context.__enter__()
        for key, value in self.attributes.items():
            if value is not None:
                span.set_attribute(key, value)
        return span

    def __exit__(self, exc_type, exc, traceback):
        return self.span_context.__exit__(exc_type, exc, traceback)


class _NoopSpan:
    def set_attribute(self, _key: str, _value: Any) -> None:
        return None


class _NoopSpanContext:
    def __enter__(self):
        return _NoopSpan()

    def __exit__(self, exc_type, exc, traceback):
        return False
