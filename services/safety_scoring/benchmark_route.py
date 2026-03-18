import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import text

# Ensure backend root is importable when running script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.safety_scoring.main import app, get_safety_scoring_db


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    k = (len(ordered) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] * (c - k) + ordered[c] * (k - f)


async def pick_random_points() -> dict:
    async for db in get_safety_scoring_db():
        start_row = (
            await db.execute(
                text(
                    """
                    SELECT
                      ST_Y(ST_StartPoint(geometry)) AS lat,
                      ST_X(ST_StartPoint(geometry)) AS lng
                    FROM ways TABLESAMPLE SYSTEM (0.2)
                    WHERE geometry IS NOT NULL
                    LIMIT 1
                    """
                )
            )
        ).fetchone()
        if not start_row:
            start_row = (
                await db.execute(
                    text(
                        """
                        SELECT
                          ST_Y(ST_StartPoint(geometry)) AS lat,
                          ST_X(ST_StartPoint(geometry)) AS lng
                        FROM ways
                        WHERE geometry IS NOT NULL
                        ORDER BY gid
                        LIMIT 1
                        """
                    )
                )
            ).fetchone()

        end_row = (
            await db.execute(
                text(
                    """
                    SELECT
                      ST_Y(ST_EndPoint(geometry)) AS lat,
                      ST_X(ST_EndPoint(geometry)) AS lng
                    FROM ways TABLESAMPLE SYSTEM (0.2)
                    WHERE geometry IS NOT NULL
                    LIMIT 1
                    """
                )
            )
        ).fetchone()
        if not end_row:
            end_row = (
                await db.execute(
                    text(
                        """
                        SELECT
                          ST_Y(ST_EndPoint(geometry)) AS lat,
                          ST_X(ST_EndPoint(geometry)) AS lng
                        FROM ways
                        WHERE geometry IS NOT NULL
                        ORDER BY gid DESC
                        LIMIT 1
                        """
                    )
                )
            ).fetchone()

        if not start_row or not end_row:
            raise RuntimeError("ways table is empty")
        return {
            "start": {"lat": float(start_row.lat), "lng": float(start_row.lng)},
            "end": {"lat": float(end_row.lat), "lng": float(end_row.lng)},
        }
    raise RuntimeError("database session unavailable")


async def benchmark_once(
    concurrency: int,
    total: int,
    warmup: int,
    timeout_s: float,
    progress_every: int,
    algorithm: str,
    payload: dict,
):
    print(f"[bench] algorithm={algorithm}", flush=True)
    print(f"[bench] payload={payload}", flush=True)
    transport = httpx.ASGITransport(app=app)
    timeout = httpx.Timeout(timeout_s)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=timeout
    ) as client:
        print(f"[bench] warmup start: {warmup} requests", flush=True)
        for _ in range(warmup):
            await client.post(f"/api/route?algorithm={algorithm}", json=payload)
        print("[bench] warmup done", flush=True)

        latencies: list[float] = []
        status_counts: dict[int, int] = {}
        errors = 0
        non_2xx = 0
        completed = 0
        sem = asyncio.Semaphore(concurrency)
        lock = asyncio.Lock()

        async def worker():
            nonlocal errors, non_2xx, completed
            async with sem:
                start = time.perf_counter()
                try:
                    resp = await client.post(f"/api/route?algorithm={algorithm}", json=payload)
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    status_counts[resp.status_code] = status_counts.get(resp.status_code, 0) + 1
                    if 200 <= resp.status_code < 300:
                        latencies.append(elapsed_ms)
                    else:
                        non_2xx += 1
                except Exception:
                    errors += 1
                async with lock:
                    completed += 1
                    if progress_every > 0 and (
                        completed % progress_every == 0 or completed == total
                    ):
                        print(
                            f"[bench] progress {completed}/{total} "
                            f"(ok={len(latencies)} err={errors})",
                            flush=True,
                        )

        print(f"[bench] benchmark start: total={total}, concurrency={concurrency}", flush=True)
        wall_start = time.perf_counter()
        await asyncio.gather(*[asyncio.create_task(worker()) for _ in range(total)])
        wall_seconds = time.perf_counter() - wall_start

    print("[bench] benchmark done", flush=True)
    print(f"requests={total} concurrency={concurrency} warmup={warmup} timeout_s={timeout_s}")
    print(f"status_counts={dict(sorted(status_counts.items()))} errors={errors} non_2xx={non_2xx}")
    if not latencies:
        print("no successful 2xx responses")
        return None
    metrics = {
        "min": round(min(latencies), 2),
        "avg": round(statistics.mean(latencies), 2),
        "p50": round(percentile(latencies, 50), 2),
        "p95": round(percentile(latencies, 95), 2),
        "p99": round(percentile(latencies, 99), 2),
        "max": round(max(latencies), 2),
    }
    throughput = round(total / wall_seconds, 2)
    print(f"throughput_rps={throughput}")
    print("latency_ms=" + str(metrics))
    return {
        "throughput_rps": throughput,
        "latency_ms": metrics,
        "errors": errors,
        "non_2xx": non_2xx,
        "status_counts": dict(sorted(status_counts.items())),
    }


async def benchmark(
    concurrency: int,
    total: int,
    warmup: int,
    timeout_s: float,
    progress_every: int,
    algorithm: str,
    include_ch: bool,
    start_lat: float | None,
    start_lng: float | None,
    end_lat: float | None,
    end_lng: float | None,
):
    if None not in (start_lat, start_lng, end_lat, end_lng):
        payload = {
            "start": {"lat": float(start_lat), "lng": float(start_lng)},
            "end": {"lat": float(end_lat), "lng": float(end_lng)},
        }
        print("[bench] using manual route points", flush=True)
    else:
        print("[bench] sampling route points from ways ...", flush=True)
        payload = await asyncio.wait_for(pick_random_points(), timeout=20.0)
    print(f"[bench] payload={payload}", flush=True)
    if algorithm == "compare":
        compare_algs = ["astar", "dijkstra", "bd_dijkstra"]
        if include_ch:
            compare_algs.append("ch")
        print(f"[bench] compare mode: {' vs '.join(compare_algs)}", flush=True)
        results = {}
        for alg in compare_algs:
            results[alg] = await benchmark_once(
                concurrency, total, warmup, timeout_s, progress_every, alg, payload
            )

        astar = results.get("astar")
        dijkstra = results.get("dijkstra")
        bd_dijkstra = results.get("bd_dijkstra")
        ch = results.get("ch")
        if astar and dijkstra and bd_dijkstra:
            a95 = astar["latency_ms"]["p95"]
            d95 = dijkstra["latency_ms"]["p95"]
            ap99 = astar["latency_ms"]["p99"]
            dp99 = dijkstra["latency_ms"]["p99"]
            b95 = bd_dijkstra["latency_ms"]["p95"]
            bp99 = bd_dijkstra["latency_ms"]["p99"]
            at = astar["throughput_rps"]
            dt = dijkstra["throughput_rps"]
            bt = bd_dijkstra["throughput_rps"]
            summary = {
                "p95_ms": {
                    "dijkstra": d95,
                    "astar": a95,
                    "bd_dijkstra": b95,
                    "astar_vs_dijkstra_improve_pct": (
                        round((d95 - a95) * 100 / d95, 2) if d95 else 0
                    ),
                    "bd_vs_dijkstra_improve_pct": round((d95 - b95) * 100 / d95, 2) if d95 else 0,
                },
                "p99_ms": {
                    "dijkstra": dp99,
                    "astar": ap99,
                    "bd_dijkstra": bp99,
                    "astar_vs_dijkstra_improve_pct": (
                        round((dp99 - ap99) * 100 / dp99, 2) if dp99 else 0
                    ),
                    "bd_vs_dijkstra_improve_pct": (
                        round((dp99 - bp99) * 100 / dp99, 2) if dp99 else 0
                    ),
                },
                "throughput_rps": {
                    "dijkstra": dt,
                    "astar": at,
                    "bd_dijkstra": bt,
                    "astar_vs_dijkstra_improve_pct": round((at - dt) * 100 / dt, 2) if dt else 0,
                    "bd_vs_dijkstra_improve_pct": round((bt - dt) * 100 / dt, 2) if dt else 0,
                },
            }
            if include_ch and ch:
                c95 = ch["latency_ms"]["p95"]
                cp99 = ch["latency_ms"]["p99"]
                ct = ch["throughput_rps"]
                summary["p95_ms"]["ch"] = c95
                summary["p95_ms"]["ch_vs_dijkstra_improve_pct"] = (
                    round((d95 - c95) * 100 / d95, 2) if d95 else 0
                )
                summary["p99_ms"]["ch"] = cp99
                summary["p99_ms"]["ch_vs_dijkstra_improve_pct"] = (
                    round((dp99 - cp99) * 100 / dp99, 2) if dp99 else 0
                )
                summary["throughput_rps"]["ch"] = ct
                summary["throughput_rps"]["ch_vs_dijkstra_improve_pct"] = (
                    round((ct - dt) * 100 / dt, 2) if dt else 0
                )
            print("[bench] summary_compare")
            print(summary)
    else:
        await benchmark_once(
            concurrency, total, warmup, timeout_s, progress_every, algorithm, payload
        )


def main():
    parser = argparse.ArgumentParser(description="Benchmark safety_scoring /api/route")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--total", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument(
        "--algorithm",
        choices=["astar", "dijkstra", "bd_dijkstra", "compare"],
        default="compare",
    )
    parser.add_argument(
        "--include-ch",
        action="store_true",
        help="Include algorithm=ch in compare mode (requires graphhopper_proxy + GraphHopper).",
    )
    parser.add_argument("--start-lat", type=float, default=None)
    parser.add_argument("--start-lng", type=float, default=None)
    parser.add_argument("--end-lat", type=float, default=None)
    parser.add_argument("--end-lng", type=float, default=None)
    args = parser.parse_args()

    asyncio.run(
        benchmark(
            concurrency=args.concurrency,
            total=args.total,
            warmup=args.warmup,
            timeout_s=args.timeout,
            progress_every=args.progress_every,
            algorithm=args.algorithm,
            include_ch=args.include_ch,
            start_lat=args.start_lat,
            start_lng=args.start_lng,
            end_lat=args.end_lat,
            end_lng=args.end_lng,
        )
    )


if __name__ == "__main__":
    main()
