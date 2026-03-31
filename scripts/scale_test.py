#!/usr/bin/env python3
"""
Async scale test runner for SafeRoute backend APIs.

Usage:
  python backend/scripts/scale_test.py
  python backend/scripts/scale_test.py --concurrency 1,5,10,20 --requests-per-stage 80
  python backend/scripts/scale_test.py --runs-per-stage 5 --warmup-requests 50
  python backend/scripts/scale_test.py --output-json /tmp/scale_report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Sequence, Set

import httpx


@dataclass
class Scenario:
    name: str
    method: str
    url: str
    expected_statuses: Set[int]
    json_body: Dict[str, Any] | None = None


@dataclass
class Sample:
    ok: bool
    status_code: int | None
    latency_ms: float
    error: str | None = None


@dataclass
class StageSummary:
    scenario: str
    concurrency: int
    requests: int
    success_rate: float
    error_rate: float
    rps: float
    avg_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float


def _percentile(sorted_values: Sequence[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = max(0, min(len(sorted_values) - 1, math.ceil((p / 100.0) * len(sorted_values)) - 1))
    return float(sorted_values[idx])


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return float(median(values))


def _build_default_scenarios() -> List[Scenario]:
    return [
        Scenario(
            name="user_management_health",
            method="GET",
            url="http://127.0.0.1:20000/health",
            expected_statuses={200},
        ),
        Scenario(
            name="notification_health",
            method="GET",
            url="http://127.0.0.1:20001/health",
            expected_statuses={200},
        ),
        Scenario(
            name="routing_health",
            method="GET",
            url="http://127.0.0.1:20002/health",
            expected_statuses={200},
        ),
        Scenario(
            name="feedback_health",
            method="GET",
            url="http://127.0.0.1:20004/health",
            expected_statuses={200},
        ),
        Scenario(
            name="sos_health",
            method="GET",
            url="http://127.0.0.1:20006/health",
            expected_statuses={200},
        ),
        Scenario(
            name="route_calculate",
            method="POST",
            url="http://127.0.0.1:20002/v1/routes/calculate",
            expected_statuses={200},
            json_body={
                "origin": {"lat": 53.3498, "lon": -6.2603},
                "destination": {"lat": 53.3438, "lon": -6.2546},
                "user_id": "scale-test-user",
                "preferences": {"optimize_for": "balanced", "transport_mode": "walking"},
            },
        ),
        Scenario(
            name="transit_plan",
            method="POST",
            url="http://127.0.0.1:20002/v1/transit/plan",
            expected_statuses={200, 404},
            json_body={
                "origin": {"lat": 53.3498, "lon": -6.2603},
                "destination": {"lat": 53.3478, "lon": -6.2597},
                "max_walking_distance_m": 1200,
                "max_transfers": 3,
                "search_window_minutes": 60,
            },
        ),
        Scenario(
            name="feedback_submit",
            method="POST",
            url="http://127.0.0.1:20004/v1/feedback/submit",
            expected_statuses={200},
            json_body={
                "user_id": "scale-test-user",
                "type": "route_quality",
                "severity": "low",
                "description": "scale test feedback payload",
            },
        ),
    ]


async def _call_once(
    client: httpx.AsyncClient, scenario: Scenario, headers: Dict[str, str] | None = None
) -> Sample:
    start = time.perf_counter()
    try:
        response = await client.request(
            method=scenario.method,
            url=scenario.url,
            json=scenario.json_body,
            headers=headers,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        ok = response.status_code in scenario.expected_statuses
        return Sample(ok=ok, status_code=response.status_code, latency_ms=elapsed_ms, error=None)
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return Sample(ok=False, status_code=None, latency_ms=elapsed_ms, error=str(exc))


def _synthetic_forwarded_ip(worker_id: int, request_index: int) -> str:
    second = worker_id % 250
    third = (request_index // 250) % 250
    fourth = (request_index % 250) + 1
    return f"10.{second}.{third}.{fourth}"


async def _execute_request_batch(
    *,
    client: httpx.AsyncClient,
    scenario: Scenario,
    concurrency: int,
    total_requests: int,
    rotate_client_ip: bool,
    request_index_offset: int,
    collect_samples: bool,
) -> tuple[List[Sample], float]:
    results: List[Sample] = []
    worker_count = max(1, concurrency)
    base_per_worker = total_requests // worker_count
    extra = total_requests % worker_count
    worker_plan: List[tuple[int, int]] = []
    next_start = request_index_offset
    for worker_id in range(worker_count):
        count = base_per_worker + (1 if worker_id < extra else 0)
        worker_plan.append((next_start, count))
        next_start += count

    async def worker(worker_id: int, start_index: int, count: int) -> None:
        for offset in range(count):
            request_index = start_index + offset
            headers = None
            if rotate_client_ip:
                headers = {
                    "X-Forwarded-For": _synthetic_forwarded_ip(worker_id, request_index),
                }
            sample = await _call_once(client, scenario, headers=headers)
            if collect_samples:
                results.append(sample)

    started = time.perf_counter()
    await asyncio.gather(
        *(
            worker(worker_id, start_index, count)
            for worker_id, (start_index, count) in enumerate(worker_plan)
        )
    )
    wall_seconds = max(0.001, time.perf_counter() - started)
    return results, wall_seconds


async def _run_stage(
    scenario: Scenario,
    *,
    concurrency: int,
    total_requests: int,
    warmup_requests: int,
    timeout_seconds: float,
    rotate_client_ip: bool,
    client_limits: httpx.Limits | None,
) -> StageSummary:
    if client_limits is None:
        client = httpx.AsyncClient(timeout=timeout_seconds)
    else:
        client = httpx.AsyncClient(timeout=timeout_seconds, limits=client_limits)
    try:
        if warmup_requests > 0:
            await _execute_request_batch(
                client=client,
                scenario=scenario,
                concurrency=concurrency,
                total_requests=warmup_requests,
                rotate_client_ip=rotate_client_ip,
                request_index_offset=0,
                collect_samples=False,
            )

        results, wall_seconds = await _execute_request_batch(
            client=client,
            scenario=scenario,
            concurrency=concurrency,
            total_requests=total_requests,
            rotate_client_ip=rotate_client_ip,
            request_index_offset=warmup_requests,
            collect_samples=True,
        )
    finally:
        await client.aclose()

    latencies = sorted(sample.latency_ms for sample in results)
    success_count = sum(1 for sample in results if sample.ok)
    error_count = len(results) - success_count

    return StageSummary(
        scenario=scenario.name,
        concurrency=concurrency,
        requests=len(results),
        success_rate=(success_count / len(results)) if results else 0.0,
        error_rate=(error_count / len(results)) if results else 0.0,
        rps=len(results) / wall_seconds,
        avg_ms=(sum(latencies) / len(latencies)) if latencies else 0.0,
        p50_ms=_percentile(latencies, 50),
        p95_ms=_percentile(latencies, 95),
        p99_ms=_percentile(latencies, 99),
    )


def _aggregate_stage_runs(runs: Sequence[StageSummary]) -> StageSummary:
    if not runs:
        raise ValueError("Expected at least one stage run to aggregate")

    base = runs[0]
    return StageSummary(
        scenario=base.scenario,
        concurrency=base.concurrency,
        requests=int(round(_median([float(row.requests) for row in runs]))),
        success_rate=_median([row.success_rate for row in runs]),
        error_rate=_median([row.error_rate for row in runs]),
        rps=_median([row.rps for row in runs]),
        avg_ms=_median([row.avg_ms for row in runs]),
        p50_ms=_median([row.p50_ms for row in runs]),
        p95_ms=_median([row.p95_ms for row in runs]),
        p99_ms=_median([row.p99_ms for row in runs]),
    )


def _parse_concurrency(value: str) -> List[int]:
    levels = []
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        levels.append(max(1, int(raw)))
    if not levels:
        raise ValueError("At least one concurrency level is required.")
    return levels


def _parse_name_list(value: str) -> Set[str]:
    names: Set[str] = set()
    for raw in value.split(","):
        item = raw.strip()
        if item:
            names.add(item)
    return names


def _sort_rows(rows: Iterable[StageSummary]) -> List[StageSummary]:
    return sorted(rows, key=lambda row: (row.scenario, row.concurrency))


def _print_summary(rows: Iterable[StageSummary]) -> None:
    header = (
        "scenario".ljust(24)
        + "conc".rjust(6)
        + "req".rjust(8)
        + "ok%".rjust(8)
        + "err%".rjust(8)
        + "rps".rjust(10)
        + "avg".rjust(10)
        + "p50".rjust(10)
        + "p95".rjust(10)
        + "p99".rjust(10)
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            row.scenario.ljust(24)
            + str(row.concurrency).rjust(6)
            + str(row.requests).rjust(8)
            + f"{row.success_rate * 100:7.1f}".rjust(8)
            + f"{row.error_rate * 100:7.1f}".rjust(8)
            + f"{row.rps:9.2f}".rjust(10)
            + f"{row.avg_ms:9.1f}".rjust(10)
            + f"{row.p50_ms:9.1f}".rjust(10)
            + f"{row.p95_ms:9.1f}".rjust(10)
            + f"{row.p99_ms:9.1f}".rjust(10)
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description="SafeRoute API scale test runner")
    parser.add_argument(
        "--concurrency",
        default="1,5,10,20",
        help="Comma-separated concurrency levels (default: 1,5,10,20)",
    )
    parser.add_argument(
        "--requests-per-stage",
        type=int,
        default=60,
        help="Measured requests per scenario per concurrency stage (default: 60)",
    )
    parser.add_argument(
        "--runs-per-stage",
        type=int,
        default=1,
        help="How many times to repeat each stage; median is reported (default: 1)",
    )
    parser.add_argument(
        "--warmup-requests",
        type=int,
        default=0,
        help="Warmup requests per stage before measurement (default: 0)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=20.0,
        help="Per-request timeout in seconds (default: 20)",
    )
    parser.add_argument(
        "--http-max-connections",
        type=int,
        default=0,
        help="httpx max_connections; 0 uses httpx defaults",
    )
    parser.add_argument(
        "--http-max-keepalive-connections",
        type=int,
        default=0,
        help="httpx max_keepalive_connections; 0 uses httpx defaults",
    )
    parser.add_argument(
        "--http-keepalive-expiry-seconds",
        type=float,
        default=-1.0,
        help="httpx keepalive_expiry in seconds; negative value uses httpx defaults",
    )
    parser.add_argument(
        "--randomize-concurrency-order",
        action="store_true",
        help="Shuffle concurrency order per scenario (default: false)",
    )
    parser.add_argument(
        "--random-seed",
        type=int,
        default=42,
        help="Seed used when --randomize-concurrency-order is enabled (default: 42)",
    )
    parser.add_argument(
        "--output-json",
        default="",
        help="Optional path to write JSON report",
    )
    parser.add_argument(
        "--no-rotate-client-ip",
        action="store_true",
        help="Disable synthetic X-Forwarded-For generation",
    )
    parser.add_argument(
        "--include-scenarios",
        default="",
        help="Optional comma-separated scenario names to include",
    )
    parser.add_argument(
        "--exclude-scenarios",
        default="",
        help="Optional comma-separated scenario names to exclude",
    )
    args = parser.parse_args()

    levels = _parse_concurrency(args.concurrency)
    scenarios = _build_default_scenarios()

    include_names = _parse_name_list(args.include_scenarios)
    exclude_names = _parse_name_list(args.exclude_scenarios)

    if include_names:
        scenarios = [scenario for scenario in scenarios if scenario.name in include_names]
    if exclude_names:
        scenarios = [scenario for scenario in scenarios if scenario.name not in exclude_names]

    if not scenarios:
        raise SystemExit("No scenarios selected. Check --include-scenarios / --exclude-scenarios.")

    runs_per_stage = max(1, int(args.runs_per_stage))
    warmup_requests = max(0, int(args.warmup_requests))
    requests_per_stage = max(1, int(args.requests_per_stage))

    raw_max_connections = int(args.http_max_connections)
    raw_max_keepalive_connections = int(args.http_max_keepalive_connections)
    raw_keepalive_expiry = float(args.http_keepalive_expiry_seconds)

    if raw_max_connections < 0:
        raise SystemExit("--http-max-connections must be >= 0")
    if raw_max_keepalive_connections < 0:
        raise SystemExit("--http-max-keepalive-connections must be >= 0")

    use_custom_http_limits = (
        raw_max_connections > 0 or raw_max_keepalive_connections > 0 or raw_keepalive_expiry >= 0
    )

    default_limits = httpx.Limits()
    max_connections = default_limits.max_connections
    max_keepalive_connections = default_limits.max_keepalive_connections
    keepalive_expiry = default_limits.keepalive_expiry

    if raw_max_connections > 0:
        max_connections = raw_max_connections
    if raw_max_keepalive_connections > 0:
        max_keepalive_connections = raw_max_keepalive_connections
    if raw_keepalive_expiry >= 0:
        keepalive_expiry = raw_keepalive_expiry

    if (
        max_connections is not None
        and max_keepalive_connections is not None
        and max_keepalive_connections > max_connections
    ):
        max_keepalive_connections = max_connections

    client_limits: httpx.Limits | None = None
    if use_custom_http_limits:
        client_limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=keepalive_expiry,
        )

    rng = random.Random(args.random_seed)

    all_rows: List[StageSummary] = []
    per_run_rows: List[Dict[str, Any]] = []
    concurrency_execution_order: Dict[str, List[int]] = {}

    for scenario in scenarios:
        print(f"\n[scale] scenario={scenario.name}")
        levels_for_scenario = list(levels)
        if args.randomize_concurrency_order:
            rng.shuffle(levels_for_scenario)
        concurrency_execution_order[scenario.name] = list(levels_for_scenario)

        for level in levels_for_scenario:
            run_rows: List[StageSummary] = []
            for run_index in range(1, runs_per_stage + 1):
                summary = await _run_stage(
                    scenario,
                    concurrency=level,
                    total_requests=requests_per_stage,
                    warmup_requests=warmup_requests,
                    timeout_seconds=args.timeout_seconds,
                    rotate_client_ip=not args.no_rotate_client_ip,
                    client_limits=client_limits,
                )
                run_rows.append(summary)
                run_payload = asdict(summary)
                run_payload["run_index"] = run_index
                per_run_rows.append(run_payload)
                print(
                    f"  run {run_index:>2}/{runs_per_stage:<2} conc={level:>3} "
                    f"req={summary.requests:>4} ok={summary.success_rate * 100:5.1f}% "
                    f"rps={summary.rps:8.2f} p95={summary.p95_ms:8.1f}ms"
                )

            aggregate = _aggregate_stage_runs(run_rows)
            all_rows.append(aggregate)
            print(
                f"  median           conc={level:>3} req={aggregate.requests:>4} "
                f"ok={aggregate.success_rate * 100:5.1f}% rps={aggregate.rps:8.2f} "
                f"p95={aggregate.p95_ms:8.1f}ms"
            )

    sorted_rows = _sort_rows(all_rows)

    print("\n[scale] summary (median across runs)")
    _print_summary(sorted_rows)

    if args.output_json:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "concurrency": levels,
            "concurrency_execution_order": concurrency_execution_order,
            "requests_per_stage": requests_per_stage,
            "warmup_requests": warmup_requests,
            "runs_per_stage": runs_per_stage,
            "timeout_seconds": args.timeout_seconds,
            "randomize_concurrency_order": args.randomize_concurrency_order,
            "random_seed": args.random_seed,
            "http_limits": {
                "mode": "custom" if client_limits is not None else "httpx_default",
                "args": {
                    "http_max_connections": raw_max_connections,
                    "http_max_keepalive_connections": raw_max_keepalive_connections,
                    "http_keepalive_expiry_seconds": raw_keepalive_expiry,
                },
                "effective": (
                    {
                        "max_connections": max_connections,
                        "max_keepalive_connections": max_keepalive_connections,
                        "keepalive_expiry_seconds": keepalive_expiry,
                    }
                    if client_limits is not None
                    else None
                ),
            },
            "results": [asdict(row) for row in sorted_rows],
            "results_per_run": per_run_rows,
        }
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\n[scale] wrote report: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
