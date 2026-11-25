"""Check that an archived HEVC clip stays decodable after Apple remuxing."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

APP = Path(os.environ.get("EAC_AI_APP_DIR", Path(__file__).resolve().parents[1] / "eac_vision_ai"))
sys.path.insert(0, str(APP))
from mp4_compat import Mp4Compatibility, needs_hvc1  # noqa: E402

FFMPEG = os.environ.get("EAC_FFMPEG", "/usr/bin/ffmpeg")


def frame_hash(path: Path) -> list[str]:
    result = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-map", "0:v:0",
                             "-frames:v", "3", "-f", "framemd5", "-"],
                            capture_output=True, text=True, timeout=30, check=True)
    return [line for line in result.stdout.splitlines() if line.startswith("0,")]


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        data = Path(directory)
        sample = os.environ.get("EAC_SAMPLE_MP4")
        source = Path(sample) if sample else data / "source.mp4"
        if not sample:
            subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=160x90:rate=3", "-frames:v", "3",
                            "-threads", "1", "-c:v", "libx265", "-preset", "ultrafast",
                            "-pix_fmt", "yuv420p", "-y", str(source)],
                           capture_output=True, timeout=45, check=True)
        assert needs_hvc1(source), "Il campione non è HEVC/hev1"
        cache = Mp4Compatibility(data, FFMPEG)
        result = asyncio.run(cache.compatible_path(source))
        assert result != source and result.is_file(), "Il remux non è stato creato"
        assert not needs_hvc1(result) and b"hvc1" in result.read_bytes()
        assert frame_hash(source) == frame_hash(result), "I fotogrammi sono cambiati"
        assert asyncio.run(cache.compatible_path(source)) == result, "La cache non è stata riutilizzata"
        print("HEVC hev1 to hvc1: video invariato e cache riutilizzata")


if __name__ == "__main__":
    main()
