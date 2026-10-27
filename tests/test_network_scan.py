import asyncio
import ipaddress
import json
import logging
import socket
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server_copilot import network_scan
from server_copilot.config import Settings
from server_copilot.network_probe import MdnsHost, SsdpReply
from server_copilot.network_scan import (
    History,
    HistoryEntry,
    Interface,
    Observations,
    ScanNetwork,
    build_device,
    compute_findings,
    device_key,
    load_history,
    local_interfaces,
    offline_devices,
    parse_cidrs,
    run_scan,
    select_networks,
    update_history,
)
from server_copilot.network_snapshot import (
    Classification,
    Device,
    Port,
    Samsung,
    Service,
    Upnp,
    read_snapshot,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
SAMSUNG_MAC = "f4:7b:5e:00:00:01"
RANDOM_MAC = "da:a1:19:00:00:01"


def eth0(ip: str = "192.168.1.10", mask: str = "255.255.255.0") -> Interface:
    return Interface(name="eth0", ip=ip, netmask=mask)


def device(ip: str = "192.168.1.20", mac: str | None = "aa:bb:cc:00:00:20", **kw: Any) -> Device:
    return Device(ip=ip, mac=mac, first_seen=NOW, last_seen=NOW, **kw)


def ports(*numbers: int) -> list[Port]:
    return [Port(port=n) for n in numbers]


def service(kind: str) -> Service:
    return Service(type=kind, name="x", port=None, source="mdns")


# --- which networks are scanned -------------------------------------------------------------


def test_cidrs_must_be_private_and_not_too_large() -> None:
    assert parse_cidrs("") == []
    assert [str(n) for n in parse_cidrs("192.168.1.0/24, 10.0.4.7/24 ,192.168.1.0/24")] == [
        "192.168.1.0/24",
        "10.0.4.0/24",
    ]
    assert str(parse_cidrs("10.0.0.0/22")[0]) == "10.0.0.0/22"


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("8.8.8.0/24", "not a private"),
        ("0.0.0.0/0", "not a private"),
        ("100.64.0.0/24", "not a private"),
        ("172.32.0.0/24", "not a private"),
        ("10.0.0.0/16", "too large"),
        ("192.168.0.0/16", "too large"),
        ("not-a-network", "Invalid network"),
        ("192.168.1.0/24,fe80::/64", "Invalid network"),
    ],
)
def test_a_cidr_outside_the_lan_is_refused(spec: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_cidrs(spec)


def test_the_subnets_of_the_physical_interfaces_are_found_without_assuming_one() -> None:
    interfaces = [
        Interface("lo", "127.0.0.1", "255.0.0.0"),
        eth0("10.20.30.5", "255.255.255.0"),
        Interface("docker0", "172.17.0.1", "255.255.0.0"),
        Interface("br-1a2b3c", "172.18.0.1", "255.255.0.0"),
        Interface("veth12", "172.19.0.1", "255.255.0.0"),
        Interface("tailscale0", "100.100.1.1", "255.192.0.0"),
        Interface("wlan0", "203.0.113.9", "255.255.255.0"),
    ]

    networks, notes = select_networks("", interfaces)

    assert [(str(n.network), n.interface, n.local_ip, n.source) for n in networks] == [
        ("10.20.30.0/24", "eth0", "10.20.30.5", "auto")
    ]
    assert notes == []


def test_a_large_subnet_is_cut_to_a_slash_24_and_says_so() -> None:
    networks, notes = select_networks("", [eth0("192.168.7.33", "255.255.0.0")])

    assert str(networks[0].network) == "192.168.7.0/24"
    assert "only 192.168.7.0/24 is scanned" in notes[0]


def test_two_interfaces_on_one_subnet_scan_it_once() -> None:
    wifi = Interface("wlan0", "192.168.1.11", "255.255.255.0")

    networks, _ = select_networks("", [eth0(), wifi])

    assert len(networks) == 1


def test_no_lan_interface_is_explained() -> None:
    networks, notes = select_networks("", [Interface("lo", "127.0.0.1", "255.0.0.0")])

    assert networks == []
    assert "network_mode: host" in notes[0]


def test_configured_networks_replace_detection() -> None:
    interfaces = [eth0("192.168.1.10"), Interface("eth1", "10.0.0.2", "255.255.255.0")]

    networks, notes = select_networks("10.0.0.0/24,172.16.5.0/24", interfaces)

    assert [(str(n.network), n.interface, n.local_ip, n.source) for n in networks] == [
        ("10.0.0.0/24", "eth1", "10.0.0.2", "configured"),
        ("172.16.5.0/24", None, None, "configured"),
    ]
    assert len(notes) == 1
    assert "no interface in 172.16.5.0/24" in notes[0]


def test_psutil_interfaces_that_are_down_or_not_ipv4_are_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def address(family: int, ip: str, mask: str | None) -> Any:
        return SimpleNamespace(family=family, address=ip, netmask=mask)

    monkeypatch.setattr(
        network_scan.psutil,
        "net_if_stats",
        lambda: {"eth0": SimpleNamespace(isup=True), "eth1": SimpleNamespace(isup=False)},
    )
    monkeypatch.setattr(
        network_scan.psutil,
        "net_if_addrs",
        lambda: {
            "eth0": [
                address(socket.AF_INET, "192.168.1.10", "255.255.255.0"),
                address(socket.AF_INET6, "fe80::1", "ffff:ffff:ffff:ffff::"),
                address(socket.AF_INET, "192.168.1.99", None),
            ],
            "eth1": [address(socket.AF_INET, "10.0.0.2", "255.255.255.0")],
            "ghost": [address(socket.AF_INET, "10.1.1.1", "255.255.255.0")],
        },
    )

    assert local_interfaces() == [eth0()]


# --- turning observations into devices ------------------------------------------------------


def test_a_device_gets_its_maker_from_the_mac_prefix() -> None:
    obs = Observations(neighbours={"192.168.1.60": SAMSUNG_MAC})

    built = build_device("192.168.1.60", obs, NOW)

    assert built.mac == SAMSUNG_MAC
    assert built.vendor is not None
    assert "Samsung" in built.vendor
    assert built.mac_randomized is False
    assert built.discovered_via == ["arp"]


def test_a_randomized_mac_has_no_maker_and_is_flagged() -> None:
    built = build_device("192.168.1.61", Observations(neighbours={"192.168.1.61": RANDOM_MAC}), NOW)

    assert built.mac_randomized is True
    assert built.vendor is None


def test_an_unregistered_mac_prefix_has_no_maker() -> None:
    built = build_device(
        "192.168.1.62", Observations(neighbours={"192.168.1.62": "fc:ff:ff:00:00:01"}), NOW
    )

    assert built.vendor is None
    assert built.mac_randomized is False


def test_a_device_seen_only_by_multicast_has_no_mac() -> None:
    obs = Observations(mdns={"192.168.1.63": MdnsHost(hostname="lamp.local", names=["Lamp"])})

    built = build_device("192.168.1.63", obs, NOW)

    assert (built.mac, built.vendor, built.mac_randomized) == (None, None, False)
    assert built.discovered_via == ["mdns"]


def test_names_prefer_what_the_device_says_over_dns() -> None:
    ip = "192.168.1.64"
    obs = Observations(
        neighbours={ip: SAMSUNG_MAC},
        mdns={ip: MdnsHost(hostname="tv.local", names=["Living room"])},
        dns={ip: "tv.lan"},
        upnp={ip: Upnp(friendly_name="UPnP name")},
        samsung={ip: Samsung(name="[TV] Samsung 7 Series")},
        ports={ip: [8001, 80]},
        gateway=ip,
    )

    built = build_device(ip, obs, NOW)

    assert (built.hostname, built.hostname_source) == ("tv.local", "mdns")
    assert built.name == "[TV] Samsung 7 Series"
    assert built.is_gateway is True
    assert [(p.port, p.service) for p in built.ports] == [(8001, "samsung-api"), (80, "http")]
    assert built.discovered_via == ["arp", "mdns", "tcp", "dns"]


def test_reverse_dns_is_the_fallback_hostname() -> None:
    ip = "192.168.1.65"

    built = build_device(ip, Observations(neighbours={ip: SAMSUNG_MAC}, dns={ip: "nas.lan"}), NOW)

    assert (built.hostname, built.hostname_source) == ("nas.lan", "dns")


# --- guessing the type ----------------------------------------------------------------------

TV_UPNP = Upnp(
    manufacturer="LG Electronics", device_type="urn:schemas-upnp-org:device:MediaRenderer:1"
)


@pytest.mark.parametrize(
    ("kind", "confidence", "fields"),
    [
        ("router", "high", {"is_gateway": True, "ports": ports(631)}),
        ("printer", "high", {"ports": ports(9100)}),
        ("printer", "high", {"services": [service("_ipp._tcp")]}),
        ("printer", "high", {"upnp": Upnp(device_type="urn:schemas-upnp-org:device:Printer:1")}),
        ("printer", "medium", {"vendor": "Brother industries, LTD."}),
        ("tv", "high", {"samsung": Samsung(device_type="Samsung SmartTV")}),
        ("tv", "medium", {"upnp": TV_UPNP}),
        ("tv", "medium", {"vendor": "LG Electronics", "ports": ports(3000)}),
        ("media_player", "medium", {"ports": ports(8060)}),
        ("media_player", "medium", {"ports": ports(8009)}),
        ("media_player", "medium", {"services": [service("_googlecast._tcp")]}),
        ("media_player", "medium", {"upnp": Upnp(device_type="MediaRenderer")}),
        ("camera", "medium", {"ports": ports(554)}),
        ("camera", "medium", {"vendor": "Hangzhou Hikvision Digital"}),
        ("nas", "medium", {"vendor": "Synology Incorporated"}),
        ("smart_home", "medium", {"vendor": "Espressif Inc."}),
        ("smart_home", "medium", {"services": [service("_hap._tcp")]}),
        ("phone_or_tablet", "high", {"ports": ports(62078)}),
        ("phone_or_tablet", "low", {"mac_randomized": True}),
        ("computer", "low", {"ports": ports(22)}),
        ("computer", "low", {"services": [service("_smb._tcp")]}),
        ("unknown", "low", {}),
        ("unknown", "low", {"ports": ports(80)}),
        ("unknown", "low", {"mac_randomized": True, "ports": ports(80)}),
    ],
)
def test_the_type_is_a_guess_with_a_confidence_and_a_reason(
    kind: str, confidence: str, fields: dict[str, Any]
) -> None:
    classified = network_scan.classify(device(**fields))

    assert (classified.device_type, classified.confidence) == (kind, confidence)
    assert bool(classified.evidence) is (kind != "unknown")


def test_a_samsung_phone_is_not_taken_for_a_tv_without_the_tv_api() -> None:
    classified = network_scan.classify(device(vendor="Samsung Electronics Co.,Ltd"))

    assert classified.device_type == "unknown"


def test_a_built_device_is_classified() -> None:
    ip = "192.168.1.66"
    obs = Observations(
        neighbours={ip: SAMSUNG_MAC}, samsung={ip: Samsung(device_type="Samsung SmartTV")}
    )

    assert build_device(ip, obs, NOW).classification.device_type == "tv"


# --- history and findings -------------------------------------------------------------------


def history(created_days_ago: float = 10) -> History:
    return History(created_at=NOW - timedelta(days=created_days_ago))


def test_what_is_seen_during_the_first_day_is_the_baseline_not_new() -> None:
    fresh = history(created_days_ago=0)
    seen = [device(ip="192.168.1.20"), device(ip="192.168.1.21", mac="aa:bb:cc:00:00:21")]

    update_history(fresh, seen, NOW)
    update_history(
        fresh, [*seen, device(ip="192.168.1.22", mac="aa:bb:cc:00:00:22")], NOW + timedelta(hours=3)
    )

    assert not any(d.is_new for d in seen)
    assert fresh.learning_until == NOW + timedelta(hours=24)


def test_a_device_that_appears_after_the_baseline_is_new_for_a_week() -> None:
    known = history()
    update_history(known, [device()], NOW - timedelta(days=9))
    newcomer = device(ip="192.168.1.99", mac="aa:bb:cc:00:00:99")

    update_history(known, [device(), newcomer], NOW)

    assert newcomer.is_new is True
    assert newcomer.first_seen == NOW

    later = device(ip="192.168.1.99", mac="aa:bb:cc:00:00:99")
    update_history(known, [device(), later], NOW + timedelta(days=8))

    assert later.is_new is False
    assert later.first_seen == NOW


def test_first_seen_survives_an_ip_change() -> None:
    known = history()
    update_history(known, [device(ip="192.168.1.20")], NOW - timedelta(days=5))

    moved = device(ip="192.168.1.30")
    update_history(known, [moved], NOW)

    assert moved.first_seen == NOW - timedelta(days=5)
    assert len(known.entries) == 1
    assert known.entries["aa:bb:cc:00:00:20"].ip == "192.168.1.30"


def test_devices_without_a_mac_are_tracked_by_address() -> None:
    known = history()
    nameless = device(ip="10.0.5.5", mac=None)

    update_history(known, [nameless], NOW)

    assert device_key(nameless) == "ip:10.0.5.5"
    assert "ip:10.0.5.5" in known.entries


def test_devices_missing_from_the_scan_are_listed_as_offline_until_forgotten() -> None:
    known = history(created_days_ago=200)
    printer = device(ip="192.168.1.50", mac="aa:bb:cc:00:00:50", name="Printer")
    printer.classification = Classification(device_type="printer", confidence="high")
    ancient = device(ip="192.168.1.51", mac="aa:bb:cc:00:00:51")
    update_history(known, [printer], NOW - timedelta(days=2))
    update_history(known, [ancient], NOW - timedelta(days=100))
    update_history(known, [device()], NOW)

    assert "aa:bb:cc:00:00:51" not in known.entries
    gone = offline_devices(known, [device()])
    assert [(g.ip, g.name, g.device_type) for g in gone] == [("192.168.1.50", "Printer", "printer")]
    assert gone[0].last_seen == NOW - timedelta(days=2)


def test_the_history_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(network_scan, "MAX_HISTORY_ENTRIES", 3)
    known = history()
    seen = [device(ip=f"10.0.0.{n}", mac=f"aa:bb:cc:00:01:{n:02x}") for n in range(1, 8)]

    update_history(known, seen, NOW)

    assert len(known.entries) == 3


def test_a_new_unknown_device_is_a_warning_and_a_known_kind_is_info() -> None:
    known = history()
    camera = device(ip="192.168.1.80", mac="aa:bb:cc:00:00:80", ports=ports(554), vendor="Reolink")
    camera.classification = network_scan.classify(camera)
    mystery = device(ip="192.168.1.81", mac=RANDOM_MAC, mac_randomized=True, ports=ports(80))
    mystery.classification = network_scan.classify(mystery)
    update_history(known, [], NOW - timedelta(days=5))
    update_history(known, [camera, mystery], NOW)

    findings = [
        f for f in compute_findings(known, [camera, mystery], NOW) if f.kind == "new_device"
    ]

    by_ip = {f.ip: f for f in findings}
    assert by_ip["192.168.1.80"].severity == "info"
    assert "Reolink camera" in by_ip["192.168.1.80"].detail
    assert by_ip["192.168.1.81"].severity == "warning"
    assert "randomized" in by_ip["192.168.1.81"].detail


def test_no_new_device_findings_while_learning() -> None:
    fresh = history(created_days_ago=0)
    seen = [device()]

    update_history(fresh, seen, NOW)

    assert compute_findings(fresh, seen, NOW) == []


def test_one_mac_on_several_addresses_is_flagged() -> None:
    twins = [
        device(ip="192.168.1.20"),
        device(ip="192.168.1.21"),
        device(ip="192.168.1.22", mac="aa:bb:cc:00:00:22"),
    ]

    findings = compute_findings(history(), twins, NOW)

    assert [(f.kind, f.severity, f.mac) for f in findings] == [
        ("duplicate_mac", "warning", "aa:bb:cc:00:00:20")
    ]
    assert "192.168.1.20, 192.168.1.21" in findings[0].detail


def test_risky_open_ports_are_flagged_per_device() -> None:
    exposed = device(ip="192.168.1.90", ports=ports(23, 80, 2375))

    quiet = device(ip="192.168.1.91", mac="aa:bb:cc:00:00:91", ports=ports(80, 443))

    findings = compute_findings(history(), [exposed, quiet], NOW)

    assert [(f.kind, f.ip) for f in findings] == [("risky_ports", "192.168.1.90")]
    assert "Telnet" in findings[0].detail
    assert "Docker API" in findings[0].detail
    assert "port 80" not in findings[0].detail


def test_a_changed_gateway_mac_is_an_alert_for_a_week() -> None:
    known = history(created_days_ago=30)
    router = device(ip="192.168.1.1", mac="aa:bb:cc:00:00:01", is_gateway=True)
    update_history(known, [router], NOW - timedelta(days=30))
    replaced = device(ip="192.168.1.1", mac="aa:bb:cc:00:00:02", is_gateway=True)
    changed_at = NOW - timedelta(days=2)

    update_history(known, [replaced], changed_at)
    update_history(known, [replaced], NOW)

    def alerts(at: datetime) -> list[tuple[str, str, str | None]]:
        return [
            (f.kind, f.severity, f.ip)
            for f in compute_findings(known, [replaced], at)
            if f.kind == "gateway_mac_changed"
        ]

    assert alerts(NOW) == [("gateway_mac_changed", "alert", "192.168.1.1")]
    assert alerts(changed_at + timedelta(days=7)) != []
    assert alerts(changed_at + timedelta(days=7, hours=1)) == []


def test_an_unchanged_gateway_raises_nothing() -> None:
    known = history(created_days_ago=30)
    router = device(ip="192.168.1.1", mac="aa:bb:cc:00:00:01", is_gateway=True)

    update_history(known, [router], NOW - timedelta(days=30))
    update_history(known, [router], NOW)

    assert compute_findings(known, [router], NOW) == []


def test_a_missing_history_starts_a_baseline(tmp_path: Path) -> None:
    loaded, note = load_history(tmp_path / "history.json", NOW)

    assert (loaded.created_at, loaded.entries, note) == (NOW, {}, None)


@pytest.mark.parametrize("content", ["", "nope", "[]", '{"created_at": "yesterday"}'])
def test_a_broken_history_restarts_the_baseline_and_says_so(tmp_path: Path, content: str) -> None:
    path = tmp_path / "history.json"
    path.write_text(content)

    loaded, note = load_history(path, NOW)

    assert loaded.entries == {}
    assert note is not None
    assert "reset" in note


def test_history_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    known = history()
    known.entries["k"] = HistoryEntry(first_seen=NOW, last_seen=NOW, ip="10.0.0.1")
    network_scan.save_history(path, known)

    loaded, note = load_history(path, NOW)

    assert note is None
    assert loaded.entries["k"].ip == "10.0.0.1"


# --- observing ------------------------------------------------------------------------------


def lan() -> list[ScanNetwork]:
    return [ScanNetwork(ipaddress.IPv4Network("192.168.1.0/24"), "eth0", "192.168.1.10", "auto")]


ARP_TABLE = """IP address       HW type     Flags       HW address            Mask     Device
192.168.1.1      0x1         0x2         aa:bb:cc:00:00:01     *        eth0
192.168.1.10     0x1         0x2         aa:bb:cc:00:00:10     *        eth0
192.168.1.50     0x1         0x2         aa:bb:cc:00:00:50     *        eth0
172.17.0.2       0x1         0x2         aa:bb:cc:00:00:99     *        docker0
"""
ROUTE_TABLE = "Iface\tDestination\tGateway\tFlags\neth0\t00000000\t0101A8C0\t0003\n"


class Seams:
    """Replaces everything that touches the network, recording what the scan asked for."""

    def __init__(self) -> None:
        self.woken: list[ipaddress.IPv4Address] = []
        self.probed: list[str] = []
        self.sources: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def wake(hosts: list[ipaddress.IPv4Address]) -> int:
            self.woken.extend(hosts)
            return len(hosts)

        async def mdns(local_ip: str) -> dict[str, MdnsHost]:
            self.sources.append(local_ip)
            return {
                "192.168.1.50": MdnsHost(
                    hostname="printer.local", names=["HL-L2350DW"], services=[service("_ipp._tcp")]
                ),
                "8.8.8.8": MdnsHost(hostname="elsewhere.example"),
            }

        async def ssdp(local_ip: str) -> dict[str, list[SsdpReply]]:
            reply = SsdpReply(
                "192.168.1.60", "http://192.168.1.60:9197/dmr", "Samsung UPnP", "ssdp:all"
            )
            return {"192.168.1.60": [reply], "10.9.9.9": [reply]}

        async def open_ports(ip: str, ports: list[int], gate: asyncio.Semaphore) -> list[int]:
            self.probed.append(ip)
            return {"192.168.1.50": [631, 9100], "192.168.1.60": [8001]}.get(ip, [])

        async def fetch_limited(client: Any, url: str) -> bytes | None:
            return b"<root><device><friendlyName>TV</friendlyName></device></root>"

        async def fetch_samsung(client: Any, ip: str) -> Samsung:
            return Samsung(name="[TV] Samsung", model_name="UE55RU7179")

        async def reverse_dns(ip: str, seconds: float) -> str | None:
            return "router.lan" if ip == "192.168.1.1" else None

        def read_proc(path: str) -> str:
            return ARP_TABLE if path == network_scan.PROC_ARP else ROUTE_TABLE

        monkeypatch.setattr(network_scan, "ARP_SETTLE_SECONDS", 0)
        for name, fake in {
            "wake_hosts": wake,
            "mdns_discover": mdns,
            "ssdp_discover": ssdp,
            "open_ports": open_ports,
            "fetch_limited": fetch_limited,
            "fetch_samsung": fetch_samsung,
            "reverse_dns": reverse_dns,
            "_read_proc": read_proc,
        }.items():
            monkeypatch.setattr(network_scan, name, fake)


async def test_observing_a_lan_combines_arp_multicast_ports_and_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seams = Seams()
    seams.install(monkeypatch)
    obs = Observations()

    await network_scan.observe(lan(), obs)

    assert len(seams.woken) == 253
    assert ipaddress.IPv4Address("192.168.1.10") not in seams.woken
    assert seams.sources == ["192.168.1.10"]
    assert obs.hosts_probed == {"192.168.1.0/24": 253}
    assert obs.neighbours == {
        "192.168.1.1": "aa:bb:cc:00:00:01",
        "192.168.1.50": "aa:bb:cc:00:00:50",
    }
    assert obs.gateway == "192.168.1.1"
    assert set(obs.mdns) == {"192.168.1.50"}
    assert set(obs.ssdp) == {"192.168.1.60"}
    assert sorted(seams.probed) == ["192.168.1.1", "192.168.1.50", "192.168.1.60"]
    assert obs.ports["192.168.1.50"] == [631, 9100]
    assert obs.upnp["192.168.1.60"].friendly_name == "TV"
    assert obs.upnp["192.168.1.60"].server == "Samsung UPnP"
    assert obs.samsung["192.168.1.60"].model_name == "UE55RU7179"
    assert obs.dns == {"192.168.1.1": "router.lan"}


def test_only_addresses_inside_the_scanned_networks_count() -> None:
    assert network_scan._in_networks("192.168.1.5", lan()) is True
    assert network_scan._in_networks("192.168.2.5", lan()) is False
    assert network_scan._in_networks("not-an-ip", lan()) is False


def test_proc_files_are_read_and_a_missing_one_is_just_empty(tmp_path: Path) -> None:
    table = tmp_path / "arp"
    table.write_text("IP address\n192.168.1.1 0x1 0x2 aa:bb:cc:00:00:01 * eth0\n")

    assert "192.168.1.1" in network_scan._read_proc(str(table))
    assert network_scan._read_proc(str(tmp_path / "missing")) == ""


async def test_a_gateway_outside_the_scanned_networks_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Seams().install(monkeypatch)
    monkeypatch.setattr(
        network_scan,
        "_read_proc",
        lambda path: (
            ARP_TABLE
            if path == network_scan.PROC_ARP
            else "Iface\tD\tG\tF\neth0\t00000000\t0100000A\t0003\n"
        ),
    )
    obs = Observations()

    await network_scan.observe(lan(), obs)

    assert obs.gateway is None


async def test_nothing_to_scan_observes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    seams = Seams()
    seams.install(monkeypatch)
    monkeypatch.setattr(network_scan, "_read_proc", lambda path: "")
    obs = Observations()

    await network_scan.observe([], obs)

    assert (obs.neighbours, obs.ports, seams.woken, seams.probed) == ({}, {}, [], [])


# --- one scan -------------------------------------------------------------------------------


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(network_scan_file=str(tmp_path / "scan" / "scan.json"), **overrides)


def fake_observe(populate: Callable[[Observations], None]) -> Any:
    async def observe(networks: list[ScanNetwork], obs: Observations) -> None:
        populate(obs)

    return observe


def home(obs: Observations) -> None:
    obs.neighbours = {"192.168.1.60": SAMSUNG_MAC, "192.168.1.1": "aa:bb:cc:00:00:01"}
    obs.gateway = "192.168.1.1"
    obs.samsung = {"192.168.1.60": Samsung(device_type="Samsung SmartTV", name="[TV] Samsung")}
    obs.ports = {"192.168.1.60": [8001]}
    obs.hosts_probed = {"192.168.1.0/24": 253}


async def test_a_scan_produces_a_snapshot_with_sorted_devices_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(network_scan, "local_interfaces", lambda: [eth0()])
    monkeypatch.setattr(network_scan, "observe", fake_observe(home))
    path = tmp_path / "history.json"

    snapshot = await run_scan(settings_for(tmp_path), path)

    assert [d.ip for d in snapshot.devices] == ["192.168.1.1", "192.168.1.60"]
    assert [d.classification.device_type for d in snapshot.devices] == ["router", "tv"]
    assert snapshot.scan.complete is True
    assert snapshot.scan.interval_seconds == 300
    assert [(n.cidr, n.interface, n.hosts_probed) for n in snapshot.scan.networks] == [
        ("192.168.1.0/24", "eth0", 253)
    ]
    assert snapshot.learning_until > snapshot.generated_at
    assert snapshot.findings == []
    assert path.exists()
    assert len(json.loads(path.read_text())["entries"]) == 2


async def test_a_second_scan_remembers_first_seen_and_lists_missing_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(network_scan, "local_interfaces", lambda: [eth0()])
    path = tmp_path / "history.json"
    monkeypatch.setattr(network_scan, "observe", fake_observe(home))
    first = await run_scan(settings_for(tmp_path), path)

    def only_the_router(obs: Observations) -> None:
        obs.neighbours = {"192.168.1.1": "aa:bb:cc:00:00:01"}
        obs.gateway = "192.168.1.1"

    monkeypatch.setattr(network_scan, "observe", fake_observe(only_the_router))
    second = await run_scan(settings_for(tmp_path), path)

    assert second.devices[0].first_seen == first.devices[0].first_seen
    assert [(o.ip, o.device_type) for o in second.offline_devices] == [("192.168.1.60", "tv")]


async def test_a_scan_that_runs_out_of_time_keeps_what_it_found_and_does_not_learn_from_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(network_scan, "local_interfaces", lambda: [eth0()])

    async def slow(networks: list[ScanNetwork], obs: Observations) -> None:
        home(obs)
        await asyncio.sleep(30)

    monkeypatch.setattr(network_scan, "observe", slow)
    path = tmp_path / "history.json"
    limited = settings_for(tmp_path).model_copy(update={"network_scan_timeout_seconds": 0.05})

    snapshot = await run_scan(limited, path)

    assert snapshot.scan.complete is False
    assert len(snapshot.devices) == 2
    assert snapshot.offline_devices == []
    assert any("limit" in note for note in snapshot.scan.notes)
    assert not path.exists()


async def test_an_unreadable_history_is_reported_in_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(network_scan, "local_interfaces", lambda: [eth0()])
    monkeypatch.setattr(network_scan, "observe", fake_observe(home))
    path = tmp_path / "history.json"
    path.write_text("garbage")

    snapshot = await run_scan(settings_for(tmp_path), path)

    assert any("history was unreadable" in note for note in snapshot.scan.notes)


async def test_scan_and_write_leaves_a_file_the_tools_can_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(network_scan, "local_interfaces", lambda: [eth0()])
    monkeypatch.setattr(network_scan, "observe", fake_observe(home))
    settings = settings_for(tmp_path)

    await network_scan.scan_and_write(settings)

    snapshot = read_snapshot(settings.network_scan_file)
    assert not isinstance(snapshot, str)
    assert len(snapshot.devices) == 2
    assert (tmp_path / "scan" / "network-history.json").exists()
    assert not (tmp_path / "scan" / ".scan.json.tmp").exists()


async def test_the_scanner_keeps_going_after_a_failed_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = 0
    enough = asyncio.Event()

    async def flaky(settings: Settings) -> None:
        nonlocal calls
        calls += 1
        if calls >= 3:
            enough.set()
        if calls == 1:
            raise RuntimeError("secret detail 192.168.1.50")

    monkeypatch.setattr(network_scan, "scan_and_write", flaky)
    settings = settings_for(tmp_path).model_copy(update={"network_scan_interval_seconds": 0.01})

    task = asyncio.create_task(network_scan.run_forever(settings))
    await asyncio.wait_for(enough.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "scan failed: RuntimeError" in caplog.text
    assert "192.168.1.50" not in caplog.text


def test_the_scanner_refuses_to_start_without_a_file_or_with_a_public_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SystemExit, match="NETWORK_SCAN_FILE"):
        network_scan.main()

    monkeypatch.setenv("SERVER_COPILOT_NETWORK_SCAN_FILE", "/tmp/scan.json")
    monkeypatch.setenv("SERVER_COPILOT_NETWORK_SCAN_CIDRS", "8.8.8.0/24")
    with pytest.raises(SystemExit, match="not a private"):
        network_scan.main()


def test_the_scanner_runs_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[Settings] = []

    def fake_run(coroutine: Any) -> None:
        coroutine.close()
        ran.append(Settings())

    monkeypatch.setenv("SERVER_COPILOT_NETWORK_SCAN_FILE", "/tmp/scan.json")
    monkeypatch.setattr(network_scan.asyncio, "run", fake_run)

    network_scan.main()

    assert len(ran) == 1
    for name in ("httpx", "httpx2", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
