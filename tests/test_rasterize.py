import io
from datetime import UTC, datetime

import pytest
from PIL import Image
from pypdf import PdfWriter

from pwg_decoder import Page, read_pages
from server_copilot import rasterize
from server_copilot.printing import build_test_page
from server_copilot.raster import RasterError, RasterTooLargeError
from server_copilot.rasterize import rasterize_pdf, rasterize_photo

pytestmark = pytest.mark.unit

A4 = "iso_a4_210x297mm"
DPI = 72
LIMIT = 64 * 1024 * 1024


def jpeg(size: tuple[int, int], color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG", quality=95)
    return out.getvalue()


def noisy_jpeg(size: tuple[int, int]) -> bytes:
    out = io.BytesIO()
    Image.effect_noise(size, 80).convert("RGB").save(out, "JPEG")
    return out.getvalue()


def black_pages_pdf(*sizes: tuple[int, int]) -> bytes:
    """A PDF with one entirely black page per ``(width, height)`` in points."""
    kids = " ".join(f"{3 + 2 * index} 0 R" for index in range(len(sizes)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(sizes)} >>".encode(),
    ]
    for index, (width, height) in enumerate(sizes):
        content = f"0 g 0 0 {width} {height} re f".encode()
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
                f"/Contents {4 + 2 * index} 0 R >>"
            ).encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
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


def black_page_pdf(width: int, height: int) -> bytes:
    return black_pages_pdf((width, height))


def blank_pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def photo(data: bytes, *, gray: bool = False, dpi: int = DPI) -> Page:
    return Page(rasterize_photo(data, media=A4, dpi=dpi, gray=gray, max_bytes=LIMIT))


def pdf_pages(
    data: bytes,
    *,
    gray: bool = True,
    dpi: int = DPI,
    pages: int = 20,
    only: list[int] | None = None,
) -> list[Page]:
    raster = rasterize_pdf(
        data, media=A4, dpi=dpi, gray=gray, max_bytes=LIMIT, max_pages=pages, pages=only
    )
    return read_pages(raster)


def test_a_landscape_photo_is_turned_to_fill_a_portrait_page() -> None:
    width, height = photo(jpeg((600, 400)), gray=True).ink_box()

    assert height > width


def test_a_portrait_photo_stays_upright() -> None:
    width, height = photo(jpeg((400, 600)), gray=True).ink_box()

    assert height > width


def test_a_photo_keeps_its_proportions_and_a_white_margin() -> None:
    page = photo(jpeg((600, 400)), gray=True, dpi=144)

    width, height = page.ink_box()
    assert abs(height / width - 1.5) < 0.02
    margin = round(5 / 25.4 * 144)
    assert width <= page.width - 2 * margin + 1
    assert height <= page.height - 2 * margin + 1
    assert set(page.rows[margin // 2]) == {255}


def test_the_photos_color_is_kept() -> None:
    page = photo(jpeg((400, 600), (200, 30, 30)))

    middle = page.rows[page.height // 2]
    pixels = {middle[i : i + 3] for i in range(0, len(middle), 3)}
    assert any(r > 170 and g < 70 and b < 70 for r, g, b in pixels)


def test_a_gray_photo_has_one_byte_per_pixel() -> None:
    page = photo(jpeg((400, 600)), gray=True)

    assert page.bytes_per_pixel == 1
    assert page.field(396, 2) == (0, 18)


def test_a_small_photo_is_scaled_up_to_fill_the_page() -> None:
    _, height = photo(jpeg((60, 40)), gray=True).ink_box()

    assert height > 600


def test_a_photo_that_is_not_an_image_is_refused() -> None:
    with pytest.raises(RasterError, match="could not be read"):
        rasterize_photo(b"\xff\xd8\xff not really", media=A4, dpi=DPI, gray=False, max_bytes=LIMIT)


def test_a_photo_with_too_many_pixels_is_refused_before_it_is_decoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rasterize, "MAX_IMAGE_PIXELS", 1000)

    with pytest.raises(RasterError, match="too many pixels"):
        rasterize_photo(jpeg((100, 100)), media=A4, dpi=DPI, gray=False, max_bytes=LIMIT)


def test_a_photo_whose_raster_is_too_big_is_refused() -> None:
    with pytest.raises(RasterTooLargeError, match="larger than 2 MB"):
        rasterize_photo(
            noisy_jpeg((900, 600)), media=A4, dpi=200, gray=False, max_bytes=2 * 1024 * 1024
        )


def test_a_pdf_page_is_drawn_with_its_content() -> None:
    (page,) = pdf_pages(build_test_page("EPSON", datetime.now(UTC)), gray=False, dpi=150)

    colors = {color for row in range(page.height) for color in page.pixels(row)}
    assert (page.width, page.height) == (1240, 1754)
    assert len(colors) > 6
    assert page.field(452) == (1,)


def test_every_page_of_a_pdf_is_rendered() -> None:
    pages = pdf_pages(blank_pdf(3))

    assert len(pages) == 3
    assert all(page.field(452) == (3,) for page in pages)
    assert all(page.pixels(0) == {bytes((255,))} for page in pages)


def test_a_page_smaller_than_the_paper_is_not_scaled() -> None:
    (page,) = pdf_pages(black_page_pdf(200, 300), dpi=144)

    width, height = page.ink_box()
    assert abs(width - 400) <= 2
    assert abs(height - 600) <= 2


def test_a_page_larger_than_the_paper_is_shrunk_to_fit() -> None:
    (page,) = pdf_pages(black_page_pdf(842, 1190), dpi=144)

    width, height = page.ink_box()
    assert abs(width - 1190) <= 2
    assert abs(height - 1684) <= 2


def test_a_landscape_page_is_turned_to_fill_a_portrait_paper() -> None:
    (page,) = pdf_pages(black_page_pdf(842, 595), dpi=144)

    width, height = page.ink_box()
    assert abs(width - 1190) <= 2
    assert abs(height - 1684) <= 2


def test_a_pdf_that_cannot_be_rendered_is_refused() -> None:
    with pytest.raises(RasterError, match="could not be rendered"):
        rasterize_pdf(
            b"%PDF-1.4 garbage", media=A4, dpi=DPI, gray=False, max_bytes=LIMIT, max_pages=20
        )


def test_a_pdf_with_more_pages_than_allowed_is_refused() -> None:
    with pytest.raises(RasterError, match="3 pages; the limit is 2"):
        pdf_pages(blank_pdf(3), pages=2)


def test_a_pdf_whose_raster_is_too_big_is_refused() -> None:
    with pytest.raises(RasterTooLargeError, match="larger than 0 MB"):
        rasterize_pdf(
            black_page_pdf(595, 842),
            media=A4,
            dpi=600,
            gray=False,
            max_bytes=1000,
            max_pages=20,
        )


THREE_PAGES = black_pages_pdf((100, 100), (200, 300), (300, 400))


def test_only_the_chosen_pages_of_a_pdf_are_rendered() -> None:
    (page,) = pdf_pages(THREE_PAGES, only=[2])

    width, height = page.ink_box()
    assert abs(width - 200) <= 2
    assert abs(height - 300) <= 2
    assert page.field(452) == (1,)


def test_several_chosen_pages_come_out_in_the_order_given() -> None:
    first, second = pdf_pages(THREE_PAGES, only=[1, 3])

    assert [abs(first.ink_box()[0] - 100) <= 2, abs(second.ink_box()[0] - 300) <= 2] == [True] * 2
    assert first.field(452) == second.field(452) == (2,)


def test_a_chosen_page_the_pdf_does_not_have_is_refused() -> None:
    with pytest.raises(RasterError, match="3 pages; some chosen pages do not exist"):
        pdf_pages(THREE_PAGES, only=[2, 4])


def test_the_page_limit_counts_only_the_chosen_pages() -> None:
    assert len(pdf_pages(THREE_PAGES, pages=2, only=[1, 3])) == 2
    with pytest.raises(RasterError, match="3 pages; the limit is 2"):
        pdf_pages(THREE_PAGES, pages=2, only=[1, 2, 3])
