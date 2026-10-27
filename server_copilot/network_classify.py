"""Best-guess device types. Everything here is an inference and is labelled as one."""

from collections.abc import Callable

from server_copilot.network_snapshot import Classification, Confidence, Device, DeviceType

PRINTER_PORTS = {631, 9100, 515}
CAST_PORTS = {8008, 8009}
CAMERA_PORTS = {554}
COMPUTER_PORTS = {22, 139, 445, 3389, 5900}
LG_TV_PORTS = {3000, 3001}
IOS_PORT = 62078
ROKU_PORT = 8060

PRINTER_VENDORS = ("brother", "lexmark", "kyocera", "ricoh", "xerox")
TV_VENDORS = ("samsung", "lg elec", "sony", "tcl", "hisense", "philips", "panasonic", "vizio")
CAMERA_VENDORS = ("hikvision", "dahua", "reolink", "amcrest")
NAS_VENDORS = ("synology", "qnap", "asustor")
SMART_HOME_VENDORS = ("espressif", "tuya", "allterco", "itead", "signify", "shelly")

PRINTER_SERVICES = ("_ipp", "_printer", "_pdl-datastream")
CAST_SERVICES = ("_googlecast", "_airplay", "_raop")
SMART_HOME_SERVICES = ("_hap", "_matter")
COMPUTER_SERVICES = ("_workstation", "_smb", "_ssh")

Rule = Callable[[Device], Classification | None]


def _verdict(kind: DeviceType, confidence: Confidence, *evidence: str) -> Classification | None:
    if not evidence:
        return None
    return Classification(device_type=kind, confidence=confidence, evidence=list(evidence))


def _ports(device: Device) -> set[int]:
    return {p.port for p in device.ports}


def _service_types(device: Device) -> list[str]:
    return [s.type for s in device.services]


def _announces(device: Device, prefixes: tuple[str, ...]) -> bool:
    return any(t.startswith(prefixes) for t in _service_types(device))


def _makers(device: Device) -> str:
    manufacturer = device.upnp.manufacturer if device.upnp else None
    return f"{device.vendor or ''} {manufacturer or ''}".lower()


def _vendor_in(device: Device, names: tuple[str, ...]) -> str | None:
    makers = _makers(device)
    return next((n for n in names if n in makers), None)


def _is_media_renderer(device: Device) -> bool:
    return bool(device.upnp and "mediarenderer" in (device.upnp.device_type or "").lower())


def _router(device: Device) -> Classification | None:
    if device.is_gateway:
        return _verdict("router", "high", "It is this host's default gateway.")
    return None


def _printer(device: Device) -> Classification | None:
    evidence = []
    if found := sorted(_ports(device) & PRINTER_PORTS):
        evidence.append(f"Printing ports are open ({', '.join(map(str, found))}).")
    if _announces(device, PRINTER_SERVICES):
        evidence.append("It announces a printing service over mDNS.")
    if device.upnp and "printer" in (device.upnp.device_type or "").lower():
        evidence.append("Its UPnP description says it is a printer.")
    if evidence:
        return _verdict("printer", "high", *evidence)
    if vendor := _vendor_in(device, PRINTER_VENDORS):
        return _verdict(
            "printer", "medium", f"The MAC prefix belongs to {vendor}, a printer maker."
        )
    return None


def _tv(device: Device) -> Classification | None:
    if device.samsung:
        kind = device.samsung.device_type or "a TV"
        return _verdict("tv", "high", f"It answered the Samsung TV API as {kind}.")
    maker = _vendor_in(device, TV_VENDORS)
    if maker and _is_media_renderer(device):
        return _verdict("tv", "medium", f"A {maker} UPnP media renderer.")
    if maker == "lg elec" and _ports(device) & LG_TV_PORTS:
        return _verdict("tv", "medium", "An LG device with the webOS control ports open.")
    return None


def _media_player(device: Device) -> Classification | None:
    evidence = []
    if ROKU_PORT in _ports(device):
        evidence.append("The Roku control port is open.")
    if _ports(device) & CAST_PORTS:
        evidence.append("The Google Cast ports are open.")
    if _announces(device, CAST_SERVICES):
        evidence.append("It announces a cast or AirPlay service over mDNS.")
    if _is_media_renderer(device):
        evidence.append("It is a UPnP media renderer.")
    return _verdict("media_player", "medium", *evidence)


def _camera(device: Device) -> Classification | None:
    evidence = []
    if _ports(device) & CAMERA_PORTS or _announces(device, ("_rtsp",)):
        evidence.append("It serves a video stream (RTSP).")
    if vendor := _vendor_in(device, CAMERA_VENDORS):
        evidence.append(f"The MAC prefix belongs to {vendor}, a camera maker.")
    return _verdict("camera", "medium", *evidence)


def _nas(device: Device) -> Classification | None:
    if vendor := _vendor_in(device, NAS_VENDORS):
        return _verdict("nas", "medium", f"The MAC prefix belongs to {vendor}, a NAS maker.")
    return None


def _smart_home(device: Device) -> Classification | None:
    evidence = []
    if vendor := _vendor_in(device, SMART_HOME_VENDORS):
        evidence.append(f"The MAC prefix belongs to {vendor}, a smart-home chip or device maker.")
    if _announces(device, SMART_HOME_SERVICES):
        evidence.append("It announces a HomeKit or Matter service.")
    return _verdict("smart_home", "medium", *evidence)


def _phone(device: Device) -> Classification | None:
    if IOS_PORT in _ports(device):
        return _verdict("phone_or_tablet", "high", "The iOS sync service port is open.")
    if device.mac_randomized and not device.ports and not device.services:
        return _verdict(
            "phone_or_tablet", "low", "A randomized MAC address with nothing open, as phones use."
        )
    return None


def _computer(device: Device) -> Classification | None:
    evidence = []
    if found := sorted(_ports(device) & COMPUTER_PORTS):
        evidence.append(
            f"Remote-access or file-sharing ports are open ({', '.join(map(str, found))})."
        )
    if _announces(device, COMPUTER_SERVICES):
        evidence.append("It announces a workstation, SSH or SMB service over mDNS.")
    return _verdict("computer", "low", *evidence)


RULES: list[Rule] = [
    _router,
    _printer,
    _tv,
    _media_player,
    _camera,
    _nas,
    _smart_home,
    _phone,
    _computer,
]


def classify(device: Device) -> Classification:
    """The first rule that matches wins; the evidence says why."""
    for rule in RULES:
        if verdict := rule(device):
            return verdict
    return Classification(device_type="unknown", confidence="low", evidence=[])
