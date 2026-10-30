"""Printing on the home printer over IPP, always in two steps: plan, then print.

``plan_print`` takes the file ChatGPT was given (or asks for a test page), checks it against the
printer and the limits below, keeps it in memory and returns a single-use code. ``print_document``
sends only what a plan held, so nothing is printed that the user has not seen summarized.
"""

import asyncio
import io
import ipaddress
import logging
import re
import secrets
import socket
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from server_copilot.ipp import (
    TAG_ENUM,
    TAG_INTEGER,
    TAG_KEYWORD,
    IppClient,
    IppError,
    IppUnreachableError,
    Operation,
)

# Under uvicorn.error so these lines reach the container log: nothing else configures logging.
logger = logging.getLogger("uvicorn.error.printing")

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PAGES = 20
MAX_COPIES = 5
CODE_TTL_SECONDS = 300
MAX_PENDING = 5
DOWNLOAD_TIMEOUT_SECONDS = 30.0
MAX_JOB_NAME_LENGTH = 80
QUEUE_LIMIT = 20
MAX_ADDRESS_LENGTH = 255
PRINT_JOB = int(Operation.PRINT_JOB)
RECENT_LIMIT = 5
TEST_PAGE_NAME = "Server Copilot test page"

PDF = "application/pdf"
JPEG = "image/jpeg"
PNG = "image/png"
SIGNATURES = ((b"%PDF-", PDF), (b"\xff\xd8\xff", JPEG), (b"\x89PNG\r\n\x1a\n", PNG))
FORMAT_NAMES = {PDF: "PDF", JPEG: "JPEG", PNG: "PNG"}

PRINTER_STATES = {3: "idle", 4: "processing", 5: "stopped"}
# job-state (RFC 8011) -> (state name, outcome)
Outcome = Literal["queued", "held", "printing", "stopped", "completed", "failed", "unknown"]
JOB_STATES: dict[int, tuple[str, Outcome]] = {
    3: ("pending", "queued"),
    4: ("pending-held", "held"),
    5: ("processing", "printing"),
    6: ("processing-stopped", "stopped"),
    7: ("canceled", "failed"),
    8: ("aborted", "failed"),
    9: ("completed", "completed"),
}
FINISHED_OUTCOMES = frozenset({"completed", "failed"})
LANDSCAPE = 4
IPP_PORT = 631
PROBE_PATHS = ("/ipp/print", "/ipp", "/")
PROBE_ATTEMPTS_HINT = "discover_network_devices lists printers with port 631 open"
PRINTER_HELP = (
    "The printer's address on the home network, such as 192.168.1.94, as found by "
    "discover_network_devices (a printer has port 631 open). The server checks that it answers "
    "as a printer."
)
INK_NOTE = ["Ink levels are the printer's own estimate; a missing level means none is reported."]
MEDIA_EXAMPLES = 12
MAX_PERCENT = 100

PRINTER_ATTRIBUTES = [
    "operations-supported",
    "printer-make-and-model",
    "printer-state",
    "printer-state-reasons",
    "printer-is-accepting-jobs",
    "document-format-supported",
    "media-supported",
    "media-ready",
    "print-color-mode-supported",
    "marker-names",
    "marker-levels",
]
JOB_ATTRIBUTES = [
    "job-id",
    "job-name",
    "job-state",
    "job-state-reasons",
    "job-originating-user-name",
    "job-impressions-completed",
]

_REASON_SUFFIX = re.compile(r"-(error|warning|report)$")
REASON_HINTS = {
    "media-empty": "The printer is out of paper.",
    "media-needed": "The printer needs paper.",
    "media-jam": "There is a paper jam.",
    "marker-supply-low": "Ink is running low.",
    "marker-supply-empty": "A cartridge or ink tank is empty.",
    "marker-waste-full": "The maintenance box is full.",
    "cover-open": "A cover is open.",
    "door-open": "A door is open.",
    "offline": "The printer reports it is offline.",
    "toner-low": "Ink is running low.",
    "toner-empty": "A cartridge or ink tank is empty.",
}
# Reasons that only describe a busy or resting printer, not a problem.
BENIGN_REASONS = frozenset({"none", "moving-to-paused", "paused", "spool-area-full"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class PrintError(ToolError):
    """A refusal whose message is safe to show to the MCP client."""


class FileInput(BaseModel):
    """A file ChatGPT hands to the tool: a temporary download link plus its id."""

    download_url: str
    file_id: str
    mime_type: str | None = None
    file_name: str | None = None


class Ink(BaseModel):
    name: str
    level_percent: int | None


class PrintJob(BaseModel):
    job_id: int | None
    name: str | None
    state: str
    outcome: Outcome
    finished: bool
    state_reasons: list[str]
    user: str | None
    pages_completed: int | None


class PrinterStatus(BaseModel):
    address: str
    model: str | None
    state: str
    ready: bool
    accepting_jobs: bool | None
    state_reasons: list[str]
    problems: list[str]
    ink: list[Ink]
    paper: list[str]
    document_formats: list[str]
    queue: list[PrintJob]
    recent_jobs: list[PrintJob]
    notes: list[str]


class PrintPlan(BaseModel):
    address: str
    file_name: str
    document_format: str
    size_bytes: int
    pages: int
    copies: int
    color: bool
    paper: str | None
    landscape: bool
    printer: str | None
    warnings: list[str]
    summary: str
    confirmation_code: str
    expires_at: str
    expires_in_seconds: int


class PrintSubmitted(BaseModel):
    address: str
    job: PrintJob
    summary: str
    message: str


class PrintJobStatus(BaseModel):
    address: str
    job: PrintJob
    message: str


@dataclass
class PendingPrint:
    printer_url: str
    document: bytes
    document_format: str
    job_name: str
    copies: int
    color_mode: str | None
    media: str | None
    landscape: bool
    summary: str
    expires: float


def _now() -> float:
    return time.time()


def _iso_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="seconds")


def _clean_name(name: str | None, fallback: str) -> str:
    cleaned = _CONTROL.sub(" ", name or "").strip()[:MAX_JOB_NAME_LENGTH].strip()
    return cleaned or fallback


class PendingPrints:
    """Single-use plans: a code is valid once, for a few minutes, and only in this process."""

    def __init__(self, ttl: int = CODE_TTL_SECONDS, limit: int = MAX_PENDING) -> None:
        self._ttl = ttl
        self._limit = limit
        self._plans: dict[str, PendingPrint] = {}

    def _purge(self, now: float) -> None:
        self._plans = {c: p for c, p in self._plans.items() if p.expires > now}

    def add(self, plan: PendingPrint, now: float) -> str:
        self._purge(now)
        while len(self._plans) >= self._limit:
            del self._plans[next(iter(self._plans))]
        plan.expires = now + self._ttl
        code = secrets.token_urlsafe(12)
        self._plans[code] = plan
        return code

    def redeem(self, code: str, now: float) -> PendingPrint:
        self._purge(now)
        plan = self._plans.pop(code.strip(), None)
        if plan is None:
            raise PrintError(
                "That confirmation code is not valid: it is wrong, expired (five minutes) or "
                "already used. Ask plan_print again."
            )
        return plan


# --- finding the printer ----------------------------------------------------------------------


def _resolve_host(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, IPP_PORT, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _is_lan_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.partition("%")[0])
    return ip.is_private and not (
        ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved
    )


def parse_address(value: str) -> tuple[str, list[str]]:
    """Split what the caller gave (an IP, a name, or an ipp:// URL) into host and IPP paths."""
    text = value.strip()
    if (
        not text
        or len(text) > MAX_ADDRESS_LENGTH
        or not text.isprintable()
        or any(c.isspace() for c in text)
    ):
        raise PrintError("printer must be the printer's address, for example 192.168.1.94.")
    try:
        parts = urlsplit(text if "://" in text else f"ipp://{text}")
        port = parts.port
    except ValueError as exc:
        raise PrintError("printer must be an IP address or name, optionally with :631.") from exc
    if (
        parts.scheme != "ipp"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or port not in (None, IPP_PORT)
    ):
        raise PrintError(
            f"printer must be an IP address or name on port {IPP_PORT} (ipp://host/path is fine)."
        )
    paths = [parts.path] if parts.path not in ("", "/") else list(PROBE_PATHS)
    return parts.hostname, paths


def _is_printer(attributes: dict[str, list[Any]]) -> bool:
    operations = attributes.get("operations-supported")
    return "printer-state" in attributes and (not operations or PRINT_JOB in operations)


async def connect_printer(
    address: str,
    transport: httpx2.AsyncBaseTransport | None = None,
    resolve: Callable[[str], list[str]] = _resolve_host,
) -> tuple[IppClient, dict[str, list[Any]]]:
    """Check that the address is a printer on the LAN and return a client for it.

    The address comes from the caller, so it is never trusted: it must resolve only to private
    LAN addresses, and the host must answer IPP as a printer that takes Print-Job. The client
    talks to the resolved IP, so a name cannot change its meaning afterwards.
    """
    host, paths = parse_address(address)
    try:
        addresses = await asyncio.to_thread(resolve, host)
    except OSError as exc:
        raise PrintError(
            f"{host!r} could not be resolved; give the printer's IP address "
            f"({PROBE_ATTEMPTS_HINT})."
        ) from exc
    if not addresses or not all(_is_lan_address(a) for a in addresses):
        raise PrintError("The printer must be on the home network (a private address); refused.")
    ip = addresses[0].partition("%")[0]
    shown = f"[{ip}]" if ":" in ip else ip
    for path in paths:
        client = IppClient(f"ipp://{shown}:{IPP_PORT}{path}", transport=transport)
        try:
            attributes = await client.get_printer_attributes(PRINTER_ATTRIBUTES)
        except IppUnreachableError as exc:
            await client.aclose()
            raise PrintError(str(exc)) from exc
        except IppError:
            await client.aclose()
            continue
        if _is_printer(attributes):
            return client, attributes
        await client.aclose()
    raise PrintError(
        f"{ip} did not answer as a printer (IPP on port {IPP_PORT}); it is not one, or it does not "
        f"print over IPP. {PROBE_ATTEMPTS_HINT.capitalize()}."
    )


# --- the file ---------------------------------------------------------------------------------


def _resolve(host: str) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


def _check_download_url(url: str) -> str:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or port not in (None, 443)
    ):
        raise PrintError("The file link must be a plain https address.")
    return parts.hostname


async def fetch_document(
    url: str,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    resolve: Callable[[str], list[str]] = _resolve,
) -> bytes:
    """Download the file from its temporary link, refusing anything that is not public https."""
    host = _check_download_url(url)
    try:
        addresses = await asyncio.to_thread(resolve, host)
    except OSError as exc:
        raise PrintError("The file link's host could not be resolved.") from exc
    if not addresses or not all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses):
        raise PrintError("The file link does not point to a public address; refused.")
    buffer = bytearray()
    try:
        async with (
            httpx2.AsyncClient(
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            ) as client,
            client.stream("GET", url) as response,
        ):
            if not response.is_success:
                raise PrintError(f"Downloading the file failed (HTTP {response.status_code}).")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_FILE_BYTES:
                raise PrintError(_too_large())
            async for chunk in response.aiter_bytes():
                buffer += chunk
                if len(buffer) > MAX_FILE_BYTES:
                    raise PrintError(_too_large())
    except httpx2.TimeoutException as exc:
        raise PrintError("Downloading the file timed out.") from exc
    except httpx2.TransportError as exc:
        raise PrintError("The file could not be downloaded.") from exc
    return bytes(buffer)


def _too_large() -> str:
    return f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB; it will not be printed."


def detect_format(data: bytes) -> str:
    for signature, mime in SIGNATURES:
        if data.startswith(signature):
            return mime
    raise PrintError("Only PDF and JPEG files can be printed; this file is neither.")


def count_pages(data: bytes, document_format: str) -> int:
    if document_format != PDF:
        return 1
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise PrintError("The PDF is password protected; it cannot be printed.")
        return len(reader.pages)
    except (PyPdfError, ValueError, OSError, RecursionError) as exc:
        raise PrintError("The PDF could not be read; it will not be printed.") from exc


def build_test_page(printer: str | None, moment: datetime) -> bytes:
    """A one-page PDF: a title, the time, and a swatch per ink so a clogged nozzle shows."""
    lines = [
        (36, 790, "Server Copilot - test page"),
        (14, 760, f"Printer: {printer or 'unknown'}"),
        (14, 740, f"Sent: {moment.astimezone(UTC).isoformat(timespec='seconds')}"),
        (14, 720, "If every swatch below is solid and clear, the printer is healthy."),
    ]
    text = "".join(
        f"BT /F1 {size} Tf 56 {y} Td ({_pdf_text(value)}) Tj ET\n" for size, y, value in lines
    )
    swatches = [
        (0, 0, 0, 0),
        (1, 0, 0, 0),
        (0, 1, 0, 0),
        (0, 0, 1, 0),
        (1, 1, 0, 0),
        (1, 0, 1, 0),
        (0, 1, 1, 0),
        (0, 0, 0, 1),
    ]
    boxes = "".join(
        f"{c} {m} {y} {k} k {56 + index * 62} 600 56 56 re f\n"
        for index, (c, m, y, k) in enumerate(swatches)
    )
    content = (text + boxes).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 5 0 R "
            b"/Resources << /Font << /F1 4 0 R >> >> >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def _pdf_text(value: str) -> str:
    ascii_only = value.encode("ascii", "replace").decode()
    return ascii_only.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


# --- the printer ------------------------------------------------------------------------------


def _first(attributes: dict[str, list[Any]], name: str) -> Any:
    values = attributes.get(name)
    return values[0] if values else None


def _reasons(attributes: dict[str, list[Any]]) -> list[str]:
    return [r for r in attributes.get("printer-state-reasons", []) if r != "none"]


def _problems(reasons: list[str]) -> list[str]:
    found: list[str] = []
    for reason in reasons:
        key = _REASON_SUFFIX.sub("", reason)
        if key in BENIGN_REASONS:
            continue
        hint = REASON_HINTS.get(key, f"The printer reports: {reason}.")
        if hint not in found:
            found.append(hint)
    return found


def _ink(attributes: dict[str, list[Any]]) -> list[Ink]:
    names = attributes.get("marker-names", [])
    levels = attributes.get("marker-levels", [])
    return [
        Ink(name=name, level_percent=level if 0 <= level <= MAX_PERCENT else None)
        for name, level in zip(names, levels, strict=False)
    ]


def _job(attributes: dict[str, list[Any]]) -> PrintJob:
    state, outcome = JOB_STATES.get(_first(attributes, "job-state"), ("unknown", "unknown"))
    reasons = [r for r in attributes.get("job-state-reasons", []) if r != "none"]
    return PrintJob(
        job_id=_first(attributes, "job-id"),
        name=_first(attributes, "job-name"),
        state=state,
        outcome=outcome,
        finished=outcome in FINISHED_OUTCOMES,
        state_reasons=reasons,
        user=_first(attributes, "job-originating-user-name"),
        pages_completed=_first(attributes, "job-impressions-completed"),
    )


async def fetch_status(
    printer: IppClient, attributes: dict[str, list[Any]], address: str
) -> PrinterStatus:
    try:
        queue = await printer.get_jobs(JOB_ATTRIBUTES, which="not-completed", limit=QUEUE_LIMIT)
        recent = await printer.get_jobs(JOB_ATTRIBUTES, which="completed", limit=RECENT_LIMIT)
    except IppError as exc:
        raise PrintError(str(exc)) from exc
    reasons = _reasons(attributes)
    state = PRINTER_STATES.get(_first(attributes, "printer-state"), "unknown")
    accepting = _first(attributes, "printer-is-accepting-jobs")
    problems = _problems(reasons)
    return PrinterStatus(
        address=address,
        model=_first(attributes, "printer-make-and-model"),
        state=state,
        ready=state != "stopped" and accepting is not False,
        accepting_jobs=accepting,
        state_reasons=reasons,
        problems=problems,
        ink=_ink(attributes),
        paper=attributes.get("media-ready", []),
        document_formats=attributes.get("document-format-supported", []),
        queue=[_job(a) for a in queue],
        recent_jobs=[_job(a) for a in recent],
        notes=INK_NOTE if attributes.get("marker-names") else [],
    )


def pick_media(wanted: str | None, supported: list[str]) -> str | None:
    """Map 'a4' or 'letter' to the printer's own media name; None means the printer's default."""
    if not wanted:
        return None
    name = wanted.strip().lower()
    if not supported or name in supported:
        return name if name in supported else wanted.strip()
    prefixes = (f"iso_{name}_", f"na_{name}_")
    matches = sorted(m for m in supported if m.startswith(prefixes))
    if matches:
        return matches[0]
    examples = ", ".join(sorted(supported)[:MEDIA_EXAMPLES])
    raise PrintError(f"The printer does not list {wanted!r} as a paper size. It lists: {examples}.")


def _color_mode(color: bool, supported: list[str]) -> str | None:
    mode = "color" if color else "monochrome"
    return mode if not supported or mode in supported else None


def _require_ready(status_attributes: dict[str, list[Any]]) -> list[str]:
    state = PRINTER_STATES.get(_first(status_attributes, "printer-state"), "unknown")
    problems = _problems(_reasons(status_attributes))
    if state == "stopped" or _first(status_attributes, "printer-is-accepting-jobs") is False:
        detail = " ".join(problems) or "No reason was given."
        raise PrintError(f"The printer is not accepting jobs right now. {detail}")
    return problems


def _job_attributes(pending: PendingPrint) -> list[tuple[int, str, Any]]:
    attributes: list[tuple[int, str, Any]] = [(TAG_INTEGER, "copies", pending.copies)]
    if pending.media:
        attributes.append((TAG_KEYWORD, "media", pending.media))
    if pending.color_mode:
        attributes.append((TAG_KEYWORD, "print-color-mode", pending.color_mode))
    if pending.landscape:
        attributes.append((TAG_ENUM, "orientation-requested", LANDSCAPE))
    return attributes


def _summary(  # noqa: PLR0913, PLR0917
    name: str, pages: int, copies: int, color: bool, media: str | None, landscape: bool
) -> str:
    parts = [
        f"{name!r}",
        f"{pages} page{'s' if pages != 1 else ''}",
        f"{copies} cop{'ies' if copies != 1 else 'y'}",
        "color" if color else "black and white",
        f"paper {media}" if media else "the printer's default paper",
        "landscape" if landscape else "portrait",
    ]
    return ", ".join(parts)


async def build_plan(  # noqa: PLR0913, PLR0917
    client: IppClient,
    attributes: dict[str, list[Any]],
    pending_plans: PendingPrints,
    fetch: Callable[[str], Awaitable[bytes]],
    file: FileInput | None,
    test_page: bool,
    copies: int,
    color: bool,
    paper: str | None,
    landscape: bool,
) -> PrintPlan:
    if (file is None) == (not test_page):
        raise PrintError("Give either a file or test_page=true, not both and not neither.")
    now = _now()
    warnings = _require_ready(attributes)
    document, name = await _load_document(file, attributes, fetch, now)
    document_format = detect_format(document)
    supported = attributes.get("document-format-supported", [])
    if supported and document_format not in supported:
        raise PrintError(
            f"The printer does not take {FORMAT_NAMES[document_format]} directly "
            f"(it takes: {', '.join(supported)}). Convert the file to PDF or JPEG."
        )
    pages = await asyncio.to_thread(count_pages, document, document_format)
    if pages > MAX_PAGES:
        raise PrintError(f"The document has {pages} pages; the limit is {MAX_PAGES}.")
    media = pick_media(paper, attributes.get("media-supported", []))
    mode = _color_mode(color, attributes.get("print-color-mode-supported", []))
    if mode is None:
        warnings.append("The printer does not list that color mode; it will use its default.")
    model = _first(attributes, "printer-make-and-model") or "the printer"
    summary = (
        f"{_summary(name, pages, copies, color, media, landscape)}, on {model} ({client.host})"
    )
    pending = PendingPrint(
        printer_url=client.printer_uri,
        document=document,
        document_format=document_format,
        job_name=name,
        copies=copies,
        color_mode=mode,
        media=media,
        landscape=landscape,
        summary=summary,
        expires=0,
    )
    code = pending_plans.add(pending, now)
    logger.info("print planned: %s", summary)
    return PrintPlan(
        address=client.host,
        file_name=name,
        document_format=document_format,
        size_bytes=len(document),
        pages=pages,
        copies=copies,
        color=color,
        paper=media,
        landscape=landscape,
        printer=_first(attributes, "printer-make-and-model"),
        warnings=warnings,
        summary=summary,
        confirmation_code=code,
        expires_at=_iso_epoch(pending.expires),
        expires_in_seconds=CODE_TTL_SECONDS,
    )


async def send_plan(client: IppClient, plan: PendingPrint) -> PrintSubmitted:
    try:
        attributes = await client.print_job(
            plan.document,
            document_format=plan.document_format,
            job_name=plan.job_name,
            job_attributes=_job_attributes(plan),
        )
    except IppError as exc:
        logger.warning("print refused: %s: %s", plan.summary, exc)
        raise PrintError(
            f"{exc} Nothing was confirmed as printed; check get_printer_status and ask "
            "plan_print again if you want to retry."
        ) from exc
    job = _job(attributes)
    logger.info("print sent: job=%s %s", job.job_id, plan.summary)
    return PrintSubmitted(
        address=client.host,
        job=job,
        summary=plan.summary,
        message=(
            "Sent to the printer. Follow it with "
            f"get_print_job(printer={client.host!r}, job_id={job.job_id}) until finished is true."
        ),
    )


async def fetch_job(client: IppClient, job_id: int) -> PrintJobStatus:
    try:
        attributes = await client.get_job(job_id, JOB_ATTRIBUTES)
    except IppError as exc:
        raise PrintError(str(exc)) from exc
    if not attributes:
        raise PrintError(f"The printer has no job {job_id}; it may be too old to remember.")
    job = _job(attributes)
    return PrintJobStatus(address=client.host, job=job, message=_job_message(job))


# --- the tools --------------------------------------------------------------------------------


def register_print_tools(
    mcp: MCPServer,
    fetch: Callable[[str], Awaitable[bytes]] = fetch_document,
    transport: httpx2.AsyncBaseTransport | None = None,
    resolve: Callable[[str], list[str]] = _resolve_host,
) -> None:
    pending_plans = PendingPrints()

    @asynccontextmanager
    async def open_printer(
        address: str,
    ) -> AsyncGenerator[tuple[IppClient, dict[str, list[Any]]]]:
        client, attributes = await connect_printer(address, transport, resolve)
        try:
            yield client, attributes
        finally:
            await client.aclose()

    @mcp.tool(
        name="get_printer_status",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def get_printer_status(
        printer: Annotated[str, Field(description=PRINTER_HELP)],
    ) -> PrinterStatus:
        """Say whether a printer on the home network is ready, and what is in its queue.

        Read-only; nothing is printed. Give the printer's address, found with
        discover_network_devices: the server checks that it really answers as a printer and
        refuses anything that is not on the home network. Reports the state (idle, processing,
        stopped), anything that needs attention (out of paper, low ink, a jam, a cover open), the
        ink levels and paper the printer reports, the paper sizes and document formats it takes,
        the jobs waiting or printing, and the last few finished ones.
        """
        async with open_printer(printer) as (client, attributes):
            return await fetch_status(client, attributes, client.host)

    @mcp.tool(
        name="plan_print",
        annotations=ToolAnnotations(
            read_only_hint=True, idempotent_hint=False, open_world_hint=True
        ),
        meta={"openai/fileParams": ["file"]},
    )
    async def plan_print(  # noqa: PLR0913, PLR0917
        printer: Annotated[str, Field(description=PRINTER_HELP)],
        file: Annotated[
            FileInput | None,
            Field(description="The PDF or JPEG file the user attached. Omit for a test page."),
        ] = None,
        test_page: Annotated[
            bool, Field(description="Print the built-in test page instead of a file.")
        ] = False,
        copies: Annotated[int, Field(ge=1, le=MAX_COPIES, description="How many copies.")] = 1,
        color: Annotated[bool, Field(description="False prints in black and white.")] = True,
        paper: Annotated[
            str | None,
            Field(
                description="Paper size such as 'a4', 'a5', 'letter', or the printer's own name. "
                "Omit for the printer's default."
            ),
        ] = None,
        landscape: Annotated[bool, Field(description="Print sideways.")] = False,
    ) -> PrintPlan:
        """Step 1 of printing: check the file and the printer, and get a confirmation code.

        Nothing is printed. Give the printer's address (find it with discover_network_devices;
        the server checks that it really is a printer on the home network), and the file the user
        attached, or set test_page to print a built-in test page. The file must be a PDF or JPEG
        of at most 10 MB and 20 pages, up to 5 copies. The server downloads it, checks it and
        keeps it for five minutes. Show the user the `summary` (it names the printer) and
        `warnings` and print only after they agree: pass the `confirmation_code` to
        print_document. The code works once; to print again, plan again.
        """
        async with open_printer(printer) as (client, attributes):
            return await build_plan(
                client,
                attributes,
                pending_plans,
                fetch,
                file,
                test_page,
                copies,
                color,
                paper,
                landscape,
            )

    @mcp.tool(
        name="print_document",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    async def print_document(
        confirmation_code: Annotated[str, Field(description="The code returned by plan_print.")],
    ) -> PrintSubmitted:
        """Step 2: send the planned document to the printer it was planned for. Uses paper and ink.

        Call it only after the user has seen the plan from plan_print and agreed. It prints
        exactly what the plan held, on the printer the plan checked. The code works once: if this
        call fails, ask for a new plan rather than guessing whether something printed, and check
        get_printer_status first. The result carries the printer's job id and its state at that
        moment; follow it with get_print_job until `finished` is true.
        """
        plan = pending_plans.redeem(confirmation_code, _now())
        async with IppClient(plan.printer_url, transport=transport) as client:
            return await send_plan(client, plan)

    @mcp.tool(
        name="get_print_job",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def get_print_job(
        printer: Annotated[str, Field(description=PRINTER_HELP)],
        job_id: Annotated[int, Field(ge=1, description="The id returned by print_document.")],
    ) -> PrintJobStatus:
        """Check a print job: queued, printing, completed or failed. Read-only.

        Give the same printer address the job was sent to. `finished` with outcome `completed`
        means the printer reports the job done; `failed` means it was canceled or aborted (see
        state_reasons). The printer forgets old jobs, so a job that finished a while ago may be
        gone.
        """
        async with open_printer(printer) as (client, _):
            return await fetch_job(client, job_id)


def _job_message(job: PrintJob) -> str:
    messages = {
        "queued": "Waiting in the printer's queue.",
        "held": "Held: the printer is waiting for something (paper, a cover, an operator).",
        "printing": "Printing now.",
        "stopped": "Stopped partway; the printer needs attention.",
        "completed": "The printer reports the job completed.",
        "failed": "The job did not print (canceled or aborted).",
        "unknown": "The printer did not report a state.",
    }
    return messages[job.outcome]


async def _load_document(
    file: FileInput | None,
    attributes: dict[str, list[Any]],
    fetch: Callable[[str], Awaitable[bytes]],
    now: float,
) -> tuple[bytes, str]:
    if file is None:
        model = _first(attributes, "printer-make-and-model")
        return build_test_page(model, datetime.fromtimestamp(now, tz=UTC)), TEST_PAGE_NAME
    document = await fetch(file.download_url)
    if not document:
        raise PrintError("The file is empty.")
    return document, _clean_name(file.file_name, "document")
