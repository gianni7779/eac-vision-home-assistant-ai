"""Low-priority audio peak index for completed recordings."""
from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path


PEAK = re.compile(rb"max_volume:\s*(-?\d+(?:\.\d+)?) dB")


class NoiseIndex:
    def __init__(self, data: Path, ffmpeg: str, vision):
        self.vision = vision
        self.ffmpeg = ffmpeg
        self.path = data / "noise.sqlite3"
        self.db: sqlite3.Connection | None = None
        self.pending = deque()
        self.queued = set()
        self.last_day_scan: dict[tuple[str, str], float] = {}
        self.task: asyncio.Task | None = None

    def start(self):
        self._connect()
        self.task = asyncio.create_task(self.run())

    def _connect(self):
        if self.db is None:
            self.db = sqlite3.connect(self.path)
            self.db.execute("CREATE TABLE IF NOT EXISTS levels (cam TEXT NOT NULL, name TEXT NOT NULL, peak REAL, PRIMARY KEY(cam,name))")
            self.db.commit()

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.db:
            self.db.close()
            self.db = None

    def enqueue(self, cam: str, paths: list[Path], recent: bool = False):
        if not paths:
            return
        self._connect()
        names = [p.name for p in paths]
        known = {name for (name,) in self.db.execute(
            "SELECT name FROM levels WHERE cam=? AND name BETWEEN ? AND ?", (cam, min(names), max(names)))}
        for path in paths:
            key = (cam, path.name)
            if path.name not in known and key not in self.queued:
                (self.pending.appendleft if recent else self.pending.append)(key)
                self.queued.add(key)

    def day(self, cam: str, day: str) -> list[dict]:
        self._connect()
        key = (cam, day)
        if time.monotonic() - self.last_day_scan.get(key, 0) >= 60:
            paths = self.vision.completed_recording_files(cam, day)
            self.enqueue(cam, paths)
            self.last_day_scan[key] = time.monotonic()
        rows = self.db.execute("SELECT name,peak FROM levels WHERE cam=? AND name LIKE ? ORDER BY name",
                               (cam, day + "%"))
        result = []
        for name, peak in rows:
            if peak is None:
                continue
            try:
                at = datetime.strptime(name, "%Y-%m-%d_%H-%M-%S.mp4").timestamp()
            except ValueError:
                continue
            result.append({"start": at, "peak_db": peak})
        return result

    async def run(self):
        cycles = 0
        while True:
            try:
                if cycles % 6 == 0:
                    for cam in self.vision.config["cameras"]:
                        self.enqueue(cam, self.vision.completed_recording_files(cam)[-2:], recent=True)
                cycles += 1
                if cycles % 720 == 0:
                    cutoff = (datetime.now() - timedelta(days=int(self.vision.config.get("retention_days", 3)) + 1)).strftime("%Y-%m-%d")
                    self.db.execute("DELETE FROM levels WHERE name < ?", (cutoff,))
                    self.db.commit()
                if self.pending:
                    cam, name = self.pending.popleft()
                    self.queued.discard((cam, name))
                    root = self.vision.storage_root(require_space=False)
                    path = root / cam / name if root else None
                    if path and path.is_file() and not path.is_symlink():
                        peak = await self.measure(path)
                        self.db.execute("INSERT OR REPLACE INTO levels(cam,name,peak) VALUES(?,?,?)", (cam, name, peak))
                        self.db.commit()
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except (OSError, sqlite3.Error, ValueError) as exc:
                print("EAC Vision: indice audio: " + type(exc).__name__, flush=True)
                await asyncio.sleep(30)

    async def measure(self, path: Path) -> float | None:
        proc = await asyncio.create_subprocess_exec(self.ffmpeg, "-hide_banner", "-nostdin", "-threads", "1",
            "-i", str(path), "-map", "0:a:0?", "-vn", "-af", "volumedetect", "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, output = await asyncio.wait_for(proc.communicate(), timeout=45)
        except asyncio.TimeoutError:
            return None
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.communicate()
        match = PEAK.search(output[-32768:])
        return round(float(match.group(1)), 1) if match else None
