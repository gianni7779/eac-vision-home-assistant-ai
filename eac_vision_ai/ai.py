"""Motion-gated YOLOX Tiny detection for the amd64 Home Assistant app."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

import core

MODEL = Path(os.environ.get("EAC_AI_MODEL", "/opt/eac/ai/yolox_tiny.onnx"))
EVENT_ID = re.compile(r"^\d{8}-\d{6}-[a-f0-9]{8}$")
LABELS = ("persona", "veicolo", "animale")
CLASSES = {0: "persona", 1: "veicolo", 2: "veicolo", 3: "veicolo", 5: "veicolo", 7: "veicolo",
           14: "animale", 15: "animale", 16: "animale", 17: "animale", 18: "animale", 19: "animale"}


def iou(a: list[float], b: list[float]) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area = a[2] * a[3] + b[2] * b[3] - inter
    return inter / area if area > 0 else 0.0


class Detector:
    def __init__(self, path: Path):
        threads = max(1, min(4, (os.cpu_count() or 2) // 2))
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.size = int(self.session.get_inputs()[0].shape[-1])
        self.device = f"CPU · ONNX Runtime · {threads} thread"
        cells = [(x, y, stride) for stride in (8, 16, 32)
                 for y in range(self.size // stride) for x in range(self.size // stride)]
        self.grid = np.asarray(cells, dtype=np.float32)

    def infer(self, frame: np.ndarray) -> list[dict]:
        height, width = frame.shape[:2]
        ratio = min(self.size / width, self.size / height)
        w, h = int(width * ratio), int(height * ratio)
        canvas = np.full((self.size, self.size, 3), 114, dtype=np.uint8)
        canvas[:h, :w] = cv2.resize(frame, (w, h), interpolation=cv2.INTER_LINEAR)
        tensor = np.ascontiguousarray(canvas.transpose(2, 0, 1)[None].astype(np.float32))
        output = self.session.run(None, {self.input_name: tensor})[0].reshape(-1, 85)
        cls = np.asarray(list(CLASSES), dtype=np.int32)
        scores = output[:, 4:5] * output[:, 5 + cls]
        best = np.argmax(scores, axis=1)
        confidence = scores[np.arange(len(output)), best]
        indices = np.flatnonzero(confidence >= .35)
        detections = []
        for i in indices:
            row = output[i]
            gx, gy, stride = self.grid[i]
            cx, cy = (row[0] + gx) * stride / ratio, (row[1] + gy) * stride / ratio
            bw, bh = np.exp(np.clip(row[2:4], -10, 10)) * stride / ratio
            x, y = max(0.0, cx - bw / 2), max(0.0, cy - bh / 2)
            bw, bh = min(width - x, bw), min(height - y, bh)
            if bw * bh < width * height * .0015:
                continue
            detections.append({"label": CLASSES[int(cls[best[i]])], "score": float(confidence[i]),
                               "box": [float(x / width), float(y / height), float(bw / width), float(bh / height)]})
        keep = []
        for det in sorted(detections, key=lambda item: item["score"], reverse=True):
            if not any(old["label"] == det["label"] and iou(old["box"], det["box"]) > .45 for old in keep):
                keep.append(det)
        return keep


class VisionAI:
    def __init__(self, vision):
        self.vision = vision
        self.config_path = core.DATA / "ai.json"
        try:
            self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.config = {}
        self.config.setdefault("enabled", True)
        self.config.setdefault("max_cameras", 4)
        self.config.setdefault("cams", {})
        self.config.setdefault("notify", {})
        self.detector: Detector | None = None
        self.status_text = "Avvio del modello AI…"
        self.rev = 1
        self.change = asyncio.Event()
        self.pending: dict[str, tuple[np.ndarray, np.ndarray, int]] = {}
        self.watchers: dict[str, asyncio.Task] = {}
        self.tasks: list[asyncio.Task] = []
        self.runtime: dict[str, dict] = {}
        self.recent: list[dict] = []
        self.event_root = core.DATA / "ai_events"
        self.inferences = 0
        self.avg_ms = 0.0
        self.last_notify: dict[tuple[str, str, str], float] = {}
        self._load_recent()

    def cam_config(self, cid: str) -> dict:
        cam = self.config["cams"].setdefault(cid, {})
        for name, default in (("detect", True), ("sensitivity", 1), ("persona", True),
                              ("veicolo", True), ("animale", True)):
            cam.setdefault(name, default)
        return cam

    def save(self):
        core.atomic_json(self.config_path, self.config)

    def changed(self):
        self.rev += 1
        self.change.set()
        self.change = asyncio.Event()

    async def start(self):
        try:
            self.detector = await asyncio.to_thread(Detector, MODEL)
            self.status_text = "AI attiva: " + self.detector.device
        except Exception as exc:
            self.status_text = "Modello AI non disponibile: " + type(exc).__name__
        self.changed()
        self.tasks = [asyncio.create_task(self.reconcile()), asyncio.create_task(self.worker()),
                      asyncio.create_task(self.cleanup())]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        for task in self.watchers.values():
            task.cancel()
        await asyncio.gather(*self.tasks, *self.watchers.values(), return_exceptions=True)
        self.watchers.clear()

    async def reconcile(self):
        while True:
            enough_memory = (core.memory_available_mb() or 9999) >= 768
            ready = self.config["enabled"] and self.detector and self.vision.relay and self.vision.relay.returncode is None and enough_memory
            wanted = [cid for cid, _ in self.vision.configured() if self.cam_config(cid)["detect"]][:int(self.config["max_cameras"])] if ready else []
            for cid in list(self.watchers):
                if cid not in wanted or self.watchers[cid].done():
                    self.watchers[cid].cancel()
                    self.watchers.pop(cid, None)
                    self.pending.pop(cid, None)
            for cid in wanted:
                if cid not in self.watchers:
                    self.watchers[cid] = asyncio.create_task(self.watch(cid))
            if self.detector and not enough_memory:
                self.status_text = "AI sospesa: meno di 768 MB liberi"
            elif self.detector and self.status_text.startswith("AI sospesa"):
                self.status_text = "AI attiva: " + self.detector.device
            await asyncio.sleep(5)

    async def watch(self, cid: str):
        runtime = self.runtime.setdefault(cid, {"motion": False, "counts": dict.fromkeys(LABELS, 0),
                                               "tracks": {}, "error": "", "watching": False, "last_infer": 0,
                                               "motion_until": 0, "last_queued": 0, "frames": 0,
                                               "inferences": 0, "last_frame": 0})
        frame_size = 640 * 360 * 3
        while True:
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(core.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-threads", "1", "-rtsp_transport", "tcp", "-rw_timeout", "5000000", "-i", core.GO_RTSP + "/" + cid,
                    "-an", "-sn", "-vf", "fps=5,scale=640:360:force_original_aspect_ratio=decrease:flags=bilinear,pad=640:360:(ow-iw)/2:(oh-ih)/2",
                    "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1", stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, stdin=asyncio.subprocess.DEVNULL)
                background = None
                while True:
                    raw = await asyncio.wait_for(proc.stdout.readexactly(frame_size), timeout=15)
                    frame = np.frombuffer(raw, np.uint8).reshape(360, 640, 3).copy()
                    now = int(time.time() * 1000)
                    runtime["frames"] += 1
                    runtime["last_frame"] = now
                    if not runtime["watching"]:
                        runtime["watching"] = True
                        runtime["error"] = ""
                        self.changed()
                    gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
                    if background is None:
                        background = gray.astype(np.float32)
                        continue
                    mask = cv2.absdiff(gray, cv2.convertScaleAbs(background)) > 25
                    cv2.accumulateWeighted(gray, background, .08)
                    sensitivity = int(self.cam_config(cid)["sensitivity"])
                    area = (.012, .005, .002)[sensitivity]
                    fraction = np.count_nonzero(mask) / mask.size
                    if area < fraction < .6:
                        runtime["motion_until"] = now + 3000
                    motion = now < runtime["motion_until"]
                    if motion != runtime["motion"]:
                        runtime["motion"] = motion
                        self.changed()
                    if motion and now - runtime["last_queued"] >= 333:
                        self.pending[cid] = (frame, mask, now)
                        runtime["last_queued"] = now
            except asyncio.CancelledError:
                raise
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
                runtime["error"] = type(exc).__name__
                runtime["watching"] = False
                runtime["motion"] = False
                self.changed()
            finally:
                if proc and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            await asyncio.sleep(10)

    async def worker(self):
        while True:
            if not self.pending:
                await asyncio.sleep(.2)
            for cid, item in list(self.pending.items()):
                if self.pending.get(cid) is not item:
                    continue
                self.pending.pop(cid, None)
                frame, mask, at = item
                start = time.monotonic()
                try:
                    detections = await asyncio.to_thread(self.detector.infer, frame)
                except Exception as exc:
                    self.status_text = "Errore dell'AI: " + type(exc).__name__
                    self.changed()
                    continue
                ms = (time.monotonic() - start) * 1000
                self.avg_ms = ms if not self.avg_ms else self.avg_ms * .95 + ms * .05
                self.inferences += 1
                runtime = self.runtime[cid]
                runtime["last_infer"] = at
                runtime["inferences"] += 1
                self.follow(cid, frame, mask, at, detections)
            self.expire()

    def follow(self, cid: str, frame: np.ndarray, mask: np.ndarray, at: int, detections: list[dict]):
        runtime = self.runtime[cid]
        cfg = self.cam_config(cid)
        shift = (-.1, 0, .1)[int(cfg["sensitivity"])]
        for label in LABELS:
            minimum = (.5 if label == "animale" else .55) - shift
            matches = [d for d in detections if d["label"] == label and cfg[label] and d["score"] >= minimum]
            count = len(matches)
            if runtime["counts"][label] != count:
                runtime["counts"][label] = count
                self.changed()
            if not matches:
                continue
            best = max(matches, key=lambda d: d["score"])
            track = runtime["tracks"].get(label)
            x, y, w, h = best["box"]
            crop = mask[max(0, int(y * 90)):min(90, int((y + h) * 90)),
                        max(0, int(x * 160)):min(160, int((x + w) * 160))]
            moving = crop.size and np.count_nonzero(crop) >= max(2, crop.size * .03)
            if not track:
                track = {"hits": 0, "first": at, "last": at, "event": None}
                runtime["tracks"][label] = track
            if track["event"] is None:
                if not moving or at - track["first"] > 3000:
                    track.update(hits=0, first=at)
                    if not moving:
                        continue
                track["hits"] += 1
            track["last"] = at
            if track["event"] is None and track["hits"] >= 2:
                track["event"] = self.publish(cid, label, best, frame, track["first"])
            elif track["event"] is not None and best["score"] > track["event"]["score"]:
                track["event"]["score"] = round(best["score"], 2)
                track["event"]["box"] = [round(v, 4) for v in best["box"]]
        self.changed()

    def publish(self, cid: str, label: str, det: dict, frame: np.ndarray, at: int) -> dict:
        event_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        event = {"id": event_id, "cam": cid, "camName": self.vision.config["cameras"][cid]["name"],
                 "label": label, "score": round(det["score"], 2), "start": at, "end": 0,
                 "box": [round(v, 4) for v in det["box"]]}
        x, y, w, h = det["box"]
        image = frame.copy()
        color = {"persona": (0, 196, 255), "veicolo": (245, 169, 63), "animale": (100, 217, 76)}[label]
        cv2.rectangle(image, (int(x * 640), int(y * 360)), (int((x + w) * 640), int((y + h) * 360)), color, 3)
        day_dir = self.event_root / datetime.now().strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            (day_dir / (event_id + ".jpg")).write_bytes(encoded.tobytes())
        self.recent.insert(0, event)
        self.recent = self.recent[:500]
        self.changed()
        if self.vision.session and self.vision.token:
            asyncio.create_task(self._send_ha_event(event))
            if self.config["notify"]:
                asyncio.create_task(self._notify_event(event))
        return event

    async def notify_targets(self) -> list[str]:
        if not self.vision.session or not self.vision.token:
            raise RuntimeError("Home Assistant non collegato")
        async with self.vision.session.get(core.HA_REST + "/services",
                headers={"Authorization": "Bearer " + self.vision.token}) as response:
            response.raise_for_status()
            services = await response.json()
        return sorted("notify." + name for domain in services if domain.get("domain") == "notify"
                      for name in domain.get("services", {}) if name.startswith("mobile_app_"))

    async def set_notify(self, body: dict) -> dict:
        target = str(body.get("target", ""))
        if target not in await self.notify_targets():
            raise ValueError("Scegli un dispositivo Home Assistant Companion disponibile")
        labels = body.get("labels", [])
        if not isinstance(labels, list) or not labels or any(label not in LABELS for label in labels):
            raise ValueError("Scegli almeno un tipo di evento")
        if not isinstance(body.get("enabled"), bool):
            raise ValueError("Stato notifiche non valido")
        self.config["notify"][target] = {"enabled": body["enabled"], "labels": list(dict.fromkeys(labels))}
        self.save()
        return self.config["notify"][target]

    async def send_notify(self, target: str, title: str, message: str) -> None:
        if target not in await self.notify_targets():
            raise ValueError("Dispositivo Home Assistant Companion non disponibile")
        assert self.vision.session
        service = target.removeprefix("notify.")
        async with self.vision.session.post(core.HA_REST + "/services/notify/" + service,
                json={"title": title, "message": message},
                headers={"Authorization": "Bearer " + self.vision.token}) as response:
            response.raise_for_status()
            await response.read()

    async def _notify_event(self, event: dict) -> None:
        names = {"persona": "Persona", "veicolo": "Veicolo", "animale": "Animale"}
        for target, choice in list(self.config["notify"].items()):
            if choice.get("enabled") and event["label"] in choice.get("labels", []):
                marker = (target, event.get("cam", ""), event["label"])
                if time.monotonic() - self.last_notify.get(marker, -1e9) < 60:
                    continue
                try:
                    await self.send_notify(target, "EAC Vision · " + names[event["label"]],
                                           event["camName"] + " · rilevato alle " + datetime.fromtimestamp(event["start"] / 1000).strftime("%H:%M"))
                    self.last_notify[marker] = time.monotonic()
                except Exception as exc:
                    print("EAC Vision AI: notifica non inviata: " + type(exc).__name__, flush=True)

    async def _send_ha_event(self, event: dict):
        try:
            async with self.vision.session.post(core.HA_REST + "/events/eac_vision_ai",
                    json={"camera_id": event["cam"], "camera_name": event["camName"],
                          "label": event["label"], "score": event["score"], "start": event["start"],
                          "event_id": event["id"]},
                    headers={"Authorization": "Bearer " + self.vision.token}) as response:
                await response.read()
        except Exception:
            pass

    def expire(self):
        now = int(time.time() * 1000)
        for runtime in self.runtime.values():
            for label, track in list(runtime["tracks"].items()):
                if track["event"] and now - track["last"] > 8000:
                    event = track["event"]
                    event["end"] = track["last"]
                    path = self.event_root / datetime.fromtimestamp(event["start"] / 1000).strftime("%Y-%m-%d") / "eventi.jsonl"
                    with path.open("a", encoding="utf-8") as out:
                        out.write(json.dumps(event, ensure_ascii=False) + "\n")
                    runtime["tracks"].pop(label)
                    self.changed()
                elif not track["event"] and now - track["first"] > 3000:
                    runtime["tracks"].pop(label)
            if now - runtime["last_infer"] > 4000 and any(runtime["counts"].values()):
                runtime["counts"] = dict.fromkeys(LABELS, 0)
                self.changed()

    def _load_recent(self):
        for path in sorted(self.event_root.glob("*/eventi.jsonl"), reverse=True)[:7]:
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    event = json.loads(line)
                    if EVENT_ID.fullmatch(event.get("id", "")):
                        self.recent.append(event)
            except (OSError, ValueError):
                continue
        self.recent.sort(key=lambda item: item["start"], reverse=True)
        self.recent = self.recent[:500]

    async def cleanup(self):
        while True:
            cutoff = datetime.now().date() - timedelta(days=max(7, int(self.vision.config["retention_days"])))
            for path in self.event_root.iterdir() if self.event_root.exists() else []:
                try:
                    if path.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.name) and datetime.strptime(path.name, "%Y-%m-%d").date() < cutoff:
                        shutil.rmtree(path)
                except (OSError, ValueError):
                    continue
            await asyncio.sleep(3600)

    def events(self, cam: str = "", label: str = "", before: int = 0, limit: int = 60) -> list[dict]:
        return [event for event in self.recent if (not cam or event["cam"] == cam)
                and (not label or event["label"] == label) and (not before or event["start"] < before)][:max(1, min(limit, 500))]

    def day_events(self, day: str, cam: str = "", label: str = "") -> list[dict]:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise ValueError
            date = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("Data eventi non valida") from exc
        start = int(datetime.combine(date, datetime.min.time()).timestamp() * 1000)
        end = int(datetime.combine(date + timedelta(days=1), datetime.min.time()).timestamp() * 1000)
        found = {event["id"]: event for event in self.recent if start <= event["start"] < end}
        path = self.event_root / day / "eventi.jsonl"
        try:
            with path.open(encoding="utf-8") as source:
                for line in source:
                    try:
                        event = json.loads(line)
                        if EVENT_ID.fullmatch(event.get("id", "")) and start <= event["start"] < end:
                            found[event["id"]] = event
                    except (ValueError, KeyError, TypeError):
                        continue
        except FileNotFoundError:
            pass
        return sorted((event for event in found.values() if (not cam or event["cam"] == cam)
                       and (not label or event["label"] == label)), key=lambda event: event["start"])

    def image(self, event_id: str) -> Path | None:
        if not EVENT_ID.fullmatch(event_id):
            return None
        day = f"{event_id[:4]}-{event_id[4:6]}-{event_id[6:8]}"
        path = self.event_root / day / (event_id + ".jpg")
        return path if path.is_file() and not path.is_symlink() else None

    def state(self) -> dict:
        cams = {}
        enabled_cams = [cid for cid, _ in self.vision.configured() if self.cam_config(cid)["detect"]]
        admitted = set(enabled_cams[:int(self.config["max_cameras"])])
        now = int(time.time() * 1000)
        for cid, cam in self.vision.configured():
            cfg = self.cam_config(cid)
            runtime = self.runtime.get(cid, {})
            cams[cid] = {"name": cam["name"], "detect": cfg["detect"], "sensitivity": cfg["sensitivity"],
                         "labels": {label: cfg[label] for label in LABELS}, "watching": runtime.get("watching", False),
                         "error": runtime.get("error", ""), "motion": runtime.get("motion", False),
                         "frames": runtime.get("frames", 0), "inferences": runtime.get("inferences", 0),
                         "last_frame": runtime.get("last_frame", 0), "last_infer": runtime.get("last_infer", 0),
                         "limited": cfg["detect"] and cid not in admitted,
                         "frame_age_s": round((now - runtime["last_frame"]) / 1000, 1) if runtime.get("last_frame") else None,
                         **{label: runtime.get("counts", {}).get(label, 0) for label in LABELS},
                         "active": [t["event"]["id"] for t in runtime.get("tracks", {}).values() if t["event"]],
                         "last": next((e for e in self.recent if e["cam"] == cid), None)}
        return {"rev": self.rev, "enabled": self.config["enabled"], "available": self.detector is not None,
                "status": self.status_text, "device": self.detector.device if self.detector else "",
                "ms": round(self.avg_ms, 1), "inferences": self.inferences,
                "max_cameras": self.config["max_cameras"], "cams": cams}

    async def wait_state(self, rev: int, seconds: int) -> dict:
        if rev == self.rev and seconds:
            event = self.change
            try:
                await asyncio.wait_for(event.wait(), min(25, max(0, seconds)))
            except asyncio.TimeoutError:
                pass
        return self.state()

    def set(self, body: dict) -> dict:
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                raise ValueError("Stato AI non valido")
            self.config["enabled"] = body["enabled"]
        if "max_cameras" in body:
            value = int(body["max_cameras"])
            if not 1 <= value <= 16:
                raise ValueError("Scegli da 1 a 16 camere AI")
            self.config["max_cameras"] = value
        cid = body.get("cam")
        if cid:
            if cid not in self.vision.config["cameras"]:
                raise ValueError("Telecamera non trovata")
            cfg = self.cam_config(cid)
            for name in ("detect", *LABELS):
                if name in body:
                    if not isinstance(body[name], bool):
                        raise ValueError("Impostazione AI non valida")
                    cfg[name] = body[name]
            if "sensitivity" in body:
                value = int(body["sensitivity"])
                if value not in (0, 1, 2):
                    raise ValueError("Sensibilità non valida")
                cfg["sensitivity"] = value
        self.save()
        self.changed()
        return self.state()
