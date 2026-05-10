from __future__ import annotations

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Phase 7 API smoke drill.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--require-ready", action="store_true")
    parser.add_argument("--request-id", default="phase7-smoke-drill")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    results = {
        "healthz": fetch(args.base_url, "/healthz", args.request_id),
        "readyz": fetch(args.base_url, "/readyz", args.request_id),
        "observability": fetch(args.base_url, "/internal/phase7/observability", args.request_id),
        "metrics": fetch(args.base_url, "/internal/phase7/metrics", args.request_id),
    }
    print(json.dumps(results, indent=2, sort_keys=True))

    if results["healthz"]["status_code"] != 200:
        return 1
    if args.require_ready and results["readyz"]["status_code"] != 200:
        return 1
    if results["observability"]["status_code"] != 200:
        return 1
    if results["metrics"]["status_code"] != 200:
        return 1
    if "randy_translation_queue_depth" not in results["metrics"].get("body", ""):
        return 1
    return 0


def fetch(base_url: str, path: str, request_id: str) -> dict:
    request = Request(
        base_url.rstrip("/") + path,
        headers={"X-Correlation-Id": request_id},
    )
    try:
        with urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
            return {
                "status_code": response.status,
                "content_type": response.headers.get("content-type", ""),
                "correlation_id": response.headers.get("x-correlation-id", ""),
                "body": parse_body(body),
            }
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        return {
            "status_code": exc.code,
            "content_type": exc.headers.get("content-type", ""),
            "correlation_id": exc.headers.get("x-correlation-id", ""),
            "body": parse_body(body),
        }
    except URLError as exc:
        return {
            "status_code": 0,
            "error": str(exc.reason),
        }


def parse_body(body: str):
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


if __name__ == "__main__":
    raise SystemExit(main())
