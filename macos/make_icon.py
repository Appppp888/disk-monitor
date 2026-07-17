#!/usr/bin/env python3
from __future__ import annotations

import math
import os
import struct
import subprocess
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ICONSET = ROOT / "build" / "AppIcon.iconset"
ICNS = ROOT / "build" / "AppIcon.icns"


def chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def write_png(path: Path, size: int) -> None:
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            nx = (x / (size - 1)) * 2 - 1
            ny = (y / (size - 1)) * 2 - 1
            radius = math.sqrt(nx * nx + ny * ny)

            if radius > 0.93:
                rgba = (0, 0, 0, 0)
            else:
                shade = max(0, min(1, 1 - radius * 0.55))
                r = int(14 + 30 * shade)
                g = int(112 + 84 * shade)
                b = int(124 + 88 * shade)
                rgba = (r, g, b, 255)

            # Drive plate.
            if -0.55 < nx < 0.55 and -0.42 < ny < 0.44:
                edge = max(abs(nx) / 0.55, abs(ny) / 0.44)
                plate = int(238 - 24 * edge)
                rgba = (plate, plate + 8, min(255, plate + 14), 255)

            # Activity bars.
            for i, height in enumerate([0.18, 0.36, 0.58]):
                cx = -0.31 + i * 0.31
                if abs(nx - cx) < 0.055 and 0.30 - height < ny < 0.30:
                    rgba = (15, 127, 140, 255)

            # Small status light.
            if (nx - 0.38) ** 2 + (ny - 0.26) ** 2 < 0.012:
                rgba = (78, 210, 148, 255)

            row.extend(rgba)
        rows.append(b"\x00" + bytes(row))

    raw = b"".join(rows)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def write_icns(entries: list[tuple[bytes, Path]], output: Path) -> None:
    body = bytearray()
    for kind, path in entries:
        data = path.read_bytes()
        body.extend(kind)
        body.extend(struct.pack(">I", len(data) + 8))
        body.extend(data)
    output.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + bytes(body))


def main() -> None:
    if ICONSET.exists():
        for item in ICONSET.iterdir():
            item.unlink()
    else:
        ICONSET.mkdir(parents=True, exist_ok=True)

    sizes = [
        ("icon_16x16.png", 16),
        ("icon_16x16@2x.png", 32),
        ("icon_32x32.png", 32),
        ("icon_32x32@2x.png", 64),
        ("icon_128x128.png", 128),
        ("icon_128x128@2x.png", 256),
        ("icon_256x256.png", 256),
        ("icon_256x256@2x.png", 512),
        ("icon_512x512.png", 512),
        ("icon_512x512@2x.png", 1024),
    ]
    for filename, size in sizes:
        write_png(ICONSET / filename, size)

    if ICNS.exists():
        ICNS.unlink()
    try:
        subprocess.check_call(
            ["iconutil", "-c", "icns", str(ICONSET), "-o", str(ICNS)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        write_icns(
            [
                (b"icp4", ICONSET / "icon_16x16.png"),
                (b"icp5", ICONSET / "icon_32x32.png"),
                (b"icp6", ICONSET / "icon_32x32@2x.png"),
                (b"ic07", ICONSET / "icon_128x128.png"),
                (b"ic08", ICONSET / "icon_256x256.png"),
                (b"ic09", ICONSET / "icon_512x512.png"),
                (b"ic10", ICONSET / "icon_512x512@2x.png"),
            ],
            ICNS,
        )


if __name__ == "__main__":
    main()
