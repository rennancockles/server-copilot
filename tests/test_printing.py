import io
import struct
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from PIL import Image
from pypdf import PdfReader, PdfWriter

from pwg_decoder import read_pages
from server_copilot import printing
from server_copilot.docker import DockerClient
from server_copilot.ipp import (
    JOB_GROUP,
    OPERATION_GROUP,
    PRINTER_GROUP,
    TAG_ENUM,
    TAG_INTEGER,
    TAG_KEYWORD,
    IppCertificateError,
    IppTlsError,
    IppUnreachableError,
    Operation,
    decode_response,
)
from server_copilot.printing import (
    PendingPrint,
    PendingPrints,
    PrintError,
    build_test_page,
    count_pages,
    detect_format,
    fetch_document,
    pick_media,
    register_print_tools,
)
from server_copilot.server import create_server

pytestmark = pytest.mark.unit

TAG_BOOLEAN = 0x22
ADDRESS = "192.168.1.94"
PLAN_URL = "ipp://192.168.1.94:631/ipp/print"
PRINTER_TOOLS = {"get_printer_status", "plan_print", "get_print_job"}
A4 = "iso_a4_210x297mm"
JPEG = b"\xff\xd8\xff\xe0 pretend this is a photo"
PNG = b"\x89PNG\r\n\x1a\n pretend"


def pdf(pages: int = 1, *, password: str | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    if password:
        writer.encrypt(password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# --- a fake Epson ----------------------------------------------------------------------------


def attribute(tag: int, name: str, value: bytes) -> bytes:
    encoded = name.encode()
    return struct.pack(">BH", tag, len(encoded)) + encoded + struct.pack(">H", len(value)) + value


def integer(name: str, value: int, tag: int = TAG_INTEGER) -> bytes:
    return attribute(tag, name, struct.pack(">i", value))


def keywords(name: str, values: list[str], tag: int = TAG_KEYWORD) -> bytes:
    return b"".join(
        attribute(tag, name if index == 0 else "", value.encode())
        for index, value in enumerate(values)
    )


def reply(status: int = 0, *groups: tuple[int, bytes]) -> httpx2.Response:
    body = b"".join(bytes([tag]) + content for tag, content in groups)
    content = struct.pack(">BBHI", 2, 0, status, 1) + body + b"\x03"
    return httpx2.Response(200, content=content)


def job_group(job_id: int, state: int, *, name: str = "report", reasons: str = "none") -> bytes:
    return (
        integer("job-id", job_id)
        + attribute(0x42, "job-name", name.encode())
        + integer("job-state", state, TAG_ENUM)
        + keywords("job-state-reasons", [reasons])
        + attribute(0x42, "job-originating-user-name", b"server-copilot")
        + integer("job-impressions-completed", 1)
    )


class FakePrinter:
    def __init__(self) -> None:
        self.state = 3
        self.accepting = True
        self.reasons = ["none"]
        self.formats = ["application/pdf", "image/jpeg", "image/urf"]
        self.media = [A4, "na_letter_8.5x11in", "iso_a6_105x148mm"]
        self.color_modes = ["color", "monochrome"]
        self.queue = [job_group(7, 5, name="someone else")]
        self.recent = [job_group(6, 9)]
        self.print_status = 0
        self.jobs: dict[int, bytes] = {42: job_group(42, 9)}
        self.requests: list[httpx2.Request] = []
        self.dpis: list[int] = []
        self.raster_types: list[str] = []

    def printer_group(self) -> bytes:
        return (
            integer("operations-supported", 2, TAG_ENUM)
            + integer("", 9, TAG_ENUM)
            + integer("", 10, TAG_ENUM)
            + integer("", 11, TAG_ENUM)
            + attribute(0x41, "printer-make-and-model", b"EPSON ET-2860 Series")
            + integer("printer-state", self.state, TAG_ENUM)
            + keywords("printer-state-reasons", self.reasons)
            + attribute(
                TAG_BOOLEAN, "printer-is-accepting-jobs", b"\x01" if self.accepting else b"\x00"
            )
            + keywords("document-format-supported", self.formats, 0x49)
            + keywords("media-supported", self.media)
            + keywords("media-ready", [A4])
            + keywords("print-color-mode-supported", self.color_modes)
            + keywords("marker-names", ["black ink", "cyan ink"], 0x42)
            + integer("marker-levels", 80)
            + integer("", -1)
            + self.raster_attributes()
        )

    def raster_attributes(self) -> bytes:
        resolutions = b"".join(
            attribute(0x32, "pwg-raster-document-resolution-supported" if index == 0 else "", value)
            for index, value in enumerate(struct.pack(">iib", dpi, dpi, 3) for dpi in self.dpis)
        )
        return resolutions + keywords("pwg-raster-document-type-supported", self.raster_types)

    def epson_like(self) -> None:
        """What the ET-2860 reports: no PDF or JPEG, only raster formats."""
        self.formats = [
            "application/octet-stream",
            "image/pwg-raster",
            "application/vnd.epson.escpr",
        ]
        self.dpis = [360, 720]
        self.raster_types = ["srgb_8", "sgray_8"]

    def __call__(self, request: httpx2.Request) -> httpx2.Response:  # noqa: PLR0911
        self.requests.append(request)
        operation = decode_response(request.content).status
        if operation == Operation.GET_PRINTER_ATTRIBUTES:
            return reply(0, (OPERATION_GROUP, b""), (PRINTER_GROUP, self.printer_group()))
        if operation == Operation.GET_JOBS:
            wanted = decode_response(request.content).group(OPERATION_GROUP)["which-jobs"][0]
            jobs = self.queue if wanted == "not-completed" else self.recent
            return reply(0, *((JOB_GROUP, job) for job in jobs))
        if operation == Operation.GET_JOB_ATTRIBUTES:
            job_id = decode_response(request.content).group(OPERATION_GROUP)["job-id"][0]
            if job_id not in self.jobs:
                return reply(0x0406, (OPERATION_GROUP, b""))
            return reply(0, (JOB_GROUP, self.jobs[job_id]))
        if operation == Operation.PRINT_JOB:
            if self.print_status:
                return reply(self.print_status, (OPERATION_GROUP, b""))
            return reply(0, (JOB_GROUP, job_group(42, 3)))
        return reply(0x0501)

    def prints(self) -> list[httpx2.Request]:
        return [
            r for r in self.requests if decode_response(r.content).status == Operation.PRINT_JOB
        ]


@pytest.fixture
def printer() -> FakePrinter:
    return FakePrinter()


def fetcher(files: dict[str, bytes]) -> Callable[[str], Any]:
    async def fetch(url: str) -> bytes:
        if url not in files:
            raise PrintError("The file could not be downloaded.")
        return files[url]

    return fetch


class Bound:
    """The MCP server with the printer's address filled in, as the assistant would pass it."""

    def __init__(self, mcp: MCPServer) -> None:
        self.mcp = mcp

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        arguments = dict(arguments or {})
        if name in PRINTER_TOOLS:
            arguments.setdefault("printer", ADDRESS)
        return await self.mcp.call_tool(name, arguments)

    async def list_tools(self) -> Any:
        return await self.mcp.list_tools()


def lan(host: str) -> list[str]:
    return [host]


def no_tls(_: str) -> str:
    raise IppTlsError("The printer did not complete a TLS handshake.")


async def nothing_advertised(_: str) -> list[tuple[str, str]]:
    return []


def make_server(
    printer: FakePrinter | Callable[[httpx2.Request], httpx2.Response],
    files: dict[str, bytes] | None = None,
    resolve: Callable[[str], list[str]] = lan,
    fetch_certificate: Callable[[str], str] = no_tls,
    discover: Callable[[str], Awaitable[list[tuple[str, str]]]] = nothing_advertised,
) -> Bound:
    mcp = MCPServer("test")
    register_print_tools(
        mcp,
        fetcher(files or {}),
        transport=httpx2.MockTransport(printer),
        resolve=resolve,
        fetch_certificate=fetch_certificate,
        discover=discover,
    )
    return Bound(mcp)


async def call(mcp: Bound, name: str, arguments: dict[str, Any] | None = None) -> Any:
    result = await mcp.call_tool(name, arguments or {})
    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    return result.structured_content


def file_argument(
    url: str = "https://files.example/doc", name: str = "report.pdf"
) -> dict[str, str]:
    return {
        "download_url": url,
        "file_id": "file_1",
        "mime_type": "application/pdf",
        "file_name": name,
    }


# --- confirmation codes ----------------------------------------------------------------------


def pending() -> PendingPrint:
    return PendingPrint(PLAN_URL, b"x", "application/pdf", "doc", 1, None, None, False, "doc", 0)


def test_a_code_works_once() -> None:
    plans = PendingPrints()
    code = plans.add(pending(), now=1000)

    assert plans.redeem(code, now=1001).job_name == "doc"
    with pytest.raises(PrintError, match="not valid"):
        plans.redeem(code, now=1002)


def test_a_code_expires_after_five_minutes() -> None:
    plans = PendingPrints()
    code = plans.add(pending(), now=1000)

    with pytest.raises(PrintError, match="expired"):
        plans.redeem(code, now=1000 + printing.CODE_TTL_SECONDS)


def test_a_code_nobody_issued_is_refused() -> None:
    with pytest.raises(PrintError, match="not valid"):
        PendingPrints().redeem("guess", now=0)


def test_the_oldest_plan_makes_room_for_a_new_one() -> None:
    plans = PendingPrints(limit=2)
    first, second, third = (plans.add(pending(), now=1000) for _ in range(3))

    with pytest.raises(PrintError):
        plans.redeem(first, now=1001)
    plans.redeem(second, now=1001)
    plans.redeem(third, now=1001)


# --- the file --------------------------------------------------------------------------------


def test_the_format_comes_from_the_bytes_not_from_the_name() -> None:
    assert detect_format(pdf()) == "application/pdf"
    assert detect_format(JPEG) == "image/jpeg"
    assert detect_format(PNG) == "image/png"
    with pytest.raises(PrintError, match="Only PDF and JPEG"):
        detect_format(b"MZ\x90\x00 an executable")


def test_pages_are_counted_and_unreadable_pdfs_refused() -> None:
    assert count_pages(pdf(3), "application/pdf") == 3
    assert count_pages(JPEG, "image/jpeg") == 1
    with pytest.raises(PrintError, match="password protected"):
        count_pages(pdf(password="secret"), "application/pdf")
    with pytest.raises(PrintError, match="could not be read"):
        count_pages(b"%PDF-1.4 truncated", "application/pdf")


def test_the_test_page_is_a_valid_one_page_pdf() -> None:
    data = build_test_page("EPSON (ET-2860) \\ Séries", datetime(2026, 10, 30, 12, tzinfo=UTC))

    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    assert "Server Copilot - test page" in text
    assert "EPSON (ET-2860)" in text
    assert "2026-10-30T12:00:00+00:00" in text


def test_media_names_are_matched_to_what_the_printer_lists() -> None:
    supported = [A4, "na_letter_8.5x11in", "iso_a6_105x148mm"]

    assert pick_media(None, supported) is None
    assert pick_media("a4", supported) == A4
    assert pick_media("Letter", supported) == "na_letter_8.5x11in"
    assert pick_media("iso_a6_105x148mm", supported) == "iso_a6_105x148mm"
    assert pick_media("a4", []) == "a4"
    with pytest.raises(PrintError, match="does not list 'a3'.*iso_a4_210x297mm"):
        pick_media("a3", supported)


def public(_: str) -> list[str]:
    return ["93.184.216.34"]


def download(
    handler: Callable[[httpx2.Request], httpx2.Response], url: str = "https://x.test/f"
) -> Any:
    return fetch_document(url, transport=httpx2.MockTransport(handler), resolve=public)


async def test_a_file_is_downloaded_from_its_link() -> None:
    assert await download(lambda _: httpx2.Response(200, content=pdf())) == pdf()


@pytest.mark.parametrize(
    "url",
    [
        "http://x.test/f",
        "ftp://x.test/f",
        "https://user:pass@x.test/f",
        "https://x.test:8443/f",
        "https:///f",
        "file:///etc/passwd",
    ],
)
async def test_only_plain_https_links_are_followed(url: str) -> None:
    with pytest.raises(PrintError, match="plain https"):
        await download(lambda _: httpx2.Response(200, content=b"x"), url)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.5", "192.168.1.94", "169.254.169.254", "::1", "fe80::1%eth0"]
)
async def test_links_to_private_addresses_are_refused(address: str) -> None:
    called: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        called.append(request)
        return httpx2.Response(200, content=b"x")

    with pytest.raises(PrintError, match="public address"):
        await fetch_document(
            "https://x.test/f", transport=httpx2.MockTransport(handler), resolve=lambda _: [address]
        )
    assert called == []


async def test_a_host_that_does_not_resolve_is_refused() -> None:
    def broken(_: str) -> list[str]:
        raise OSError("no such host")

    with pytest.raises(PrintError, match="could not be resolved"):
        await fetch_document("https://x.test/f", resolve=broken)


async def test_redirects_are_not_followed() -> None:
    redirect = httpx2.Response(302, headers={"Location": "http://169.254.169.254/"})

    with pytest.raises(PrintError, match="HTTP 302"):
        await download(lambda _: redirect)


async def test_a_file_that_is_too_large_is_refused_by_header_or_by_size() -> None:
    declared = httpx2.Response(
        200, headers={"Content-Length": str(printing.MAX_FILE_BYTES + 1)}, content=b"x"
    )
    streamed = httpx2.Response(200, content=b"x" * (printing.MAX_FILE_BYTES + 1))

    for response in (declared, streamed):
        with pytest.raises(PrintError, match="larger than 10 MB"):
            await download(lambda _, r=response: r)  # type: ignore[misc]


async def test_download_failures_do_not_leak_the_link() -> None:
    def refuse(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("boom https://x.test/f?signature=SECRET")

    def slow(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow")

    with pytest.raises(PrintError, match="could not be downloaded") as refused:
        await download(refuse)
    with pytest.raises(PrintError, match="timed out"):
        await download(slow)
    with pytest.raises(PrintError, match="HTTP 404"):
        await download(lambda _: httpx2.Response(404))
    assert "SECRET" not in str(refused.value)


# --- get_printer_status ----------------------------------------------------------------------


async def test_the_status_reports_state_ink_paper_and_the_queue(printer: FakePrinter) -> None:
    status = await call(make_server(printer), "get_printer_status")

    assert status["model"] == "EPSON ET-2860 Series"
    assert (status["state"], status["ready"], status["accepting_jobs"]) == ("idle", True, True)
    assert status["problems"] == []
    assert status["ink"] == [
        {"name": "black ink", "level_percent": 80},
        {"name": "cyan ink", "level_percent": None},
    ]
    assert status["paper"] == [A4]
    assert "application/pdf" in status["document_formats"]
    assert [(j["job_id"], j["outcome"]) for j in status["queue"]] == [(7, "printing")]
    assert [(j["job_id"], j["outcome"]) for j in status["recent_jobs"]] == [(6, "completed")]
    assert status["notes"]
    assert printer.prints() == []


async def test_the_status_says_what_needs_attention(printer: FakePrinter) -> None:
    printer.state = 5
    printer.reasons = ["media-empty-error", "marker-supply-low-warning", "paused"]

    status = await call(make_server(printer), "get_printer_status")

    assert (status["state"], status["ready"]) == ("stopped", False)
    assert status["problems"] == ["The printer is out of paper.", "Ink is running low."]


async def test_an_unreachable_printer_is_reported_after_one_attempt() -> None:
    seen: list[httpx2.Request] = []

    def down(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        raise httpx2.ConnectError("no route")

    with pytest.raises(ToolError, match="Cannot reach the printer"):
        await make_server(down).call_tool("get_printer_status")

    assert len(seen) == 1


# --- plan_print and print_document -----------------------------------------------------------


async def test_the_test_page_is_planned_then_printed_after_the_code(printer: FakePrinter) -> None:
    mcp = make_server(printer)

    plan = await call(mcp, "plan_print", {"test_page": True})

    assert printer.prints() == []
    assert (plan["pages"], plan["copies"], plan["color"]) == (1, 1, True)
    assert plan["file_name"] == printing.TEST_PAGE_NAME
    assert plan["printer"] == "EPSON ET-2860 Series"
    assert plan["expires_in_seconds"] == printing.CODE_TTL_SECONDS
    assert "EPSON ET-2860 Series (192.168.1.94)" in plan["summary"]

    submitted = await call(mcp, "print_document", {"confirmation_code": plan["confirmation_code"]})

    assert submitted["job"]["job_id"] == 42
    assert (submitted["job"]["outcome"], submitted["job"]["finished"]) == ("queued", False)
    assert "get_print_job(printer='192.168.1.94', job_id=42)" in submitted["message"]
    (sent,) = printer.prints()
    operation = decode_response(sent.content).group(OPERATION_GROUP)
    assert operation["document-format"] == ["application/pdf"]
    assert operation["job-name"] == [printing.TEST_PAGE_NAME]
    assert sent.content.endswith(b"%%EOF\n")
    assert decode_response(sent.content).group(JOB_GROUP) == {
        "copies": [1],
        "print-color-mode": ["color"],
    }


async def test_a_users_pdf_is_printed_with_the_options_planned(printer: FakePrinter) -> None:
    document = pdf(3)
    mcp = make_server(printer, {"https://files.example/doc": document})

    plan = await call(
        mcp,
        "plan_print",
        {
            "file": file_argument(),
            "copies": 2,
            "color": False,
            "paper": "letter",
            "landscape": True,
        },
    )
    await call(mcp, "print_document", {"confirmation_code": plan["confirmation_code"]})

    assert (plan["pages"], plan["copies"], plan["paper"]) == (3, 2, "na_letter_8.5x11in")
    assert "report.pdf" in plan["summary"]
    assert "black and white" in plan["summary"]
    (sent,) = printer.prints()
    request = decode_response(sent.content)
    assert request.group(JOB_GROUP) == {
        "copies": [2],
        "media": ["na_letter_8.5x11in"],
        "print-color-mode": ["monochrome"],
        "orientation-requested": [4],
    }
    assert request.group(OPERATION_GROUP)["job-name"] == ["report.pdf"]
    assert sent.content.endswith(document)


async def test_a_jpeg_is_printed_as_a_jpeg(printer: FakePrinter) -> None:
    mcp = make_server(printer, {"https://files.example/doc": JPEG})

    plan = await call(mcp, "plan_print", {"file": file_argument(name="photo.jpg")})
    await call(mcp, "print_document", {"confirmation_code": plan["confirmation_code"]})

    assert (plan["document_format"], plan["pages"]) == ("image/jpeg", 1)
    (sent,) = printer.prints()
    assert decode_response(sent.content).group(OPERATION_GROUP)["document-format"] == ["image/jpeg"]


async def test_a_code_prints_once_even_if_asked_twice(printer: FakePrinter) -> None:
    mcp = make_server(printer)
    plan = await call(mcp, "plan_print", {"test_page": True})
    code = {"confirmation_code": plan["confirmation_code"]}

    await call(mcp, "print_document", code)
    with pytest.raises(ToolError, match="not valid"):
        await mcp.call_tool("print_document", code)

    assert len(printer.prints()) == 1


async def test_printing_without_a_plan_prints_nothing(printer: FakePrinter) -> None:
    with pytest.raises(ToolError, match="not valid"):
        await make_server(printer).call_tool("print_document", {"confirmation_code": "made-up"})

    assert printer.requests == []


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "either a file or test_page"),
        ({"test_page": True, "file": file_argument()}, "either a file or test_page"),
        ({"test_page": True, "copies": printing.MAX_COPIES + 1}, "less than or equal to 5"),
        ({"test_page": True, "copies": 0}, "greater than or equal to 1"),
        ({"test_page": True, "paper": "a3"}, "does not list 'a3'"),
    ],
)
async def test_a_plan_with_bad_arguments_is_refused(
    printer: FakePrinter, arguments: dict[str, Any], message: str
) -> None:
    mcp = make_server(printer, {"https://files.example/doc": pdf()})

    with pytest.raises(ToolError, match=message):
        await mcp.call_tool("plan_print", arguments)

    assert printer.prints() == []


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (pdf(printing.MAX_PAGES + 1), "21 pages; the limit is 20"),
        (PNG, "does not take PNG"),
        (b"MZ not a document", "Only PDF and JPEG"),
        (pdf(password="x"), "password protected"),
        (b"", "file is empty"),
    ],
)
async def test_a_file_the_printer_cannot_or_should_not_print_is_refused(
    printer: FakePrinter, content: bytes, message: str
) -> None:
    mcp = make_server(printer, {"https://files.example/doc": content})

    with pytest.raises(ToolError, match=message):
        await mcp.call_tool("plan_print", {"file": file_argument()})


async def test_a_file_that_cannot_be_downloaded_is_refused(printer: FakePrinter) -> None:
    mcp = make_server(printer, {})

    with pytest.raises(ToolError, match="could not be downloaded"):
        await mcp.call_tool("plan_print", {"file": file_argument()})


async def test_a_stopped_printer_is_not_planned_for(printer: FakePrinter) -> None:
    printer.accepting = False
    printer.reasons = ["media-jam-error"]

    with pytest.raises(ToolError, match="not accepting jobs.*paper jam"):
        await make_server(printer).call_tool("plan_print", {"test_page": True})


async def test_a_printer_that_needs_attention_is_planned_with_a_warning(
    printer: FakePrinter,
) -> None:
    printer.reasons = ["marker-supply-low-warning"]

    plan = await call(make_server(printer), "plan_print", {"test_page": True})

    assert plan["warnings"] == ["Ink is running low."]


async def test_an_unknown_color_mode_falls_back_to_the_printer_default(
    printer: FakePrinter,
) -> None:
    printer.color_modes = ["color"]
    mcp = make_server(printer)

    plan = await call(mcp, "plan_print", {"test_page": True, "color": False})
    await call(mcp, "print_document", {"confirmation_code": plan["confirmation_code"]})

    assert "color mode" in plan["warnings"][0]
    (sent,) = printer.prints()
    assert "print-color-mode" not in decode_response(sent.content).group(JOB_GROUP)


async def test_the_job_name_is_cleaned_before_it_reaches_the_printer(printer: FakePrinter) -> None:
    mcp = make_server(printer, {"https://files.example/doc": pdf()})
    hostile = "a\r\nb" + "x" * 200

    plan = await call(mcp, "plan_print", {"file": file_argument(name=hostile)})

    assert "\n" not in plan["file_name"]
    assert len(plan["file_name"]) <= printing.MAX_JOB_NAME_LENGTH


async def test_a_refusal_by_the_printer_consumes_the_code_and_says_what_to_do(
    printer: FakePrinter,
) -> None:
    printer.print_status = 0x040A
    mcp = make_server(printer)
    plan = await call(mcp, "plan_print", {"test_page": True})
    code = {"confirmation_code": plan["confirmation_code"]}

    with pytest.raises(ToolError, match="document format.*plan_print again"):
        await mcp.call_tool("print_document", code)
    with pytest.raises(ToolError, match="not valid"):
        await mcp.call_tool("print_document", code)


# --- get_print_job ---------------------------------------------------------------------------


async def test_a_finished_job_reports_a_verifiable_status(printer: FakePrinter) -> None:
    result = await call(make_server(printer), "get_print_job", {"job_id": 42})

    assert result["job"]["outcome"] == "completed"
    assert result["job"]["finished"] is True
    assert result["job"]["pages_completed"] == 1
    assert "completed" in result["message"]


async def test_a_failed_job_says_so(printer: FakePrinter) -> None:
    printer.jobs[43] = job_group(43, 8, reasons="job-canceled-by-user")

    result = await call(make_server(printer), "get_print_job", {"job_id": 43})

    assert (result["job"]["outcome"], result["job"]["finished"]) == ("failed", True)
    assert result["job"]["state_reasons"] == ["job-canceled-by-user"]


async def test_a_job_the_printer_forgot_is_explained(printer: FakePrinter) -> None:
    with pytest.raises(ToolError, match="0x0406"):
        await make_server(printer).call_tool("get_print_job", {"job_id": 999})


# --- registration ----------------------------------------------------------------------------


async def test_the_tools_are_registered_with_honest_annotations(printer: FakePrinter) -> None:
    tools = {t.name: t for t in await make_server(printer).list_tools()}

    assert {"get_printer_status", "plan_print", "print_document", "get_print_job"} <= set(tools)
    for name in ("get_printer_status", "plan_print", "get_print_job"):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
    annotations = tools["print_document"].annotations
    assert annotations is not None
    assert annotations.read_only_hint is False
    assert tools["plan_print"].meta == {"openai/fileParams": ["file"]}
    schema = str(tools["plan_print"].input_schema)
    assert "download_url" in schema
    assert "file_id" in schema


async def test_the_server_registers_the_printing_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DockerClient(
        "tcp://proxy:2375", transport=httpx2.MockTransport(lambda r: httpx2.Response(200))
    )

    names = {t.name for t in await create_server(client).list_tools()}

    assert {"get_printer_status", "plan_print", "print_document", "get_print_job"} <= names


# --- the address the caller gives -------------------------------------------------------------


def ipp_reply(*groups: tuple[int, bytes]) -> httpx2.Response:
    return reply(0, (OPERATION_GROUP, b""), *groups)


async def test_the_address_is_checked_by_asking_the_host_over_ipp(printer: FakePrinter) -> None:
    status = await call(make_server(printer), "get_printer_status", {"printer": "192.168.1.94"})

    assert status["address"] == "192.168.1.94"
    (request,) = printer.requests[:1]
    assert str(request.url) == "http://192.168.1.94:631/ipp/print"


@pytest.mark.parametrize(
    "address",
    ["192.168.1.94", "192.168.1.94:631", "ipp://192.168.1.94", "ipp://192.168.1.94:631/ipp/print"],
)
async def test_the_usual_ways_to_write_an_address_are_accepted(
    printer: FakePrinter, address: str
) -> None:
    status = await call(make_server(printer), "get_printer_status", {"printer": address})

    assert status["address"] == "192.168.1.94"


async def test_the_printers_ipp_path_is_found_by_trying_the_usual_ones(
    printer: FakePrinter,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/ipp":
            return printer(request)
        return httpx2.Response(404)

    status = await call(make_server(handler), "get_printer_status")

    assert status["model"] == "EPSON ET-2860 Series"


async def test_a_path_given_in_the_address_is_the_only_one_tried(printer: FakePrinter) -> None:
    await call(
        make_server(printer), "get_printer_status", {"printer": "ipp://192.168.1.94/custom/ipp"}
    )

    assert {r.url.path for r in printer.requests} == {"/custom/ipp"}


async def test_a_name_is_resolved_once_and_then_only_its_ip_is_used(printer: FakePrinter) -> None:
    mcp = make_server(printer, resolve=lambda host: ["192.168.1.94"])

    status = await call(mcp, "get_printer_status", {"printer": "epson.lan"})

    assert status["address"] == "192.168.1.94"
    assert {r.url.host for r in printer.requests} == {"192.168.1.94"}


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "8.8.8.8",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        "100.64.0.1",
        "[::1]",
        "[fe80::1]",
        "[2001:4860:4860::8888]",
    ],
)
async def test_an_address_outside_the_home_network_is_refused_without_a_request(
    printer: FakePrinter, address: str
) -> None:
    mcp = make_server(printer, resolve=lan)

    with pytest.raises(ToolError, match="home network"):
        await mcp.call_tool("get_printer_status", {"printer": address})

    assert printer.requests == []


async def test_a_name_with_one_public_address_among_private_ones_is_refused(
    printer: FakePrinter,
) -> None:
    mcp = make_server(printer, resolve=lambda host: ["192.168.1.94", "93.184.216.34"])

    with pytest.raises(ToolError, match="home network"):
        await mcp.call_tool("get_printer_status", {"printer": "tricky.example"})

    assert printer.requests == []


async def test_a_private_ipv6_printer_is_reached_by_its_bracketed_address(
    printer: FakePrinter,
) -> None:
    mcp = make_server(printer, resolve=lambda host: ["fd00::94"])

    status = await call(mcp, "get_printer_status", {"printer": "printer.lan"})

    assert status["address"] == "fd00::94"
    assert str(printer.requests[0].url) == "http://[fd00::94]:631/ipp/print"


@pytest.mark.parametrize(
    "address",
    [
        "",
        "   ",
        "http://192.168.1.94/ipp/print",
        "ipps://192.168.1.94:8443",
        "192.168.1.94:8080",
        "ipp://user:secret@192.168.1.94",
        "ipp://192.168.1.94/ipp?x=1",
        "192.168.1.94 and more",
        "192.168.1.94\nHost: evil",
        "a" * 300,
    ],
)
async def test_an_address_in_the_wrong_form_is_refused_without_a_request(
    printer: FakePrinter, address: str
) -> None:
    with pytest.raises(ToolError, match="printer must be"):
        await make_server(printer).call_tool("get_printer_status", {"printer": address})

    assert printer.requests == []


async def test_a_name_that_does_not_resolve_is_refused() -> None:
    def broken(_: str) -> list[str]:
        raise OSError("no such host")

    mcp = make_server(FakePrinter(), resolve=broken)

    with pytest.raises(ToolError, match="could not be resolved.*discover_network_devices"):
        await mcp.call_tool("get_printer_status", {"printer": "nowhere.lan"})


@pytest.mark.parametrize(
    "handler",
    [
        lambda _: httpx2.Response(404),
        lambda _: httpx2.Response(200, content=b"<html>router login</html>"),
        lambda _: httpx2.Response(200, content=b"\x02\x00\x00\x00\x00\x00\x00\x01\x03"),
        lambda _: ipp_reply((PRINTER_GROUP, integer("printer-up-time", 5))),
    ],
)
async def test_a_host_that_does_not_answer_as_a_printer_is_refused(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> None:
    with pytest.raises(ToolError, match="did not answer as a printer"):
        await make_server(handler).call_tool("get_printer_status", {"printer": "192.168.1.1"})


async def test_a_host_that_answers_ipp_but_cannot_print_is_not_a_printer(
    printer: FakePrinter,
) -> None:
    scanner_only = (
        integer("operations-supported", 9, TAG_ENUM)
        + integer("printer-state", 3, TAG_ENUM)
        + attribute(0x41, "printer-make-and-model", b"A scanner")
    )

    mcp = make_server(lambda _: ipp_reply((PRINTER_GROUP, scanner_only)))

    with pytest.raises(ToolError, match="did not answer as a printer"):
        await mcp.call_tool("get_printer_status", {"printer": "192.168.1.50"})


async def test_the_test_page_is_never_sent_to_a_host_that_is_not_a_printer() -> None:
    seen: list[httpx2.Request] = []

    def web_server(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, content=b"hello")

    mcp = make_server(web_server)

    with pytest.raises(ToolError, match="did not answer as a printer"):
        await mcp.call_tool("plan_print", {"printer": "192.168.1.1", "test_page": True})

    assert all(decode_response(r.content).status == Operation.GET_PRINTER_ATTRIBUTES for r in seen)


async def test_the_plan_names_the_printer_and_the_job_goes_to_it(printer: FakePrinter) -> None:
    mcp = make_server(printer)

    plan = await call(
        mcp, "plan_print", {"printer": "ipp://192.168.1.94/ipp/print", "test_page": True}
    )
    before = len(printer.requests)
    submitted = await call(mcp, "print_document", {"confirmation_code": plan["confirmation_code"]})

    assert plan["address"] == submitted["address"] == "192.168.1.94"
    assert "get_print_job(printer='192.168.1.94', job_id=42)" in submitted["message"]
    assert [str(r.url) for r in printer.requests[before:]] == ["http://192.168.1.94:631/ipp/print"]
    assert len(printer.prints()) == 1


# --- IPPS ------------------------------------------------------------------------------------

PEM = "-----BEGIN CERTIFICATE-----\nEPSON\n-----END CERTIFICATE-----\n"


def fake_certificate(seen: list[str] | None = None) -> Callable[[str], str]:
    def fetch(host: str) -> str:
        if seen is not None:
            seen.append(host)
        return PEM

    return fetch


def https_only(printer: FakePrinter) -> Callable[[httpx2.Request], httpx2.Response]:
    """Like the Epson ET-2860: plain IPP gets 426 Upgrade Required, IPPS works."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.scheme == "http":
            return httpx2.Response(426)
        return printer(request)

    return handler


async def test_a_printer_that_demands_ipps_is_reached_over_tls_from_a_bare_ip(
    printer: FakePrinter,
) -> None:
    hosts: list[str] = []
    mcp = make_server(https_only(printer), fetch_certificate=fake_certificate(hosts))

    status = await call(mcp, "get_printer_status", {})

    assert status["model"] == "EPSON ET-2860 Series"
    assert status["ready"] is True
    assert hosts == ["192.168.1.94"]
    assert {str(r.url) for r in printer.requests} == {"https://192.168.1.94:631/ipp/print"}


async def test_ipps_is_tried_before_plain_ipp_and_the_certificate_is_read_once(
    printer: FakePrinter,
) -> None:
    hosts: list[str] = []
    mcp = make_server(
        lambda request: httpx2.Response(404) if request.url.path != "/ipp" else printer(request),
        fetch_certificate=fake_certificate(hosts),
    )

    await call(mcp, "get_printer_status", {})

    assert hosts == ["192.168.1.94"]
    assert printer.requests[0].url.scheme == "https"


async def test_a_printer_without_tls_falls_back_to_plain_ipp(printer: FakePrinter) -> None:
    mcp = make_server(printer)

    await call(mcp, "get_printer_status", {})

    assert {str(r.url) for r in printer.requests} == {"http://192.168.1.94:631/ipp/print"}


async def test_an_ipps_address_is_accepted_and_never_downgraded_to_plain_ipp(
    printer: FakePrinter,
) -> None:
    mcp = make_server(printer, fetch_certificate=no_tls)

    with pytest.raises(ToolError, match="TLS handshake"):
        await mcp.call_tool("get_printer_status", {"printer": "ipps://192.168.1.94/ipp/print"})

    assert printer.requests == []


async def test_plan_print_accepts_ipps_prepares_the_test_page_and_prints_nothing(
    printer: FakePrinter,
) -> None:
    mcp = make_server(https_only(printer), fetch_certificate=fake_certificate())

    plan = await call(
        mcp, "plan_print", {"printer": "ipps://192.168.1.94:631/ipp/print", "test_page": True}
    )

    assert plan["address"] == "192.168.1.94"
    assert plan["confirmation_code"]
    assert printer.prints() == []
    assert {str(r.url) for r in printer.requests} == {"https://192.168.1.94:631/ipp/print"}


async def test_after_confirmation_the_job_is_sent_over_ipps_and_followed(
    printer: FakePrinter,
) -> None:
    mcp = make_server(https_only(printer), fetch_certificate=fake_certificate())
    plan = await call(mcp, "plan_print", {"printer": "ipps://192.168.1.94", "test_page": True})
    before = len(printer.requests)

    submitted = await call(mcp, "print_document", {"confirmation_code": plan["confirmation_code"]})
    job = await call(
        mcp,
        "get_print_job",
        {"printer": "ipps://192.168.1.94", "job_id": submitted["job"]["job_id"]},
    )

    assert [str(r.url) for r in printer.requests[before : before + 1]] == [
        "https://192.168.1.94:631/ipp/print"
    ]
    assert len(printer.prints()) == 1
    assert job["job"]["finished"] is True


async def test_the_plan_pins_the_certificate_it_saw_and_the_print_uses_it(
    monkeypatch: pytest.MonkeyPatch, printer: FakePrinter
) -> None:
    trusted: list[tuple[str, str | None]] = []
    original = printing.IppClient

    class Recording(original):  # type: ignore[valid-type, misc]
        def __init__(self, url: str, **kwargs: Any) -> None:
            trusted.append((url, kwargs.get("trusted_certificate")))
            super().__init__(url, **kwargs)

    monkeypatch.setattr(printing, "IppClient", Recording)
    mcp = make_server(https_only(printer), fetch_certificate=fake_certificate())
    plan = await call(mcp, "plan_print", {"test_page": True})

    await call(mcp, "print_document", {"confirmation_code": plan["confirmation_code"]})

    assert ("ipps://192.168.1.94:631/ipp/print", PEM) in trusted[-1:]
    assert all(certificate is None for url, certificate in trusted if url.startswith("ipp:"))


async def test_a_plain_ipp_address_for_an_ipps_only_printer_says_to_use_ipps(
    printer: FakePrinter,
) -> None:
    mcp = make_server(https_only(printer), fetch_certificate=fake_certificate())

    with pytest.raises(ToolError, match=r"encrypted IPP.*ipps://192.168.1.94"):
        await mcp.call_tool("get_printer_status", {"printer": "ipp://192.168.1.94/ipp/print"})


async def test_a_printer_that_demands_tls_but_fails_the_handshake_is_reported_as_such() -> None:
    mcp = make_server(lambda _: httpx2.Response(426), fetch_certificate=no_tls)

    with pytest.raises(ToolError, match=r"only accepts encrypted IPP.*TLS handshake"):
        await mcp.call_tool("get_printer_status", {"printer": "192.168.1.94"})


async def test_a_changed_certificate_stops_the_print() -> None:
    def impostor(request: httpx2.Request) -> httpx2.Response:
        if decode_response(request.content).status == Operation.GET_PRINTER_ATTRIBUTES:
            return FakePrinter()(request)
        raise IppCertificateError("The certificate of the printer is not the one that was trusted.")

    mcp = make_server(impostor, fetch_certificate=fake_certificate())
    plan = await call(mcp, "plan_print", {"printer": "ipps://192.168.1.94", "test_page": True})

    with pytest.raises(ToolError, match="not the one that was trusted.*Nothing was confirmed"):
        await mcp.call_tool("print_document", {"confirmation_code": plan["confirmation_code"]})


async def test_an_unreachable_ipps_port_is_reported_as_unreachable() -> None:
    def unreachable(_: str) -> str:
        raise IppUnreachableError("Cannot reach the printer at 192.168.1.94:631: refused.")

    mcp = make_server(FakePrinter(), fetch_certificate=unreachable)

    with pytest.raises(ToolError, match="Cannot reach the printer"):
        await mcp.call_tool("get_printer_status", {"printer": "192.168.1.94"})


async def test_the_path_the_printer_advertises_is_tried_first(printer: FakePrinter) -> None:
    asked: list[str] = []

    async def advertised(ip: str) -> list[tuple[str, str]]:
        asked.append(ip)
        return [("ipps", "/ipp/epson"), ("ipp", "/legacy")]

    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return printer(request) if request.url.path == "/ipp/epson" else httpx2.Response(404)

    mcp = make_server(handler, fetch_certificate=fake_certificate(), discover=advertised)

    await call(mcp, "get_printer_status", {})

    assert asked == ["192.168.1.94"]
    assert set(seen) == {"https://192.168.1.94:631/ipp/epson"}


async def test_a_path_given_by_the_caller_skips_the_mdns_lookup(printer: FakePrinter) -> None:
    async def advertised(_: str) -> list[tuple[str, str]]:
        raise AssertionError("mDNS must not be asked when the path is known")

    mcp = make_server(printer, discover=advertised)

    await call(mcp, "get_printer_status", {"printer": "ipp://192.168.1.94/ipp/print"})


async def test_a_wrong_path_is_not_enough_to_call_a_host_unreachable() -> None:
    mcp = make_server(lambda _: httpx2.Response(404), fetch_certificate=fake_certificate())

    with pytest.raises(ToolError, match="did not answer as a printer"):
        await mcp.call_tool("get_printer_status", {"printer": "192.168.1.94"})


async def test_an_mdns_name_that_does_not_resolve_explains_why() -> None:
    def broken(_: str) -> list[str]:
        raise OSError("no such host")

    mcp = make_server(FakePrinter(), resolve=broken)

    with pytest.raises(ToolError, match=r"could not be resolved.*\.local.*mDNS"):
        await mcp.call_tool("get_printer_status", {"printer": "EPSON0DD6F6.local"})


@pytest.mark.parametrize(
    ("address", "host", "scheme", "path"),
    [
        ("192.168.1.94", "192.168.1.94", None, None),
        ("192.168.1.94:631", "192.168.1.94", None, None),
        ("ipps://192.168.1.94", "192.168.1.94", "ipps", None),
        ("ipps://192.168.1.94:631/ipp/print", "192.168.1.94", "ipps", "/ipp/print"),
        ("ipp://printer.lan/ipp", "printer.lan", "ipp", "/ipp"),
    ],
)
def test_the_address_is_split_into_host_scheme_and_path(
    address: str, host: str, scheme: str | None, path: str | None
) -> None:
    assert printing.parse_address(address) == printing.Address(host, scheme, path)


# --- a printer that takes only raster ---------------------------------------------------------


def raster_header(request: httpx2.Request) -> tuple[bytes, dict[str, tuple[int, ...]]]:
    """The document that came after the IPP message, and a few header fields of its page."""
    document = request.content[request.content.index(b"RaS2") :]
    header = document[4:]
    return document, {
        "dpi": struct.unpack_from(">2I", header, 276),
        "points": struct.unpack_from(">2I", header, 352),
        "pixels": struct.unpack_from(">2I", header, 372),
        "depth": struct.unpack_from(">I", header, 388),
        "total": struct.unpack_from(">I", header, 452),
    }


async def test_a_printer_without_pdf_gets_the_test_page_drawn_as_pwg_raster(
    printer: FakePrinter,
) -> None:
    printer.epson_like()
    mcp = make_server(https_only(printer), fetch_certificate=fake_certificate())

    plan = await call(mcp, "plan_print", {"printer": "ipps://192.168.1.94", "test_page": True})
    submitted = await call(mcp, "print_document", {"confirmation_code": plan["confirmation_code"]})

    assert plan["document_format"] == "image/pwg-raster"
    assert plan["pages"] == 1
    assert plan["paper"] == A4
    assert f"paper {A4}" in plan["summary"]
    (job,) = printer.prints()
    operation = decode_response(job.content).group(OPERATION_GROUP)
    assert operation["document-format"] == ["image/pwg-raster"]
    document, header = raster_header(job)
    assert header == {
        "dpi": (360, 360),
        "points": (595, 842),
        "pixels": (2975, 4210),
        "depth": (24,),
        "total": (1,),
    }
    assert plan["size_bytes"] == len(document)
    assert decode_response(job.content).group(JOB_GROUP)["media"] == [A4]
    assert submitted["job"]["job_id"] == 42


async def test_the_raster_page_uses_the_paper_asked_for(printer: FakePrinter) -> None:
    printer.epson_like()
    printer.media = [A4, "na_letter_8.5x11in"]
    mcp = make_server(printer)

    plan = await call(mcp, "plan_print", {"test_page": True, "paper": "letter"})
    await call(mcp, "print_document", {"confirmation_code": plan["confirmation_code"]})

    _, header = raster_header(printer.prints()[0])
    assert plan["paper"] == "na_letter_8.5x11in"
    assert header["points"] == (612, 792)
    assert header["pixels"] == (3060, 3960)


async def test_the_raster_resolution_defaults_when_the_printer_lists_none(
    printer: FakePrinter,
) -> None:
    printer.epson_like()
    printer.dpis = []
    mcp = make_server(printer)

    plan = await call(mcp, "plan_print", {"test_page": True})
    await call(mcp, "print_document", {"confirmation_code": plan["confirmation_code"]})

    _, header = raster_header(printer.prints()[0])
    assert header["dpi"] == (300, 300)


async def test_a_printer_that_lists_only_gray_raster_gets_a_gray_page(
    printer: FakePrinter,
) -> None:
    printer.epson_like()
    printer.raster_types = ["sgray_8"]
    mcp = make_server(printer)

    plan = await call(mcp, "plan_print", {"test_page": True})
    await call(mcp, "print_document", {"confirmation_code": plan["confirmation_code"]})

    _, header = raster_header(printer.prints()[0])
    assert header["depth"] == (8,)


async def test_a_printer_that_takes_pdf_still_gets_the_pdf_test_page(printer: FakePrinter) -> None:
    mcp = make_server(printer)

    plan = await call(mcp, "plan_print", {"test_page": True})

    assert plan["document_format"] == "application/pdf"


def photo_jpeg(size: tuple[int, int] = (600, 400), *, noisy: bool = False) -> bytes:
    out = io.BytesIO()
    image = Image.effect_noise(size, 80).convert("RGB") if noisy else Image.new("RGB", size, "red")
    image.save(out, "JPEG")
    return out.getvalue()


async def plan_and_print(mcp: Bound, arguments: dict[str, Any]) -> tuple[Any, Any]:
    plan = await call(mcp, "plan_print", arguments)
    submitted = await call(mcp, "print_document", {"confirmation_code": plan["confirmation_code"]})
    return plan, submitted


async def test_a_pdf_is_converted_for_a_printer_that_takes_only_raster(
    printer: FakePrinter,
) -> None:
    printer.epson_like()
    mcp = make_server(printer, files={"https://files/a.pdf": pdf(3)})

    plan, _ = await plan_and_print(mcp, {"file": file_argument("https://files/a.pdf", "a.pdf")})

    (job,) = printer.prints()
    document, header = raster_header(job)
    assert plan["document_format"] == "image/pwg-raster"
    assert plan["pages"] == 3
    assert plan["paper"] == A4
    assert plan["size_bytes"] == len(document)
    assert any("converted" in warning for warning in plan["warnings"])
    assert decode_response(job.content).group(OPERATION_GROUP)["document-format"] == [
        "image/pwg-raster"
    ]
    assert header["total"] == (3,)
    assert header["dpi"] == (360, 360)
    assert header["pixels"] == (2975, 4210)
    assert len(read_pages(document)) == 3


async def test_a_photo_is_converted_for_a_printer_that_takes_only_raster(
    printer: FakePrinter,
) -> None:
    printer.epson_like()
    mcp = make_server(printer, files={"https://files/p.jpg": photo_jpeg()})

    plan, _ = await plan_and_print(mcp, {"file": file_argument("https://files/p.jpg", "photo.jpg")})

    document, header = raster_header(printer.prints()[0])
    assert plan["document_format"] == "image/pwg-raster"
    assert plan["pages"] == 1
    assert header["total"] == (1,)
    (page,) = read_pages(document)
    assert (page.width, page.height) == (2975, 4210)


async def test_a_black_and_white_request_sends_a_gray_raster(printer: FakePrinter) -> None:
    printer.epson_like()
    mcp = make_server(printer, files={"https://files/a.pdf": pdf()})

    await plan_and_print(mcp, {"file": file_argument("https://files/a.pdf"), "color": False})

    _, header = raster_header(printer.prints()[0])
    assert header["depth"] == (8,)


async def test_landscape_is_not_sent_for_a_converted_file_and_the_plan_says_so(
    printer: FakePrinter,
) -> None:
    printer.epson_like()
    mcp = make_server(printer, files={"https://files/a.pdf": pdf()})

    plan, _ = await plan_and_print(
        mcp, {"file": file_argument("https://files/a.pdf"), "landscape": True}
    )

    (job,) = printer.prints()
    assert "orientation-requested" not in decode_response(job.content).group(JOB_GROUP)
    assert plan["landscape"] is False
    assert any("Landscape" in warning for warning in plan["warnings"])


async def test_the_paper_asked_for_is_used_for_a_converted_file(printer: FakePrinter) -> None:
    printer.epson_like()
    printer.media = [A4, "na_letter_8.5x11in"]
    mcp = make_server(printer, files={"https://files/a.pdf": pdf()})

    plan, _ = await plan_and_print(
        mcp, {"file": file_argument("https://files/a.pdf"), "paper": "letter"}
    )

    _, header = raster_header(printer.prints()[0])
    assert plan["paper"] == "na_letter_8.5x11in"
    assert header["points"] == (612, 792)


async def test_a_conversion_that_would_use_too_much_memory_is_refused_before_confirmation(
    monkeypatch: pytest.MonkeyPatch, printer: FakePrinter
) -> None:
    printer.epson_like()
    monkeypatch.setattr(printing, "MAX_RASTER_BYTES", 2 * 1024 * 1024)
    mcp = make_server(printer, files={"https://files/p.jpg": photo_jpeg((900, 600), noisy=True)})

    with pytest.raises(ToolError, match="larger than 2 MB"):
        await mcp.call_tool("plan_print", {"file": file_argument("https://files/p.jpg")})

    assert printer.prints() == []


async def test_a_photo_that_cannot_be_decoded_is_refused(printer: FakePrinter) -> None:
    printer.epson_like()
    mcp = make_server(printer, files={"https://files/p.jpg": JPEG})

    with pytest.raises(ToolError, match="could not be read"):
        await mcp.call_tool("plan_print", {"file": file_argument("https://files/p.jpg")})


async def test_a_png_is_still_refused_on_a_printer_that_takes_only_raster(
    printer: FakePrinter,
) -> None:
    printer.epson_like()
    mcp = make_server(printer, files={"https://files/p.png": PNG})

    with pytest.raises(ToolError, match=r"does not take PNG.*Convert the file to PDF or JPEG"):
        await mcp.call_tool("plan_print", {"file": file_argument("https://files/p.png")})


async def test_a_printer_that_takes_pdf_is_sent_the_pdf_untouched(printer: FakePrinter) -> None:
    document = pdf()
    mcp = make_server(printer, files={"https://files/a.pdf": document})

    plan, _ = await plan_and_print(mcp, {"file": file_argument("https://files/a.pdf")})

    assert plan["document_format"] == "application/pdf"
    assert plan["size_bytes"] == len(document)
    assert printer.prints()[0].content.endswith(document)


async def test_a_pdf_is_still_told_to_convert_when_the_printer_takes_jpeg_only(
    printer: FakePrinter,
) -> None:
    printer.formats = ["image/jpeg"]
    mcp = make_server(printer, files={"https://files/a.pdf": pdf()})

    with pytest.raises(ToolError, match="Convert the file to PDF or JPEG"):
        await mcp.call_tool("plan_print", {"file": file_argument("https://files/a.pdf")})
