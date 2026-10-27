"""The LAN scanner service: probes the allowed subnets on a schedule and writes a snapshot.

It runs next to the MCP server (same image, host network, no capabilities); the MCP server only
reads the snapshot it leaves behind.
"""

import asyncio
import contextlib
import ipaddress
import logging
import signal
import socket
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx2
import psutil
from netaddr import EUI, AddrFormatError, NotRegisteredError
from pydantic import AwareDatetime, BaseModel, Field, ValidationError

from server_copilot.config import Settings, get_settings
from server_copilot.network_classify import classify
from server_copilot.network_probe import (
    PORT_NAMES,
    SAMSUNG_PORT,
    MdnsHost,
    SsdpReply,
    descriptor_url,
    fetch_limited,
    fetch_samsung,
    mdns_discover,
    open_ports,
    parse_arp_table,
    parse_default_gateway,
    parse_upnp_description,
    reverse_dns,
    ssdp_discover,
    wake_hosts,
)
from server_copilot.network_snapshot import (
    SCHEMA_VERSION,
    Device,
    DeviceType,
    Finding,
    NetworkSource,
    OfflineDevice,
    Port,
    Samsung,
    ScanInfo,
    ScannedNetwork,
    Snapshot,
    Upnp,
    Via,
    clean,
    write_atomic,
)

logger = logging.getLogger("server_copilot.scanner")

PROC_ARP = "/proc/net/arp"
PROC_ROUTE = "/proc/net/route"
PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
VIRTUAL_INTERFACE_PREFIXES = (
    "lo",
    "docker",
    "br-",
    "veth",
    "virbr",
    "vmnet",
    "tun",
    "tap",
    "tailscale",
    "wg",
    "cni",
    "flannel",
    "kube",
    "zt",
)
MIN_PREFIX_CONFIGURED = 22
MIN_PREFIX_AUTO = 24
MAX_CONNECTS = 128
ARP_SETTLE_SECONDS = 1.0
DNS_TIMEOUT = 1.5
HTTP_TIMEOUT = 2.0
LEARNING_PERIOD = timedelta(hours=24)
NEW_DEVICE_WINDOW = timedelta(days=7)
GATEWAY_CHANGE_WINDOW = timedelta(days=7)
FORGET_AFTER = timedelta(days=90)
MAX_HISTORY_ENTRIES = 2000
MAX_OFFLINE_DEVICES = 100
LOCALLY_ADMINISTERED = 0x02
METHODS = ["arp", "mdns", "ssdp", "tcp-connect", "upnp", "samsung-api", "reverse-dns"]

RISKY_PORTS = {
    21: "FTP (files travel unencrypted)",
    23: "Telnet (unencrypted remote shell)",
    2375: "the Docker API (remote control without a password)",
    3389: "Remote Desktop",
    5900: "VNC (remote screen)",
}


@dataclass(frozen=True)
class ScanNetwork:
    network: ipaddress.IPv4Network
    interface: str | None
    local_ip: str | None
    source: NetworkSource


@dataclass(frozen=True)
class Interface:
    name: str
    ip: str
    netmask: str


@dataclass
class Observations:
    neighbours: dict[str, str] = field(default_factory=dict)
    gateway: str | None = None
    mdns: dict[str, MdnsHost] = field(default_factory=dict)
    ssdp: dict[str, list[SsdpReply]] = field(default_factory=dict)
    ports: dict[str, list[int]] = field(default_factory=dict)
    upnp: dict[str, Upnp] = field(default_factory=dict)
    samsung: dict[str, Samsung] = field(default_factory=dict)
    dns: dict[str, str] = field(default_factory=dict)
    hosts_probed: dict[str, int] = field(default_factory=dict)


# --- which networks may be probed -----------------------------------------------------------


def parse_cidrs(spec: str) -> list[ipaddress.IPv4Network]:
    """The configured CIDRs; each must be private IPv4 and no larger than a /22."""
    networks: list[ipaddress.IPv4Network] = []
    for raw in spec.split(","):
        entry = raw.strip()
        if not entry:
            continue
        try:
            network = ipaddress.IPv4Network(entry, strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid network {entry!r}: {exc}") from exc
        if not any(network.subnet_of(private) for private in PRIVATE_NETWORKS):
            raise ValueError(f"{entry} is not a private (RFC 1918) network; only a LAN is scanned.")
        if network.prefixlen < MIN_PREFIX_CONFIGURED:
            raise ValueError(f"{entry} is too large; use /{MIN_PREFIX_CONFIGURED} or smaller.")
        if network not in networks:
            networks.append(network)
    return networks


def local_interfaces() -> list[Interface]:
    """IPv4 addresses of the interfaces that are up."""
    stats = psutil.net_if_stats()
    found: list[Interface] = []
    for name, addresses in psutil.net_if_addrs().items():
        if name not in stats or not stats[name].isup:
            continue
        found.extend(
            Interface(name=name, ip=a.address, netmask=a.netmask)
            for a in addresses
            if a.family == socket.AF_INET and a.netmask
        )
    return found


def _auto_networks(interfaces: list[Interface]) -> tuple[list[ScanNetwork], list[str]]:
    networks: list[ScanNetwork] = []
    notes: list[str] = []
    for item in interfaces:
        if item.name.startswith(VIRTUAL_INTERFACE_PREFIXES):
            continue
        address = ipaddress.IPv4Address(item.ip)
        if not any(address in private for private in PRIVATE_NETWORKS):
            continue
        network = ipaddress.IPv4Interface(f"{item.ip}/{item.netmask}").network
        if network.prefixlen < MIN_PREFIX_AUTO:
            network = ipaddress.IPv4Network(f"{item.ip}/{MIN_PREFIX_AUTO}", strict=False)
            notes.append(
                f"{item.name} is on a larger subnet; only {network} is scanned. "
                "Set SERVER_COPILOT_NETWORK_SCAN_CIDRS to scan more."
            )
        if all(existing.network != network for existing in networks):
            networks.append(ScanNetwork(network, item.name, item.ip, "auto"))
    return networks, notes


def _configured_networks(
    cidrs: list[ipaddress.IPv4Network], interfaces: list[Interface]
) -> tuple[list[ScanNetwork], list[str]]:
    networks: list[ScanNetwork] = []
    notes: list[str] = []
    for cidr in cidrs:
        local = next((i for i in interfaces if ipaddress.IPv4Address(i.ip) in cidr), None)
        if local is None:
            notes.append(
                f"This host has no interface in {cidr}: only direct probes are possible there "
                "(no MAC addresses, mDNS or SSDP)."
            )
        networks.append(
            ScanNetwork(
                cidr, local.name if local else None, local.ip if local else None, "configured"
            )
        )
    return networks, notes


def select_networks(
    configured: str, interfaces: list[Interface]
) -> tuple[list[ScanNetwork], list[str]]:
    cidrs = parse_cidrs(configured)
    if cidrs:
        return _configured_networks(cidrs, interfaces)
    networks, notes = _auto_networks(interfaces)
    if not networks:
        notes.append(
            "No private LAN interface was found. Run the scanner with network_mode: host, or set "
            "SERVER_COPILOT_NETWORK_SCAN_CIDRS."
        )
    return networks, notes


def _in_networks(ip: str, networks: list[ScanNetwork]) -> bool:
    try:
        address = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return any(address in item.network for item in networks)


# --- observing ------------------------------------------------------------------------------


def _read_proc(path: str) -> str:
    try:
        return Path(path).read_text(encoding="ascii", errors="replace")
    except OSError:
        return ""


async def _sweep(networks: list[ScanNetwork], obs: Observations) -> None:
    for item in networks:
        hosts = [h for h in item.network.hosts() if str(h) != item.local_ip]
        obs.hosts_probed[str(item.network)] = len(hosts)
        await wake_hosts(hosts)


async def _multicast(networks: list[ScanNetwork], obs: Observations) -> None:
    sources = sorted({item.local_ip for item in networks if item.local_ip})
    mdns = await asyncio.gather(*(mdns_discover(ip) for ip in sources))
    ssdp = await asyncio.gather(*(ssdp_discover(ip) for ip in sources))
    for found in mdns:
        for ip, host in found.items():
            if _in_networks(ip, networks):
                obs.mdns[ip] = host
    for found_ssdp in ssdp:
        for ip, replies in found_ssdp.items():
            if _in_networks(ip, networks):
                obs.ssdp.setdefault(ip, []).extend(replies)


async def _discover_layer2(networks: list[ScanNetwork], obs: Observations) -> None:
    await asyncio.gather(_sweep(networks, obs), _multicast(networks, obs))
    await asyncio.sleep(ARP_SETTLE_SECONDS)
    local = {item.local_ip for item in networks}
    table = parse_arp_table(_read_proc(PROC_ARP))
    obs.neighbours = {
        ip: mac for ip, mac in table.items() if ip not in local and _in_networks(ip, networks)
    }
    gateway = parse_default_gateway(_read_proc(PROC_ROUTE))
    obs.gateway = gateway if gateway and _in_networks(gateway, networks) else None


async def _probe_ports(ips: list[str], obs: Observations) -> None:
    gate = asyncio.Semaphore(MAX_CONNECTS)
    ports = list(PORT_NAMES)
    results = await asyncio.gather(*(open_ports(ip, ports, gate) for ip in ips))
    obs.ports = dict(zip(ips, results, strict=True))


async def _describe_upnp(client: httpx2.AsyncClient, ip: str, obs: Observations) -> None:
    replies = obs.ssdp.get(ip, [])
    for url in dict.fromkeys(u for u in (descriptor_url(r) for r in replies) if u):
        if body := await fetch_limited(client, url):
            if upnp := parse_upnp_description(body):
                server = next((r.server for r in replies if r.server), None)
                obs.upnp[ip] = upnp.model_copy(update={"server": clean(server) if server else None})
            return


async def _describe_samsung(client: httpx2.AsyncClient, ip: str, obs: Observations) -> None:
    if info := await fetch_samsung(client, ip):
        obs.samsung[ip] = info


async def _lookup_name(ip: str, obs: Observations) -> None:
    if name := await reverse_dns(ip, DNS_TIMEOUT):
        obs.dns[ip] = name


async def _enrich(ips: list[str], obs: Observations) -> None:
    tasks = []
    async with httpx2.AsyncClient(
        timeout=HTTP_TIMEOUT, follow_redirects=False, trust_env=False
    ) as client:
        for ip in ips:
            if ip in obs.ssdp:
                tasks.append(_describe_upnp(client, ip, obs))
            if SAMSUNG_PORT in obs.ports.get(ip, []):
                tasks.append(_describe_samsung(client, ip, obs))
            if not (obs.mdns.get(ip) and obs.mdns[ip].hostname):
                tasks.append(_lookup_name(ip, obs))
        await asyncio.gather(*tasks)


async def observe(networks: list[ScanNetwork], obs: Observations) -> None:
    await _discover_layer2(networks, obs)
    local = {item.local_ip for item in networks}
    alive = sorted(
        (set(obs.neighbours) | set(obs.mdns) | set(obs.ssdp)) - local,
        key=ipaddress.IPv4Address,
    )
    await _probe_ports(alive, obs)
    await _enrich(alive, obs)


# --- turning observations into devices ------------------------------------------------------


def lookup_vendor(mac: str) -> str | None:
    try:
        return EUI(mac).oui.registration().org or None
    except (NotRegisteredError, AddrFormatError, ValueError):
        return None


def is_randomized(mac: str) -> bool:
    return bool(int(mac.split(":", maxsplit=1)[0], 16) & LOCALLY_ADMINISTERED)


def _hostname(ip: str, obs: Observations) -> tuple[str | None, Literal["mdns", "dns"] | None]:
    host = obs.mdns.get(ip)
    if host and host.hostname:
        return host.hostname, "mdns"
    if ip in obs.dns:
        return obs.dns[ip], "dns"
    return None, None


def _friendly_name(ip: str, obs: Observations) -> str | None:
    samsung, upnp, host = obs.samsung.get(ip), obs.upnp.get(ip), obs.mdns.get(ip)
    candidates = [
        samsung.name if samsung else None,
        upnp.friendly_name if upnp else None,
        host.names[0] if host and host.names else None,
    ]
    return next((c for c in candidates if c), None)


def _discovered_via(ip: str, obs: Observations) -> list[Via]:
    flags: list[tuple[Via, bool]] = [
        ("arp", ip in obs.neighbours),
        ("mdns", ip in obs.mdns),
        ("ssdp", ip in obs.ssdp),
        ("tcp", bool(obs.ports.get(ip))),
        ("dns", ip in obs.dns),
    ]
    return [name for name, seen in flags if seen]


def build_device(ip: str, obs: Observations, now: datetime) -> Device:
    mac = obs.neighbours.get(ip)
    randomized = bool(mac and is_randomized(mac))
    hostname, source = _hostname(ip, obs)
    host = obs.mdns.get(ip)
    device = Device(
        ip=ip,
        mac=mac,
        vendor=None if mac is None or randomized else lookup_vendor(mac),
        mac_randomized=randomized,
        is_gateway=ip == obs.gateway,
        hostname=hostname,
        hostname_source=source,
        name=_friendly_name(ip, obs),
        discovered_via=_discovered_via(ip, obs),
        ports=[Port(port=p, service=PORT_NAMES.get(p)) for p in obs.ports.get(ip, [])],
        services=host.services if host else [],
        upnp=obs.upnp.get(ip),
        samsung=obs.samsung.get(ip),
        first_seen=now,
        last_seen=now,
    )
    device.classification = classify(device)
    return device


# --- history and findings -------------------------------------------------------------------


class HistoryEntry(BaseModel):
    first_seen: AwareDatetime
    last_seen: AwareDatetime
    ip: str | None = None
    mac: str | None = None
    vendor: str | None = None
    name: str | None = None
    hostname: str | None = None
    device_type: DeviceType = "unknown"


class History(BaseModel):
    schema_version: int = SCHEMA_VERSION
    created_at: AwareDatetime
    entries: dict[str, HistoryEntry] = Field(default_factory=dict)
    gateway_mac: str | None = None
    gateway_changed_at: AwareDatetime | None = None

    @property
    def learning_until(self) -> datetime:
        return self.created_at + LEARNING_PERIOD


def device_key(device: Device) -> str:
    return device.mac or f"ip:{device.ip}"


def load_history(path: Path, now: datetime) -> tuple[History, str | None]:
    """The saved history, or a fresh one (with the reason) when it is missing or unreadable."""
    try:
        return History.model_validate_json(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return History(created_at=now), None
    except (OSError, ValueError, ValidationError):
        return (
            History(created_at=now),
            "The device history was unreadable and was reset: new-device detection restarted.",
        )


def save_history(path: Path, history: History) -> None:
    write_atomic(str(path), history.model_dump_json())


def _remember(history: History, device: Device, now: datetime) -> None:
    key = device_key(device)
    known = history.entries.get(key)
    first_seen = known.first_seen if known else now
    history.entries[key] = HistoryEntry(
        first_seen=first_seen,
        last_seen=now,
        ip=device.ip,
        mac=device.mac,
        vendor=device.vendor,
        name=device.name,
        hostname=device.hostname,
        device_type=device.classification.device_type,
    )
    device.first_seen = first_seen
    device.is_new = history.learning_until < first_seen and now - first_seen <= NEW_DEVICE_WINDOW


def _track_gateway(history: History, devices: list[Device], now: datetime) -> None:
    gateway = next((d for d in devices if d.is_gateway and d.mac), None)
    if gateway is None:
        return
    if history.gateway_mac and history.gateway_mac != gateway.mac:
        history.gateway_changed_at = now
    history.gateway_mac = gateway.mac


def _prune(history: History, now: datetime) -> None:
    fresh = {k: e for k, e in history.entries.items() if now - e.last_seen <= FORGET_AFTER}
    newest = sorted(fresh.items(), key=lambda item: item[1].last_seen, reverse=True)
    history.entries = dict(newest[:MAX_HISTORY_ENTRIES])


def update_history(history: History, devices: list[Device], now: datetime) -> None:
    for device in devices:
        _remember(history, device, now)
    _track_gateway(history, devices, now)
    _prune(history, now)


def offline_devices(history: History, devices: list[Device]) -> list[OfflineDevice]:
    online = {device_key(d) for d in devices}
    gone = [(k, e) for k, e in history.entries.items() if k not in online]
    gone.sort(key=lambda item: item[1].last_seen, reverse=True)
    return [
        OfflineDevice(
            ip=e.ip,
            mac=e.mac,
            vendor=e.vendor,
            name=e.name,
            hostname=e.hostname,
            device_type=e.device_type,
            first_seen=e.first_seen,
            last_seen=e.last_seen,
        )
        for _, e in gone[:MAX_OFFLINE_DEVICES]
    ]


def _risky(device: Device) -> list[str]:
    return [
        f"{RISKY_PORTS[p.port]} on port {p.port}" for p in device.ports if p.port in RISKY_PORTS
    ]


def _new_device_findings(devices: list[Device]) -> list[Finding]:
    findings = []
    for device in devices:
        if not device.is_new:
            continue
        kind = device.classification.device_type
        what = f"{device.vendor or 'unidentified'} {kind}".replace("unknown", "device")
        suspicious = kind == "unknown" or bool(_risky(device))
        findings.append(
            Finding(
                kind="new_device",
                severity="warning" if suspicious else "info",
                ip=device.ip,
                mac=device.mac,
                detail=(
                    f"A new {what} appeared at {device.ip}, first seen "
                    f"{device.first_seen.date().isoformat()}."
                    + (" Its MAC address is randomized." if device.mac_randomized else "")
                ),
            )
        )
    return findings


def _duplicate_mac_findings(devices: list[Device]) -> list[Finding]:
    by_mac: dict[str, list[str]] = {}
    for device in devices:
        if device.mac:
            by_mac.setdefault(device.mac, []).append(device.ip)
    return [
        Finding(
            kind="duplicate_mac",
            severity="warning",
            mac=mac,
            detail=(
                f"One MAC address answers for several IPs ({', '.join(ips)}). It can be one device "
                "with several addresses, or something impersonating the others."
            ),
        )
        for mac, ips in sorted(by_mac.items())
        if len(ips) > 1
    ]


def _risky_port_findings(devices: list[Device]) -> list[Finding]:
    return [
        Finding(
            kind="risky_ports",
            severity="warning",
            ip=device.ip,
            mac=device.mac,
            detail=f"{device.ip} exposes {'; '.join(risky)}.",
        )
        for device in devices
        if (risky := _risky(device))
    ]


def _gateway_findings(history: History, devices: list[Device], now: datetime) -> list[Finding]:
    changed = history.gateway_changed_at
    if changed is None or now - changed > GATEWAY_CHANGE_WINDOW:
        return []
    gateway = next((d for d in devices if d.is_gateway), None)
    return [
        Finding(
            kind="gateway_mac_changed",
            severity="alert",
            ip=gateway.ip if gateway else None,
            mac=history.gateway_mac,
            detail=(
                f"The default gateway's MAC address changed on {changed.date().isoformat()}. "
                "That is expected after replacing the router, and is also how ARP spoofing looks."
            ),
        )
    ]


def compute_findings(history: History, devices: list[Device], now: datetime) -> list[Finding]:
    return [
        *_gateway_findings(history, devices, now),
        *_duplicate_mac_findings(devices),
        *_new_device_findings(devices),
        *_risky_port_findings(devices),
    ]


# --- one scan -------------------------------------------------------------------------------


def _numeric_ip(device: Device) -> ipaddress.IPv4Address:
    return ipaddress.IPv4Address(device.ip)


async def run_scan(settings: Settings, history_path: Path) -> Snapshot:
    started = datetime.now(UTC)
    networks, notes = select_networks(settings.network_scan_cidrs, local_interfaces())
    obs = Observations()
    complete = True
    try:
        async with asyncio.timeout(settings.network_scan_timeout_seconds):
            await observe(networks, obs)
    except TimeoutError:
        complete = False
        notes.append(
            f"The scan hit its {settings.network_scan_timeout_seconds} s limit; the list may be "
            "partial."
        )

    now = datetime.now(UTC)
    history, history_note = load_history(history_path, started)
    if history_note:
        notes.append(history_note)
    alive = set(obs.neighbours) | set(obs.mdns) | set(obs.ssdp) | set(obs.ports)
    devices = sorted((build_device(ip, obs, now) for ip in alive), key=_numeric_ip)
    if complete:
        update_history(history, devices, now)
        save_history(history_path, history)
    else:
        for device in devices:
            _remember(history, device, now)
    return Snapshot(
        schema_version=SCHEMA_VERSION,
        generated_at=now,
        learning_until=history.learning_until,
        scan=ScanInfo(
            started_at=started,
            finished_at=now,
            duration_seconds=round((now - started).total_seconds(), 1),
            complete=complete,
            interval_seconds=settings.network_scan_interval_seconds,
            networks=[
                ScannedNetwork(
                    cidr=str(item.network),
                    interface=item.interface,
                    source=item.source,
                    hosts_probed=obs.hosts_probed.get(str(item.network), 0),
                )
                for item in networks
            ],
            methods=METHODS,
            notes=notes,
        ),
        devices=devices,
        offline_devices=offline_devices(history, devices) if complete else [],
        findings=compute_findings(history, devices, now),
    )


async def scan_and_write(settings: Settings) -> None:
    path = settings.network_scan_file
    snapshot = await run_scan(settings, Path(path).with_name("network-history.json"))
    write_atomic(path, snapshot.model_dump_json())
    logger.info(
        "scan finished: %d devices on %d networks in %.1f s (complete=%s)",
        len(snapshot.devices),
        len(snapshot.scan.networks),
        snapshot.scan.duration_seconds,
        snapshot.scan.complete,
    )


async def run_forever(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    while not stop.is_set():
        try:
            await scan_and_write(settings)
        except Exception as exc:  # noqa: BLE001
            logger.error("scan failed: %s", type(exc).__name__)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), settings.network_scan_interval_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # their INFO lines carry device IPs
    settings = get_settings()
    if not settings.network_scan_file:
        sys.exit("Configuration error: set SERVER_COPILOT_NETWORK_SCAN_FILE.")
    try:
        parse_cidrs(settings.network_scan_cidrs)
    except ValueError as exc:
        sys.exit(f"Configuration error: {exc}")
    asyncio.run(run_forever(settings))


if __name__ == "__main__":
    main()
