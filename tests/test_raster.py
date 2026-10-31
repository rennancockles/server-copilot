import struct

import pytest

from server_copilot import raster
from server_copilot.raster import build_test_page, media_points

pytestmark = pytest.mark.unit

A4 = "iso_a4_210x297mm"
LETTER = "na_letter_8.5x11in"
HEADER_SIZE = 1796
WHITE = bytes((255, 255, 255))


class Page:
    """A PWG raster page read back the way a printer would, independent of the writer."""

    def __init__(self, data: bytes) -> None:
        assert data[:4] == b"RaS2"
        self.header = data[4 : 4 + HEADER_SIZE]
        self.width, self.height = self.field(372, 2)
        self.bytes_per_pixel = self.field(388)[0] // 8
        position = 4 + HEADER_SIZE
        row_size = self.width * self.bytes_per_pixel
        self.rows: list[bytes] = []
        while len(self.rows) < self.height:
            repeat = data[position] + 1
            position += 1
            line = bytearray()
            while len(line) < row_size:
                control = data[position]
                position += 1
                assert control != 128, "128 is reserved"
                if control <= 127:
                    line += data[position : position + self.bytes_per_pixel] * (control + 1)
                    position += self.bytes_per_pixel
                else:
                    count = 257 - control
                    line += data[position : position + count * self.bytes_per_pixel]
                    position += count * self.bytes_per_pixel
            assert len(line) == row_size
            self.rows += [bytes(line)] * repeat
        assert len(self.rows) == self.height
        assert position == len(data)

    def field(self, offset: int, count: int = 1) -> tuple[int, ...]:
        return struct.unpack_from(f">{count}I", self.header, offset)

    def pixels(self, row: int) -> set[bytes]:
        size = self.bytes_per_pixel
        data = self.rows[row]
        return {data[i : i + size] for i in range(0, len(data), size)}


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
