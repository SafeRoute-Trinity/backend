#!/usr/bin/env python3
"""
Run a reproducible routing-service scale experiment for 1/2/3 nodes.

What this script does:
1) Starts dedicated Redis containers (split rate-limit/cache backends).
2) Starts N routing_service nodes behind an nginx load balancer.
3) Runs backend/scripts/scale_test.py for route_calculate only.
4) Repeats for cold (cache disabled) / hot (cache enabled) modes.
5) Writes JSON/CSV/Markdown summaries and an RPS comparison chart.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence

DEFAULT_ROUTE_PAYLOAD: Dict[str, Any] = {
    "origin": {"lat": 53.3498, "lon": -6.2603},
    "destination": {"lat": 53.3438, "lon": -6.2546},
    "user_id": "scale-test-user",
    "preferences": {"optimize_for": "balanced", "transport_mode": "walking"},
}


def _parse_csv_ints(value: str) -> List[int]:
    values: List[int] = []
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        parsed = int(item)
        if parsed <= 0:
            raise ValueError("All numeric values must be > 0")
        values.append(parsed)
    if not values:
        raise ValueError("Expected at least one value")
    return values


def _parse_modes(value: str) -> List[str]:
    allowed = {"cold", "hot"}
    modes: List[str] = []
    for raw in value.split(","):
        item = raw.strip().lower()
        if not item:
            continue
        if item not in allowed:
            raise ValueError(f"Unsupported mode: {item} (allowed: cold,hot)")
        modes.append(item)
    if not modes:
        raise ValueError("Expected at least one mode")
    deduped: List[str] = []
    for mode in modes:
        if mode not in deduped:
            deduped.append(mode)
    return deduped


def _run(cmd: Sequence[str], *, cwd: Path | None = None, capture_output: bool = False) -> str:
    result = subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        check=True,
        text=True,
        capture_output=capture_output,
    )
    if capture_output:
        return result.stdout.strip()
    return ""


def _docker_rm_if_exists_safe(names: Sequence[str]) -> None:
    if not names:
        return
    subprocess.run(
        ["docker", "rm", "-f", *names],
        check=False,
        text=True,
        capture_output=True,
    )


def _ensure_docker_network(network_name: str) -> None:
    inspect = subprocess.run(
        ["docker", "network", "inspect", network_name],
        check=False,
        text=True,
        capture_output=True,
    )
    if inspect.returncode == 0:
        return
    _run(["docker", "network", "create", network_name])


def _start_redis(name: str, network_name: str, image: str) -> None:
    _docker_rm_if_exists_safe([name])
    _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            network_name,
            image,
            "redis-server",
            "--save",
            "",
            "--appendonly",
            "no",
        ]
    )


def _flush_redis(name: str) -> None:
    _run(["docker", "exec", name, "redis-cli", "FLUSHALL"])


def _routing_container_env(
    *,
    mode: str,
    rate_limit_enabled: bool,
    route_audit_enabled: bool,
    route_cache_ttl_seconds: int,
    route_cache_weights_version_local_ttl_seconds: float,
    rate_limit_redis_max_connections: int,
    route_cache_redis_max_connections: int,
    rate_limit_redis_name: str,
    route_cache_redis_name: str,
    postgis_host: str,
    postgis_port: int,
    postgis_user: str,
    postgis_password: str,
    postgis_database: str,
    database_url: str,
    route_engine: str,
) -> Dict[str, str]:
    env = {
        "POSTGIS_HOST": postgis_host,
        "POSTGIS_PORT": str(postgis_port),
        "POSTGIS_USER": postgis_user,
        "POSTGIS_PASSWORD": postgis_password,
        "POSTGIS_DATABASE": postgis_database,
        "DATABASE_URL": database_url,
        "PORT": "80",
        "ROUTE_ENGINE": route_engine,
        # Rate limiter backend (split Redis)
        "REDIS_HOST": rate_limit_redis_name,
        "REDIS_PORT": "6379",
        "REDIS_DB": "0",
        "RATE_LIMIT_ENABLED": "true" if rate_limit_enabled else "false",
        "RATE_LIMIT_DEFAULT_LIMIT": "1000000",
        "RATE_LIMIT_AUTH_LIMIT": "1000000",
        "RATE_LIMIT_REDIS_MAX_CONNECTIONS": str(max(1, rate_limit_redis_max_connections)),
        "ROUTING_ROUTE_AUDIT_ENABLED": "true" if route_audit_enabled else "false",
        # Route result cache backend (split Redis)
        "ROUTE_RESULT_CACHE_BACKEND": "redis",
        "ROUTE_RESULT_CACHE_REDIS_HOST": route_cache_redis_name,
        "ROUTE_RESULT_CACHE_REDIS_PORT": "6379",
        "ROUTE_RESULT_CACHE_REDIS_DB": "0",
        "ROUTE_RESULT_CACHE_TTL_SECONDS": str(route_cache_ttl_seconds),
        "ROUTE_RESULT_CACHE_REDIS_MAX_CONNECTIONS": str(max(1, route_cache_redis_max_connections)),
        "ROUTE_RESULT_CACHE_WEIGHTS_VERSION_LOCAL_CACHE_TTL_SECONDS": str(
            max(0.0, route_cache_weights_version_local_ttl_seconds)
        ),
    }
    env["ROUTE_RESULT_CACHE_ENABLED"] = "true" if mode == "hot" else "false"
    return env


def _start_routing_node(
    *,
    name: str,
    image: str,
    network_name: str,
    cpus: float,
    memory: str,
    env: Dict[str, str],
) -> None:
    cmd: List[str] = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--network",
        network_name,
        "--cpus",
        str(cpus),
        "--memory",
        memory,
    ]
    for key, value in env.items():
        cmd.extend(["-e", f"{key}={value}"])
    cmd.append(image)
    _run(cmd)


def _write_nginx_config(path: Path, node_count: int, upstream_keepalive: int) -> None:
    upstream_servers = "\n".join(
        [f"        server bench-routing-{i}:80;" for i in range(1, node_count + 1)]
    )
    keepalive_line = f"        keepalive {upstream_keepalive};" if upstream_keepalive > 0 else ""
    connection_header_value = '""' if upstream_keepalive > 0 else '"close"'
    content = textwrap.dedent(f"""
        events {{}}

        http {{
            upstream routing_backend {{
        {upstream_servers}
        {keepalive_line}
            }}

            server {{
                listen 80;

                location / {{
                    proxy_pass http://routing_backend;
                    proxy_http_version 1.1;
                    proxy_set_header Connection {connection_header_value};
                }}
            }}
        }}
        """).strip()
    path.write_text(content + "\n", encoding="utf-8")


def _start_lb(
    *, name: str, network_name: str, host_port: int, nginx_conf_path: Path, nginx_image: str
) -> None:
    _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            network_name,
            "-p",
            f"{host_port}:80",
            "-v",
            f"{nginx_conf_path}:/etc/nginx/nginx.conf:ro",
            nginx_image,
        ]
    )


def _wait_for_health(url: str, timeout_seconds: int) -> None:
    started = time.time()
    while time.time() - started < timeout_seconds:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError(f"Timed out waiting for healthy endpoint: {url}")


def _warm_route(route_url: str) -> None:
    data = json.dumps(DEFAULT_ROUTE_PAYLOAD).encode("utf-8")
    req = urllib.request.Request(
        route_url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Warmup call failed with status={resp.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Warmup call failed with status={e.code}, body={body}") from e


def _run_scale_test(
    *,
    python_bin: str,
    repo_root: Path,
    concurrency: str,
    requests_per_stage: int,
    runs_per_stage: int,
    warmup_requests: int,
    randomize_concurrency_order: bool,
    random_seed: int,
    http_max_connections: int,
    http_max_keepalive_connections: int,
    http_keepalive_expiry_seconds: float,
    timeout_seconds: float,
    output_json: Path,
    no_rotate_client_ip: bool,
) -> None:
    cmd = [
        python_bin,
        str(repo_root / "backend/scripts/scale_test.py"),
        "--include-scenarios",
        "route_calculate",
        "--concurrency",
        concurrency,
        "--requests-per-stage",
        str(requests_per_stage),
        "--runs-per-stage",
        str(max(1, runs_per_stage)),
        "--warmup-requests",
        str(max(0, warmup_requests)),
        "--random-seed",
        str(random_seed),
        "--timeout-seconds",
        str(timeout_seconds),
        "--output-json",
        str(output_json),
    ]
    if http_max_connections > 0:
        cmd.extend(["--http-max-connections", str(http_max_connections)])
    if http_max_keepalive_connections > 0:
        cmd.extend(["--http-max-keepalive-connections", str(http_max_keepalive_connections)])
    if http_keepalive_expiry_seconds >= 0:
        cmd.extend(["--http-keepalive-expiry-seconds", str(http_keepalive_expiry_seconds)])
    if randomize_concurrency_order:
        cmd.append("--randomize-concurrency-order")
    if no_rotate_client_ip:
        cmd.append("--no-rotate-client-ip")
    _run(cmd, cwd=repo_root)


def _load_results(path: Path) -> List[Dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError(f"Invalid benchmark result file: {path}")
    return results


def _write_csv(rows: List[Dict[str, Any]], path: Path) -> None:
    columns = [
        "mode",
        "nodes",
        "scenario",
        "concurrency",
        "requests",
        "success_rate",
        "error_rate",
        "rps",
        "avg_ms",
        "p50_ms",
        "p95_ms",
        "p99_ms",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in columns})


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _build_markdown_summary(
    *,
    rows: List[Dict[str, Any]],
    modes: Sequence[str],
    node_counts: Sequence[int],
    conc_levels: Sequence[int],
) -> str:
    lines: List[str] = []
    lines.append("# Routing Scale Experiment")
    lines.append("")
    lines.append("## RPS by concurrency")
    lines.append("")

    for mode in modes:
        lines.append(f"### Mode: `{mode}`")
        lines.append("")
        header = "| Concurrency | " + " | ".join([f"{n} node(s)" for n in node_counts]) + " |"
        sep = "|" + "---|" * (len(node_counts) + 1)
        lines.append(header)
        lines.append(sep)
        for conc in conc_levels:
            cells = [str(conc)]
            for nodes in node_counts:
                match = next(
                    (
                        row
                        for row in rows
                        if row["mode"] == mode
                        and row["nodes"] == nodes
                        and row["concurrency"] == conc
                    ),
                    None,
                )
                cells.append(f"{match['rps']:.2f}" if match else "-")
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    lines.append("## Average metrics")
    lines.append("")
    lines.append("| Mode | Nodes | Avg RPS | Avg p95 (ms) |")
    lines.append("|---|---:|---:|---:|")
    for mode in modes:
        for nodes in node_counts:
            subset = [row for row in rows if row["mode"] == mode and row["nodes"] == nodes]
            avg_rps = _mean([float(row["rps"]) for row in subset])
            avg_p95 = _mean([float(row["p95_ms"]) for row in subset])
            lines.append(f"| {mode} | {nodes} | {avg_rps:.2f} | {avg_p95:.1f} |")
    lines.append("")
    return "\n".join(lines)


def _try_render_plot(
    *,
    rows: List[Dict[str, Any]],
    modes: Sequence[str],
    node_counts: Sequence[int],
    output_png: Path,
) -> bool:
    try:
        import matplotlib.pyplot as plt  # type: ignore
    except Exception:
        return False

    mode_to_rows: Dict[str, List[Dict[str, Any]]] = {mode: [] for mode in modes}
    for row in rows:
        mode_to_rows[row["mode"]].append(row)

    fig, axes = plt.subplots(1, len(modes), figsize=(7 * len(modes), 5), sharey=True)
    if len(modes) == 1:
        axes = [axes]

    for idx, mode in enumerate(modes):
        ax = axes[idx]
        for nodes in node_counts:
            subset = [
                row
                for row in mode_to_rows[mode]
                if row["nodes"] == nodes and row["scenario"] == "route_calculate"
            ]
            subset.sort(key=lambda item: int(item["concurrency"]))
            xs = [int(item["concurrency"]) for item in subset]
            ys = [float(item["rps"]) for item in subset]
            ax.plot(xs, ys, marker="o", linewidth=2, label=f"{nodes} node(s)")
        ax.set_title(f"Mode: {mode}")
        ax.set_xlabel("Concurrency")
        ax.set_ylabel("RPS")
        ax.grid(True, alpha=0.3)
        ax.legend()

    fig.suptitle("route_calculate throughput comparison")
    fig.tight_layout()
    fig.savefig(output_png, dpi=160)
    return True


def _cleanup_runtime(
    *,
    max_nodes: int,
    lb_name: str,
    rate_limit_redis_name: str,
    route_cache_redis_name: str,
    keep_redis: bool,
) -> None:
    routing_names = [f"bench-routing-{i}" for i in range(1, max_nodes + 1)]
    _docker_rm_if_exists_safe([lb_name, *routing_names])
    if not keep_redis:
        _docker_rm_if_exists_safe([rate_limit_redis_name, route_cache_redis_name])


def main() -> int:
    parser = argparse.ArgumentParser(description="Run routing-service scale experiment")
    parser.add_argument("--nodes", default="1,2,3", help="Node counts, e.g. 1,2,3")
    parser.add_argument("--modes", default="cold,hot", help="Cache modes: cold,hot")
    parser.add_argument("--concurrency", default="10,20,40,80", help="Concurrency levels")
    parser.add_argument("--requests-per-stage", type=int, default=800)
    parser.add_argument(
        "--runs-per-stage",
        type=int,
        default=3,
        help="Repeat each stage N times and use median in scale_test (default: 3)",
    )
    parser.add_argument(
        "--warmup-requests",
        type=int,
        default=120,
        help="Warmup requests per stage before measurement (default: 120)",
    )
    parser.add_argument(
        "--randomize-concurrency-order",
        action="store_true",
        help="Shuffle concurrency order in scale_test (default: false).",
    )
    parser.add_argument(
        "--random-seed",
        type=int,
        default=42,
        help="Seed for randomized concurrency order (default: 42).",
    )
    parser.add_argument(
        "--http-max-connections",
        type=int,
        default=0,
        help="httpx max_connections passed to scale_test; 0 uses defaults",
    )
    parser.add_argument(
        "--http-max-keepalive-connections",
        type=int,
        default=0,
        help="httpx max_keepalive_connections passed to scale_test; 0 uses defaults",
    )
    parser.add_argument(
        "--http-keepalive-expiry-seconds",
        type=float,
        default=-1.0,
        help="httpx keepalive expiry passed to scale_test; negative uses defaults",
    )
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--python-bin", default=sys.executable)
    parser.add_argument("--image", default="saferoute/routing-service:arm64-local")
    parser.add_argument("--nginx-image", default="nginx:1.27-alpine")
    parser.add_argument("--redis-image", default="redis:7-alpine")
    parser.add_argument("--network", default="saferoute-bench-net")
    parser.add_argument("--lb-port", type=int, default=20002)
    parser.add_argument(
        "--lb-upstream-keepalive",
        type=int,
        default=0,
        help="Nginx upstream keepalive connections (default: 0 = disabled)",
    )
    parser.add_argument("--cpus", type=float, default=1.0)
    parser.add_argument("--memory", default="1536m")
    parser.add_argument("--route-cache-ttl-seconds", type=int, default=120)
    parser.add_argument("--route-cache-weights-version-local-ttl-seconds", type=float, default=5.0)
    parser.add_argument("--rate-limit-redis-max-connections", type=int, default=256)
    parser.add_argument("--route-cache-redis-max-connections", type=int, default=256)
    parser.add_argument(
        "--rate-limit-enabled",
        action="store_true",
        help="Enable rate limiter during experiment (default: disabled).",
    )
    parser.add_argument(
        "--route-audit-enabled",
        action="store_true",
        help="Enable route audit writes in /v1/routes/calculate (default: disabled).",
    )
    parser.add_argument(
        "--rotate-client-ip",
        action="store_true",
        help="Rotate X-Forwarded-For values in scale_test (default: off).",
    )
    parser.add_argument(
        "--keep-stack-running",
        action="store_true",
        help="Keep benchmark containers running after experiment.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Output directory (default: /tmp/route_scale_experiment_<timestamp>)",
    )
    parser.add_argument("--postgis-host", default="host.docker.internal")
    parser.add_argument("--postgis-port", type=int, default=5432)
    parser.add_argument("--postgis-user", default="saferoute")
    parser.add_argument("--postgis-password", default="saferoute")
    parser.add_argument("--postgis-database", default="saferoute_geo")
    parser.add_argument(
        "--database-url",
        default="postgresql://saferoute:saferoute@host.docker.internal:5432/saferoute_geo?sslmode=disable",
    )
    parser.add_argument("--route-engine", default="pgrouting")
    args = parser.parse_args()

    node_counts = _parse_csv_ints(args.nodes)
    conc_levels = _parse_csv_ints(args.concurrency)
    modes = _parse_modes(args.modes)

    repo_root = Path(__file__).resolve().parents[2]
    now_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = (
        Path(args.output_dir).expanduser()
        if args.output_dir
        else Path("/tmp") / f"route_scale_experiment_{now_tag}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    lb_name = "bench-routing-lb"
    rate_limit_redis_name = "bench-rate-limit-redis"
    route_cache_redis_name = "bench-route-cache-redis"

    print(f"[experiment] repo_root={repo_root}")
    print(f"[experiment] output_dir={output_dir}")
    print(f"[experiment] modes={modes} nodes={node_counts} concurrency={conc_levels}")

    _ensure_docker_network(args.network)
    _start_redis(rate_limit_redis_name, args.network, args.redis_image)
    _start_redis(route_cache_redis_name, args.network, args.redis_image)

    all_rows: List[Dict[str, Any]] = []
    run_outputs: List[Dict[str, Any]] = []

    try:
        for mode in modes:
            for nodes in node_counts:
                print(f"\n[experiment] mode={mode} nodes={nodes}")

                _cleanup_runtime(
                    max_nodes=max(node_counts),
                    lb_name=lb_name,
                    rate_limit_redis_name=rate_limit_redis_name,
                    route_cache_redis_name=route_cache_redis_name,
                    keep_redis=True,
                )
                _flush_redis(rate_limit_redis_name)
                _flush_redis(route_cache_redis_name)

                env = _routing_container_env(
                    mode=mode,
                    rate_limit_enabled=args.rate_limit_enabled,
                    route_audit_enabled=args.route_audit_enabled,
                    route_cache_ttl_seconds=args.route_cache_ttl_seconds,
                    route_cache_weights_version_local_ttl_seconds=(
                        args.route_cache_weights_version_local_ttl_seconds
                    ),
                    rate_limit_redis_max_connections=args.rate_limit_redis_max_connections,
                    route_cache_redis_max_connections=args.route_cache_redis_max_connections,
                    rate_limit_redis_name=rate_limit_redis_name,
                    route_cache_redis_name=route_cache_redis_name,
                    postgis_host=args.postgis_host,
                    postgis_port=args.postgis_port,
                    postgis_user=args.postgis_user,
                    postgis_password=args.postgis_password,
                    postgis_database=args.postgis_database,
                    database_url=args.database_url,
                    route_engine=args.route_engine,
                )

                for idx in range(1, nodes + 1):
                    _start_routing_node(
                        name=f"bench-routing-{idx}",
                        image=args.image,
                        network_name=args.network,
                        cpus=args.cpus,
                        memory=args.memory,
                        env=env,
                    )

                nginx_conf = output_dir / f"nginx_{mode}_{nodes}.conf"
                _write_nginx_config(
                    nginx_conf,
                    node_count=nodes,
                    upstream_keepalive=max(0, int(args.lb_upstream_keepalive)),
                )
                _start_lb(
                    name=lb_name,
                    network_name=args.network,
                    host_port=args.lb_port,
                    nginx_conf_path=nginx_conf,
                    nginx_image=args.nginx_image,
                )

                health_url = f"http://127.0.0.1:{args.lb_port}/health"
                _wait_for_health(health_url, timeout_seconds=90)
                _warm_route(f"http://127.0.0.1:{args.lb_port}/v1/routes/calculate")

                output_json = output_dir / f"{mode}_{nodes}node.json"
                _run_scale_test(
                    python_bin=args.python_bin,
                    repo_root=repo_root,
                    concurrency=args.concurrency,
                    requests_per_stage=args.requests_per_stage,
                    runs_per_stage=args.runs_per_stage,
                    warmup_requests=args.warmup_requests,
                    randomize_concurrency_order=args.randomize_concurrency_order,
                    random_seed=args.random_seed,
                    http_max_connections=args.http_max_connections,
                    http_max_keepalive_connections=args.http_max_keepalive_connections,
                    http_keepalive_expiry_seconds=args.http_keepalive_expiry_seconds,
                    timeout_seconds=args.timeout_seconds,
                    output_json=output_json,
                    no_rotate_client_ip=not args.rotate_client_ip,
                )

                run_outputs.append({"mode": mode, "nodes": nodes, "output_json": str(output_json)})
                for row in _load_results(output_json):
                    merged = {
                        "mode": mode,
                        "nodes": nodes,
                        **row,
                    }
                    all_rows.append(merged)

        combined_json_path = output_dir / "combined_results.json"
        combined_payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "modes": modes,
            "node_counts": node_counts,
            "concurrency_levels": conc_levels,
            "requests_per_stage": args.requests_per_stage,
            "runs_per_stage": args.runs_per_stage,
            "warmup_requests": args.warmup_requests,
            "timeout_seconds": args.timeout_seconds,
            "randomize_concurrency_order": args.randomize_concurrency_order,
            "random_seed": args.random_seed,
            "http_limits": {
                "mode": (
                    "custom"
                    if (
                        args.http_max_connections > 0
                        or args.http_max_keepalive_connections > 0
                        or args.http_keepalive_expiry_seconds >= 0
                    )
                    else "httpx_default"
                ),
                "max_connections_arg": args.http_max_connections,
                "max_keepalive_connections_arg": args.http_max_keepalive_connections,
                "keepalive_expiry_seconds_arg": args.http_keepalive_expiry_seconds,
            },
            "rate_limit_enabled": args.rate_limit_enabled,
            "route_audit_enabled": args.route_audit_enabled,
            "rate_limit_redis_max_connections": args.rate_limit_redis_max_connections,
            "rotate_client_ip": args.rotate_client_ip,
            "route_cache_redis_max_connections": args.route_cache_redis_max_connections,
            "route_cache_weights_version_local_ttl_seconds": (
                args.route_cache_weights_version_local_ttl_seconds
            ),
            "lb_upstream_keepalive": args.lb_upstream_keepalive,
            "runs": run_outputs,
            "results": all_rows,
        }
        combined_json_path.write_text(json.dumps(combined_payload, indent=2), encoding="utf-8")

        csv_path = output_dir / "combined_results.csv"
        _write_csv(all_rows, csv_path)

        summary_md_path = output_dir / "summary.md"
        summary_md = _build_markdown_summary(
            rows=all_rows,
            modes=modes,
            node_counts=node_counts,
            conc_levels=conc_levels,
        )
        summary_md_path.write_text(summary_md, encoding="utf-8")

        chart_path = output_dir / "rps_by_concurrency.png"
        chart_ok = _try_render_plot(
            rows=all_rows,
            modes=modes,
            node_counts=node_counts,
            output_png=chart_path,
        )

        print("\n[experiment] done")
        print(f"[experiment] combined_json={combined_json_path}")
        print(f"[experiment] combined_csv={csv_path}")
        print(f"[experiment] summary_md={summary_md_path}")
        if chart_ok:
            print(f"[experiment] chart_png={chart_path}")
        else:
            print("[experiment] chart_png=SKIPPED (matplotlib unavailable)")
    finally:
        if not args.keep_stack_running:
            _cleanup_runtime(
                max_nodes=max(node_counts),
                lb_name=lb_name,
                rate_limit_redis_name=rate_limit_redis_name,
                route_cache_redis_name=route_cache_redis_name,
                keep_redis=False,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
