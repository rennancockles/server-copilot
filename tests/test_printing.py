import io
import struct
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pypdf import PdfReader, PdfWriter

from server_copilot import printing
from server_copilot.docker import DockerClient
from server_copilot.ipp import (
    JOB_GROUP,
    OPERATION_GROUP,
    PRINTER_GROUP,
    TAG_ENUM,
    TAG_INTEGER,
    TAG_KEYWORD,
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
        )

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


def make_server(
    printer: FakePrinter | Callable[[httpx2.Request], httpx2.Response],
    files: dict[str, bytes] | None = None,
    resolve: Callable[[str], list[str]] = lan,
) -> Bound:
    mcp = MCPServer("test")
    register_print_tools(
        mcp, fetcher(files or {}), transport=httpx2.MockTransport(printer), resolve=resolve
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
        "ipps://192.168.1.94",
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
