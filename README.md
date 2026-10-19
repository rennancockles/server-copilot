# server-copilot

MCP server to inspect and manage a home server (Docker containers, logs, metrics).

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
