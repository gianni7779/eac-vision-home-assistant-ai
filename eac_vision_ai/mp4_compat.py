"""Serve Apple-compatible HEVC MP4 files without decoding the video."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path


def needs_hvc1(path: Path) -> bool:
    """FFmpeg's segment muxer writes the HEVC sample entry near either end."""
    with path.open("rb") as source:
        size = source.seek(0, os.SEEK_END)
        source.seek(max(0, size - 2 * 1024 * 1024))
        tail = source.read()
        if b"hev1" in tail and b"hvcC" in tail:
            return True
        source.seek(0)
        head = source.read(min(size, 2 * 1024 * 1024))
        return b"hev1" in head and b"hvcC" in head


class Mp4Compatibility:
    def __init__(self, data: Path, ffmpeg: str, max_bytes: int = 256 * 1024 * 1024):
        self.directory = data / "apple_mp4"
        self.ffmpeg = ffmpeg
        self.max_bytes = max_bytes
        self.lock = asyncio.Lock()

    async def compatible_path(self, source: Path) -> Path:
        """Return a cached hvc1 remux for hev1 files; H.264 files pass through."""
        try:
            stat = await asyncio.to_thread(source.stat)
            identity = f"{source}:{stat.st_size}:{stat.st_mtime_ns}".encode()
            target = self.directory / (hashlib.sha256(identity).hexdigest() + ".mp4")
            if target.is_file():
                return target
            if not await asyncio.to_thread(needs_hvc1, source):
                return source
            async with self.lock:
                if target.is_file():
                    return target
                self.directory.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(".partial")
                try:
                    process = await asyncio.create_subprocess_exec(
                        self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                        "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
                        "-c", "copy", "-tag:v:0", "hvc1", "-movflags", "+faststart",
                        "-f", "mp4", "-y", str(temporary),
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                    try:
                        _, error = await asyncio.wait_for(process.communicate(), timeout=60)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.communicate()
                        raise RuntimeError("Remux HEVC scaduto")
                    if process.returncode or not temporary.is_file() or temporary.stat().st_size == 0:
                        raise RuntimeError("Remux HEVC fallito: " + error[-300:].decode("utf-8", "replace"))
                    temporary.replace(target)
                    try:
                        await asyncio.to_thread(self._trim, target)
                    except OSError:
                        pass  # A cache cleanup failure must not hide a playable file.
                    return target
                finally:
                    temporary.unlink(missing_ok=True)
        except (OSError, RuntimeError) as exc:
            print("EAC Vision: compatibilità MP4 Apple: " + type(exc).__name__, flush=True)
            return source

    def _trim(self, keep: Path) -> None:
        files = sorted(((p.stat().st_mtime_ns, p.stat().st_size, p)
                        for p in self.directory.glob("*.mp4") if p.is_file() and not p.is_symlink()))
        total = sum(size for _, size, _ in files)
        for _, size, path in files:
            if total <= self.max_bytes:
                break
            if path == keep:
                continue
            path.unlink(missing_ok=True)
            total -= size
