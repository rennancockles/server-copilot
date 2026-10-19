# server-copilot

MCP server to inspect and manage a home server (Docker containers, logs, metrics).

## Authentication

The MCP endpoint (`/mcp`) requires `Authorization: Bearer <token>` and answers `401` otherwise.
Only `/health` is public, so the container healthcheck keeps working.

Generate a token and put it in `.env` (never commit it):

```
echo "SERVER_COPILOT_AUTH_TOKEN=$(openssl rand -hex 32)" >> .env
```

- The server refuses to start without a token (minimum 32 characters), and `docker compose up`
  fails if `SERVER_COPILOT_AUTH_TOKEN` is unset.
- For local development only, `SERVER_COPILOT_AUTH_DISABLED=true` turns authentication off and
  logs a warning at startup. Do not set it in compose.
- The token is compared in constant time, is not part of the settings `repr`, and is never logged;
  rejected requests log only the method, path and client address.
- The token travels in a header, so keep the tunnel on HTTPS (cloudflared already terminates TLS).
  Rotate it by changing `.env` and running `docker compose up -d`.

Connecting clients:

```
claude mcp add --transport http server-copilot https://<your-tunnel-host>/mcp \
  --header "Authorization: Bearer <token>"
```

In MCP Inspector, choose Streamable HTTP, enter the URL and add the same header under
Authentication.

## Host metrics

`get_host_metrics` reports CPU %, load average, memory, swap and uptime of the host. These come
from `/proc`, which inside a Linux container already shows host values, so nothing needs to be
mounted for them. Disk usage is reported only for the filesystems you choose: bind-mount each one
read-only and list it in `SERVER_COPILOT_HOST_DISKS` as `name=/container/path`, comma separated:

```yaml
environment:
  SERVER_COPILOT_HOST_DISKS: root=/host/root,data=/host/data
volumes:
  - /:/host/root:ro
  - /mnt/data:/host/data:ro
```

Only the filesystem's free/used numbers are read; the server exposes no file access. Mount a
specific path (for example `/mnt/data`) rather than `/` if you want the smallest possible footprint.

## docker-socket-proxy permissions

server-copilot never touches `docker.sock`; it talks to the Docker API through
[docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) on the
`server-copilot-net` network (`SERVER_COPILOT_DOCKER_HOST`). The server only issues
read-only `GET` requests, so keep `POST=0` (the proxy default) and enable just the
sections it needs:

| Proxy variable | Needed for                                    |
| -------------- | --------------------------------------------- |
| `CONTAINERS=1` | list/inspect containers, logs, stats          |
| `INFO=1`       | Docker daemon info                            |
| `SYSTEM=1`     | disk usage (`/system/df`)                     |
| `EVENTS=1`     | recent Docker events                          |
| `VERSION=1`    | connection check (enabled by default)         |

When the proxy blocks an endpoint (HTTP 403), the error returned to the MCP client names
the variable to enable, e.g. `Enable CONTAINERS=1 on the docker-socket-proxy.`
