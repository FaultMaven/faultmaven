# Troubleshooting 502 Bad Gateway on NGINX Reverse Proxy

Applies to NGINX 1.18+ acting as a reverse proxy in front of an application
upstream. This guide covers the single symptom of clients receiving HTTP 502
responses, and walks through the conditions that produce it.

## Symptom

Clients receive `502 Bad Gateway`. The NGINX error log records one of:

```
connect() failed (111: Connection refused) while connecting to upstream
upstream timed out (110: Connection timed out) while reading response header from upstream
upstream sent too big header while reading response header from upstream
no live upstreams while connecting to upstream
```

The access log shows status 502 with an `upstream_response_time` that is either
near zero (refused) or equal to the configured timeout.

## Diagnosis

Check which error text is present:

```bash
tail -200 /var/log/nginx/error.log | grep -E 'upstream'
```

Confirm whether the upstream is listening:

```bash
ss -ltnp | grep :8080
curl -sv http://127.0.0.1:8080/healthz
```

Check header sizes and timeouts in the active configuration:

```bash
nginx -T | grep -E 'proxy_(read|connect|send)_timeout|proxy_buffer'
```

## Why this happens

**The upstream process is not listening.** The application crashed, failed to
bind, or is still starting. `connect() failed (111: Connection refused)` is the
signature. Restart the upstream and add a readiness gate so NGINX is not sent
traffic before the app is accepting it.

**The upstream is too slow.** The application takes longer than
`proxy_read_timeout` (default 60s) to send response headers, and NGINX gives up.
The signature is `upstream timed out (110)`. Either raise `proxy_read_timeout`
for the affected location or fix the slow endpoint.

**The response headers are too large.** A large `Set-Cookie` or a verbose auth
header overflows `proxy_buffer_size` (default 4k/8k). The signature is
`upstream sent too big header`. Raise `proxy_buffer_size` and
`proxy_buffers` for that location.

**Every upstream in the pool is marked down.** After `max_fails` failures within
`fail_timeout`, NGINX ejects a server; when all are ejected the log records
`no live upstreams`. Fix the underlying health of the pool members, and consider
raising `max_fails` if the ejection is too aggressive for your traffic.

**DNS resolution for the upstream has gone stale.** When the upstream is given
as a hostname, NGINX resolves it once at startup unless a `resolver` directive
with a TTL is configured. If the backing IP changes, NGINX keeps dialling the
old address and gets connection refused. Configure `resolver` with a explicit
`valid=` TTL, or use an upstream block with a variable to force re-resolution.

## Prevention

- Add upstream health checks and a readiness endpoint.
- Alert on `nginx_upstream_responses{status="502"}` rather than on total 5xx.
