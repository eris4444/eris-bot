"""A small QR code encoder (byte mode, versions 1-40, ECC L/M/Q/H) and a PNG writer, pure Python.

Follows ISO/IEC 18004: Reed-Solomon over GF(256) (0x11D), block interleaving, the 8 masks with the
standard penalty rules, BCH-coded format and version information.
"""
import struct
import zlib

ECL = {"L": (0, 1), "M": (1, 0), "Q": (2, 3), "H": (3, 2)}   # name -> (table row, format bits)

ECC_CODEWORDS_PER_BLOCK = (
    (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)
NUM_ERROR_CORRECTION_BLOCKS = (
    (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)

MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def raw_modules(ver):
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        a = ver // 7 + 2
        n -= (25 * a - 10) * a - 55
        if ver >= 7:
            n -= 36
    return n


def data_codewords(ver, ecl):
    row = ECL[ecl][0]
    return raw_modules(ver) // 8 - ECC_CODEWORDS_PER_BLOCK[row][ver] * NUM_ERROR_CORRECTION_BLOCKS[row][ver]


def gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def rs_divisor(degree):
    res = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            res[j] = gf_mul(res[j], root)
            if j + 1 < degree:
                res[j] ^= res[j + 1]
        root = gf_mul(root, 0x02)
    return res


def rs_remainder(data, divisor):
    res = [0] * len(divisor)
    for b in data:
        factor = b ^ res.pop(0)
        res.append(0)
        for i, coef in enumerate(divisor):
            res[i] ^= gf_mul(coef, factor)
    return res


class QR:
    def __init__(self, data, ecl="M", version=None, mask=None):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.ecl = ecl
        for ver in ([version] if version else range(1, 41)):
            cc = 8 if ver < 10 else 16
            if 4 + cc + 8 * len(data) <= data_codewords(ver, ecl) * 8:
                break
        else:
            raise ValueError("too long for a QR code")
        self.version = ver
        self.size = ver * 4 + 17
        bits = []

        def put(val, n):
            bits.extend((val >> i) & 1 for i in reversed(range(n)))
        put(0b0100, 4)
        put(len(data), cc)
        for b in data:
            put(b, 8)
        cap = data_codewords(ver, ecl) * 8
        put(0, min(4, cap - len(bits)))
        put(0, -len(bits) % 8)
        pad = 0xEC
        while len(bits) < cap:
            put(pad, 8)
            pad ^= 0xEC ^ 0x11
        codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]
        self._function_patterns()
        self._draw_codewords(self._interleave(codewords))
        if mask is None:
            best, best_pen = 0, None
            for m in range(8):
                self._apply_mask(m)
                self._format_bits(m)
                pen = self._penalty()
                if best_pen is None or pen < best_pen:
                    best, best_pen = m, pen
                self._apply_mask(m)
            mask = best
        self.mask = mask
        self._apply_mask(mask)
        self._format_bits(mask)

    # ------------------------------------------------------------ function patterns
    def _set(self, x, y, dark):
        self.modules[y][x] = dark
        self.function[y][x] = True

    def _function_patterns(self):
        s = self.size
        for i in range(s):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for (cx, cy) in ((3, 3), (s - 4, 3), (3, s - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < s and 0 <= y < s:
                        self._set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = self._alignment_positions()
        n = len(pos)
        for i in range(n):
            for j in range(n):
                if (i == 0 and j == 0) or (i == 0 and j == n - 1) or (i == n - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(pos[i] + dx, pos[j] + dy, max(abs(dx), abs(dy)) != 1)
        self._format_bits(0)
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.version << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = s - 11 + i % 3, i // 3
                self._set(a, b, bit)
                self._set(b, a, bit)

    def _alignment_positions(self):
        ver = self.version
        if ver == 1:
            return []
        n = ver // 7 + 2
        step = 26 if ver == 32 else (ver * 4 + n * 2 + 1) // (n * 2 - 2) * 2
        return list(reversed([self.size - 7 - i * step for i in range(n - 1)] + [6]))

    def _format_bits(self, mask):
        data = ECL[self.ecl][1] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        s = self.size
        for i in range(0, 6):
            self._set(8, i, bit(i))
        self._set(8, 7, bit(6))
        self._set(8, 8, bit(7))
        self._set(7, 8, bit(8))
        for i in range(9, 15):
            self._set(14 - i, 8, bit(i))
        for i in range(0, 8):
            self._set(s - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set(8, s - 15 + i, bit(i))
        self._set(8, s - 8, True)

    # ------------------------------------------------------------ data
    def _interleave(self, data):
        row = ECL[self.ecl][0]
        nblocks = NUM_ERROR_CORRECTION_BLOCKS[row][self.version]
        ecclen = ECC_CODEWORDS_PER_BLOCK[row][self.version]
        raw = raw_modules(self.version) // 8
        nshort = nblocks - raw % nblocks
        shortlen = raw // nblocks
        div = rs_divisor(ecclen)
        blocks, k = [], 0
        for i in range(nblocks):
            dat = data[k:k + shortlen - ecclen + (0 if i < nshort else 1)]
            k += len(dat)
            ecc = rs_remainder(dat, div)
            if i < nshort:
                dat = dat + [0]
            blocks.append(dat + ecc)
        out = []
        for i in range(len(blocks[0])):
            for j, blk in enumerate(blocks):
                if i != shortlen - ecclen or j >= nshort:
                    out.append(blk[i])
        return out

    def _draw_codewords(self, data):
        s = self.size
        i = 0
        right = s - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(s):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = (s - 1 - vert) if upward else vert
                    if not self.function[y][x] and i < len(data) * 8:
                        self.modules[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def _apply_mask(self, m):
        f = MASKS[m]
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and f(x, y):
                    self.modules[y][x] = not self.modules[y][x]

    def _penalty(self):
        s, M = self.size, self.modules
        pen = 0
        lines = [M[y] for y in range(s)] + [[M[y][x] for y in range(s)] for x in range(s)]
        pat1 = [True, False, True, True, True, False, True, False, False, False, False]
        pat2 = list(reversed(pat1))
        for line in lines:
            run, prev = 0, None
            for v in line:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        pen += 3 + run - 5
                    run, prev = 1, v
            if run >= 5:
                pen += 3 + run - 5
            for i in range(s - 10):
                seg = line[i:i + 11]
                if seg == pat1 or seg == pat2:
                    pen += 40
        for y in range(s - 1):
            for x in range(s - 1):
                c = M[y][x]
                if c == M[y][x + 1] == M[y + 1][x] == M[y + 1][x + 1]:
                    pen += 3
        dark = sum(sum(1 for v in row if v) for row in M)
        total = s * s
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        pen += k * 10
        return pen

    # ------------------------------------------------------------ output
    def png(self, scale=8, border=4):
        dim = (self.size + 2 * border) * scale
        white, black = b"\xff", b"\x00"
        raw = bytearray()
        blank = b"\x00" + white * dim
        for _ in range(border * scale):
            raw += blank
        for row in self.modules:
            line = bytearray(b"\x00")
            line += white * (border * scale)
            for v in row:
                line += (black if v else white) * scale
            line += white * (border * scale)
            for _ in range(scale):
                raw += line
        for _ in range(border * scale):
            raw += blank

        def chunk(tag, body):
            return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", dim, dim, 8, 0, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))

    def text(self):
        return "\n".join("".join("██" if v else "  " for v in row) for row in self.modules)


def png(data, ecl="M"):
    return QR(data, ecl).png()
