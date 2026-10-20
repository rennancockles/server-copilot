<p align="center">
  <img src="assets/logo.png" alt="Server Copilot" width="400">
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
| `get_disk_usage`          | Largest files and directories inside a configured disk, with sizes.               |
| `list_files`              | Names, types, sizes and dates in one directory of a configured disk (no contents). |
| `recent_events`           | Docker events (start, die, oom, restart, health) in a time window.                |
| `find_problem_containers` | Unhealthy, restart-looping, OOM-killed, dead or crashed containers.               |

All tools are annotated read-only. Environment variables and labels whose names look sensitive
(`password`, `token`, `key`, `secret`, ...) and credentials inside URLs are masked, but **log
content is returned as-is**, so anyone holding the access token can read what your containers log.

## Quick start

You need Docker with Compose on the server, and a way to reach the service (for example a
cloudflared tunnel).

**1. Create the networks and the docker-socket-proxy** (skip what you already have). Two external
networks keep the pieces apart: `docker-api` joins the proxy and the services allowed to talk to
Docker, and `edge` joins the tunnel and the services it exposes. The proxy is the only thing that
touches the Docker socket; enable just what the server reads and keep `POST=0`:

```
docker network create docker-api
docker network create edge
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
      EVENTS: 1 # recent_events
      VERSION: 1
      POST: 0
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - docker-api

networks:
  docker-api:
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

**4. Expose it** through your cloudflared tunnel. Add the cloudflared container to the `edge`
network (keep the network it already uses) and add a public hostname to the tunnel that points to
`http://server-copilot:8000`:

```yaml
cloudflared:
  networks: [default, edge]
networks:
  edge:
    external: true
```

Never put cloudflared on `docker-api`: an internet-facing container must not be able to reach the
Docker API. Keep the tunnel on HTTPS, because the token travels in a header.

**5. Connect a client** (see below).

## Configuration

All settings are environment variables with the `SERVER_COPILOT_` prefix, read from the process
environment or from `.env`:

| Variable                       | Default                           | Purpose                                                  |
| ------------------------------ | --------------------------------- | -------------------------------------------------------- |
| `SERVER_COPILOT_AUTH_TOKEN`    | _(required)_                      | Bearer token for `/mcp`, at least 32 characters.         |
| `SERVER_COPILOT_AUTH_DISABLED` | `false`                           | Turn authentication off. Local development only.         |
| `SERVER_COPILOT_URL_TOKEN`     | _(empty)_                         | Optional secret for `/mcp/<token>` (ChatGPT). See below. |
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
- `SERVER_COPILOT_URL_TOKEN` (optional) opens a second door for clients that cannot send a header:
  `POST /mcp/<token>` serves the same tools. It is a separate secret, so you can rotate or remove it
  without touching the bearer token, and the bearer token never appears in a URL. Any other
  `/mcp/...` path answers `404`; only `POST` is accepted. Use only letters, digits, `-` and `_`
  (at least 32 characters, different from the bearer token).

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

`get_host_metrics` only reads free/used numbers. `get_disk_usage` (next section) also lists names
and sizes. Mount a specific path (for example `/mnt/data`) instead of `/` for the smallest footprint.

## Disk usage

`get_disk_usage(disk, path, ...)` answers "what is eating my disk": the largest files and
directories under a path, largest first, with their share of the total. The disks are the ones you
already list in `SERVER_COPILOT_HOST_DISKS`, and `path` is relative to the disk root, so the mounts
you choose are the only scope the tool has. To look inside a home directory:

```yaml
environment:
  SERVER_COPILOT_HOST_DISKS: home=/host/home
user: "1000:1000" # the host user, so a mode 750 home is readable
volumes:
  - /home/youruser:/host/home:ro
```

Then ask for `disk="home"` and drill down with the `path` of a large directory from the answer
(`Containers/plex`, ...). `max_depth` (up to 4) controls how many levels are reported.

What it does and does not do:

- It reports names and sizes, never file contents, and it only runs `du`. The mount is read-only.
- Symlinks are not followed, and a `path` that resolves outside the disk is refused.
- Other filesystems mounted below the path (an external drive) are skipped unless
  `cross_filesystems` is true, so they do not appear in the list by default.
- One scan runs at a time, at low priority, and stops after `timeout_seconds` (20 by default, 60 at
  most), returning what had finished and saying so.
- Entries it cannot read are counted and mentioned, not hidden.

Mounting a whole home directory lets the container read everything the host user can, including
`.ssh`. The tool never opens those files, but a flaw in the server would be able to. If that worries
you, mount only the directories you want analysed, each as its own disk (for example your data and
media folders).

### Listing files

`list_files(disk, path, ...)` lists one directory of a configured disk, like `ls`: name, type (file,
directory, symlink or other), size (files only), modification time and whether the server can read
it. It uses the same disks and the same path rules as `get_disk_usage`.

- It returns names, types, sizes and dates only. It never reads file contents, and symlinks are
  listed as symlinks, without their target, and never followed.
- Names starting with `.` are left out, and a hidden directory cannot be entered by name or through
  a symlink, unless `include_hidden` is true. Credentials usually live there (`.ssh`, `.env`,
  `.aws`). This is a safe default, not a lock: whoever holds the token can set `include_hidden`.
  `get_disk_usage` does not apply this rule and can still show those names.
- `sort` is `name`, `size` (largest first) or `modified` (newest first). `limit` (up to 500) and
  `offset` page through large directories. A directory with more than 20,000 entries, or one that
  takes more than 10 seconds to read, is only partly read, and the answer says so.
- A directory's own size is not shown; use `get_disk_usage` for that.

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

**ChatGPT:** its connectors only offer OAuth or no authentication, so use the secret URL. Generate
a token (`openssl rand -hex 32`), set it as `SERVER_COPILOT_URL_TOKEN`, restart, and in ChatGPT
(_Settings, Connectors, Developer mode_) create a connector with the URL
`https://<your-host>/mcp/<url-token>` and authentication set to _None_ (menu names are ChatGPT's and
change). Treat that URL like a password:

- The server runs uvicorn without its access log, because request lines would print the secret, and
  its own rejection log shows `/mcp/<redacted>`. Cloudflare still records request URLs in its
  security events, and ChatGPT stores the URL in the connector.
- All tools are read-only, but they include container logs and host metrics. To revoke access,
  change or unset `SERVER_COPILOT_URL_TOKEN` and restart; the old URL answers `404` at once.
- The server answers statelessly with JSON, which is what ChatGPT connectors expect.

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
