#!/usr/bin/env python3
"""Generate the page-background paper grain (public/grain-{light,dark}.png).

Our own texture, matched to the feel of a fine paper grain: a 248px tileable
field of Gaussian noise, lightly blurred (neighbour correlation ~0.8) and
scaled to a standard deviation of ~1.7 grey levels, shown at 124px (two
texels per CSS pixel). Stored as a transparent overlay (white texels lift,
black texels darken) so one tile works on any palette's ground colour.
Deterministic: same seed, same file. Run: python3 scripts/gen-grain.py
"""
import math
import random
import struct
import zlib
from pathlib import Path

N = 248
SD_LEVELS = 1.6      # target standard deviation in grey levels on the ground
SIGMA = 1.2          # blur radius in texels (gives ~0.8 neighbour correlation)
OUT = Path(__file__).resolve().parent.parent / "public"


def field(seed: int) -> list[float]:
    rnd = random.Random(seed)
    f = [rnd.gauss(0, 1) for _ in range(N * N)]
    r = 3
    k = [math.exp(-(i * i) / (2 * SIGMA * SIGMA)) for i in range(-r, r + 1)]
    s = sum(k)
    k = [v / s for v in k]
    tmp = [0.0] * (N * N)
    for y in range(N):  # horizontal pass, wrapping so the tile has no seams
        row = y * N
        for x in range(N):
            tmp[row + x] = sum(k[i + r] * f[row + (x + i) % N] for i in range(-r, r + 1))
    out = [0.0] * (N * N)
    for y in range(N):  # vertical pass
        for x in range(N):
            out[y * N + x] = sum(k[i + r] * tmp[((y + i) % N) * N + x] for i in range(-r, r + 1))
    mean = sum(out) / len(out)
    sd = math.sqrt(sum((v - mean) ** 2 for v in out) / len(out))
    return [(v - mean) / sd for v in out]


def png(path: Path, rgba: bytes) -> None:
    raw = b"".join(b"\x00" + rgba[y * N * 4:(y + 1) * N * 4] for y in range(N))
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.write_bytes(data)


def overlay(noise: list[float], ground: float) -> bytes:
    """White texels lift by d levels, black texels darken by d, on a ground of luminance `ground`."""
    out = bytearray()
    for z in noise:
        d = max(-5.5, min(5.5, z * SD_LEVELS))
        if d >= 0:
            a = d / max(1.0, 255 - ground)
            out += bytes((255, 255, 255, min(255, round(a * 255))))
        else:
            a = -d / max(1.0, ground)
            out += bytes((0, 0, 0, min(255, round(a * 255))))
    return bytes(out)


if __name__ == "__main__":
    noise = field(seed=20261025)
    OUT.mkdir(exist_ok=True)
    png(OUT / "grain-light.png", overlay(noise, ground=236))
    png(OUT / "grain-dark.png", overlay(noise, ground=20))
    print("wrote", OUT / "grain-light.png", "and", OUT / "grain-dark.png")
