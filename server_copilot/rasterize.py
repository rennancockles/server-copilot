"""Renders an uploaded PDF or JPEG into PWG raster pages, for printers that take neither.

Both inputs come from the conversation, so they are treated as hostile: the size of every page is
fixed by the paper, never by the file, so a huge page or photo cannot ask for a huge bitmap; images
above ``MAX_IMAGE_PIXELS`` are refused before they are decoded; and the finished raster has a byte
limit. pdfium is not thread-safe, so rendering is serialized.
"""

import io
import threading
from collections.abc import Iterator

import pypdfium2 as pdfium
from PIL import Image, ImageOps, UnidentifiedImageError

from server_copilot.raster import (
    MM_PER_INCH,
    POINTS_PER_INCH,
    RasterError,
    encode_pages,
    page_pixels,
)

MAX_IMAGE_PIXELS = 100_000_000
PHOTO_MARGIN_MM = 5
WHITE = 255
_PDFIUM_LOCK = threading.Lock()


def _centered(image: Image.Image, width: int, height: int, gray: bool) -> Image.Image:
    """The image on a white page of the paper's size, in the middle."""
    mode = "L" if gray else "RGB"
    page = Image.new(mode, (width, height), WHITE if gray else (WHITE, WHITE, WHITE))
    if image.mode != mode:
        image = image.convert(mode)
    page.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
    return page


def _rows(page: Image.Image) -> Iterator[bytes]:
    data = page.tobytes()
    size = page.width * len(page.getbands())
    for start in range(0, len(data), size):
        yield data[start : start + size]


def _render_pdf_page(
    page: pdfium.PdfPage, width: int, height: int, dpi: int, gray: bool
) -> Image.Image:
    """One PDF page at its own size, or smaller to fit the paper, turned to fill it best."""
    page_width, page_height = page.get_size()
    turn = (page_width > page_height) != (width > height)
    if turn:
        page_width, page_height = page_height, page_width
    scale = min(dpi / POINTS_PER_INCH, width / page_width, height / page_height)
    bitmap = page.render(
        scale=scale,
        rotation=90 if turn else 0,
        fill_color=(WHITE, WHITE, WHITE, WHITE),
        grayscale=gray,
    )
    try:
        return _centered(bitmap.to_pil(), width, height, gray)
    finally:
        bitmap.close()


def rasterize_pdf(  # noqa: PLR0913
    document: bytes, *, media: str, dpi: int, gray: bool, max_bytes: int, max_pages: int
) -> bytes:
    """A PDF as PWG raster pages on ``media`` at ``dpi``."""
    width, height = page_pixels(media, dpi)
    failure = RasterError("The PDF could not be rendered for this printer; it will not be printed.")
    with _PDFIUM_LOCK:
        try:
            pdf = pdfium.PdfDocument(document)
        except pdfium.PdfiumError as exc:
            raise failure from exc
        try:
            count = len(pdf)
            if count > max_pages:
                raise RasterError(f"The document has {count} pages; the limit is {max_pages}.")
            pages = (
                _rows(_render_pdf_page(pdf[index], width, height, dpi, gray))
                for index in range(count)
            )
            return encode_pages(
                pages, total_pages=count, media=media, dpi=dpi, gray=gray, max_bytes=max_bytes
            )
        except pdfium.PdfiumError as exc:
            raise failure from exc
        finally:
            pdf.close()


def _photo(document: bytes, width: int, height: int, dpi: int, gray: bool) -> Image.Image:
    """The photo, upright, as large as fits inside a small margin, on a page of the paper's size."""
    try:
        with Image.open(io.BytesIO(document)) as opened:
            if opened.width * opened.height > MAX_IMAGE_PIXELS:
                raise RasterError("The photo has too many pixels to print; use a smaller one.")
            image = ImageOps.exif_transpose(opened).convert("L" if gray else "RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise RasterError("The photo could not be read; it will not be printed.") from exc
    margin = round(PHOTO_MARGIN_MM / MM_PER_INCH * dpi)
    box_width, box_height = width - 2 * margin, height - 2 * margin
    if (image.width > image.height) != (box_width > box_height):
        image = image.transpose(Image.Transpose.ROTATE_90)
    scale = min(box_width / image.width, box_height / image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return _centered(image.resize(size, Image.Resampling.LANCZOS), width, height, gray)


def rasterize_photo(document: bytes, *, media: str, dpi: int, gray: bool, max_bytes: int) -> bytes:
    """A JPEG as one PWG raster page on ``media`` at ``dpi``."""
    width, height = page_pixels(media, dpi)
    page = _photo(document, width, height, dpi, gray)
    return encode_pages(
        [_rows(page)], total_pages=1, media=media, dpi=dpi, gray=gray, max_bytes=max_bytes
    )
