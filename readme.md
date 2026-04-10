# SafeRoute Backend

Python 3.11 FastAPI microservices backend. Eight services share a common `libs/` layer for distributed consistency (CAS), structured logging, Auth0 JWT verification, Redis rate limiting, RabbitMQ messaging, and PostGIS spatial queries.

---

## Repository Structure

```
backend/
├── services/
│   ├── user_management/     # Registration, login, profile, trusted contacts  (port 20000)
│   ├── notification/        # SMS/email delivery, outbox consumer             (port 20001)
│   ├── routing_service/     # Route calculation, safety-weighted graph        (port 20002)
│   ├── safety_scoring/      # PostGIS safety factor queries                   (port 20003)
│   ├── feedback/            # User feedback, spam/reCAPTCHA filtering         (port 20004)
│   ├── data_cleaner/        # Data retention jobs                             (port 20005) *
│   ├── sos/                 # Emergency SOS, Garda station lookup, Twilio     (port 20006)
│   ├── graphhopper_proxy/   # HTTP proxy to GraphHopper CH engine             (port 20007)
│   └── coordinator/         # 2PC transaction coordinator                     (port 20008)
│
│   * data_cleaner is defined in constants.py but not included in docker-compose or K8s manifests
├── libs/
│   ├── auth/auth0_verify.py      # Auth0 RS256 JWT verification (JWKS)
│   ├── fastapi_service.py        # Service factory (CORS, logging, CAS, metrics, health)
│   ├── structured_logging.py     # JSON logging for Azure Container Insights
│   ├── trace_context.py          # X-Trace-ID propagation middleware
│   ├── cas_enforcer.py           # PostgreSQL-backed compare-and-swap enforcer
│   ├── cas_logger.py             # CAS state-transition logger
│   ├── cas_sync.py               # CAS state synchronisation helpers
│   ├── rate_limiter.py           # Redis fixed-window rate limiter
│   ├── outbox.py                 # Transactional outbox pattern
│   ├── rabbitmq.py               # aio-pika RabbitMQ client
│   ├── db.py                     # SQLAlchemy async session factory
│   ├── audit_logger.py           # Immutable audit trail writer
│   ├── two_pc.py                 # Two-phase commit protocol
│   ├── twilio_client.py          # Twilio SMS/voice client
│   ├── http_client.py            # HTTPX client with retry
│   └── service_urls.py           # Inter-service URL registry
├── models/
│   ├── base.py, user_models.py, emergency.py
│   ├── feedback.py, outbox.py, audit.py, cas_state.py
├── scripts/
│   ├── compute_safety_factors.py
│   ├── populate_ways_safety_factors.py
│   ├── export_ways_to_osm.py
│   ├── graphhopper_build_cache.sh
│   └── migrations/001_ways_safety_scores.sql
├── docker-compose.yml
├── requirements.txt
├── pyproject.toml              # Black, Ruff, isort, mypy config
└── TESTING.md
```

---

## Services

| Service | Port | Docker Image | Responsibility |
|---|---|---|---|
| user-management | 20000 | `saferoute/user_management:latest` | Auth, profile, trusted contacts |
| notification | 20001 | `saferoute/notification:latest` | Outbox consumer, SMS/voice delivery via Twilio |
| routing-service | 20002 | `saferoute/routing_service:latest` | Route calculation, safety weighting, route cache |
| safety-scoring | 20003 | `saferoute/safety_scoring:latest` | PostGIS safety factor queries |
| feedback | 20004 | `saferoute/feedback:latest` | User feedback, reCAPTCHA spam filtering, SMTP email |
| data_cleaner | 20005 | - | Data retention jobs (defined, not deployed) |
| sos | 20006 | `saferoute/sos:latest` | Emergency SOS, Garda station lookup, Twilio calls |
| graphhopper-proxy | 20007 | `saferoute/graphhopper-proxy:latest` | Proxy to GraphHopper CH engine (port 8989) |
| coordinator | 20008 | `saferoute/coordinator:latest` | 2PC distributed transaction coordinator |
| postgis | 5433 (local) | `postgis/postgis:15-3.3` | Spatial database (docker-compose) |

Every service exposes `/health`, `/ready`, and `/metrics` automatically via the service factory.

---

## Getting Started

### Prerequisites

- Docker Desktop >= 24
- Python 3.11 (for running services without Docker)

### Run Everything (Docker Compose)

```bash
docker compose up --build

# Single service
docker compose up routing-service postgis --build

# Logs
docker compose logs -f notification-service
```

### Run a Service Locally (no Docker)

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Set required env vars (see Environment Variables section)
uvicorn services.user_management.main:app --host 0.0.0.0 --port 20000 --reload
```

> **Important:** Always run from the `backend/` root. The entire `backend/` directory is the Docker build context for every service, so `libs/` and `models/` are importable from any service.

---

## Environment Variables

### All services

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL async URL: `postgresql+asyncpg://user:pass@host:5432/db` |
| `AUTH0_DOMAIN` | Auth0 tenant domain (e.g. `saferouteapp.eu.auth0.com`) |
| `AUTH0_AUDIENCE` | Auth0 API audience identifier |
| `REDIS_HOST` | Redis hostname (e.g. `redis.saferoute.svc.cluster.local` in cluster). Used for rate limiting, session/auth token caching, and route result caching. |
| `PORT` | Port the container listens on (default `80`) |

### notification service

| Variable | Default | Description |
|---|---|---|
| `OUTBOX_WORKER_ENABLED` | `false` | Enable the outbox polling worker |
| `OUTBOX_POLL_INTERVAL_SECONDS` | `2` | Poll interval in seconds |

### sos service (Twilio)

| Variable | Description |
|---|---|
| `TWILIO_ACCOUNT_SID` | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | Twilio auth token |
| `TWILIO_FROM_NUMBER` | Sending phone number (E.164) |

### graphhopper-proxy

| Variable | Default |
|---|---|
| `GRAPHHOPPER_BASE_URL` | `http://host.docker.internal:8989` |
| `GRAPHHOPPER_PROFILE` | `foot` |

### safety-scoring

| Variable | Description |
|---|---|
| `POSTGIS_HOST`, `POSTGIS_PORT`, `POSTGIS_USER`, `POSTGIS_PASSWORD`, `POSTGIS_DATABASE` | PostGIS connection |
| `GRAPHHOPPER_PROXY_SERVICE_URL` | URL of graphhopper-proxy service |

---

## Shared Library (`libs/`)

### Service Factory - `libs/fastapi_service.py`

Every service is created through the factory, which wires in:
- CORS middleware
- Structured JSON logging (Azure Monitor compatible)
- Trace-ID middleware (`X-Trace-ID` header propagation)
- CAS conflict handling (returns HTTP 409 on state conflict)
- Prometheus metrics middleware + `/metrics` endpoint
- `/health` and `/ready` endpoints
- Redis rate limiting middleware
- Startup/shutdown lifecycle hooks

### Structured Logging - `libs/structured_logging.py` + `libs/trace_context.py`

Emits JSON to stdout for Azure Container Insights. Every log line includes:

```json
{
  "timestamp": "2026-04-07T12:00:00Z",
  "service": "routing-service",
  "instance_id": "pod-name",
  "level": "INFO",
  "trace_id": "abc123",
  "message": "...",
  "module": "main",
  "function": "calculate_route",
  "line": 42
}
```

CAS operations additionally include: `cas_operation`, `cas_sequence`, `cas_expected_state`, `cas_new_state`, `cas_row_version`.

### CAS - `libs/cas_enforcer.py`

Database-backed compare-and-swap for distributed consistency. State is stored in the `saferoute.cas_state` PostgreSQL table with a 24-hour TTL. On successful transitions, a message is published to the Redis `cas:state_changes` pub/sub channel. Prevents split-brain when multiple replicas handle concurrent writes.

### Auth0 JWT - `libs/auth/auth0_verify.py`

Uses `PyJWKClient` to fetch the JWKS from `https://<AUTH0_DOMAIN>/.well-known/jwks.json`. Validates signature, expiration, audience, and issuer. Constants (`AUTH0_DOMAIN`, `API_AUDIENCE`, `ISSUER`) loaded from `common/constants`.

### Rate Limiting - `libs/rate_limiter.py`

Redis fixed-window counter per `rate_limit:{service}:{client_ip}:{window_id}`. Default: 100 req/60s. Auth endpoints: 10 req/60s. Returns HTTP 429 with `Retry-After` header. Degrades gracefully if Redis is unreachable.

### Transactional Outbox - `libs/outbox.py`

Events are written to the `outbox` table in the same DB transaction as the business operation, guaranteeing at-least-once delivery to RabbitMQ. The notification service polls (`OUTBOX_WORKER_ENABLED=true`) and publishes pending events.

---

## Databases

Two separate databases are used:

### PostgreSQL - `saferoute` (port 5432)

Application database. Connection string: `postgresql+asyncpg://saferoute:<pass>@host:5432/saferoute`

Access in cluster: `kubectl port-forward -n data svc/postgresql 5432:5432`

| Table | Description |
|---|---|
| `users` | User accounts and profiles |
| `trusted_contacts` | Emergency contacts per user |
| `user_preferences` | App settings |
| `user_safety_weights` | Per-user safety factor weights (overrides defaults) |
| `route_cache` | Cached route results (keyed by origin/destination/weights_hash) |
| `outbox` | Transactional outbox for async event delivery |
| `cas_state` | CAS distributed consistency state (24h TTL) |
| `audit_log` | Immutable audit trail |
| `feedback` | User-submitted route/safety feedback |
| `emergency_records` | SOS events |

### PostGIS - `saferoute_geo` (port 5433 local / 5432 internal)

Spatial database. Image: `postgis/postgis:15-3.3`. Used exclusively by the safety-scoring service for geospatial queries.

Access in cluster: `kubectl port-forward -n data svc/postgis 5433:5432`

| Table | Description |
|---|---|
| `ways` | Street segments with pre-computed safety scores and geometries |
| `safety_ways_factor_sync_state` | Batch ETL progress tracking |

---

## Safety Scoring

Safety scores are pre-computed by ETL scripts and stored in the `ways` table. The four dimensions and their weights:

| Factor | Weight |
|---|---|
| Street lighting | 35% |
| CCTV coverage | 30% |
| Crime history | 25% |
| Path quality | 10% |

Time-of-day modifiers are applied at query time by the safety-scoring service.

```bash
# Run ETL pipeline
python scripts/compute_safety_factors.py
python scripts/populate_ways_safety_factors.py
psql $DATABASE_URL -f scripts/migrations/001_ways_safety_scores.sql
```

---

## Routing

**Walking routes:**
```
GraphHopper (Contraction Hierarchies, foot profile, port 8989)
  -> OpenRouteService (api.openrouteservice.org/v2/directions/foot-walking, fallback)
  -> pgRouting Dijkstra on PostGIS (final fallback)
```

**Transit routes** (`POST /v1/transit/plan`):
```
1. Check route_cache (keyed by origin/destination/weights_hash)
2. Cache miss -> POST /directions/v2/computeRoutes (Google Routes API)
3. For each walk leg in the itinerary:
   safety-scoring computes weighted geometry
4. Write result back to route_cache
```

Results are cached in `saferoute.route_cache`.

### GraphHopper Setup

GraphHopper runs as a separate process outside Docker, listening on port 8989.

```bash
# Export street network from PostGIS
python scripts/export_ways_to_osm.py

# Build CH cache (requires Java 11+)
bash scripts/graphhopper_build_cache.sh
```

---

## Linting and Code Style

| Tool | Version | Config |
|---|---|---|
| Black | 26.3.1 | `line-length = 100`, double quotes |
| Ruff | 0.15.6 | Rules: E, F, B, I (PEP8, Flake8, Bugbear, isort) |
| isort | 8.0.1 | `profile = "black"` |
| mypy | 1.10.0 | Type checking |

```bash
black --check .
ruff check .
```

---

## CI/CD

GitHub Actions (`backend-ci-cd.yml`) runs on every push and PR to `main`:

### Lint (always runs)
Black and Ruff check. Fails the pipeline on any formatting or lint error.

### Test (`if: false` - currently disabled)
Pytest with coverage. Target: 80% coverage. Disabled pending stable test environment.

### Build and Push (push to `main` only)
Detects which services changed (compares `HEAD~1..HEAD`). Rebuilds only changed services. If `requirements.txt`, `libs/`, or `.github/workflows/` changed - rebuilds all services.

Images are pushed to Docker Hub as `saferoute/<service>:latest` for `linux/amd64`.

```bash
# Build manually (run from backend/ root)
docker buildx build --platform linux/amd64 \
  -f services/user_management/dockerfile \
  -t saferoute/user_management:latest --push .
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `401 Unauthorized` | Check `AUTH0_DOMAIN` and `AUTH0_AUDIENCE` match your tenant |
| PostGIS connection refused | `docker compose up postgis` first |
| RabbitMQ error | Check `RABBITMQ_URL`; notification service needs `OUTBOX_WORKER_ENABLED=true` |
| GraphHopper 502 | Ensure GraphHopper process is running on port 8989 and cache is built |
| Rate limit 429 | Redis may be down; check `REDIS_HOST` |
| `Import error: libs` | Build context must be `backend/` root, not a service subdirectory |
| Black/Ruff CI failure | Run `black .` and `ruff check . --fix` locally before pushing |
