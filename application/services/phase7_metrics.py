from __future__ import annotations


def render_prometheus_metrics(snapshot: dict) -> str:
    lines = [
        "# HELP randy_translation_queue_depth Current RabbitMQ queue depth.",
        "# TYPE randy_translation_queue_depth gauge",
    ]
    for queue_name, depth in sorted(snapshot.get("queue_depth", {}).items()):
        lines.append(
            f'randy_translation_queue_depth{{queue="{_escape_label(queue_name)}"}} {int(depth)}'
        )

    lines.extend(
        [
            "# HELP randy_translation_dlq_count Current dead-letter queue depth.",
            "# TYPE randy_translation_dlq_count gauge",
            f"randy_translation_dlq_count {int(snapshot.get('dlq_count', 0))}",
            "# HELP randy_translation_stage_latency_seconds Pipeline stage latency.",
            "# TYPE randy_translation_stage_latency_seconds gauge",
        ]
    )
    for stage, metrics in sorted(snapshot.get("stage_latency_seconds", {}).items()):
        for quantile in ("avg", "p95"):
            lines.append(
                "randy_translation_stage_latency_seconds"
                f'{{stage="{_escape_label(stage)}",quantile="{quantile}"}} '
                f"{float(metrics.get(quantile, 0.0))}"
            )

    lines.extend(
        [
            "# HELP randy_translation_stage_status_count Pipeline stage executions by status.",
            "# TYPE randy_translation_stage_status_count counter",
        ]
    )
    for stage, counts in sorted(snapshot.get("stage_status_counts", {}).items()):
        for status, count in sorted(counts.items()):
            lines.append(
                "randy_translation_stage_status_count"
                f'{{stage="{_escape_label(stage)}",status="{_escape_label(status)}"}} '
                f"{int(count)}"
            )

    lines.extend(
        [
            "# HELP randy_translation_stage_retry_count Pipeline retry count by stage.",
            "# TYPE randy_translation_stage_retry_count counter",
        ]
    )
    for stage, count in sorted(snapshot.get("retry_count", {}).items()):
        lines.append(
            f'randy_translation_stage_retry_count{{stage="{_escape_label(stage)}"}} {int(count)}'
        )

    discovery = snapshot.get("discovery_freshness", {})
    if discovery.get("age_seconds") is not None:
        lines.extend(
            [
                "# HELP randy_translation_discovery_freshness_seconds Age of latest completed discovery sync.",
                "# TYPE randy_translation_discovery_freshness_seconds gauge",
                f"randy_translation_discovery_freshness_seconds {float(discovery['age_seconds'])}",
            ]
        )

    review = snapshot.get("review_aging_seconds", {})
    lines.extend(
        [
            "# HELP randy_translation_review_aging_seconds Pending review age.",
            "# TYPE randy_translation_review_aging_seconds gauge",
            f"randy_translation_review_aging_seconds{{quantile=\"oldest\"}} {float(review.get('oldest', 0.0))}",
            f"randy_translation_review_aging_seconds{{quantile=\"avg\"}} {float(review.get('avg', 0.0))}",
            f"randy_translation_review_aging_seconds{{quantile=\"p95\"}} {float(review.get('p95', 0.0))}",
            "# HELP randy_translation_pending_review_count Pending review count.",
            "# TYPE randy_translation_pending_review_count gauge",
            f"randy_translation_pending_review_count {int(review.get('pending_count', 0))}",
        ]
    )
    return "\n".join(lines) + "\n"


def _escape_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
