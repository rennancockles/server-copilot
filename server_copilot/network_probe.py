"""Unprivileged LAN discovery primitives: nothing here needs a raw socket or a capability."""

import asyncio
import contextlib
import ipaddress
import json
import re
import socket
import struct
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx2

from server_copilot.network_snapshot import Samsung, Service, Upnp

MDNS_GROUP = ("224.0.0.251", 5353)
SSDP_GROUP = ("239.255.255.250", 1900)
DISCARD_PORT = 9
SWEEP_BATCH = 64
SWEEP_PAUSE = 0.05
CONNECT_TIMEOUT = 0.6
MAX_DATAGRAM = 9000
MAX_DATAGRAMS = 512
MAX_BODY_BYTES = 64 * 1024
MAX_DNS_RECORDS = 256
MAX_POINTER_JUMPS = 16
MAX_NAME_LENGTH = 255
MAX_SSDP_LINES = 40
DNS_RESPONSE_FLAG = 0x8000
DNS_HEADER = struct.Struct("!6H")
DNS_RECORD = struct.Struct("!HHIH")
SRV_FIXED = struct.Struct("!3H")
MDNS_TTL = 255
SSDP_TTL = 4
SSDP_MX = 2
ARP_COMPLETE = 0x2
ROUTE_GATEWAY = 0x2
TYPE_A = 1
TYPE_PTR = 12
TYPE_TXT = 16
TYPE_SRV = 33
CLASS_IN = 1
META_QUERY = "_services._dns-sd._udp.local"
SAMSUNG_PORT = 8001

# Ports worth knowing about on a home network. Each gets a single short connect attempt.
PORT_NAMES: dict[int, str] = {
    21: "ftp",
    22: "ssh",
    23: "telnet",
    53: "dns",
    80: "http",
    139: "netbios",
    443: "https",
    445: "smb",
    515: "lpd",
    554: "rtsp",
    631: "ipp",
    1883: "mqtt",
    2375: "docker-api",
    3000: "webos",
    3001: "webos-tls",
    3389: "rdp",
    5000: "http-alt",
    5001: "https-alt",
    5900: "vnc",
    7676: "samsung-remote",
    8001: "samsung-api",
    8002: "samsung-api-tls",
    8008: "cast",
    8009: "cast-tls",
    8060: "roku",
    8080: "http-proxy",
    8443: "https-alt",
    9100: "jetdirect",
    9197: "dlna",
    62078: "ios-lockdown",
}

MDNS_SERVICE_TYPES = [
    "_ipp._tcp",
    "_ipps._tcp",
    "_printer._tcp",
    "_pdl-datastream._tcp",
    "_airplay._tcp",
    "_raop._tcp",
    "_googlecast._tcp",
    "_hap._tcp",
    "_matter._tcp",
    "_smb._tcp",
    "_ssh._tcp",
    "_workstation._tcp",
    "_http._tcp",
    "_spotify-connect._tcp",
    "_rtsp._tcp",
    "_companion-link._tcp",
]

# Tizen release shipped on Samsung TVs of each model year.
TIZEN_BY_YEAR = {
    2015: "2.3",
    2016: "2.4",
    2017: "3.0",
    2018: "4.0",
    2019: "5.0",
    2020: "5.5",
    2021: "6.0",
    2022: "6.5",
    2023: "7.0",
    2024: "8.0",
}
_MODEL_CODE_YEAR = re.compile(r"^(\d{2})_")


def parse_arp_table(text: str) -> dict[str, str]:
    """IPv4 -> MAC of the complete entries of /proc/net/arp."""
    table: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:  # noqa: PLR2004
            continue
        ip, _hw_type, flags, mac = fields[:4]
        try:
            complete = int(flags, 16) & ARP_COMPLETE
            ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        if complete and mac != "00:00:00:00:00:00":
            table[ip] = mac.lower()
    return table


def parse_default_gateway(text: str) -> str | None:
    """The default route's gateway from /proc/net/route (addresses are little-endian hex)."""
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000":  # noqa: PLR2004
            continue
        try:
            if int(fields[3], 16) & ROUTE_GATEWAY:
                return str(ipaddress.IPv4Address(struct.pack("<I", int(fields[2], 16))))
        except (ValueError, struct.error):
            continue
    return None


async def wake_hosts(hosts: list[ipaddress.IPv4Address]) -> int:
    """Send one byte to each host so the kernel ARPs for it; the answers fill the neighbour table.

    Paced in small batches, and the datagram goes to the discard port, so nothing acts on it.
    """
    sent = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        for index, host in enumerate(hosts, start=1):
            with contextlib.suppress(OSError):
                sock.sendto(b"\x00", (str(host), DISCARD_PORT))
                sent += 1
            if index % SWEEP_BATCH == 0:
                await asyncio.sleep(SWEEP_PAUSE)
    return sent


async def _connects(ip: str, port: int, seconds: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), seconds)
    except (TimeoutError, OSError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


async def open_ports(
    ip: str, ports: list[int], gate: asyncio.Semaphore, seconds: float = CONNECT_TIMEOUT
) -> list[int]:
    """Ports that accept a TCP connection, probed with a plain connect (no data is sent)."""

    async def one(port: int) -> int | None:
        async with gate:
            return port if await _connects(ip, port, seconds) else None

    results = await asyncio.gather(*(one(port) for port in ports))
    return sorted(port for port in results if port is not None)


async def reverse_dns(ip: str, seconds: float) -> str | None:
    loop = asyncio.get_running_loop()
    try:
        name, _ = await asyncio.wait_for(loop.getnameinfo((ip, 0), socket.NI_NAMEREQD), seconds)
    except (TimeoutError, OSError):
        return None
    return name


def _multicast_socket(local_ip: str, ttl: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setblocking(False)
        sock.bind((local_ip, 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("B", ttl))
    except OSError:
        sock.close()
        raise
    return sock


async def _query(
    local_ip: str, payload: bytes, group: tuple[str, int], *, ttl: int, wait: float
) -> list[tuple[bytes, str]]:
    """Send one datagram to a multicast group and collect the unicast answers for ``wait`` s."""
    loop = asyncio.get_running_loop()
    try:
        sock = _multicast_socket(local_ip, ttl)
    except OSError:
        return []
    replies: list[tuple[bytes, str]] = []
    with sock:
        try:
            await loop.sock_sendto(sock, payload, group)
        except OSError:
            return []
        end = loop.time() + wait
        while len(replies) < MAX_DATAGRAMS and (remaining := end - loop.time()) > 0:
            try:
                data, address = await asyncio.wait_for(
                    loop.sock_recvfrom(sock, MAX_DATAGRAM), remaining
                )
            except (TimeoutError, OSError):
                break
            replies.append((data, address[0]))
    return replies


# --- mDNS -----------------------------------------------------------------------------------


@dataclass
class DnsRecord:
    name: str
    rtype: int
    ip: str | None = None
    target: str | None = None
    port: int | None = None
    txt: dict[str, str] = field(default_factory=dict)


def _parse_txt(data: bytes) -> dict[str, str]:
    """The ``key=value`` strings of a TXT record; a string that runs past the record is dropped."""
    entries: dict[str, str] = {}
    position = 0
    while position < len(data):
        end = position + 1 + data[position]
        if end > len(data):
            break
        key, _, value = data[position + 1 : end].decode("utf-8", "replace").partition("=")
        if key:
            entries.setdefault(key.lower(), value)
        position = end
    return entries


def _label_end(data: bytes, position: int) -> tuple[bytes, int]:
    length = data[position]
    end = position + 1 + length
    if end > len(data):
        raise ValueError("label runs past the packet")
    return data[position + 1 : end], end


def read_name(data: bytes, offset: int) -> tuple[str, int]:
    """A DNS name at ``offset`` and the offset just after it, following compression pointers."""
    labels: list[str] = []
    position = offset
    resume: int | None = None
    jumps = 0
    while True:
        if position >= len(data):
            raise ValueError("name runs past the packet")
        marker = data[position] & 0xC0
        if marker == 0xC0:  # noqa: PLR2004
            if position + 1 >= len(data) or jumps >= MAX_POINTER_JUMPS:
                raise ValueError("bad compression pointer")
            resume = position + 2 if resume is None else resume
            position = ((data[position] & 0x3F) << 8) | data[position + 1]
            jumps += 1
            continue
        if marker:
            raise ValueError("reserved label type")
        if data[position] == 0:
            return ".".join(labels), resume if resume is not None else position + 1
        label, position = _label_end(data, position)
        labels.append(label.decode("utf-8", "replace"))
        if sum(len(item) + 1 for item in labels) > MAX_NAME_LENGTH:
            raise ValueError("name too long")


def _record(data: bytes, offset: int) -> tuple[DnsRecord, int]:
    name, offset = read_name(data, offset)
    rtype, _rclass, _ttl, length = DNS_RECORD.unpack_from(data, offset)
    start = offset + DNS_RECORD.size
    end = start + length
    if end > len(data):
        raise ValueError("record runs past the packet")
    record = DnsRecord(name=name, rtype=rtype)
    if rtype == TYPE_A and length == 4:  # noqa: PLR2004
        record.ip = socket.inet_ntoa(data[start:end])
    elif rtype == TYPE_PTR:
        record.target = read_name(data, start)[0]
    elif rtype == TYPE_SRV and length > SRV_FIXED.size:
        record.port = SRV_FIXED.unpack_from(data, start)[2]
        record.target = read_name(data, start + SRV_FIXED.size)[0]
    elif rtype == TYPE_TXT:
        record.txt = _parse_txt(data[start:end])
    return record, end


def parse_dns_response(data: bytes) -> list[DnsRecord]:
    """A, PTR and SRV records of an mDNS response; an empty list when it is malformed."""
    try:
        _, flags, questions, answers, authority, additional = DNS_HEADER.unpack_from(data)
        if not flags & DNS_RESPONSE_FLAG:
            return []
        offset = DNS_HEADER.size
        for _ in range(questions):
            offset = read_name(data, offset)[1] + 4
        total = min(answers + authority + additional, MAX_DNS_RECORDS)
        records = []
        for _ in range(total):
            record, offset = _record(data, offset)
            records.append(record)
    except (ValueError, struct.error, IndexError):
        return []
    return records


def build_mdns_query(names: list[str]) -> bytes:
    """One packet asking for the PTR records of every name."""
    packet = DNS_HEADER.pack(0, 0, len(names), 0, 0, 0)
    for name in names:
        for label in name.split("."):
            packet += bytes([len(label)]) + label.encode("ascii")
        packet += b"\x00" + struct.pack("!HH", TYPE_PTR, CLASS_IN)
    return packet


@dataclass
class MdnsHost:
    hostname: str | None = None
    names: list[str] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)


def _resolve_mdns(replies: list[tuple[bytes, str]]) -> dict[str, MdnsHost]:
    addresses: dict[str, str] = {}
    services: dict[str, tuple[str, int | None]] = {}
    pointers: list[tuple[str, str, str]] = []
    for data, source in replies:
        for record in parse_dns_response(data):
            key = record.name.lower()
            if record.rtype == TYPE_A and record.ip:
                addresses[key] = record.ip
            elif record.rtype == TYPE_SRV and record.target:
                services[key] = (record.target, record.port)
            elif record.rtype == TYPE_PTR and record.target and key != META_QUERY:
                pointers.append((record.name, record.target, source))

    hosts: dict[str, MdnsHost] = {}
    for service_type, instance, source in pointers:
        target, port = services.get(instance.lower(), (None, None))
        ip = addresses.get(target.lower()) if target else None
        host = hosts.setdefault(ip or source, MdnsHost())
        host.hostname = host.hostname or target
        label = instance.removesuffix(f".{service_type}")
        host.names.append(label)
        host.services.append(
            Service(type=service_type.removesuffix(".local"), name=label, port=port, source="mdns")
        )
    return hosts


async def mdns_discover(
    local_ip: str, group: tuple[str, int] = MDNS_GROUP, wait: float = 3.0
) -> dict[str, MdnsHost]:
    """Devices that answer an mDNS query, by IP. Needs no special port or membership."""
    names = [f"{kind}.local" for kind in MDNS_SERVICE_TYPES] + [META_QUERY]
    replies = await _query(local_ip, build_mdns_query(names), group, ttl=MDNS_TTL, wait=wait)
    return _resolve_mdns(replies)


IPP_SERVICES = {"_ipps._tcp": "ipps", "_ipp._tcp": "ipp"}
_RESOURCE_PATH = re.compile(r"^[A-Za-z0-9._~/-]{1,64}$")


def _has_dot_segment(path: str) -> bool:
    return any(segment in ("", ".", "..") for segment in path.strip("/").split("/"))


def printer_endpoints(
    replies: list[tuple[bytes, str]], printer_ip: str, port: int
) -> list[tuple[str, str]]:
    """The ``(scheme, path)`` pairs a printer advertises for IPP, ipps first.

    Only answers sent by ``printer_ip`` itself, for a service on ``port``, count, and the path
    (the ``rp`` TXT key) must be a plain resource path: another device on the LAN cannot steer
    a request to anywhere else.
    """
    records = [
        record
        for data, source in replies
        if source == printer_ip
        for record in parse_dns_response(data)
    ]
    services = {r.name.lower(): r for r in records if r.rtype == TYPE_SRV}
    texts = {r.name.lower(): r.txt for r in records if r.rtype == TYPE_TXT}
    pointers = [
        (r.name.lower().removesuffix(".local"), r.target.lower())
        for r in records
        if r.rtype == TYPE_PTR and r.target
    ]
    found: list[tuple[str, str]] = []
    for service_type, scheme in IPP_SERVICES.items():
        for kind, instance in pointers:
            service = services.get(instance)
            path = texts.get(instance, {}).get("rp", "")
            if kind != service_type or service is None or service.port != port:
                continue
            if not _RESOURCE_PATH.match(path) or _has_dot_segment(path):
                continue
            endpoint = (scheme, "/" + path.lstrip("/"))
            if endpoint not in found:
                found.append(endpoint)
    return found


async def mdns_printer_endpoints(
    local_ip: str,
    printer_ip: str,
    port: int,
    group: tuple[str, int] = MDNS_GROUP,
    wait: float = 1.5,
) -> list[tuple[str, str]]:
    """Ask the LAN for a printer's IPP service records and return what ``printer_ip`` advertises."""
    names = [f"{kind}.local" for kind in IPP_SERVICES]
    replies = await _query(local_ip, build_mdns_query(names), group, ttl=MDNS_TTL, wait=wait)
    return printer_endpoints(replies, printer_ip, port)


# --- SSDP / UPnP ----------------------------------------------------------------------------


@dataclass
class SsdpReply:
    ip: str
    location: str | None
    server: str | None
    search_target: str | None


def build_msearch() -> bytes:
    host = f"{SSDP_GROUP[0]}:{SSDP_GROUP[1]}"
    lines = [
        "M-SEARCH * HTTP/1.1",
        f"HOST: {host}",
        'MAN: "ssdp:discover"',
        f"MX: {SSDP_MX}",
        "ST: ssdp:all",
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def parse_ssdp_reply(data: bytes, source_ip: str) -> SsdpReply | None:
    lines = data.decode("utf-8", "replace").split("\r\n")[: MAX_SSDP_LINES + 1]
    if not lines or not lines[0].upper().startswith("HTTP/1.1 200"):
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return SsdpReply(
        ip=source_ip,
        location=headers.get("location"),
        server=headers.get("server"),
        search_target=headers.get("st"),
    )


def descriptor_url(reply: SsdpReply) -> str | None:
    """The LOCATION to fetch, only if it points back at the device that answered.

    A device must not be able to make this server request some other address.
    """
    if not reply.location:
        return None
    try:
        parts = urlsplit(reply.location)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "http" or parts.hostname != reply.ip or parts.username or parts.password:
        return None
    if port is not None and port < 1:
        return None
    return reply.location


async def ssdp_discover(
    local_ip: str, group: tuple[str, int] = SSDP_GROUP, wait: float = SSDP_MX + 1.0
) -> dict[str, list[SsdpReply]]:
    replies = await _query(local_ip, build_msearch(), group, ttl=SSDP_TTL, wait=wait)
    found: dict[str, list[SsdpReply]] = {}
    for data, source in replies:
        if reply := parse_ssdp_reply(data, source):
            found.setdefault(source, []).append(reply)
    return found


async def fetch_limited(client: httpx2.AsyncClient, url: str) -> bytes | None:
    """GET a small document from the LAN; anything larger than the cap is dropped."""
    try:
        async with client.stream("GET", url) as response:
            if response.status_code != httpx2.codes.OK:
                return None
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    return None
            return bytes(body)
    except (httpx2.HTTPError, OSError, ValueError):
        return None


def _local_name(tag: str) -> str:
    return tag.rpartition("}")[2]


def parse_upnp_description(body: bytes) -> Upnp | None:
    """Model details from a UPnP device description (XML with DTDs and entities refused)."""
    upper = body.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        return None
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None
    device = next((e for e in root.iter() if _local_name(e.tag) == "device"), None)
    if device is None:
        return None
    values = {_local_name(child.tag): (child.text or "").strip() for child in device}
    return Upnp(
        friendly_name=values.get("friendlyName") or None,
        manufacturer=values.get("manufacturer") or None,
        model_name=values.get("modelName") or None,
        model_number=values.get("modelNumber") or None,
        device_type=values.get("deviceType") or None,
    )


# --- Samsung --------------------------------------------------------------------------------


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def parse_samsung_info(payload: Any) -> Samsung | None:
    """The TV's answer to ``GET /api/v2/``. The Tizen version is worked out from the model year."""
    device = payload.get("device") if isinstance(payload, dict) else None
    if not isinstance(device, dict):
        return None
    firmware = _text(device.get("firmwareVersion"))
    code = _text(device.get("model"))
    operating_system = _text(device.get("OS"))
    year_match = _MODEL_CODE_YEAR.match(code or "")
    year = 2000 + int(year_match.group(1)) if year_match else None
    tizen = TIZEN_BY_YEAR.get(year) if year and operating_system == "Tizen" else None
    return Samsung(
        name=_text(device.get("name")),
        model_name=_text(device.get("modelName")),
        model_code=code,
        firmware=None if firmware is None or firmware.lower() == "unknown" else firmware,
        os=operating_system,
        device_type=_text(device.get("type")),
        api_version=_text(payload.get("version")),
        release_year=year if tizen else None,
        tizen_version=tizen,
    )


async def fetch_samsung(client: httpx2.AsyncClient, ip: str) -> Samsung | None:
    body = await fetch_limited(client, f"http://{ip}:{SAMSUNG_PORT}/api/v2/")
    if body is None:
        return None
    try:
        return parse_samsung_info(json.loads(body))
    except ValueError:
        return None
