# Scale Testing (Backend APIs)

Run from repo root:

```bash
python backend/scripts/scale_test.py
```

Custom concurrency steps:

```bash
python backend/scripts/scale_test.py --concurrency 1,5,10,20,40 --requests-per-stage 80
```

More stable stages (warmup + repeated runs + median):

```bash
python backend/scripts/scale_test.py \
  --include-scenarios route_calculate \
  --concurrency 10,20,40,80 \
  --requests-per-stage 800 \
  --runs-per-stage 5 \
  --warmup-requests 120 \
  --randomize-concurrency-order
```

By default, `scale_test.py` uses httpx built-in connection limits.
Only set `--http-max-connections`, `--http-max-keepalive-connections`,
or `--http-keepalive-expiry-seconds` when you explicitly want to override them.

Export JSON report:

```bash
python backend/scripts/scale_test.py --output-json /tmp/saferoute_scale_report.json
```

Run only selected scenarios:

```bash
python backend/scripts/scale_test.py --include-scenarios route_calculate,transit_plan
```

Exclude scenarios (useful when a dependency is unavailable locally):

```bash
python backend/scripts/scale_test.py --exclude-scenarios feedback_submit
```

What it measures per endpoint/stage:

- success rate / error rate
- requests per second (RPS)
- latency: avg, p50, p95, p99

Default scenarios include:

- health endpoints for `user_management`, `notification`, `routing_service`, `feedback`, `sos`
- `routing_service` route calculation
- `routing_service` transit planning
- `feedback` submission

## Route scale experiment (cold/hot cache, 1/2/3 nodes)

For distributed routing benchmarking (split Redis + node scaling), run:

```bash
python backend/scripts/route_scale_experiment.py \
  --python-bin /Users/yuanchenfan/miniforge3/envs/saferoute/bin/python \
  --image saferoute/routing-service:arm64-local \
  --nodes 1,2,3 \
  --modes cold,hot \
  --concurrency 10,20,40,80 \
  --requests-per-stage 800 \
  --runs-per-stage 3 \
  --warmup-requests 120
```

This script:

- starts dedicated Redis containers for `rate_limit` and `route_result_cache`
- runs two cache modes:
  - `cold`: `ROUTE_RESULT_CACHE_ENABLED=false`
  - `hot`: `ROUTE_RESULT_CACHE_ENABLED=true`
- can disable rate limiting for cleaner throughput tests (`RATE_LIMIT_ENABLED=false`, default behavior of this script)
- disables route audit writes by default (`ROUTING_ROUTE_AUDIT_ENABLED=false`) to avoid DB write noise in throughput tests; pass `--route-audit-enabled` to include audit cost
- writes outputs to `/tmp/route_scale_experiment_<timestamp>/`:
  - `combined_results.json`
  - `combined_results.csv`
  - `summary.md`
  - `rps_by_concurrency.png` (when matplotlib is available)
