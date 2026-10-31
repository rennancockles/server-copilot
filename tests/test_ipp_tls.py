import datetime
import socket
import ssl
import struct
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from server_copilot.ipp import (
    IppCertificateError,
    IppClient,
    IppTlsError,
    IppUnreachableError,
    fetch_peer_certificate,
)

PRINTER_GROUP = 0x04
STATE_ATTRIBUTE = (
    bytes([0x23]) + struct.pack(">H", 13) + b"printer-state" + struct.pack(">Hi", 4, 3)
)
IPP_ANSWER = struct.pack(">BBHI", 2, 0, 0, 1) + bytes([PRINTER_GROUP]) + STATE_ATTRIBUTE + b"\x03"


def sockets_available() -> bool:
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
    except OSError:
        return False
    return True


pytestmark = [pytest.mark.unit, pytest.mark.skipif(not sockets_available(), reason="no sockets")]


@dataclass
class Printer:
    port: int
    certificate: str


def make_certificate(common_name: str) -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return certificate.public_bytes(serialization.Encoding.PEM).decode(), key_pem


class IppHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "application/ipp")
        self.send_header("Content-Length", str(len(IPP_ANSWER)))
        self.end_headers()
        self.wfile.write(IPP_ANSWER)

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.fixture
def printer(tmp_path: Path) -> Iterator[Printer]:
    certificate, key = make_certificate("EPSON0DD6F6")
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_text(certificate)
    key_file.write_text(key)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    server = ThreadingHTTPServer(("127.0.0.1", 0), IppHandler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Printer(server.server_address[1], certificate)
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def plaintext_port() -> Iterator[int]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def answer() -> None:
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            with connection:
                connection.sendall(b"HTTP/1.1 426 Upgrade Required\r\n\r\n")

    threading.Thread(target=answer, daemon=True).start()
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


def test_the_certificate_a_printer_presents_is_read_in_pem_form(printer: Printer) -> None:
    assert fetch_peer_certificate("127.0.0.1", printer.port) == printer.certificate


async def test_a_client_that_trusts_the_printers_certificate_talks_to_it(printer: Printer) -> None:
    async with IppClient(
        f"ipps://127.0.0.1:{printer.port}/ipp/print", trusted_certificate=printer.certificate
    ) as client:
        attributes = await client.get_printer_attributes(["printer-state"])

    assert attributes == {"printer-state": [3]}


async def test_a_different_certificate_is_refused_even_though_the_handshake_would_work(
    printer: Printer,
) -> None:
    other, _ = make_certificate("someone else")

    async with IppClient(
        f"ipps://127.0.0.1:{printer.port}/ipp/print", trusted_certificate=other
    ) as client:
        with pytest.raises(IppCertificateError, match="not the one that was trusted"):
            await client.get_printer_attributes(["printer-state"])


async def test_a_self_signed_certificate_is_refused_unless_it_was_pinned(printer: Printer) -> None:
    async with IppClient(f"ipps://127.0.0.1:{printer.port}/ipp/print") as client:
        with pytest.raises(IppCertificateError):
            await client.get_printer_attributes(["printer-state"])


def test_an_address_that_does_not_speak_tls_is_a_tls_error(plaintext_port: int) -> None:
    with pytest.raises(IppTlsError, match="TLS handshake"):
        fetch_peer_certificate("127.0.0.1", plaintext_port)


def test_a_closed_port_is_unreachable_not_a_tls_error() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    with pytest.raises(IppUnreachableError, match="Cannot reach"):
        fetch_peer_certificate("127.0.0.1", port)
