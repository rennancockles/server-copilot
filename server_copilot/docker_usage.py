from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from server_copilot.docker import DockerClient, DockerError

# Pinned: Docker 29 changed the default /system/df shape; 1.44 keeps the list-based one and is
# still the minimum API of current engines.
DF_PATH = "/v1.44/system/df"
DF_TIMEOUT = 60.0
SHORT_ID_LENGTH = 12
DEFAULT_TOP = 20
MAX_TOP = 100

CandidateKind = Literal["image", "container", "volume", "build_cache"]


class UsageSummary(BaseModel):
    count: int
    size_bytes: int
    reclaimable_bytes: int


class CleanupCandidate(BaseModel):
    kind: CandidateKind
    name: str
    size_bytes: int | None
    reason: str


class DockerDiskUsage(BaseModel):
    images: UsageSummary
    containers: UsageSummary
    volumes: UsageSummary
    build_cache: UsageSummary
    total_bytes: int
    reclaimable_bytes: int
    cleanup_candidates: list[CleanupCandidate]
    candidates_not_shown: int
    notes: list[str]


def _size(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _short_id(value: str) -> str:
    return value.removeprefix("sha256:")[:SHORT_ID_LENGTH]


def _summary(count: int, size: int, candidates: list[CleanupCandidate]) -> UsageSummary:
    return UsageSummary(
        count=count,
        size_bytes=size,
        reclaimable_bytes=sum(c.size_bytes or 0 for c in candidates),
    )


def _image_candidates(images: list[dict[str, Any]]) -> list[CleanupCandidate]:
    candidates = []
    for image in images:
        if image.get("Containers") != 0:
            continue
        tags = [t for t in image.get("RepoTags") or [] if t != "<none>:<none>"]
        size = _size(image.get("Size")) or 0
        unique = max(size - (_size(image.get("SharedSize")) or 0), 0)
        reason = "not used by any container" if tags else "untagged and not used by any container"
        candidates.append(
            CleanupCandidate(
                kind="image",
                name=tags[0] if tags else _short_id(image.get("Id", "")),
                size_bytes=unique,
                reason=reason,
            )
        )
    return candidates


def _container_candidates(containers: list[dict[str, Any]]) -> list[CleanupCandidate]:
    return [
        CleanupCandidate(
            kind="container",
            name=(c.get("Names") or [c.get("Id", "")])[0].lstrip("/"),
            size_bytes=_size(c.get("SizeRw")) or 0,
            reason=f"container is not running ({c.get('Status') or c.get('State')})",
        )
        for c in containers
        if c.get("State") != "running"
    ]


def _volume_candidates(volumes: list[dict[str, Any]]) -> list[CleanupCandidate]:
    return [
        CleanupCandidate(
            kind="volume",
            name=v.get("Name", ""),
            size_bytes=_size((v.get("UsageData") or {}).get("Size")),
            reason=(
                "not referenced by any container; may hold the data of a stopped project, "
                "check before removing"
            ),
        )
        for v in volumes
        if (v.get("UsageData") or {}).get("RefCount") == 0
    ]


def _build_cache_candidates(entries: list[dict[str, Any]]) -> list[CleanupCandidate]:
    return [
        CleanupCandidate(
            kind="build_cache",
            name=_short_id(e.get("ID", "")),
            size_bytes=_size(e.get("Size")) or 0,
            reason=f"unused {e.get('Type') or 'build'} cache",
        )
        for e in entries
        if not e.get("InUse")
    ]


def build_report(raw: dict[str, Any], *, top: int = DEFAULT_TOP) -> DockerDiskUsage:
    images = raw.get("Images") or []
    containers = raw.get("Containers") or []
    volumes = raw.get("Volumes") or []
    build_cache = raw.get("BuildCache") or []

    image_candidates = _image_candidates(images)
    container_candidates = _container_candidates(containers)
    volume_candidates = _volume_candidates(volumes)
    cache_candidates = _build_cache_candidates(build_cache)

    # LayersSize counts layers shared between images once; summing Size would not.
    images_size = _size(raw.get("LayersSize"))
    if images_size is None:
        images_size = sum(_size(i.get("Size")) or 0 for i in images)

    summaries = {
        "images": _summary(len(images), images_size, image_candidates),
        "containers": _summary(
            len(containers),
            sum(_size(c.get("SizeRw")) or 0 for c in containers),
            container_candidates,
        ),
        "volumes": _summary(
            len(volumes),
            sum(_size((v.get("UsageData") or {}).get("Size")) or 0 for v in volumes),
            volume_candidates,
        ),
        "build_cache": _summary(
            len(build_cache), sum(_size(e.get("Size")) or 0 for e in build_cache), cache_candidates
        ),
    }

    everything = [*image_candidates, *container_candidates, *volume_candidates, *cache_candidates]
    everything.sort(key=lambda c: -(c.size_bytes or 0))

    notes = []
    unknown = sum(1 for c in volume_candidates if c.size_bytes is None)
    if unknown:
        notes.append(f"{unknown} unused volume(s) have an unknown size and count as 0 reclaimable.")

    return DockerDiskUsage(
        **summaries,
        total_bytes=sum(s.size_bytes for s in summaries.values()),
        reclaimable_bytes=sum(s.reclaimable_bytes for s in summaries.values()),
        cleanup_candidates=everything[:top],
        candidates_not_shown=max(len(everything) - top, 0),
        notes=notes,
    )


async def fetch_docker_disk_usage(
    docker: DockerClient, *, top: int = DEFAULT_TOP
) -> DockerDiskUsage:
    raw = await docker.get_json(DF_PATH, time_limit=DF_TIMEOUT)
    return build_report(raw, top=top)


def register_docker_usage_tools(mcp: MCPServer, docker: DockerClient) -> None:
    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def get_docker_disk_usage(
        top: Annotated[
            int,
            Field(
                ge=1, le=MAX_TOP, description="How many cleanup candidates to list, largest first."
            ),
        ] = DEFAULT_TOP,
    ) -> DockerDiskUsage:
        """Show how much disk Docker uses (images, containers, volumes, build cache) and what
        could be cleaned up, with the estimated space each item would free.

        Read-only: nothing is pruned or removed. Image estimates only count layers unique to
        each image, so they are a lower bound when several candidates share layers. Unused
        volumes may still hold data you want, so review them before removing. Needs SYSTEM=1
        on the docker-socket-proxy and can take several seconds on hosts with many images.
        """
        try:
            return await fetch_docker_disk_usage(docker, top=top)
        except DockerError as exc:
            raise ToolError(str(exc)) from exc
