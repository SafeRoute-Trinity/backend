# Redis Cache Implementation Guide

## Overview

The user management service has integrated Redis caching functionality to cache user data (username, password hash, TTL, etc.), improving service performance and supporting operation in Kubernetes environments.

## Features

### 1. Cached Data Types

- **User Data** (`user:{user_id}`): Stores complete user information, including:
  - user_id
  - email
  - password_hash
  - phone
  - name
  - device_id
  - created_at
  - last_login
  - preferences

- **Email Index** (`user:email:{email}`): Used to quickly lookup user ID by email

- **Auth Token** (`auth:token:{token}`): Stores authentication token information, including:
  - user_id
  - email
  - expires_in
  - created_at

### 2. Cache Strategy

- **Default TTL**: 3600 seconds (1 hour)
- **Auth Token TTL**: 3600 seconds (1 hour)
- **Configurable**: Configure via environment variables `REDIS_CACHE_TTL` and `REDIS_AUTH_TOKEN_TTL`

### 3. Cache Operations

- **User Registration**: Automatically cache newly registered user data
- **User Login**: 
  - First lookup user from cache
  - Verify password hash
  - Update last login time
  - Generate and cache new authentication token
- **Get User Info**: Prioritize reading from cache, fallback to in-memory storage on cache miss
- **Update User Preferences**: Update both cache and in-memory storage

## Environment Configuration

### Kubernetes Environment Variables

Configured in `deployment.yml`:

```yaml
- name: REDIS_HOST
  value: redis.data.svc.cluster.local
- name: REDIS_PORT
  value: "6379"
- name: REDIS_PASSWORD
  valueFrom:
    secretKeyRef:
      name: redis-secret
      key: password
- name: REDIS_CACHE_TTL
  valueFrom:
    configMapKeyRef:
      name: user-management-config
      key: redis.cache.ttl
- name: REDIS_AUTH_TOKEN_TTL
  valueFrom:
    configMapKeyRef:
      name: user-management-config
      key: redis.auth.token.ttl
```

### Local Development Environment

Set the following environment variables:

```bash
export REDIS_HOST=localhost
export REDIS_PORT=6379
export REDIS_PASSWORD=your_password  # If Redis has password set
export REDIS_CACHE_TTL=3600
export REDIS_AUTH_TOKEN_TTL=3600
```

## Deployment Steps

### 1. Create Redis Secret

Create Redis secret in K8s:

```bash
kubectl create secret generic redis-secret \
  --from-literal=password=YOUR_SECURE_PASSWORD \
  -n data
```

Or use the provided example file:

```bash
cp manifests/k8s/secrets/redis-secret.yml.example manifests/k8s/secrets/redis-secret.yml
# Edit the file and set the password
kubectl apply -f manifests/k8s/secrets/redis-secret.yml
```

### 2. Ensure Redis Service is Running

Check Redis service status:

```bash
kubectl get pods -n data -l app=redis
kubectl get svc -n data redis
```

### 3. Deploy User Management Service

```bash
# Build and push Docker image
docker build -t saferoute/user-management:latest -f backend/services/user_management/dockerfile backend/

# Deploy to K8s
kubectl apply -f manifests/k8s/saferoute/user-management/
```

### 4. Verify Deployment

Check service health status:

```bash
# Check Pod status
kubectl get pods -n saferoute -l app=user-management

# Check service logs
kubectl logs -n saferoute -l app=user-management

# Test health check endpoint
curl http://your-ingress-url/health/user-management
```

Response should include Redis connection status:

```json
{
  "status": "ok",
  "service": "user_management",
  "redis": "connected"
}
```

## Troubleshooting

### Redis Connection Failed

1. **Check if Redis service is running**:
   ```bash
   kubectl get pods -n data -l app=redis
   ```

2. **Check network connectivity**:
   ```bash
   kubectl exec -it <user-management-pod> -n saferoute -- \
     nc -zv redis.data.svc.cluster.local 6379
   ```

3. **Check if password is correct**:
   ```bash
   kubectl get secret redis-secret -n data -o yaml
   ```

4. **View service logs**:
   ```bash
   kubectl logs -n saferoute -l app=user-management | grep -i redis
   ```

### Cache Not Working

- Check if environment variables are set correctly
- View service logs to confirm Redis connection status
- Verify if data exists in Redis: `kubectl exec -it <redis-pod> -n data -- redis-cli -a $REDIS_PASSWORD KEYS "user:*"`

## Performance Optimization Recommendations

1. **Adjust TTL**: Adjust cache expiration time based on business requirements
2. **Monitor cache hit rate**: Add metrics to monitor cache performance
3. **Use connection pool**: Redis client is already configured with connection pool and health checks
4. **Consider cache warming**: Preload commonly used data at service startup

## Code Structure

- `libs/redis_client.py`: Redis client wrapper with automatic environment detection
- `services/user_management/main.py`: User management service with Redis cache integration
- `manifests/k8s/saferoute/user-management/`: K8s deployment configuration

## Important Notes

1. **Password Security**: Ensure Redis password is secure and do not commit to code repository
2. **Data Consistency**: Handle data consistency between cache and database according to business requirements
3. **Failure Degradation**: If Redis is unavailable, service will automatically degrade to in-memory storage (development environment only)
4. **Production Environment**: In production, Redis connection failure should cause service startup to fail
