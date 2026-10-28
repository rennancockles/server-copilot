<p align="center">
  <img src="assets/logo.png" alt="Server Copilot" width="400">
</p>

# server-copilot

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant look at your home server:
containers, logs, resource usage, host metrics and Docker events. It is **read-only**, with one
exception that never touches Docker itself (asking the host to update a stack, see _Updating Compose
projects_), and it talks to Docker only through [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy), never
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
| `list_files`              | Names, types, sizes and dates in one directory of a configured disk or readable directory (no contents). |
| `read_text_file`          | The text of any visible text file in a readable directory (runbooks, scripts, configs), in pages of lines; hidden files, binaries and anything outside the directory are refused. |
| `plan_compose_update`     | Step 1 of updating a Compose project: what would happen, warnings, and a single-use confirmation code (read-only). |
| `update_compose_project`  | Step 2: asks the host runner to pull the images and recreate what changed. Needs the code; the only tool that changes the server. |
| `get_compose_update_status` | Queued, running, succeeded, failed or rejected updates, with steps, container health and the output tail. |
| `recent_events`           | Docker events (start, die, oom, restart, health) in a time window.                |
| `find_problem_containers` | Unhealthy, restart-looping, OOM-killed, dead or crashed containers.               |
| `get_backup_status`       | Whether the host backups ran, are recent and can be recovered: last run, snapshot ages, integrity check and restore test. |
| `discover_network_devices` | Devices on the home LAN with IP, MAC, maker, name, guessed type and open ports, plus findings (new device, changed gateway MAC, ...). |
| `inspect_network_device`  | Everything known about one LAN device (by IP, MAC or name), observed facts kept apart from guesses; for a Samsung TV, its model, name, firmware and Tizen version. |

All tools are annotated read-only except `update_compose_project`, which only queues a request
for the host runner (and needs a confirmation code from `plan_compose_update`). Environment variables and labels whose names look sensitive
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
| `SERVER_COPILOT_READABLE_DIRS` | _(empty)_                         | Directories whose text files `read_text_file` may return, `name=/container/path`, comma separated, each mounted read-only (see Reading text files). Empty disables the tool. |
| `SERVER_COPILOT_UPDATE_REQUESTS_DIR` | _(empty)_                   | Folder where update requests are queued (mount it read-write). Empty disables the update tools (see Updating Compose projects). |
| `SERVER_COPILOT_UPDATE_RESULTS_DIR` | _(empty)_                    | Folder where the host runner leaves its results (mount it read-only). |
| `SERVER_COPILOT_DASHBOARD_REFRESH_SECONDS` | `30`                  | Dashboard only: seconds between its reads of the server (10 to 600); the page is served from memory (see Dashboard). |
| `SERVER_COPILOT_BACKUP_STATUS_FILE` | _(empty)_                    | JSON file the host writes about its backups (see Backup status). Empty makes `get_backup_status` answer `unknown`. |
| `SERVER_COPILOT_NETWORK_SCAN_FILE` | _(empty)_                     | Snapshot the scanner service writes and the network tools read (see Network discovery). Empty makes them answer `unknown`. The compose file sets it. |
| `SERVER_COPILOT_NETWORK_SCAN_CIDRS` | _(empty)_                    | Scanner only: private networks it may probe, comma separated, each at most a `/22`. Empty scans the subnets of the host's own interfaces. |
| `SERVER_COPILOT_NETWORK_SCAN_INTERVAL_SECONDS` | `300`             | Scanner only: seconds between scans (60 to 86400).       |
| `SERVER_COPILOT_NETWORK_SCAN_TIMEOUT_SECONDS` | `120`              | Scanner only: time limit of one scan (10 to 900); a scan that hits it keeps what it found and says so. |

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
  `offline_access`, so access is the audience plus the allowlist. Every tool is read-only except
  `update_compose_project`, which can only ask the host to update a stack on its own allowlist.

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

## Reading text files

`read_text_file` returns the text of a file so an assistant can quote the current runbook (the
disaster recovery steps, the from-scratch drill) or look at a script or a config in a later
conversation. It only reads: it never runs a command it finds, so a step in a runbook or a line in a
script is information, not an action.

Mount **only the folders you want read**, read-only, and name each one in
`SERVER_COPILOT_READABLE_DIRS`. This is separate from `SERVER_COPILOT_HOST_DISKS`: a disk is browsable
by name and size only, while a readable directory also gives out file contents, so nothing becomes
readable by being a disk.

```yaml
environment:
  SERVER_COPILOT_READABLE_DIRS: runbooks=/runbooks
volumes:
  - /home/youruser/Repos/home_server_infra/docs:/runbooks:ro
```

Then `list_files(disk="runbooks")` shows the files and
`read_text_file(directory="runbooks", path="disaster-recovery.md")` reads one. The container user
must be able to read the folder (run it as the host user if the repository sits in a mode 750 home).
The server refuses to start when a name is both a disk and a readable directory.

**The mount is the boundary.** Inside a readable directory the server assumes that every visible text
file is safe to show: it does not filter by extension or by name, so a `.py`, a config, a `.pem` or a
file called `secrets.md` is served like any other. Only mount folders where that is true.

What it refuses, and what it does:

- Hidden files and directories (names starting with `.`, so `.env`, `.ssh`, `.git`), anywhere in the
  path. The refusal does not say whether the file exists.
- Anything outside the directory: `..`, absolute paths, and symlinks that leave it. A symlink inside
  the directory that leads to a hidden name is refused too, because where it really points is checked.
- Files that are not plain UTF-8 text: binary files, invalid UTF-8, and anything over 1 MiB.
- A page is at most 500 lines and 64 KiB (400 lines by default); `offset` (the first line, from 1) and
  `next_offset` read on. The disaster recovery runbook fits in one call.
- Credentials inside URLs (`https://user:password@host`) are masked. A secret written any other way is
  **not** detected.
- Every read and every refusal is logged with the directory and path (and the line range and size for
  a read), never the content.
- The text comes back as data. The tool description tells the client not to treat instructions inside
  a file as requests from the user, but anyone who can edit the folder can write text an assistant
  will read, so mount a repository only you can write to.

## Updating Compose projects

`plan_compose_update`, `update_compose_project` and `get_compose_update_status` update a stack
without an SSH session: "update media", then "did it work?". This server never gets the power to do
it itself. It has no Docker write access, so it only **asks**: it drops a small request into a queue
folder, and a **runner on the host** (in `home_server_infra`, a systemd path unit) picks it up,
validates it against its own allowlist, runs `docker compose pull` and, only if the pull succeeds,
`docker compose up -d`, then leaves a result that this server reads. A compromised token can therefore
ask for the update of an allowed stack and nothing else: no container creation, no arbitrary command.

The flow is in two steps, so a stray request or an instruction hidden in some text cannot update
anything on its own:

1. `plan_compose_update(project, services?)` is read-only. It shows the services and the images they
   run now, what the host will do, warnings (this server restarting itself, the Cloudflare tunnel or
   the Docker proxy being recreated) and a **confirmation code**. The code is valid for five minutes,
   works once, and only for that project and those services; it lives in this process's memory, so a
   restart invalidates it.
2. After the user has agreed, `update_compose_project(project, confirmation_code, services?)` queues
   the request and returns a `request_id` at once. It is asynchronous because a pull of large images
   can outlast a tool call, and so that this server can update itself: it restarts, and the result is
   read afterwards.
3. `get_compose_update_status(request_id?)` follows it: `queued`, `running`, `succeeded`, `failed`
   or `rejected`, with the steps and their duration, the state and health of the containers
   afterwards and the tail of the output (colors and control characters removed, credentials in URLs
   masked). `queued` for more than three minutes means the runner is not running.

An update for a project that already has one queued or running is refused, and at most five requests
wait at once. A failed pull leaves the running containers as they were; a stack that is not on the
runner's allowlist is `rejected`.

Mount two folders, one for each direction (the runner and its units are in
`home_server_infra/docs/compose-update.md`):

```yaml
environment:
  SERVER_COPILOT_UPDATE_REQUESTS_DIR: /updates/requests
  SERVER_COPILOT_UPDATE_RESULTS_DIR: /updates/results
volumes:
  - /var/lib/home-server-infra-update/requests:/updates/requests
  - /var/lib/home-server-infra-update/results:/updates/results:ro
```

Keep the results folder read-only: the container can queue requests, but it cannot forge a result.
The requests folder must be writable by the container's user.

### The contract with the runner

A request is `requests/<id>.json` (`id` is 32 lowercase hex characters), written under a hidden
temporary name and renamed into place:

```json
{"schema_version": 1, "id": "<id>", "project": "media", "services": ["plex"],
 "requested_at": "2026-10-28T12:00:00+00:00"}
```

The runner reads at most 4 KiB, accepts only these keys and names that match
`[A-Za-z0-9][A-Za-z0-9_.-]*`, and moves the file out of `requests/` before it acts. It then writes
`results/<id>.json` (replacing it atomically, first as `running`, finally with the outcome); this
server reads at most 64 KiB, ignores unknown keys, reduces every name and message to plain characters
and ignores a file whose `schema_version` is not `1` or whose `id` does not match its name:

```json
{"schema_version": 1, "id": "<id>", "project": "media", "services": ["plex"],
 "requested_at": "2026-10-28T12:00:00+00:00", "started_at": "2026-10-28T12:00:02+00:00",
 "finished_at": "2026-10-28T12:01:10+00:00", "status": "succeeded", "failed_step": null,
 "message": "media was updated.",
 "steps": [{"name": "pull", "ok": true, "seconds": 41.2}, {"name": "up", "ok": true, "seconds": 9.0},
           {"name": "verify", "ok": true, "seconds": 12.5}],
 "containers": [{"service": "plex", "name": "media-plex-1", "state": "running",
                 "health": "healthy", "image": "plexinc/pms-docker:latest"}],
 "output_tail": "Container media-plex-1  Recreated\n"}
```

`status` is `running`, `succeeded`, `failed` or `rejected`; `failed_step` is `pull`, `up` or `verify`.
All timestamps carry a timezone.

## Dashboard

`server-copilot-dashboard` serves a page with the state of the server, made to be read from across the
room on a TV and to open in any browser on the LAN: **Servidor** (CPU, memory, each disk, how much
Docker uses and could give back, the latest stack updates, uptime), **Containers** (running of
total, the ones with problems, the busiest by CPU and memory), **Backup** (last backup and how long
it took, snapshot age and size of each repository, free space on the Drive, the weekly check and the
restore test) and **Rede** (devices online by type, new devices, known devices that went away, and
the scanner's findings). The text of the page is in Portuguese.

It is a separate process of the same image, never part of the MCP server. The MCP is reachable from the
Internet through the tunnel; this page is not meant to be, and it has **no login**, so keep it off the
`edge` network and out of the tunnel: anyone on your LAN (a guest on your Wi-Fi included) can open it.
It only reads, shows no secret, log, path or MAC address, and answers `GET` only.

```yaml
server-copilot-dashboard:
  image: ghcr.io/rennancockles/server-copilot:latest
  command: ["server-copilot-dashboard"]
  environment:
    SERVER_COPILOT_PORT: 8081
    SERVER_COPILOT_DOCKER_HOST: tcp://docker-socket-proxy:2375
    SERVER_COPILOT_HOST_DISKS: root=/host/root
    SERVER_COPILOT_BACKUP_STATUS_FILE: /host/backup-status/backup-status.json
    SERVER_COPILOT_NETWORK_SCAN_FILE: /var/lib/server-copilot-network/scan.json
    SERVER_COPILOT_UPDATE_REQUESTS_DIR: /updates/requests   # optional: the latest updates
    SERVER_COPILOT_UPDATE_RESULTS_DIR: /updates/results
  ports: ["8084:8081"]
  networks: [docker-api]          # Docker through the proxy; not the edge network
  volumes:                        # the same mounts as the MCP service, all read-only
    - /:/host/root:ro
    - /var/lib/home-server-infra-status:/host/backup-status:ro
    - network-scan:/var/lib/server-copilot-network:ro
    - /var/lib/home-server-infra-update/requests:/updates/requests:ro
    - /var/lib/home-server-infra-update/results:/updates/results:ro
  read_only: true
  cap_drop: [ALL]
```

How it works: a background task reads the server every `SERVER_COPILOT_DASHBOARD_REFRESH_SECONDS` (30
by default) with the same code the MCP tools use, and keeps the result in memory. The page asks for
`/api/status` every 15 seconds and shows what was last read, so ten TVs cost the same as none. Each
section is read on its own: a section that fails or takes too long shows "Indisponível" with the
reason, and the others keep working. The colour of the page is the worst state of the sections it
could read, and `/api/status` reports `stale: true` (and the page dims) when the server stopped
refreshing.

- **States:** a disk at 85% or memory at 90% is a warning, 95% and 97% an error; a container that is
  unhealthy, in a restart loop, OOM-killed or dead is an error, one that exited with an error a
  warning; the backup states are the ones of `get_backup_status`; scanner alerts are errors and its
  warnings are warnings; an update that failed or was refused in the last 24 hours is a warning (an
  older one is only listed). Docker usage is information, and never changes the colour.
- **What it needs:** the Docker figures come from `/system/df`, so the proxy needs `SYSTEM=1` (the MCP
  tool `get_docker_disk_usage` needs it too). The updates come from the same two folders as the MCP's
  update tools, mounted read-only; leave them out and that block is simply not shown. The list of
  devices that went away leaves out phones and tablets, which come and go with their owners, and shows
  names only, never MAC addresses.
- **Small screens:** each panel shows what fits and drops the least important lines first (the extra
  Docker details, the uptime, the older updates, the second list of consumers...), so nothing is ever
  cut in half; on a TV there is room for all of it.
- **OLED screens:** a page that never changes burns into the panel, so the page uses a black background
  and dim text, and drifts a few pixels every minute. Still, do not leave it on all day: close it (or
  turn the TV off) when nobody is looking.
- **The page** is plain HTML, CSS and JavaScript (no build, no external file) written for old browsers
  too. It builds the screen only with `textContent`, and the policy it is served with
  (`default-src 'none'`, scripts only from itself) means that a container or device name can never
  become markup.
- **Endpoints:** `/` (the page), `/api/status` (JSON), `/health`. Nothing else.

## Network discovery

`discover_network_devices` and `inspect_network_device` answer "what is on my network?", "what is the
printer's IP?", "which ones are the TVs?" and "is anything unusual going on?". They run nothing
themselves: they read a snapshot that a small **scanner service** writes every few minutes, so an
answer is as old as the last scan (`age_minutes` says how old).

### Why a separate scanner

MAC addresses, mDNS and SSDP only exist on the LAN itself. Inside a Docker bridge network the
container sees the bridge (`172.x`), not your LAN, and multicast does not cross it. Moving
`server-copilot` to the host network would remove it from `docker-api` and `edge` (the tunnel could no
longer reach it by name) and open `/mcp` on every interface. So the scanner is a second service from
the **same image**, with `network_mode: host`, and it is opt-in. It publishes no port, drops every
capability, has a read-only root filesystem and gets neither the access token nor the Docker proxy.
The MCP server only reads what it leaves in a shared volume, mounted read-only.

```
docker compose --profile lan-scan up -d    # starts server-copilot-scanner next to server-copilot
docker compose ps                          # the scanner becomes "healthy" after its first scan
```

The first scan takes under a minute. Until it ends, the tools answer `status: unknown`, with the
reason. Host networking works on a Linux host; Docker Desktop (macOS, Windows) runs containers in a VM
and would only see that VM's network.

### What the scanner does

It only talks to **private IPv4 networks** (`10/8`, `172.16/12`, `192.168/16`). By default it scans the
subnets of the host's physical interfaces (Docker bridges, `veth`, VPN and loopback interfaces are
skipped), so nothing assumes `192.168.1.0/24`; a subnet larger than a `/24` is cut to the `/24` around
the host's address and a note says so. To choose exactly what is scanned, set
`SERVER_COPILOT_NETWORK_SCAN_CIDRS=192.168.1.0/24,10.0.5.0/24`. Anything that is not private, or is
larger than a `/22`, stops the scanner at startup with a clear message. Nothing on the Internet is
ever probed.

Per scan, in this order, all without privileges or raw sockets (no `nmap`, no extra capability):

1. **ARP**: one byte to the discard port of each address, paced in small batches, so the kernel asks
   "who has this IP?"; the scanner then reads the host's neighbour table (`/proc/net/arp`) for MAC
   addresses, and `/proc/net/route` for the default gateway.
2. **mDNS and SSDP**: one multicast query each, on every scanned interface, with a few seconds to
   answer.
3. **TCP connect** (no data sent) to 30 well-known ports (SSH, HTTP(S), SMB, printing, RTSP, casting,
   Samsung and LG TV control, ...) of the devices found in steps 1 and 2.
4. **UPnP description** of each device that answered SSDP, **Samsung TV API** (`GET :8001/api/v2/`, no
   login) when port 8001 is open, and a reverse DNS lookup for devices without an mDNS name.

It never logs in, pairs, sends commands or changes anything on a device. Every probe has a timeout,
connections are capped at 128 at once, a whole scan has a time limit, and every response is size
limited. A UPnP description is only fetched from the very device that announced it (a device cannot
point the scanner at another address), and XML with a DTD or entities is refused. Logs hold only
counts and timings, never addresses, MACs or names.

### What you get back

Each device carries IP, MAC (when it is on the same LAN segment), maker (from the MAC prefix), hostname
and friendly name, open ports, announced services and when it was first and last seen. Observed facts
and guesses are kept apart:

- **Observed**: IP, MAC, open ports, mDNS and UPnP services, what a Samsung TV reports about itself
  (name, model, model code, firmware when it gives one, OS, API version).
- **Inferred**, with a `confidence` and the `evidence` behind it: the device type (`router`, `printer`,
  `tv`, `media_player`, `camera`, `nas`, `computer`, `phone_or_tablet`, `smart_home`, `unknown`), the maker
  from the MAC prefix (none for a randomized MAC, as phones use), and the Samsung release year and
  Tizen version, which come from the model code (`19_...` is a 2019 TV, Tizen 5.0).

`inspect_network_device` accepts an IP, a MAC in any common notation, or part of a name, maker or
model. A device that was seen before but is missing now comes back with `online: false` and its last
known address, so "where was the printer?" still has an answer.

### Findings: "is anything unusual?"

The scanner keeps a small history (`network-history.json`, next to the snapshot) of the devices it has
seen. During the first 24 hours it only learns; after that it reports:

| Finding               | Severity           | When                                                                  |
| --------------------- | ------------------ | --------------------------------------------------------------------- |
| `new_device`          | `info` / `warning` | A device first seen in the last 7 days, after the learning period. `warning` when its type is unknown or it exposes a risky port. |
| `gateway_mac_changed` | `alert`            | The default gateway's MAC changed (kept for 7 days). Expected after replacing the router; it is also how ARP spoofing looks. |
| `duplicate_mac`       | `warning`          | One MAC answers for several IPs: a device with several addresses, or something impersonating others. |
| `risky_ports`         | `warning`          | FTP, Telnet, RDP, VNC or the Docker API (2375) is open on a device.   |

A device that was off during the learning period and shows up later is reported as new, like any
other newcomer. Deleting the history file starts a new learning period.

### Things to know

- IPv4 only, and only devices that share a broadcast domain with the host appear with a MAC. A guest
  Wi-Fi or VLAN that the router isolates cannot be seen from here; one that it routes can be scanned
  by listing it in `SERVER_COPILOT_NETWORK_SCAN_CIDRS`, but then without MACs, mDNS or SSDP.
- Phones and laptops often use a different, randomized MAC per network: they are listed, but have no
  maker, and a changed MAC does not mean a different device.
- Many devices ignore probes while asleep; a TV in standby may answer ARP but not its API, so its type
  stays a guess. If one device is missing, wake it and wait for the next scan.
- Device names, hostnames and models come from the devices themselves. They are reduced to plain
  characters and 64 characters, and the tool descriptions tell the client to treat them as untrusted
  text, but a name is still attacker-controlled input on a network with guests or IoT gear.
- The answer contains the MAC addresses and names of your devices; anyone holding the access token
  can read them.

Examples: "What devices are on my network?", "What is the IP of the printer?", "Show me the TVs and
their model and firmware.", "Has any new device appeared this week?", "Is the router's MAC still the
same?".

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
