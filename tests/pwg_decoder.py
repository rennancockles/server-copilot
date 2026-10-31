"""A PWG raster reader for tests, written from the spec and independent of the writer."""

import struct

HEADER_SIZE = 1796
WHITE = bytes((255, 255, 255))


class Page:
    """One page read back the way a printer would."""

    def __init__(self, data: bytes, start: int = 4) -> None:
        assert data[:4] == b"RaS2"
        self.header = data[start : start + HEADER_SIZE]
        self.width, self.height = self.field(372, 2)
        self.bytes_per_pixel = self.field(388)[0] // 8
        position = start + HEADER_SIZE
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
        self.end = position

    def field(self, offset: int, count: int = 1) -> tuple[int, ...]:
        return struct.unpack_from(f">{count}I", self.header, offset)

    def pixels(self, row: int) -> set[bytes]:
        size = self.bytes_per_pixel
        data = self.rows[row]
        return {data[i : i + size] for i in range(0, len(data), size)}

    def ink_box(self) -> tuple[int, int]:
        """Width and height in pixels of the area that is not white (a gray page)."""
        assert self.bytes_per_pixel == 1
        inked = [len(row) - row.count(255) for row in self.rows]
        return max(inked), sum(1 for count in inked if count)


def read_pages(data: bytes) -> list[Page]:
    pages: list[Page] = []
    position = 4
    while position < len(data):
        page = Page(data, position)
        pages.append(page)
        position = page.end
    return pages
