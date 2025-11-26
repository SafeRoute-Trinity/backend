# OpenRouteService Integration Setup

## Overview

The routing service now integrates with OpenRouteService (ORS) to provide route calculation and isochrone services compatible with Mapbox frontend.

## Environment Variables

### Required

- `ORS_API_KEY`: Your OpenRouteService API key
  - Get your API key from: https://openrouteservice.org/dev/#/account
  - Free tier available with 2,000 requests/day

### Optional

- `LOG_LEVEL`: Logging level (default: `info`)
- `DATABASE_HOST`, `DATABASE_PORT`, etc.: Database configuration (if needed)
- `REDIS_HOST`, `REDIS_PORT`, etc.: Redis configuration (if needed)

## Local Development

1. Create a `.env` file in the service directory:
```bash
ORS_API_KEY=your_api_key_here
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Run the service:
```bash
uvicorn services.routing_service.main:app --host 0.0.0.0 --port 20002 --reload
```

## API Endpoints

### GET /route

Get route from OpenRouteService and convert to Mapbox-compatible format.

**Query Parameters:**
- `start` (required): Start coordinates as "lat,lon"
- `end` (required): End coordinates as "lat,lon"
- `profile` (optional): Routing profile (default: "driving-car")
  - Options: `driving-car`, `foot-walking`, `cycling-regular`, `cycling-road`, etc.

**Example:**
```bash
curl "http://localhost:20002/route?start=53.342,-6.256&end=53.345,-6.262&profile=driving-car"
```

**Response:**
GeoJSON FeatureCollection with LineString features compatible with Mapbox.

### GET /isochrone

Get isochrones from OpenRouteService and convert to Mapbox-compatible format.

**Query Parameters:**
- `location` (required): Location coordinates as "lat,lon"
- `profile` (optional): Routing profile (default: "driving-car")
- `range` (optional): Comma-separated list of ranges (default: "600,1200,1800")
- `range_type` (optional): "time" or "distance" (default: "time")

**Example:**
```bash
curl "http://localhost:20002/isochrone?location=53.342,-6.256&range=600,1200,1800&range_type=time"
```

**Response:**
GeoJSON FeatureCollection with Polygon features compatible with Mapbox.

## Kubernetes Deployment

### 1. Create OpenRouteService Secret

```bash
kubectl create secret generic openrouteservice-secret \
  --from-literal=api-key=YOUR_ORS_API_KEY \
  -n saferoute
```

### 2. Update ConfigMap

The ConfigMap has been updated with `ors.api.enabled: "true"`.

### 3. Update Deployment

The deployment.yml has been updated to include:
```yaml
- name: ORS_API_KEY
  valueFrom:
    secretKeyRef:
      name: openrouteservice-secret
      key: api-key
```

### 4. Deploy

```bash
kubectl apply -f manifests/k8s/saferoute/routing-service/
```

## Testing

Run unit tests:
```bash
pytest services/routing_service/tests/test_openrouteservice.py -v
```

## Response Format

### Route Response (Mapbox-compatible)

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": {
        "type": "LineString",
        "coordinates": [[lon, lat], ...]
      },
      "properties": {
        "route_index": 0,
        "is_primary": true,
        "distance_m": 1000,
        "duration_s": 120,
        "distance": 1.0,
        "duration": 2.0
      }
    }
  ]
}
```

### Isochrone Response (Mapbox-compatible)

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": {
        "type": "Polygon",
        "coordinates": [[[lon, lat], ...]]
      },
      "properties": {
        "value": 600,
        "range_type": "time"
      }
    }
  ]
}
```

## Error Handling

The service handles various error cases:
- Missing API key: Returns 503 Service Unavailable
- Invalid coordinates: Returns 400 Bad Request
- OpenRouteService API failure: Returns 502 Bad Gateway
- Conversion errors: Returns 500 Internal Server Error

All errors are logged for debugging.

## Logging

The service logs:
- API requests to OpenRouteService
- Successful responses
- Errors and exceptions
- Conversion operations

Log level can be configured via `LOG_LEVEL` environment variable.

