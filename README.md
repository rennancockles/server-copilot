<p align="center">
  <img src="assets/logo.png" alt="Server Copilot" width="280">
</p>

# server-copilot

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant look at your home server:
containers, logs, resource usage, host metrics and Docker events. It is **read-only** and talks to
Docker only through [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy), never
through `docker.sock` directly.

## Tools

| Tool                      | What it does                                                                      |
| ------------------------- | --------------------------------------------------------------------------------- |
| `ping`                    | Liveness check.                                                                   |
| `list_containers`         | List containers; filter by status, name or label; shows compose project/service.  |
| `inspect_container`       | State, health, restarts, ports, networks, mounts, env (secrets masked).           |
| `get_container_logs`      | Logs with `tail`, `since`/`until`, text/regex filter and a byte limit.            |
| `get_container_stats`     | CPU %, memory, network and block I/O of one container.                            |
| `get_containers_overview` | Usage of every running container, sorted, with totals.                            |
| `get_host_metrics`        | Host CPU, load, memory, swap, uptime and disk usage per configured mount.         |
| `recent_events`           | Docker events (start, die, oom, restart, health) in a time window.                |
| `find_problem_containers` | Unhealthy, restart-looping, OOM-killed, dead or crashed containers.               |

All tools are annotated read-only. Environment variables and labels whose names look sensitive
(`password`, `token`, `key`, `secret`, ...) and credentials inside URLs are masked, but **log
content is returned as-is**, so anyone holding the access token can read what your containers log.

## Quick start

You need Docker with Compose on the server, and a way to reach the service (for example a
cloudflared tunnel).

**1. Create the network and the docker-socket-proxy** (skip if you already have them). The proxy
is the only thing that touches the Docker socket; enable just what the server reads and keep
`POST=0`:

```
docker network create server-copilot-net
```

```yaml
# docker-compose.yml of the proxy (do not publish its port)
services:
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy
    container_name: docker-socket-proxy
    restart: unless-stopped
    environment:
      CONTAINERS: 1 # list/inspect, logs, stats
      INFO: 1
      SYSTEM: 1
      EVENTS: 1 # recent_events
      VERSION: 1
      POST: 0
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - server-copilot-net

networks:
  server-copilot-net:
    external: true
```

When the proxy blocks an endpoint (HTTP 403), the error returned to the MCP client names the
variable to enable, for example `Enable CONTAINERS=1 on the docker-socket-proxy.`

**2. Configure.** Copy the example file and set a token:

```
cp .env.example .env
sed -i "s/^SERVER_COPILOT_AUTH_TOKEN=.*/SERVER_COPILOT_AUTH_TOKEN=$(openssl rand -hex 32)/" .env
```

**3. Start it:**

```
docker compose pull
docker compose up -d
docker compose ps        # should become "healthy"
```

The compose file runs the image published to GitHub Container Registry
(`ghcr.io/rennancockles/server-copilot`). It uses the `latest` tag; set
`SERVER_COPILOT_VERSION=0.1.0` in `.env` to pin a release. If the package is private, log in first
(see _CI and releases_).

**4. Expose it** by pointing your cloudflared tunnel at `http://server-copilot:8000`
(the container joins `server-copilot-net`; add the tunnel to that network or to a network shared
with it). Keep the tunnel on HTTPS, because the token travels in a header.

**5. Connect a client** (see below).

## Configuration

All settings are environment variables with the `SERVER_COPILOT_` prefix, read from the process
environment or from `.env`:

| Variable                       | Default                           | Purpose                                                  |
| ------------------------------ | --------------------------------- | -------------------------------------------------------- |
| `SERVER_COPILOT_AUTH_TOKEN`    | _(required)_                      | Bearer token for `/mcp`, at least 32 characters.         |
| `SERVER_COPILOT_AUTH_DISABLED` | `false`                           | Turn authentication off. Local development only.         |
| `SERVER_COPILOT_HOST`          | `0.0.0.0`                         | Interface to bind. Keep it in the container.             |
| `SERVER_COPILOT_PORT`          | `8000`                            | Port inside the container.                               |
| `SERVER_COPILOT_DOCKER_HOST`   | `tcp://docker-socket-proxy:2375`  | docker-socket-proxy endpoint (`tcp://host:port`).        |
| `SERVER_COPILOT_HOST_DISKS`    | _(empty)_                         | Disks to report, `name=/container/path`, comma separated. |

## Authentication

`/mcp` requires `Authorization: Bearer <token>` and answers `401` otherwise. Only `/health` is
public, so the container healthcheck keeps working.

- The server refuses to start without a token, and `docker compose up` fails if
  `SERVER_COPILOT_AUTH_TOKEN` is unset.
- `SERVER_COPILOT_AUTH_DISABLED=true` is for local development. It logs a warning at startup; never
  put it in the server's `.env`, because compose loads that file into the container.
- The token is compared in constant time, is not part of the settings `repr` and is never logged;
  rejected requests log only the method, path and client address.
- Rotate it by editing `.env` and your client configuration, then `docker compose up -d`. Do it
  whenever the token may have been exposed (pasted in a chat, screenshot, lost laptop).

## Host metrics

`get_host_metrics` reports CPU %, load average, memory, swap and uptime of the host. These come
from `/proc`, which inside a Linux container already shows host values, so nothing needs to be
mounted for them. Disk usage is reported only for the filesystems you choose: bind-mount each one
read-only and list it in `SERVER_COPILOT_HOST_DISKS`:

```yaml
environment:
  SERVER_COPILOT_HOST_DISKS: root=/host/root,data=/host/data
volumes:
  - /:/host/root:ro
  - /mnt/data:/host/data:ro
```

Only free/used numbers are read; the server exposes no file access. Mount a specific path (for
example `/mnt/data`) instead of `/` for the smallest footprint.

## Connecting clients

**Claude Code:**

```
claude mcp add --transport http server-copilot https://<your-host>/mcp \
  --header "Authorization: Bearer <token>"
```

or in `.mcp.json` (keep this file out of version control):

```json
{
  "mcpServers": {
    "server-copilot": {
      "type": "http",
      "url": "https://<your-host>/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

The header value must start with `Bearer ` (with the space). A bare token gets a `401`, which some
clients report as an OAuth error such as `auth() returned AUTHORIZED without authorization code`.

**Claude Desktop** and other clients that only accept local (stdio) servers can use the
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote) bridge with the same URL and header.
Claude.ai custom connectors expect OAuth, which this server does not implement.

**MCP Inspector:** run `npx @modelcontextprotocol/inspector`, choose _Streamable HTTP_, enter the
URL and add the `Authorization` header.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```
make install          # uv sync
make lint             # ruff, format check, mypy
make ty               # ty type check
make test             # pytest with coverage (minimum 75%)
```

Run it locally against a proxy on `localhost:2375`:

```
SERVER_COPILOT_AUTH_DISABLED=true SERVER_COPILOT_HOST=127.0.0.1 \
SERVER_COPILOT_DOCKER_HOST=tcp://localhost:2375 uv run server-copilot
```

## CI and releases

`.github/workflows/ci.yml` runs on every pull request and on every push to `main`:

1. `lint` (ruff, format check, mypy, ty) and `test` (pytest, coverage minimum 75%) in parallel.
2. `docker`: builds the image, starts it and checks that `/health` answers and `/mcp` rejects
   unauthenticated requests.
3. `release` (only on `main`, only if all of the above passed): runs
   [python-semantic-release](https://python-semantic-release.readthedocs.io), which reads the
   [Conventional Commits](https://www.conventionalcommits.org) since the last tag, bumps the version
   in `pyproject.toml`, `server_copilot/__init__.py` and `uv.lock`, updates `CHANGELOG.md`, commits,
   tags (`0.1.0`, no `v` prefix) and creates a GitHub release.
4. `publish` (only if step 3 actually released): builds the image for `linux/amd64` and
   `linux/arm64` from the release tag and pushes it to GitHub Container Registry.

Commit types decide what happens: `feat` bumps the minor version, `fix` and `perf` bump the patch
version, and `chore`, `docs`, `ci`, `refactor`, `test`, `style` and `build` publish nothing. While
the version is `0.x`, a breaking change (`feat!:` or a `BREAKING CHANGE:` footer) bumps the major
version to `1.0.0`. Preview the next version locally with `make release-dry`.

Images are published as `ghcr.io/<owner>/server-copilot` with the tags `<version>` (for example
`0.1.0`), `<major>.<minor>` and `latest`. The compose file already points at it; to update the
server, run `docker compose pull && docker compose up -d`. To test unreleased code, build it under a
local tag and select that tag:

```
docker build -t ghcr.io/rennancockles/server-copilot:dev .
SERVER_COPILOT_VERSION=dev docker compose up -d
```

The package is private by default. Either make it public in the package settings on GitHub, or log
in on the server with a personal access token that has `read:packages`:
`echo <token> | docker login ghcr.io -u <user> --password-stdin`.

If `main` has branch protection, allow `github-actions[bot]` to push, because the release commit
and tag are pushed by the workflow.
