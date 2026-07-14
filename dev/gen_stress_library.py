#!/usr/bin/env python3
"""Generate a synthetic Navidrome music library at Skope-scale (#cover-stall).

Shape matches the real bug report exactly: ~5,200 albums / ~73k tracks
(≈14 tracks per album, 564GB library — ours uses 1-second silent MP3s so the
whole thing fits in ~2.5GB and scans in minutes). Every album gets a UNIQUE
cover.jpg, with a size mix that makes server-side thumbnail generation
genuinely expensive on first touch (that cost is the prime suspect):

  60%  600×600   (typical rip)
  30%  1500×1500 (bandcamp-era)
  10%  3000×3000 (DJ-mix / scan territory — the resize hogs)

The output folder is PORTABLE: point any Navidrome at it (native binary,
Docker on Ubuntu, wherever). Tags are written with mutagen so Navidrome
groups albums/artists correctly; per-album folders carry cover.jpg (folder
art is what Navidrome prefers and what getCoverArt resizes).

Usage:
    .venv/bin/python dev/gen_stress_library.py /mnt/Storage/jt-stress-library \
        [--albums 5200] [--tracks-per-album 14] [--seed 42]

Idempotent-ish: albums are numbered; re-running with the same target skips
folders that already look complete, so an interrupted run resumes.
"""

from __future__ import annotations

import argparse
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mutagen.id3 import ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK
from PIL import Image, ImageDraw

GENRES = ["Electronic", "Rock", "Jazz", "Ambient", "House", "Folk", "Classical", "Hip-Hop"]
COVER_MIX = [(600, 0.60), (1500, 0.30), (3000, 0.10)]


def make_template_mp3(path: Path) -> None:
    """One second of silence, 128kbps CBR — ~16KB. All tracks copy this."""
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-t", "1", "-b:a", "128k", "-codec:a", "libmp3lame", str(path)],
        check=True, capture_output=True,
    )


def cover_size(rng: random.Random) -> int:
    r = rng.random()
    acc = 0.0
    for size, frac in COVER_MIX:
        acc += frac
        if r <= acc:
            return size
    return COVER_MIX[-1][0]


def make_cover(path: Path, album_no: int, size: int, rng: random.Random) -> None:
    """Unique-looking cover: random two-tone diagonal + big album number.
    JPEG quality 90 so the 3000px ones are realistically multi-MB."""
    c1 = tuple(rng.randrange(30, 220) for _ in range(3))
    c2 = tuple(rng.randrange(30, 220) for _ in range(3))
    im = Image.new("RGB", (size, size), c1)
    d = ImageDraw.Draw(im)
    d.polygon([(0, 0), (size, 0), (0, size)], fill=c2)
    # Number block — no font dependency, just chunky rectangles per digit.
    digits = str(album_no)
    bw = size // (len(digits) * 2 + 1)
    x = bw
    for ch in digits:
        seg = int(ch)
        d.rectangle([x, size // 3, x + bw, size // 3 + (seg + 2) * size // 14],
                    fill=(255, 255, 255))
        x += bw * 2
    im.save(path, "JPEG", quality=90)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("--albums", type=int, default=5200)
    ap.add_argument("--tracks-per-album", type=int, default=14)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    root = Path(args.target)
    root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        template = Path(td) / "t.mp3"
        make_template_mp3(template)
        template_bytes = template.read_bytes()

    done = skipped = 0
    for a in range(1, args.albums + 1):
        artist = f"Stress Artist {((a - 1) % 400) + 1:03d}"  # ~13 albums/artist
        album = f"Stress Album {a:05d}"
        adir = root / artist / album
        cover = adir / "cover.jpg"
        last_track = adir / f"{args.tracks_per_album:02d} - Track {args.tracks_per_album:02d}.mp3"
        if cover.exists() and last_track.exists():
            skipped += 1
            continue
        adir.mkdir(parents=True, exist_ok=True)
        make_cover(cover, a, cover_size(rng), rng)
        year = 1970 + (a % 55)
        genre = GENRES[a % len(GENRES)]
        for t in range(1, args.tracks_per_album + 1):
            f = adir / f"{t:02d} - Track {t:02d}.mp3"
            f.write_bytes(template_bytes)
            tags = ID3()
            tags.add(TIT2(encoding=3, text=f"Track {t:02d} ({album})"))
            tags.add(TALB(encoding=3, text=album))
            tags.add(TPE1(encoding=3, text=artist))
            tags.add(TPE2(encoding=3, text=artist))
            tags.add(TRCK(encoding=3, text=str(t)))
            tags.add(TCON(encoding=3, text=genre))
            tags.add(TDRC(encoding=3, text=str(year)))
            tags.save(f)
        done += 1
        if done % 250 == 0:
            print(f"  {done + skipped}/{args.albums} albums…", flush=True)

    total_tracks = args.albums * args.tracks_per_album
    du = shutil.disk_usage(root)
    print(f"done: {done} generated, {skipped} skipped (already complete)")
    print(f"library: {args.albums} albums / {total_tracks} tracks at {root}")
    print(f"target disk free: {du.free // (1024**3)}GB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
