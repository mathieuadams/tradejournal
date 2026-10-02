"""Small dependency-free PNG chart renderer (candles, volume, lines, zones, labeled levels) for the bot's final
visual check by Claude. Pure Python: an RGB canvas, a 5x7 bitmap font, zlib for the PNG."""
import struct
import zlib

FONT = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "D": ["11100", "10010", "10001", "10001", "10001", "10010", "11100"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01111"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    "J": ["00111", "00010", "00010", "00010", "00010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "10001", "11001", "10101", "10011", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "10101", "01010"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    "+": ["00000", "00100", "00100", "11111", "00100", "00100", "00000"],
    ":": ["00000", "01100", "01100", "00000", "01100", "01100", "00000"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    "%": ["11001", "11010", "00010", "00100", "01000", "01011", "10011"],
    "$": ["00100", "01111", "10100", "01110", "00101", "11110", "00100"],
    "(": ["00010", "00100", "01000", "01000", "01000", "00100", "00010"],
    ")": ["01000", "00100", "00010", "00010", "00010", "00100", "01000"],
    " ": ["00000"] * 7,
}

BG = (255, 255, 255)
GRID = (234, 236, 240)
TEXT = (40, 44, 52)
UP = (22, 163, 74)
DOWN = (220, 38, 38)


class Canvas:
    def __init__(self, w, h, bg=BG):
        self.w, self.h = w, h
        self.px = bytearray(bytes(bg) * (w * h))

    def set(self, x, y, c, a=1.0):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            if a >= 1:
                self.px[i:i + 3] = bytes(c)
            else:
                for k in range(3):
                    self.px[i + k] = int(self.px[i + k] * (1 - a) + c[k] * a)

    def rect(self, x0, y0, x1, y1, c, a=1.0):
        x0, x1 = sorted((int(x0), int(x1)))
        y0, y1 = sorted((int(y0), int(y1)))
        for y in range(max(0, y0), min(self.h, y1 + 1)):
            for x in range(max(0, x0), min(self.w, x1 + 1)):
                self.set(x, y, c, a)

    def line(self, x0, y0, x1, y1, c, width=1, dash=0):
        x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err, n = dx + dy, 0
        while True:
            if not dash or (n // dash) % 2 == 0:
                for o in range(width):
                    self.set(x0, y0 + o, c)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy
            n += 1

    def text(self, x, y, s, c=TEXT, scale=2, bg=None):
        s = str(s).upper()
        if bg:
            self.rect(x - 2, y - 2, x + len(s) * 6 * scale, y + 7 * scale + 1, bg)
        for ch in s:
            g = FONT.get(ch, FONT[" "])
            for r, row in enumerate(g):
                for k, bit in enumerate(row):
                    if bit == "1":
                        self.rect(x + k * scale, y + r * scale, x + k * scale + scale - 1, y + r * scale + scale - 1, c)
            x += 6 * scale

    def png(self):
        raw = b"".join(b"\x00" + bytes(self.px[y * self.w * 3:(y + 1) * self.w * 3]) for y in range(self.h))
        chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _fmt(v):
    return f"{v:.2f}" if v < 100 else f"{v:.1f}" if v < 1000 else f"{v:.0f}"


def render(bars, title, lines=None, levels=None, zones=None, width=1100, height=620):
    """bars: [{t,o,h,l,c,v}]. lines: [(label, color, [value or None per bar])]. levels: [(label, value, color, dashed)].
    zones: [(label, lo, hi, color, start_index)] shaded from start_index to the right edge."""
    lines, levels, zones = lines or [], levels or [], zones or []
    cv = Canvas(width, height)
    left, right, top = 10, 112, 40
    vol_h = int(height * 0.16)
    bottom = height - 26
    pbot = bottom - vol_h - 8
    n = len(bars)
    if not n:
        cv.text(left, 10, f"{title}: NO DATA")
        return cv.png()
    vals = [b["h"] for b in bars] + [b["l"] for b in bars]
    for _, _, ser in lines:
        vals += [v for v in ser if v is not None]
    lo, hi = min(vals), max(vals)
    near = [v for _, v, _, _ in levels if v and lo * 0.85 <= v <= hi * 1.15]
    lo, hi = min([lo] + near), max([hi] + near)
    pad = (hi - lo) * 0.04 or 1
    lo, hi = lo - pad, hi + pad
    pw = width - left - right
    step = pw / n
    xo = lambda i: left + step * (i + 0.5)
    y = lambda v: top + (hi - v) / (hi - lo) * (pbot - top)
    # grid + price axis
    for k in range(7):
        v = lo + (hi - lo) * k / 6
        yy = int(y(v))
        cv.line(left, yy, width - right, yy, GRID)
        cv.text(width - right + 8, yy - 6, _fmt(v), (120, 126, 136), 2)
    # zones (fair value gaps)
    for label, zlo, zhi, col, start in zones:
        if zlo is None or zhi is None:
            continue
        x0 = left + step * max(0, start)
        cv.rect(x0, y(max(zlo, zhi)), width - right, y(min(zlo, zhi)), col, 0.18)
        cv.text(int(x0) + 4, int(y(max(zlo, zhi))) + 3, label, col, 2)
    # volume
    vmax = max(b["v"] for b in bars) or 1
    vb = bottom
    for i, b in enumerate(bars):
        hh = (b["v"] / vmax) * vol_h
        col = UP if b["c"] >= b["o"] else DOWN
        cv.rect(xo(i) - max(1, step * 0.35), vb - hh, xo(i) + max(1, step * 0.35), vb, col, 0.45)
    # candles
    bw = max(1, step * 0.32)
    for i, b in enumerate(bars):
        col = UP if b["c"] >= b["o"] else DOWN
        cv.line(xo(i), y(b["h"]), xo(i), y(b["l"]), col)
        cv.rect(xo(i) - bw, y(max(b["o"], b["c"])), xo(i) + bw, y(min(b["o"], b["c"])), col)
    # indicator lines
    for label, col, ser in lines:
        prev = None
        for i, v in enumerate(ser):
            if v is None:
                prev = None
                continue
            if prev is not None:
                cv.line(xo(i - 1), y(prev), xo(i), y(v), col, 2)
            prev = v
    # horizontal levels with right-side labels
    used = []
    for label, v, col, dashed in levels:
        if v is None or not (lo <= v <= hi):
            continue
        yy = int(y(v))
        cv.line(left, yy, width - right, yy, col, 2, 8 if dashed else 0)
        ty = yy - 7
        while any(abs(ty - u) < 16 for u in used):
            ty += 16
        used.append(ty)
        cv.text(width - right - 6 - len(f"{label} {_fmt(v)}") * 12, ty, f"{label} {_fmt(v)}", col, 2, BG)
    # title + legend
    cv.text(left, 10, title, TEXT, 2)
    lx = left + len(title) * 12 + 24
    for label, col, _ in lines:
        cv.rect(lx, 14, lx + 16, 17, col)
        cv.text(lx + 22, 10, label, col, 2)
        lx += 22 + len(label) * 12 + 18
    cv.text(left, height - 18, f"{bars[0]['t'][:10]} TO {bars[-1]['t'][:16].replace('T', ' ')}", (120, 126, 136), 2)
    return cv.png()
