"""A minimal PWG raster (PWG 5102.4) writer, just enough for the built-in test page.

Some printers (the Epson EcoTank ET-2860 among them) take neither PDF nor JPEG over IPP, only
``image/pwg-raster``. The test page is simple (solid blocks and fine lines), so it is drawn row by
row straight into that format: no rendering library is needed. The page is described as a list of
``(colors, count)`` segments, each a row of equal-width blocks repeated ``count`` times, and each
distinct row is compressed once.
"""

import re
import struct
from itertools import groupby

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


def media_points(name: str) -> tuple[int, int] | None:
    """The width and height in points of a PWG media name such as ``iso_a4_210x297mm``."""
    match = _MEDIA_SIZE.search(name)
    if not match:
        return None
    unit = POINTS_PER_INCH / (MM_PER_INCH if match.group(3) == "mm" else 1)
    return round(float(match.group(1)) * unit), round(float(match.group(2)) * unit)


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


def _pack_row(row: bytes, bytes_per_pixel: int) -> bytes:
    """One row in PWG's run-length form: ``n-1, pixel`` repeats, ``257-n, pixels...`` literals."""
    pixels = [row[i : i + bytes_per_pixel] for i in range(0, len(row), bytes_per_pixel)]
    runs: list[tuple[bytes, int]] = []
    for pixel, group in groupby(pixels):
        length = len(list(group))
        while length > 0:
            runs.append((pixel, min(length, MAX_RUN)))
            length -= MAX_RUN
    out = bytearray()
    index = 0
    while index < len(runs):
        pixel, length = runs[index]
        if length > 1:
            out += bytes([length - 1]) + pixel
            index += 1
            continue
        singles = [pixel]
        index += 1
        while index < len(runs) and runs[index][1] == 1 and len(singles) < MAX_RUN:
            singles.append(runs[index][0])
            index += 1
        if len(singles) == 1:
            out += b"\x00" + singles[0]
        else:
            out += bytes([257 - len(singles)]) + b"".join(singles)
    return bytes(out)


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


def _page_points(media: str) -> tuple[int, int]:
    return media_points(media) or A4_POINTS


def _header(width: int, height: int, dpi: int, media: str, gray: bool) -> bytes:
    points = _page_points(media)
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
    put(452, 1, 1, 1)  # TotalPageCount, CrossFeedTransform, FeedTransform
    put(464, 0, 0, width, height)  # ImageBox
    put(480, 0xFFFFFF)  # AlternatePrimary: white
    name = media.encode("ascii", "replace")[:63]
    header[1732 : 1732 + len(name)] = name  # PageSizeName
    return bytes(header)


def build_test_page(media: str, dpi: int, *, gray: bool = False) -> bytes:
    """A one-page PWG raster file of the test page for ``media`` at ``dpi``."""
    points = _page_points(media)
    width = round(points[0] * dpi / POINTS_PER_INCH)
    height = round(points[1] * dpi / POINTS_PER_INCH)
    bytes_per_pixel = 1 if gray else 3
    body = bytearray()
    packed_rows: dict[tuple[Color, ...], bytes] = {}
    for colors, count in _layout(height):
        key = tuple(colors)
        if key not in packed_rows:
            packed_rows[key] = _pack_row(_row(colors, width, gray), bytes_per_pixel)
        packed = packed_rows[key]
        remaining = count
        while remaining > 0:
            repeat = min(remaining, MAX_LINE_REPEAT)
            body += bytes([repeat - 1]) + packed
            remaining -= repeat
    return SYNC_WORD + _header(width, height, dpi, media, gray) + bytes(body)
