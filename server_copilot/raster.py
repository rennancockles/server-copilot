"""A minimal PWG raster (PWG 5102.4) writer.

Some printers (the Epson EcoTank ET-2860 among them) take neither PDF nor JPEG over IPP, only
``image/pwg-raster``. This module writes that format: the built-in test page is drawn straight
into it, and ``rasterize`` renders uploaded documents into it. A page is a sequence of pixel rows
(``width * bytes_per_pixel`` bytes each, chunked RGB or gray); identical rows are sent once with
a line-repeat count and each distinct row is run-length compressed.
"""

import re
import struct
from collections.abc import Iterable, Iterator
from functools import cache

Color = tuple[int, int, int]
Segment = tuple[list[Color], int]

SYNC_WORD = b"RaS2"
HEADER_SIZE = 1796
SRGB_8 = "srgb_8"
SGRAY_8 = "sgray_8"
COLOR_SPACES = {SRGB_8: 19, SGRAY_8: 18}
POINTS_PER_INCH = 72
MM_PER_INCH = 25.4
MAX_RUN = 128
MAX_LINE_REPEAT = 256
LINE_PIXELS = 2
MIN_REPEAT_SHARE = 5
DEFAULT_MEDIA = "iso_a4_210x297mm"
A4_POINTS = (595, 842)

WHITE: Color = (255, 255, 255)
SWATCHES: list[Color] = [
    WHITE,
    (0, 255, 255),
    (255, 0, 255),
    (255, 255, 0),
    (0, 0, 255),
    (0, 255, 0),
    (255, 0, 0),
    (0, 0, 0),
]
INKS: list[Color] = [(0, 255, 255), (255, 0, 255), (255, 255, 0), (0, 0, 0)]
GRAY_RAMP: list[Color] = [(v, v, v) for v in range(255, -1, -17)]

_MEDIA_SIZE = re.compile(r"_(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)(mm|in)$")


class RasterError(Exception):
    """A document that cannot be turned into raster pages; the message is safe to show."""


class RasterTooLargeError(RasterError):
    """The raster pages would take more memory than the limit allows."""


def media_points(name: str) -> tuple[int, int] | None:
    """The width and height in points of a PWG media name such as ``iso_a4_210x297mm``."""
    match = _MEDIA_SIZE.search(name)
    if not match:
        return None
    unit = POINTS_PER_INCH / (MM_PER_INCH if match.group(3) == "mm" else 1)
    return round(float(match.group(1)) * unit), round(float(match.group(2)) * unit)


def page_pixels(media: str, dpi: int) -> tuple[int, int]:
    """The width and height in pixels of a ``media`` page at ``dpi`` (A4 if the name is odd)."""
    points = media_points(media) or A4_POINTS
    return (
        round(points[0] * dpi / POINTS_PER_INCH),
        round(points[1] * dpi / POINTS_PER_INCH),
    )


# --- compression ------------------------------------------------------------------------------


@cache
def _run_pattern(bytes_per_pixel: int) -> re.Pattern[bytes]:
    """One pixel, then up to 127 more of the same, always starting on a pixel boundary."""
    return re.compile(rb"(?s)(.{%d})(\1{0,%d})" % (bytes_per_pixel, MAX_RUN - 1))


def _literals(row: bytes, bytes_per_pixel: int) -> bytes:
    """A row sent as literal blocks only: no runs are looked for."""
    out = bytearray()
    step = MAX_RUN * bytes_per_pixel
    for start in range(0, len(row), step):
        chunk = row[start : start + step]
        count = len(chunk) // bytes_per_pixel
        out += (b"\x00" if count == 1 else bytes([257 - count])) + chunk
    return bytes(out)


def _repeats_little(row: bytes, bytes_per_pixel: int) -> bool:
    """Whether fewer than one pixel in five equals its neighbour (a photo, not a drawing)."""
    size = len(row) - bytes_per_pixel
    if size <= 0:
        return True
    differ = int.from_bytes(row[:-bytes_per_pixel]) ^ int.from_bytes(row[bytes_per_pixel:])
    same = differ.to_bytes(size).count(b"\x00" * bytes_per_pixel)
    return same * MIN_REPEAT_SHARE < len(row) // bytes_per_pixel


def _pack_runs(runs: list[tuple[bytes, int]]) -> bytes:
    """Runs as ``n-1, pixel`` repeats and ``257-n, pixels...`` literals of lone pixels."""
    out = bytearray()
    index = 0
    while index < len(runs):
        pixel, length = runs[index]
        index += 1
        if length > 1:
            out += bytes([length - 1]) + pixel
            continue
        singles = [pixel]
        while index < len(runs) and runs[index][1] == 1 and len(singles) < MAX_RUN:
            singles.append(runs[index][0])
            index += 1
        if len(singles) == 1:
            out += b"\x00" + pixel
        else:
            out += bytes([257 - len(singles)]) + b"".join(singles)
    return bytes(out)


def _pack_row(row: bytes, bytes_per_pixel: int) -> bytes:
    """One row in PWG's run-length form."""
    pixel = row[:bytes_per_pixel]
    count = len(row) // bytes_per_pixel
    if row == pixel * count:
        whole, rest = divmod(count, MAX_RUN)
        packed = (bytes([MAX_RUN - 1]) + pixel) * whole
        return packed + bytes([rest - 1]) + pixel if rest else packed
    if _repeats_little(row, bytes_per_pixel):
        return _literals(row, bytes_per_pixel)
    pattern = _run_pattern(bytes_per_pixel)
    runs = [
        (match.group(1), len(match.group(2)) // bytes_per_pixel + 1)
        for match in pattern.finditer(row)
    ]
    return _pack_runs(runs)


def _encode_rows(rows: Iterable[bytes], bytes_per_pixel: int) -> Iterator[bytes]:
    """The page body: each distinct run of equal rows as ``repeat - 1`` and the packed row."""
    previous: bytes | None = None
    packed = b""
    repeat = 0
    for row in rows:
        if previous is not None and repeat < MAX_LINE_REPEAT and row == previous:
            repeat += 1
            continue
        if previous is not None:
            yield bytes([repeat - 1]) + packed
        if row != previous:
            packed = _pack_row(row, bytes_per_pixel)
        previous, repeat = row, 1
    if previous is not None:
        yield bytes([repeat - 1]) + packed


# --- pages ------------------------------------------------------------------------------------


def _header(  # noqa: PLR0913, PLR0917
    width: int, height: int, dpi: int, media: str, gray: bool, total_pages: int
) -> bytes:
    points = media_points(media) or A4_POINTS
    bytes_per_pixel = 1 if gray else 3
    header = bytearray(HEADER_SIZE)
    header[0:10] = b"PwgRaster\x00"

    def put(offset: int, *values: int) -> None:
        struct.pack_into(f">{len(values)}I", header, offset, *values)

    put(276, dpi, dpi)  # HWResolution
    put(340, 1)  # NumCopies
    put(352, *points)  # PageSize, in points
    put(372, width, height)
    put(384, 8, bytes_per_pixel * 8, width * bytes_per_pixel)  # bits per color/pixel, bytes/line
    put(396, 0, COLOR_SPACES[SGRAY_8 if gray else SRGB_8])  # chunked pixels, color space
    put(420, bytes_per_pixel)  # NumColors
    put(452, total_pages, 1, 1)  # TotalPageCount, CrossFeedTransform, FeedTransform
    put(464, 0, 0, width, height)  # ImageBox
    put(480, 0xFFFFFF)  # AlternatePrimary: white
    name = media.encode("ascii", "replace")[:63]
    header[1732 : 1732 + len(name)] = name  # PageSizeName
    return bytes(header)


def encode_pages(  # noqa: PLR0913
    pages: Iterable[Iterable[bytes]],
    *,
    total_pages: int,
    media: str,
    dpi: int,
    gray: bool,
    max_bytes: int | None = None,
) -> bytes:
    """A PWG raster file: every page is ``media`` at ``dpi``, given as its pixel rows.

    Raises ``RasterTooLargeError`` as soon as the file would pass ``max_bytes``.
    """
    width, height = page_pixels(media, dpi)
    bytes_per_pixel = 1 if gray else 3
    row_size = width * bytes_per_pixel
    out = bytearray(SYNC_WORD)
    for rows in pages:
        out += _header(width, height, dpi, media, gray, total_pages)
        count = 0
        checked = (row for row in rows if len(row) == row_size)
        for chunk in _encode_rows(checked, bytes_per_pixel):
            out += chunk
            count += chunk[0] + 1
            if max_bytes is not None and len(out) > max_bytes:
                raise RasterTooLargeError(
                    f"Converted for this printer the document is larger than "
                    f"{max_bytes // (1024 * 1024)} MB; print fewer pages or a lighter file."
                )
        if count != height:
            raise RasterError("A page could not be drawn at the printer's size.")
    return bytes(out)


# --- the test page ----------------------------------------------------------------------------


def _gray(color: Color) -> int:
    red, green, blue = color
    return (red * 299 + green * 587 + blue * 114) // 1000


def _row(colors: list[Color], width: int, gray: bool) -> bytes:
    """One pixel row: a white margin, the colors as equal blocks, a white margin."""
    margin = width // 12
    inner = width - 2 * margin
    pixels = bytearray()
    for index, color in enumerate(colors):
        block = inner * (index + 1) // len(colors) - inner * index // len(colors)
        pixels += (bytes([_gray(color)]) if gray else bytes(color)) * block
    edge = (bytes([_gray(WHITE)]) if gray else bytes(WHITE)) * margin
    return bytes(edge + pixels + edge)


def _layout(height: int) -> list[Segment]:
    """The test page, top to bottom: ink swatches, nozzle-check lines per ink, a gray ramp."""
    segments: list[Segment] = [([WHITE], height // 14), (SWATCHES, height * 12 // 100)]
    segments.append(([WHITE], height // 30))
    for ink in INKS:
        for _ in range(height * 6 // 100 // (2 * LINE_PIXELS)):
            segments += [([ink], LINE_PIXELS), ([WHITE], LINE_PIXELS)]
        segments.append(([WHITE], height // 60))
    segments.append((GRAY_RAMP, height * 8 // 100))
    segments.append(([WHITE], height - sum(count for _, count in segments)))
    return segments


def build_test_page(media: str, dpi: int, *, gray: bool = False) -> bytes:
    """A one-page PWG raster file of the test page for ``media`` at ``dpi``."""
    width, height = page_pixels(media, dpi)
    drawn: dict[tuple[Color, ...], bytes] = {}

    def rows() -> Iterator[bytes]:
        for colors, count in _layout(height):
            key = tuple(colors)
            if key not in drawn:
                drawn[key] = _row(colors, width, gray)
            for _ in range(count):
                yield drawn[key]

    return encode_pages([rows()], total_pages=1, media=media, dpi=dpi, gray=gray)
