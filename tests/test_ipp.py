import struct
from collections.abc import Callable

import httpx2
import pytest

from server_copilot import ipp
from server_copilot.ipp import (
    JOB_GROUP,
    OPERATION_GROUP,
    PRINTER_GROUP,
    TAG_ENUM,
    TAG_INTEGER,
    TAG_KEYWORD,
    TAG_NAME,
    TAG_URI,
    IppClient,
    IppEndpointError,
    IppError,
    IppUpgradeRequiredError,
    Operation,
    decode_response,
    encode_request,
    printer_http_url,
    printer_uri,
)

pytestmark = pytest.mark.unit

TAG_BOOLEAN = 0x22
TAG_BEGIN_COLLECTION = 0x34
TAG_END_COLLECTION = 0x37
TAG_MEMBER_NAME = 0x4A


def attribute(tag: int, name: str, value: bytes) -> bytes:
    encoded = name.encode()
    return struct.pack(">BH", tag, len(encoded)) + encoded + struct.pack(">H", len(value)) + value


def integer(name: str, value: int, tag: int = TAG_INTEGER) -> bytes:
    return attribute(tag, name, struct.pack(">i", value))


def response_bytes(status: int, *groups: tuple[int, bytes]) -> bytes:
    body = b"".join(bytes([tag]) + content for tag, content in groups)
    return struct.pack(">BBHI", 2, 0, status, 1) + body + b"\x03"


def test_printer_urls_become_http_and_ipp_addresses() -> None:
    assert (
        printer_http_url("ipp://192.168.1.94:631/ipp/print") == "http://192.168.1.94:631/ipp/print"
    )
    assert printer_http_url("ipp://printer.lan/ipp/print") == "http://printer.lan:631/ipp/print"
    assert printer_http_url("http://10.0.0.5/ipp") == "http://10.0.0.5:631/ipp"
    assert printer_uri("ipp://192.168.1.94/ipp/print") == "ipp://192.168.1.94:631/ipp/print"


def test_ipps_printer_urls_are_spoken_to_over_https_and_keep_the_ipps_uri() -> None:
    assert (
        printer_http_url("ipps://192.168.1.94:631/ipp/print")
        == "https://192.168.1.94:631/ipp/print"
    )
    assert printer_http_url("ipps://[fd00::94]/ipp") == "https://[fd00::94]:631/ipp"
    assert printer_uri("ipps://192.168.1.94/ipp/print") == "ipps://192.168.1.94:631/ipp/print"
    assert printer_uri("https://192.168.1.94/ipp") == "ipps://192.168.1.94:631/ipp"


@pytest.mark.parametrize("url", ["", "192.168.1.94", "ftp://p/x", "ipp://", "ipps://"])
def test_a_printer_url_that_is_not_ipp_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="Invalid printer URL"):
        printer_http_url(url)


def test_a_request_carries_the_operation_and_its_attributes() -> None:
    payload = encode_request(
        Operation.PRINT_JOB,
        7,
        [(TAG_URI, "printer-uri", "ipp://p:631/ipp/print"), (TAG_NAME, "job-name", "report")],
        [(TAG_INTEGER, "copies", 2), (TAG_ENUM, "orientation-requested", 4)],
    )

    assert payload[:2] == b"\x02\x00"
    assert struct.unpack(">HI", payload[2:8]) == (Operation.PRINT_JOB, 7)
    assert payload.endswith(b"\x03")
    parsed = decode_response(payload)
    assert parsed.group(OPERATION_GROUP)["attributes-charset"] == ["utf-8"]
    assert parsed.group(OPERATION_GROUP)["printer-uri"] == ["ipp://p:631/ipp/print"]
    assert parsed.group(OPERATION_GROUP)["job-name"] == ["report"]
    assert parsed.group(JOB_GROUP) == {"copies": [2], "orientation-requested": [4]}


def test_a_multi_valued_attribute_is_sent_as_one_name_and_several_values() -> None:
    payload = encode_request(
        Operation.GET_PRINTER_ATTRIBUTES,
        1,
        [(TAG_KEYWORD, "requested-attributes", ["printer-state", "media-ready"])],
    )

    assert decode_response(payload).group(OPERATION_GROUP)["requested-attributes"] == [
        "printer-state",
        "media-ready",
    ]
    assert attribute(TAG_KEYWORD, "", b"media-ready") in payload


def test_a_response_is_decoded_to_python_values() -> None:
    printer = (
        attribute(0x41, "printer-state-message", b"")
        + integer("printer-state", 3, TAG_ENUM)
        + attribute(TAG_BOOLEAN, "printer-is-accepting-jobs", b"\x01")
        + attribute(TAG_KEYWORD, "printer-state-reasons", b"media-low-warning")
        + attribute(TAG_KEYWORD, "", b"marker-supply-low-warning")
        + attribute(0x35, "printer-current-time", b"\x00" * 11)
    )
    data = response_bytes(0x0001, (OPERATION_GROUP, b""), (PRINTER_GROUP, printer))

    response = decode_response(data)

    assert response.status == 0x0001
    assert response.group(PRINTER_GROUP) == {
        "printer-state-message": [""],
        "printer-state": [3],
        "printer-is-accepting-jobs": [True],
        "printer-state-reasons": ["media-low-warning", "marker-supply-low-warning"],
    }


def test_collections_are_skipped_without_losing_the_attributes_around_them() -> None:
    collection = (
        attribute(TAG_BEGIN_COLLECTION, "media-col-ready", b"")
        + attribute(TAG_MEMBER_NAME, "", b"media-size")
        + attribute(TAG_BEGIN_COLLECTION, "", b"")
        + attribute(TAG_MEMBER_NAME, "", b"x-dimension")
        + integer("", 21000)
        + attribute(TAG_END_COLLECTION, "", b"")
        + attribute(TAG_MEMBER_NAME, "", b"media-type")
        + attribute(TAG_KEYWORD, "", b"stationery")
        + attribute(TAG_END_COLLECTION, "", b"")
    )
    printer = (
        attribute(TAG_KEYWORD, "media-ready", b"iso_a4_210x297mm")
        + collection
        + attribute(TAG_KEYWORD, "media-default", b"iso_a4_210x297mm")
    )

    group = decode_response(response_bytes(0, (PRINTER_GROUP, printer))).group(PRINTER_GROUP)

    assert group == {"media-ready": ["iso_a4_210x297mm"], "media-default": ["iso_a4_210x297mm"]}


def test_resolutions_are_decoded_to_dpi_whatever_unit_the_printer_uses() -> None:
    printer = (
        attribute(
            0x32, "pwg-raster-document-resolution-supported", struct.pack(">iib", 360, 360, 3)
        )
        + attribute(0x32, "", struct.pack(">iib", 100, 100, 4))
        + attribute(0x32, "short", b"\x00\x00")
    )

    group = decode_response(response_bytes(0, (PRINTER_GROUP, printer))).group(PRINTER_GROUP)

    assert group == {"pwg-raster-document-resolution-supported": [(360, 360), (254, 254)]}


def test_several_job_groups_are_kept_apart() -> None:
    first = integer("job-id", 11) + integer("job-state", 9, TAG_ENUM)
    second = integer("job-id", 12) + integer("job-state", 5, TAG_ENUM)

    response = decode_response(response_bytes(0, (JOB_GROUP, first), (JOB_GROUP, second)))

    assert [g["job-id"] for g in response.groups_of(JOB_GROUP)] == [[11], [12]]
    assert response.group(PRINTER_GROUP) == {}


@pytest.mark.parametrize("data", [b"", b"\x02\x00\x00", response_bytes(0, (4, b"\x44\x00"))])
def test_a_response_that_is_not_ipp_or_is_cut_short_is_an_error(data: bytes) -> None:
    with pytest.raises(IppError):
        decode_response(data)


def make_client(handler: httpx2.MockTransport) -> IppClient:
    return IppClient("ipp://printer.lan:631/ipp/print", transport=handler)


async def test_the_client_posts_ipp_to_the_printer_and_checks_the_status() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200, content=response_bytes(0, (PRINTER_GROUP, integer("printer-state", 3, TAG_ENUM)))
        )

    client = make_client(httpx2.MockTransport(handler))
    attributes = await client.get_printer_attributes(["printer-state"])
    await client.aclose()

    assert attributes == {"printer-state": [3]}
    assert str(seen[0].url) == "http://printer.lan:631/ipp/print"
    assert seen[0].headers["content-type"] == "application/ipp"
    assert struct.unpack(">H", seen[0].content[2:4])[0] == Operation.GET_PRINTER_ATTRIBUTES


async def test_print_job_sends_the_document_right_after_the_ipp_message() -> None:
    seen: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.content)
        return httpx2.Response(200, content=response_bytes(0, (JOB_GROUP, integer("job-id", 42))))

    client = make_client(httpx2.MockTransport(handler))
    job = await client.print_job(
        b"%PDF-1.4 body",
        document_format="application/pdf",
        job_name="report",
        job_attributes=[(TAG_INTEGER, "copies", 1)],
    )

    assert job == {"job-id": [42]}
    assert seen[0].endswith(b"\x03%PDF-1.4 body")
    assert decode_response(seen[0]).group(OPERATION_GROUP)["document-format"] == ["application/pdf"]


async def test_an_ipp_error_status_is_explained() -> None:
    transport = httpx2.MockTransport(
        lambda _: httpx2.Response(200, content=response_bytes(0x040A, (OPERATION_GROUP, b"")))
    )

    with pytest.raises(IppError, match=r"0x040a.*does not accept this document format"):
        await make_client(transport).get_printer_attributes([])


async def test_http_426_means_the_printer_only_accepts_ipps() -> None:
    client = make_client(httpx2.MockTransport(lambda _: httpx2.Response(426)))

    with pytest.raises(IppUpgradeRequiredError, match=r"426 Upgrade Required.*ipps://"):
        await client.get_printer_attributes([])


async def test_a_wrong_path_is_an_endpoint_error() -> None:
    client = make_client(httpx2.MockTransport(lambda _: httpx2.Response(404)))

    with pytest.raises(IppEndpointError, match="HTTP 404.*not be its IPP endpoint"):
        await client.get_printer_attributes([])


async def test_an_ipps_client_posts_to_https_with_the_ipps_printer_uri() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200, content=response_bytes(0, (PRINTER_GROUP, integer("printer-state", 3, TAG_ENUM)))
        )

    client = IppClient("ipps://192.168.1.94:631/ipp/print", transport=httpx2.MockTransport(handler))
    await client.get_printer_attributes(["printer-state"])
    await client.aclose()

    assert str(seen[0].url) == "https://192.168.1.94:631/ipp/print"
    uri = decode_response(seen[0].content).group(OPERATION_GROUP)["printer-uri"]
    assert uri == ["ipps://192.168.1.94:631/ipp/print"]


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (lambda _: httpx2.Response(500), "HTTP 500"),
        (lambda _: httpx2.Response(200, content=b"<html>"), "not an IPP response"),
        (lambda _: (_ for _ in ()).throw(httpx2.ConnectError("refused")), "Cannot reach"),
        (lambda _: (_ for _ in ()).throw(httpx2.ReadTimeout("slow")), "Timed out"),
        (
            lambda _: httpx2.Response(
                200, content=b"\x02\x00\x00\x00\x00\x00\x00\x01" + b"x" * ipp.MAX_RESPONSE_BYTES
            ),
            "unexpectedly large",
        ),
    ],
)
async def test_failures_reaching_the_printer_are_clear(
    handler: Callable[[httpx2.Request], httpx2.Response], message: str
) -> None:
    client = make_client(httpx2.MockTransport(handler))

    with pytest.raises(IppError, match=message):
        await client.get_printer_attributes([])
