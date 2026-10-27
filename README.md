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
| `get_compose_projects`    | Compose projects with status, directory, compose files, services and containers. |
| `get_container_logs`      | Logs with `tail`, `since`/`until`, text/regex filter and a byte limit.            |
| `get_container_stats`     | CPU %, memory, network and block I/O of one container.                            |
| `get_containers_overview` | Usage of every running container, sorted, with totals.                            |
| `get_docker_disk_usage`   | Docker disk use (images, containers, volumes, build cache) and what could be cleaned up. |
| `get_host_metrics`        | Host CPU, load, memory, swap, uptime and disk usage per configured mount.         |
| `get_disk_usage`          | Largest files and directories inside a configured disk, with sizes.               |
| `list_files`              | Names, types, sizes and dates in one directory of a configured disk (no contents). |
| `recent_events`           | Docker events (start, die, oom, restart, health) in a time window.                |
| `find_problem_containers` | Unhealthy, restart-looping, OOM-killed, dead or crashed containers.               |
| `get_backup_status`       | Whether the host backups ran, are recent and can be recovered: last run, snapshot ages, integrity check and restore test. |

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
      SYSTEM: 1 # get_docker_disk_usage (read-only /system/df)
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
| `SERVER_COPILOT_PUBLIC_URL`    | _(empty)_                         | Public https origin, no path. Turns on OAuth with the next two. |
| `SERVER_COPILOT_AUTHKIT_DOMAIN` | _(empty)_                        | WorkOS AuthKit domain (`https://<app>.authkit.app`).     |
| `SERVER_COPILOT_OAUTH_ALLOWED_EMAILS` | _(empty)_                  | Verified emails allowed to sign in, comma separated. Empty allows nobody. |
| `SERVER_COPILOT_HOST`          | `0.0.0.0`                         | Interface to bind. Keep it in the container.             |
| `SERVER_COPILOT_PORT`          | `8000`                            | Port inside the container.                               |
| `SERVER_COPILOT_DOCKER_HOST`   | `tcp://docker-socket-proxy:2375`  | docker-socket-proxy endpoint (`tcp://host:port`).        |
| `SERVER_COPILOT_HOST_DISKS`    | _(empty)_                         | Disks to report, `name=/container/path`, comma separated. |
| `SERVER_COPILOT_BACKUP_STATUS_FILE` | _(empty)_                    | JSON file the host writes about its backups (see Backup status). Empty makes `get_backup_status` answer `unknown`. |

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
- OAuth sign-in (below) is an extra way in for clients that cannot send a header, such as ChatGPT.
  The bearer token keeps working next to it.

### OAuth sign-in (WorkOS AuthKit)

Set `SERVER_COPILOT_PUBLIC_URL` and `SERVER_COPILOT_AUTHKIT_DOMAIN` (both or neither; both must be
`https://`) and `/mcp` also accepts an access token issued by a [WorkOS AuthKit](https://workos.com/docs/authkit/mcp)
project. The server implements no OAuth endpoints: AuthKit owns sign-in, consent and token issuing,
and this server only validates the token.

- Every request checks the signature (JWKS), issuer, expiry and **audience**. The audience must be
  exactly `<SERVER_COPILOT_PUBLIC_URL>/mcp`, so a token issued for another MCP (the KanbAI connector,
  say) or for the AuthKit client id is refused with `401`.
- The caller's **verified email** must be on `SERVER_COPILOT_OAUTH_ALLOWED_EMAILS`. The list is
  deny-by-default: empty means nobody gets in (the server warns at startup). A valid token of
  anyone else gets `403`.
- Clients find the AuthKit domain through `/.well-known/oauth-protected-resource/mcp` (public) and
  the `resource_metadata` link in the `WWW-Authenticate` header of the `401`.
- There are no custom scopes: AuthKit offers only `openid`, `email`, `profile` and
  `offline_access`, so access is the audience plus the allowlist. Every tool is read-only anyway.

Set it up in the WorkOS dashboard: enable AuthKit, enable *Client ID Metadata Document* and/or
*Dynamic Client Registration* (*Connect → Configuration*), and add the **Resource Indicator**
`https://<your-host>/mcp` (this exact URL, the same one you give the client). Turn *Sign up* off and
invite your own email if the login should stay private.

Revoking access, recovering from a wrong allowlist or a lost WorkOS project, updating PyJWT and
watching the sign-in logs are covered in `docs/oauth-operations.md` of the kanbai_server repository,
which runs on the same WorkOS project.

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
- Names starting with `.` are left out of the list, and a hidden directory cannot be entered,
  unless `include_hidden` is true. Their size still counts in the totals (`.cache` is often the
  largest consumer), so the visible entries can add up to less than the total, and a note says
  how many were left out.

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
  `get_disk_usage` follows the same rule.
- `sort` is `name`, `size` (largest first) or `modified` (newest first). `limit` (up to 500) and
  `offset` page through large directories. A directory with more than 20,000 entries, or one that
  takes more than 10 seconds to read, is only partly read, and the answer says so.
- A directory's own size is not shown; use `get_disk_usage` for that.

## Backup status

`get_backup_status` answers questions such as "did yesterday's backup run?", "when was the last
backup?", "did the monthly restore test pass?" and "how much space is left on the Drive?". It runs
nothing: it reads a small JSON file that the host writes after every backup run, repository check
and restore test, and works out the verdict when you ask.

Bind-mount the **folder** that holds the file, read-only, and point the setting at the file:

```yaml
environment:
  SERVER_COPILOT_BACKUP_STATUS_FILE: /host/backup-status/backup-status.json
volumes:
  - /var/lib/home-server-infra-status:/host/backup-status:ro
```

Mount the folder, not the file: the host replaces the file atomically, and a bind-mounted single
file keeps pointing at the old copy. The folder must be readable by the user the container runs as,
and should hold nothing but this file.

The answer reports three things separately, because "the job ran" is not "the backup can be
recovered": `backup` (last run with its result, duration and failed step names; the age of the
newest snapshot in each repository; size and free space), `integrity` (the periodic `restic check`)
and `restore_test` (the monthly restore, with the next run when known). Ages are in hours and every
timestamp carries its timezone. The overall `status` is:

| Status    | When                                                                                     |
| --------- | ---------------------------------------------------------------------------------------- |
| `error`   | The last run failed; there is no successful run; the last success or a repository's newest snapshot is older than `max_backup_age_hours` (26 by default); a repository has no readable snapshot; the integrity check or the restore test failed. |
| `warning` | The integrity check (9 days) or the restore test (35 days) never ran or is older than expected; the status file itself was not refreshed within `max_backup_age_hours`. |
| `ok`      | None of the above.                                                                       |
| `unknown` | There is no usable data: not configured, file missing, unreadable, larger than 64 KiB, not valid JSON, not the expected format or schema version, or the read timed out (5 s). Never read it as healthy. |

Only the fields below are read; anything else in the file is ignored, so it can never reach a
response. Failed steps are step names, cut to safe characters and 60 characters. Paths, logs,
passwords and tokens are never part of the file or of the answer.

```json
{
  "schema_version": 1,
  "generated_at": "2026-10-27T05:07:12Z",
  "max_backup_age_hours": 26,
  "backup": {
    "last_run": {
      "started_at": "2026-10-27T05:06:20Z",
      "finished_at": "2026-10-27T05:07:08Z",
      "duration_seconds": 48,
      "result": "ok",
      "failed_steps": []
    },
    "last_success_at": "2026-10-27T05:07:08Z",
    "repositories": {
      "local": {"newest_snapshot_at": "2026-10-27T05:06:40Z", "size_bytes": 800000000, "free_bytes": null},
      "drive": {"newest_snapshot_at": "2026-10-27T05:06:41Z", "size_bytes": 790000000, "free_bytes": 30000000000}
    }
  },
  "integrity": {"finished_at": "2026-10-25T07:02:00Z", "duration_seconds": 40, "result": "ok"},
  "restore_test": {
    "finished_at": "2026-10-01T08:03:00Z",
    "duration_seconds": 38,
    "result": "ok",
    "next_run_at": "2026-11-01T08:00:00Z"
  }
}
```

`schema_version` must be `1`; all timestamps need a timezone. A new version of the file needs a
matching version of the server, and until then the answer is `unknown`.

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
Claude.ai custom connectors expect OAuth, which works with the sign-in above.

**ChatGPT:** its connectors only offer OAuth or no authentication, so enable the OAuth sign-in
above. In ChatGPT (_Settings, Connectors, Developer mode_) create a connector with the URL
`https://<your-host>/mcp` and authentication set to _OAuth_ (menu names are ChatGPT's and change);
it sends you to AuthKit to sign in. To revoke access, remove the email from
`SERVER_COPILOT_OAUTH_ALLOWED_EMAILS` and restart, or remove the user in the WorkOS dashboard. The
server answers statelessly with JSON, which is what ChatGPT connectors expect.

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
   in `pyproject.toml`, `server_copilot/__init__.py`, `uv.lock` and the two ChatGPT plugin manifests
   (see below), updates `CHANGELOG.md`, commits, tags (`0.1.0`, no `v` prefix) and creates a GitHub
   release.
4. `plugin` (only if step 3 actually released): zips `chatgpt-plugin/` and attaches
   `server-copilot-chatgpt-plugin-<version>.zip` to that GitHub release.
5. `publish` (only if step 3 actually released): builds the image for `linux/amd64` and
   `linux/arm64` from the release tag and pushes it to GitHub Container Registry.

Commit types decide what happens: `feat` bumps the minor version, `fix` and `perf` bump the patch
version, and `chore`, `docs`, `ci`, `refactor`, `test`, `style` and `build` publish nothing. While
the version is `0.x`, a breaking change (`feat!:` or a `BREAKING CHANGE:` footer) bumps the major
version to `1.0.0`. Preview the next version locally with `make release-dry`.

### The ChatGPT plugin

`chatgpt-plugin/` holds the plugin that presents this server inside ChatGPT: the manifests
(`plugin.json`, `.codex-plugin/plugin.json`), `.app.json` with the app id and the images in
`assets/`. Its version follows the server's: every release rewrites the `version` of both manifests,
so do not edit it by hand.

To publish an update, download `server-copilot-chatgpt-plugin-<version>.zip` from the release page
and upload it as the plugin update in ChatGPT. The zip holds the *contents* of the folder at its
root (dotfiles included, no `.DS_Store`). `make plugin-zip` builds the same zip into `dist/`
without a release.

**Before the first zip:** `.app.json` ships with a placeholder id
(`asdk_app_REPLACE_WITH_THE_APP_ID`). Put the app id ChatGPT gave you there (and the plugin `name`
in both manifests if ChatGPT assigned one). Until then the build refuses to produce a zip, so the
`plugin` job of the next release fails instead of shipping an invalid plugin; the release and the
image are not affected.

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
