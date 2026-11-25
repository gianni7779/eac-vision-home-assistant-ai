"""Exercise the deployed go2rtc → RTSP → FFmpeg → raw frame path."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP = Path(os.environ.get("EAC_AI_APP_DIR", Path(__file__).resolve().parents[1] / "eac_vision_ai"))
sys.path.insert(0, str(APP))
import core  # noqa: E402
from ai import ai_ffmpeg_args, ffmpeg_error  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "go2rtc.yaml"
        config.write_text('api:\n  listen: "127.0.0.1:11985"\nrtsp:\n'
                          '  listen: "127.0.0.1:18555"\nwebrtc:\n  listen: ""\nsrtp:\n  listen: ""\nstreams:\n'
                          '  synthetic: "ffmpeg:virtual?video=testsrc&size=640x360#video=h264"\n', encoding="utf-8")
        environment = os.environ.copy()
        environment["PATH"] = str(Path(core.FFMPEG).parent) + os.pathsep + environment.get("PATH", "")
        with open(Path(directory) / "go2rtc.log", "wb") as log:
            relay = subprocess.Popen([core.GO2RTC, "-config", str(config)],
                                     stdout=log, stderr=subprocess.STDOUT, env=environment,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                time.sleep(2)
                command = ai_ffmpeg_args("rtsp://127.0.0.1:18555/synthetic?video")
                command[-1:-1] = ["-frames:v", "1"]
                result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        timeout=25, check=False)
                expected = 640 * 360 * 3
                if result.returncode or len(result.stdout) != expected:
                    print(ffmpeg_error(result.stderr.decode("utf-8", "replace"), result.returncode))
                    print("bytes=", len(result.stdout), "expected=", expected)
                    print((Path(directory) / "go2rtc.log").read_text(encoding="utf-8", errors="replace")[-1500:])
                    return 1
                print(f"RTSP/FFmpeg decoder OK: {len(result.stdout)} bytes per frame")
                return 0
            finally:
                relay.terminate()
                try:
                    relay.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    relay.kill()
                    relay.wait()


if __name__ == "__main__":
    raise SystemExit(main())
