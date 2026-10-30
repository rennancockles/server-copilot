"""A minimal IPP client: just the operations needed to print a document and follow the job.

IPP is HTTP POST of a small binary message (RFC 8010) followed, for Print-Job, by the document.
Only what the printing tools use is implemented: requests carry integers, enums, keywords, names
and mime types; responses are decoded to plain Python values (collections are skipped).
"""

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Self
from urllib.parse import urlsplit

import httpx2

IPP_VERSION = (2, 0)
HEADER_SIZE = 8
INT_SIZE = 4
FIRST_VALUE_TAG = 0x10
DEFAULT_TIMEOUT = 15.0
CONNECT_TIMEOUT = 5.0
MAX_RESPONSE_BYTES = 1024 * 1024
USER_NAME = "server-copilot"

# Delimiter and value tags (RFC 8010, section 3.5.1 and 3.5.2).
OPERATION_GROUP = 0x01
JOB_GROUP = 0x02
END_GROUP = 0x03
PRINTER_GROUP = 0x04
TAG_INTEGER = 0x21
TAG_BOOLEAN = 0x22
TAG_ENUM = 0x23
TAG_NAME = 0x42
TAG_KEYWORD = 0x44
TAG_URI = 0x45
TAG_CHARSET = 0x47
TAG_LANGUAGE = 0x48
TAG_MIME = 0x49
TAG_BEGIN_COLLECTION = 0x34
TAG_MEMBER_NAME = 0x4A
TAG_END_COLLECTION = 0x37
_TEXT_TAGS = frozenset({0x41, 0x42, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49})

STATUS_SUCCESS_LIMIT = 0x0100
STATUS_NAMES = {
    0x0400: "the request was malformed",
    0x0401: "the printer refused the request (forbidden)",
    0x0402: "the printer needs authentication",
    0x0403: "the printer refused the request (not possible)",
    0x0404: "the request timed out",
    0x0405: "the printer does not know that object",
    0x0406: "that object is gone",
    0x0407: "the printer does not accept that kind of request",
    0x0408: "the request was too long",
    0x0409: "the address is too long",
    0x040A: "the printer does not accept this document format",
    0x040B: "the printer does not support one of the print options",
    0x040C: "the printer cannot reach the document",
    0x040D: "the printer could not read the document",
    0x040E: "the job is already completed or canceled",
    0x0500: "the printer reported an internal error",
    0x0501: "the printer does not support that operation",
    0x0502: "the printer is temporarily unavailable",
    0x0503: "the printer is not accepting jobs right now",
    0x0504: "the printer is busy",
    0x0505: "the job was canceled",
    0x0506: "the printer is not accepting jobs",
    0x0507: "the printer is busy",
    0x0508: "the printer does not support multiple documents",
}


class Operation(IntEnum):
    PRINT_JOB = 0x0002
    GET_JOB_ATTRIBUTES = 0x0009
    GET_JOBS = 0x000A
    GET_PRINTER_ATTRIBUTES = 0x000B


class IppError(Exception):
    """The printer cannot be reached or refused the request; safe to show to the MCP client."""


class IppUnreachableError(IppError):
    """Nothing answered at the address: wrong host, powered off, or the connection timed out."""


@dataclass
class IppResponse:
    status: int
    groups: list[tuple[int, dict[str, list[Any]]]] = field(default_factory=list)

    def group(self, tag: int) -> dict[str, list[Any]]:
        return next((attrs for group_tag, attrs in self.groups if group_tag == tag), {})

    def groups_of(self, tag: int) -> list[dict[str, list[Any]]]:
        return [attrs for group_tag, attrs in self.groups if group_tag == tag]


def printer_http_url(printer_url: str) -> str:
    """The HTTP address an ``ipp://host[:port]/path`` printer URI is spoken to on."""
    parts = urlsplit(printer_url.strip())
    if parts.scheme not in ("ipp", "http") or not parts.hostname:
        raise ValueError(
            f"Invalid printer URL {printer_url!r}; expected ipp://host:631/ipp/print "
            "(ipps:// is not supported)."
        )
    port = parts.port or 631
    path = parts.path or "/"
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"http://{host}:{port}{path}"


def printer_uri(printer_url: str) -> str:
    """The ``printer-uri`` operation attribute: the same address with the ipp scheme."""
    return "ipp" + printer_http_url(printer_url).removeprefix("http")


def _attribute(tag: int, name: str, value: bytes) -> bytes:
    encoded = name.encode()
    return struct.pack(">BH", tag, len(encoded)) + encoded + struct.pack(">H", len(value)) + value


def _value(tag: int, value: Any) -> bytes:
    if tag in (TAG_INTEGER, TAG_ENUM):
        return struct.pack(">i", int(value))
    if tag == TAG_BOOLEAN:
        return b"\x01" if value else b"\x00"
    return str(value).encode()


def _encode_group(group: int, attributes: list[tuple[int, str, Any]]) -> bytes:
    out = bytes([group])
    for tag, name, value in attributes:
        values = value if isinstance(value, list) else [value]
        for index, single in enumerate(values):
            out += _attribute(tag, name if index == 0 else "", _value(tag, single))
    return out


def encode_request(
    operation: Operation,
    request_id: int,
    operation_attributes: list[tuple[int, str, Any]],
    job_attributes: list[tuple[int, str, Any]] | None = None,
) -> bytes:
    header = struct.pack(">BBHI", *IPP_VERSION, int(operation), request_id)
    base = [
        (TAG_CHARSET, "attributes-charset", "utf-8"),
        (TAG_LANGUAGE, "attributes-natural-language", "en"),
    ]
    body = _encode_group(OPERATION_GROUP, base + operation_attributes)
    if job_attributes:
        body += _encode_group(JOB_GROUP, job_attributes)
    return header + body + bytes([END_GROUP])


def _decode_value(tag: int, raw: bytes) -> Any:
    if tag in (TAG_INTEGER, TAG_ENUM) and len(raw) == INT_SIZE:
        return struct.unpack(">i", raw)[0]
    if tag == TAG_BOOLEAN and len(raw) == 1:
        return raw != b"\x00"
    if tag in _TEXT_TAGS:
        return raw.decode("utf-8", errors="replace")
    return None


def _read_attribute(data: bytes, position: int) -> tuple[str, bytes, int]:
    """Read ``name-length name value-length value`` at position; return (name, value, next)."""
    (name_length,) = struct.unpack_from(">H", data, position)
    position += 2
    name = data[position : position + name_length].decode("utf-8", errors="replace")
    position += name_length
    (value_length,) = struct.unpack_from(">H", data, position)
    position += 2
    return name, data[position : position + value_length], position + value_length


def decode_response(data: bytes) -> IppResponse:
    if len(data) < HEADER_SIZE + 1:
        raise IppError("The printer sent back something that is not an IPP response.")
    response = IppResponse(status=struct.unpack(">H", data[2:4])[0])
    position = HEADER_SIZE
    current: dict[str, list[Any]] | None = None
    last_name: str | None = None
    depth = 0
    try:
        while position < len(data) and data[position] != END_GROUP:
            tag = data[position]
            position += 1
            if tag < FIRST_VALUE_TAG:
                current = {}
                response.groups.append((tag, current))
                last_name = None
                continue
            name, raw, position = _read_attribute(data, position)
            if tag == TAG_BEGIN_COLLECTION:
                if depth == 0 and name:
                    last_name = name
                depth += 1
            elif tag == TAG_END_COLLECTION:
                depth = max(0, depth - 1)
            elif not depth and tag != TAG_MEMBER_NAME and current is not None:
                last_name = name or last_name
                value = _decode_value(tag, raw)
                if last_name and value is not None:
                    current.setdefault(last_name, []).append(value)
    except struct.error as exc:
        raise IppError("The printer sent back a truncated IPP response.") from exc
    return response


class IppClient:
    def __init__(
        self,
        printer_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.http_url = printer_http_url(printer_url)
        self.printer_uri = printer_uri(printer_url)
        self.host = urlsplit(self.http_url).hostname or ""
        self._request_id = 0
        self._client = httpx2.AsyncClient(
            timeout=httpx2.Timeout(timeout, connect=CONNECT_TIMEOUT),
            transport=transport,
            trust_env=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _operation_attributes(
        self, extra: list[tuple[int, str, Any]]
    ) -> list[tuple[int, str, Any]]:
        return [
            (TAG_URI, "printer-uri", self.printer_uri),
            (TAG_NAME, "requesting-user-name", USER_NAME),
            *extra,
        ]

    async def _send(self, payload: bytes, *, document: bytes = b"") -> IppResponse:
        try:
            response = await self._client.post(
                self.http_url,
                content=payload + document,
                headers={"Content-Type": "application/ipp"},
            )
        except httpx2.TimeoutException as exc:
            raise IppUnreachableError(
                f"Timed out talking to the printer at {self.http_url}."
            ) from exc
        except httpx2.TransportError as exc:
            raise IppUnreachableError(
                f"Cannot reach the printer at {self.http_url}: {exc}. Check that it is on, on "
                "the network, and that the address has not changed."
            ) from exc
        if not response.is_success:
            raise IppError(f"The printer answered HTTP {response.status_code} at {self.http_url}.")
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise IppError("The printer sent back an unexpectedly large response.")
        return decode_response(response.content)

    def _checked(self, response: IppResponse) -> IppResponse:
        if response.status >= STATUS_SUCCESS_LIMIT:
            reason = STATUS_NAMES.get(response.status, "the printer refused the request")
            raise IppError(f"IPP error 0x{response.status:04x}: {reason}.")
        return response

    async def _call(
        self,
        operation: Operation,
        operation_attributes: list[tuple[int, str, Any]],
        job_attributes: list[tuple[int, str, Any]] | None = None,
        document: bytes = b"",
    ) -> IppResponse:
        self._request_id += 1
        payload = encode_request(
            operation,
            self._request_id,
            self._operation_attributes(operation_attributes),
            job_attributes,
        )
        return self._checked(await self._send(payload, document=document))

    async def get_printer_attributes(self, requested: list[str]) -> dict[str, list[Any]]:
        response = await self._call(
            Operation.GET_PRINTER_ATTRIBUTES, [(TAG_KEYWORD, "requested-attributes", requested)]
        )
        return response.group(PRINTER_GROUP)

    async def get_jobs(
        self, requested: list[str], *, which: str, limit: int
    ) -> list[dict[str, list[Any]]]:
        response = await self._call(
            Operation.GET_JOBS,
            [
                (TAG_KEYWORD, "which-jobs", which),
                (TAG_INTEGER, "limit", limit),
                (TAG_KEYWORD, "requested-attributes", requested),
            ],
        )
        return response.groups_of(JOB_GROUP)

    async def get_job(self, job_id: int, requested: list[str]) -> dict[str, list[Any]]:
        response = await self._call(
            Operation.GET_JOB_ATTRIBUTES,
            [
                (TAG_INTEGER, "job-id", job_id),
                (TAG_KEYWORD, "requested-attributes", requested),
            ],
        )
        return response.group(JOB_GROUP)

    async def print_job(
        self,
        document: bytes,
        *,
        document_format: str,
        job_name: str,
        job_attributes: list[tuple[int, str, Any]],
    ) -> dict[str, list[Any]]:
        response = await self._call(
            Operation.PRINT_JOB,
            [
                (TAG_NAME, "job-name", job_name),
                (TAG_MIME, "document-format", document_format),
            ],
            job_attributes,
            document,
        )
        return response.group(JOB_GROUP)
