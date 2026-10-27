import json
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

SCHEMA_VERSION = 1
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_TEXT_LENGTH = 64
MAX_DETAIL_LENGTH = 240
MAX_LIST_ITEMS = 32

DeviceType = Literal[
    "router",
    "printer",
    "tv",
    "media_player",
    "camera",
    "nas",
    "computer",
    "phone_or_tablet",
    "smart_home",
    "unknown",
]
Confidence = Literal["high", "medium", "low"]
Via = Literal["arp", "mdns", "ssdp", "tcp", "dns"]
Severity = Literal["info", "warning", "alert"]
FindingKind = Literal["new_device", "gateway_mac_changed", "duplicate_mac", "risky_ports"]
NetworkSource = Literal["configured", "auto"]

_UNSAFE_TEXT = re.compile(r"[^\w .,:;()\[\]/+'@#&-]")


def clean(text: str, limit: int = MAX_TEXT_LENGTH) -> str:
    """Device-supplied text is untrusted: keep plain characters only, on one short line."""
    return " ".join(_UNSAFE_TEXT.sub("", text).split())[:limit]


def _clean_detail(text: str) -> str:
    return clean(text, MAX_DETAIL_LENGTH)


Clean = Annotated[str, AfterValidator(clean)]
Detail = Annotated[str, AfterValidator(_clean_detail)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Port(_Model):
    port: int = Field(ge=1, le=65535)
    service: Clean | None = None


class Service(_Model):
    type: Clean
    name: Clean | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    source: Literal["mdns", "ssdp"]


class Upnp(_Model):
    friendly_name: Clean | None = None
    manufacturer: Clean | None = None
    model_name: Clean | None = None
    model_number: Clean | None = None
    device_type: Clean | None = None
    server: Clean | None = None


class Samsung(_Model):
    """What the TV itself reports on its local API, plus values worked out from them."""

    name: Clean | None = None
    model_name: Clean | None = None
    model_code: Clean | None = None
    firmware: Clean | None = None
    os: Clean | None = None
    device_type: Clean | None = None
    api_version: Clean | None = None
    release_year: int | None = None
    tizen_version: Clean | None = None


class Classification(_Model):
    device_type: DeviceType = "unknown"
    confidence: Confidence = "low"
    evidence: list[Detail] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)


class Device(_Model):
    ip: str
    mac: str | None = None
    vendor: Clean | None = None
    mac_randomized: bool = False
    is_gateway: bool = False
    hostname: Clean | None = None
    hostname_source: Literal["mdns", "dns"] | None = None
    name: Clean | None = None
    discovered_via: list[Via] = Field(default_factory=list)
    ports: list[Port] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    services: list[Service] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    upnp: Upnp | None = None
    samsung: Samsung | None = None
    classification: Classification = Field(default_factory=Classification)
    first_seen: AwareDatetime
    last_seen: AwareDatetime
    is_new: bool = False


class OfflineDevice(_Model):
    ip: str | None = None
    mac: str | None = None
    vendor: Clean | None = None
    name: Clean | None = None
    hostname: Clean | None = None
    device_type: DeviceType = "unknown"
    first_seen: AwareDatetime
    last_seen: AwareDatetime


class Finding(_Model):
    kind: FindingKind
    severity: Severity
    ip: str | None = None
    mac: str | None = None
    detail: Detail


class ScannedNetwork(_Model):
    cidr: str
    interface: Clean | None = None
    source: NetworkSource
    hosts_probed: int = Field(ge=0)


class ScanInfo(_Model):
    started_at: AwareDatetime
    finished_at: AwareDatetime
    duration_seconds: float = Field(ge=0)
    complete: bool
    interval_seconds: int = Field(gt=0)
    networks: list[ScannedNetwork] = Field(default_factory=list)
    methods: list[Clean] = Field(default_factory=list)
    notes: list[Detail] = Field(default_factory=list)


class Snapshot(_Model):
    schema_version: int
    generated_at: AwareDatetime
    learning_until: AwareDatetime
    scan: ScanInfo
    devices: list[Device] = Field(default_factory=list)
    offline_devices: list[OfflineDevice] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


def _read_text(path: str) -> str:
    with Path(path).open("rb") as handle:
        data = handle.read(MAX_SNAPSHOT_BYTES + 1)
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise ValueError(f"The scan file is larger than {MAX_SNAPSHOT_BYTES // 1024} KiB.")
    return data.decode("utf-8")


def _parse(document: Any) -> Snapshot | str:
    version = document.get("schema_version") if isinstance(document, dict) else None
    if version != SCHEMA_VERSION:
        return f"Unsupported scan schema version {clean(str(version))} (expected {SCHEMA_VERSION})."
    try:
        return Snapshot.model_validate(document)
    except ValidationError:
        return "The network scan file has an unexpected format."


def read_snapshot(path: str) -> Snapshot | str:
    """The snapshot the scanner wrote, or the reason it cannot be used."""
    if not path:
        return (
            "Network scanning is not configured: set SERVER_COPILOT_NETWORK_SCAN_FILE and run "
            "the scanner service (docker compose --profile lan-scan up -d)."
        )
    try:
        document = json.loads(_read_text(path))
    except FileNotFoundError:
        return "No network scan exists yet: the scanner has not finished its first scan."
    except OSError as exc:
        return f"The network scan file cannot be read ({exc.strerror})."
    except ValueError as exc:
        return f"The network scan file is not usable: {exc}"
    return _parse(document)


def write_atomic(path: str, text: str) -> None:
    """Replace the file in one step, so a reader never sees half a snapshot."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o644)
    os.replace(temporary, target)
