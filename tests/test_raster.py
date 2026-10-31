import pytest

from pwg_decoder import Page, read_pages
from server_copilot import raster
from server_copilot.raster import build_test_page, media_points

pytestmark = pytest.mark.unit

A4 = "iso_a4_210x297mm"
LETTER = "na_letter_8.5x11in"
WHITE = bytes((255, 255, 255))


def test_media_names_give_their_size_in_points() -> None:
    assert media_points(A4) == (595, 842)
    assert media_points(LETTER) == (612, 792)
    assert media_points("iso_a6_105x148mm") == (298, 420)
    assert media_points("custom_min_3x5in") == (216, 360)
    assert media_points("na_letter") is None
    assert media_points("") is None


def test_the_page_header_describes_an_srgb_page_of_the_requested_size() -> None:
    page = Page(build_test_page(A4, 360))

    assert page.header[:10] == b"PwgRaster\x00"
    assert page.field(276, 2) == (360, 360)
    assert page.field(352, 2) == (595, 842)
    assert (page.width, page.height) == (2975, 4210)
    assert page.field(384, 3) == (8, 24, 2975 * 3)
    assert page.field(396, 2) == (0, 19)
    assert page.field(420) == (3,)
    assert page.header[1732:].rstrip(b"\x00") == A4.encode()


def test_a_letter_page_follows_the_paper_and_resolution() -> None:
    page = Page(build_test_page(LETTER, 300))

    assert page.field(352, 2) == (612, 792)
    assert (page.width, page.height) == (2550, 3300)


def test_an_unknown_paper_falls_back_to_a4() -> None:
    page = Page(build_test_page("na_letter", 300))

    assert page.field(352, 2) == (595, 842)


def test_the_test_page_has_white_margins_ink_swatches_and_fine_lines() -> None:
    page = Page(build_test_page(A4, 300))

    assert page.pixels(0) == {WHITE}
    assert page.pixels(page.height - 1) == {WHITE}
    swatch_row = next(row for row in range(page.height) if len(page.pixels(row)) == 8)
    assert page.pixels(swatch_row) == {
        WHITE,
        bytes((0, 255, 255)),
        bytes((255, 0, 255)),
        bytes((255, 255, 0)),
        bytes((0, 0, 255)),
        bytes((0, 255, 0)),
        bytes((255, 0, 0)),
        bytes((0, 0, 0)),
    }
    for ink in ((0, 255, 255), (255, 0, 255), (255, 255, 0), (0, 0, 0)):
        inked = [r for r in range(page.height) if page.pixels(r) == {WHITE, bytes(ink)}]
        assert len(inked) > 20
        assert all(inked[i + 1] - inked[i] in (1, 3) for i in range(len(inked) - 1) if i < 20)


def test_a_gray_page_has_one_byte_per_pixel_and_a_gray_color_space() -> None:
    page = Page(build_test_page(A4, 300, gray=True))

    assert page.bytes_per_pixel == 1
    assert page.field(384, 3) == (8, 8, page.width)
    assert page.field(396, 2) == (0, 18)
    assert page.field(420) == (1,)
    assert len({p for row in range(page.height) for p in page.pixels(row)}) > 8


def test_the_compressed_page_is_small() -> None:
    assert len(build_test_page(A4, 720)) < 1_000_000


@pytest.mark.parametrize(
    "pixels",
    [
        [1, 1, 1, 2, 3, 4, 4],
        [1],
        [1, 2],
        [7] * 300,
        list(range(200)),
        [1, 2, 3] * 100,
        [5] + [6] * 128 + [7] + [8] * 129,
    ],
)
def test_a_row_is_packed_into_repeats_and_literals_that_read_back_the_same(
    pixels: list[int],
) -> None:
    packed = raster._pack_row(bytes(pixels), 1)

    decoded = bytearray()
    position = 0
    while position < len(packed):
        control = packed[position]
        position += 1
        assert control != 128
        if control <= 127:
            decoded += packed[position : position + 1] * (control + 1)
            position += 1
        else:
            count = 257 - control
            decoded += packed[position : position + count]
            position += count
    assert list(decoded) == pixels


def unpack(packed: bytes, bytes_per_pixel: int) -> bytes:
    decoded = bytearray()
    position = 0
    while position < len(packed):
        control = packed[position]
        position += 1
        assert control != 128
        if control <= 127:
            decoded += packed[position : position + bytes_per_pixel] * (control + 1)
            position += bytes_per_pixel
        else:
            size = (257 - control) * bytes_per_pixel
            decoded += packed[position : position + size]
            position += size
    return bytes(decoded)


def noise(count: int, seed: int) -> list[bytes]:
    state = seed
    out = []
    for _ in range(count):
        state = (state * 1103515245 + 12345) % 2**31
        out.append(bytes(((state >> 8) & 255, (state >> 16) & 255, (state >> 4) & 255)))
    return out


@pytest.mark.parametrize(
    "pixels",
    [
        [bytes((9, 9, 9))] * 1000,
        noise(1000, 1),
        noise(130, 2),
        noise(1, 3),
        [bytes((1, 2, 3))] * 128 + noise(3, 4) + [bytes((5, 6, 7))] * 129,
        noise(40, 5) * 3,
        [pixel for pixel in noise(60, 6) for _ in range(7)],
    ],
)
def test_rgb_rows_are_packed_without_losing_a_pixel(pixels: list[bytes]) -> None:
    row = b"".join(pixels)

    assert unpack(raster._pack_row(row, 3), 3) == row


def test_a_busy_row_is_not_much_bigger_than_its_pixels() -> None:
    row = b"".join(noise(2975, 7))

    assert len(raster._pack_row(row, 3)) < len(row) * 1.02


def test_pages_with_equal_rows_are_sent_once_with_a_repeat_count() -> None:
    width, height = raster.page_pixels(A4, 72)
    white = bytes(width * 3 * [255])
    black = bytes(width * 3 * [0])

    data = raster.encode_pages(
        [[white] * height, ([black] * 10) + [white] * (height - 10)],
        total_pages=2,
        media=A4,
        dpi=72,
        gray=False,
    )

    first, second = read_pages(data)
    assert first.field(452) == second.field(452) == (2,)
    assert first.pixels(0) == {WHITE}
    assert second.pixels(9) == {bytes((0, 0, 0))}
    assert second.pixels(10) == {WHITE}
    assert len(data) < 2 * 1796 + 1000


def test_a_page_with_rows_of_the_wrong_size_is_refused() -> None:
    with pytest.raises(raster.RasterError, match="printer's size"):
        raster.encode_pages([[b"\x00" * 7] * 842], total_pages=1, media=A4, dpi=72, gray=False)


def test_a_raster_larger_than_the_limit_is_stopped_while_it_is_written() -> None:
    width, height = raster.page_pixels(A4, 72)
    varied = (b"".join(noise(width, row)) for row in range(height))

    with pytest.raises(raster.RasterTooLargeError, match="larger than 1 MB"):
        raster.encode_pages(
            [varied], total_pages=1, media=A4, dpi=72, gray=False, max_bytes=1024 * 1024
        )
