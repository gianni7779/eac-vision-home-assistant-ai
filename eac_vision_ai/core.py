"""Home Assistant discovery, go2rtc relay and copy-only recording."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import aiohttp

DATA = Path(os.environ.get("EAC_DATA", "/data"))
MEDIA = Path(os.environ.get("EAC_MEDIA", "/media"))
GO2RTC = os.environ.get("EAC_GO2RTC", "/usr/local/bin/go2rtc")
FFMPEG = os.environ.get("EAC_FFMPEG", "/usr/bin/ffmpeg")
HA_REST = os.environ.get("EAC_HA_REST", "http://supervisor/core/api")
HA_WS = os.environ.get("EAC_HA_WS", "ws://supervisor/core/websocket")
HA_CONFIG = Path(os.environ.get("EAC_HA_CONFIG", "/homeassistant"))
GO_API = "http://127.0.0.1:11984"
GO_RTSP = "rtsp://127.0.0.1:18554"
MAX_CAMERAS = 16
APP_NAME = "EAC Vision AI Home Assistant"
APP_VERSION = "0.1.2"
FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.mp4$")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def camera_id(entity_id: str) -> str:
    return "cam_" + hashlib.sha256(entity_id.encode()).hexdigest()[:12]


def valid_source(value: str) -> bool:
    try:
        if not isinstance(value, str):
            return False
        uri = urlsplit(value)
        return uri.scheme in {"rtsp", "rtsps", "onvif"} and bool(uri.hostname) and not any(c in value for c in "\r\n\0")
    except ValueError:
        return False


def onvif_source(data: dict, profile_token: str) -> str | None:
    """Build the same ONVIF profile source used by the HA camera entity."""
    host = str(data.get("host") or "").strip()
    if not host or any(ch in host for ch in "/?#@ \r\n\0"):
        return None
    try:
        port = int(data.get("port") or 80)
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    try:
        if ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
    except ValueError:
        if ":" in host or "[" in host or "]" in host:
            return None
    user = str(data.get("username") or "")
    password = str(data.get("password") or "")
    credentials = quote(user, safe="") + (":" + quote(password, safe="") if password else "") + "@" if user else ""
    query = "?" + urlencode({"subtype": profile_token}) if profile_token else ""
    return f"onvif://{credentials}{host}:{port}{query}"


def onvif_config_entries() -> dict[str, dict]:
    path = HA_CONFIG / ".storage" / "core.config_entries"
    content = json.loads(path.read_text(encoding="utf-8"))
    entries = content.get("data", {}).get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("Configurazione ONVIF di Home Assistant non valida")
    return {entry["entry_id"]: entry.get("data", {}) for entry in entries
            if isinstance(entry, dict) and entry.get("domain") == "onvif"
            and isinstance(entry.get("entry_id"), str) and isinstance(entry.get("data"), dict)}


def memory_available_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def container_resources() -> dict:
    """Current cgroup usage; CPU percentage is one-core equivalent."""
    result = {"memory_mb": None, "cpu_usec": None}
    try:
        result["memory_mb"] = round(int(Path("/sys/fs/cgroup/memory.current").read_text()) / 1024**2)
    except (OSError, ValueError):
        pass
    try:
        for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines():
            if line.startswith("usage_usec "):
                result["cpu_usec"] = int(line.split()[1])
                break
    except (OSError, ValueError, IndexError):
        pass
    return result


class Vision:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        self.config_path = DATA / "vision.json"
        try:
            self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.config = {}
        self.config.setdefault("cameras", {})
        self.config.setdefault("storage_path", "")
        self.config.setdefault("storage_id", "")
        self.config.setdefault("selected", [])
        self.config.setdefault("show_names", True)
        self.config.setdefault("retention_days", 3)
        self.config.setdefault("max_gb", 0)
        if self.config.get("record_policy") != "h24":
            for cam in self.config["cameras"].values():
                if valid_source(cam.get("source", "")):
                    cam["mode"] = "continuous"
            self.config["record_policy"] = "h24"
            self.save()
        # Sources from earlier versions are replaced by the ONVIF integration.
        old_sources = False
        for cam in self.config["cameras"].values():
            old_sources = cam.pop("source", None) is not None or old_sources
        if old_sources:
            self.save()
        self.secret_path = DATA / "client_key"
        if not self.secret_path.exists():
            self.secret_path.write_text(secrets.token_urlsafe(12), encoding="ascii")
            os.chmod(self.secret_path, 0o600)
        self.client_key = self.secret_path.read_text(encoding="ascii").strip()
        self.token = os.environ.get("SUPERVISOR_TOKEN", "")
        self.session: aiohttp.ClientSession | None = None
        self.registry: list[dict] = []
        self.states: dict[str, dict] = {}
        self.discovered: list[dict] = []
        self.sources: dict[str, str] = {}
        self.source_error = "In attesa della configurazione ONVIF di Home Assistant"
        self.ha_error = "In attesa di Home Assistant"
        self.relay: asyncio.subprocess.Process | None = None
        self.recorders: dict[str, asyncio.subprocess.Process] = {}
        self.progress_tasks: dict[str, asyncio.Task] = {}
        self.record_progress: dict[str, float] = {}
        self.record_time: dict[str, int] = {}
        self.record_errors: dict[str, str] = {}
        self.retry_at: dict[str, float] = {}
        self.relay_retry_at = 0.0
        self.last_loop_error = ""
        self.motion_until: dict[str, float] = {}
        self.memory_paused = False
        self.tasks: list[asyncio.Task] = []
        self._usage_at = 0.0
        self._usage_bytes = 0
        self._cpu_sample: tuple[float, int] | None = None

    def set_client_key(self, value: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", value):
            raise ValueError("Il codice deve avere 12-64 caratteri: lettere, numeri, - o _")
        tmp = self.secret_path.with_suffix(".tmp")
        tmp.write_text(value, encoding="ascii")
        os.chmod(tmp, 0o600)
        tmp.replace(self.secret_path)
        self.client_key = value

    def usage_bytes(self) -> int:
        if time.monotonic() - self._usage_at > 60:
            self._usage_bytes = sum(p.stat().st_size for cid in self.config["cameras"]
                                    for p in self.recording_files(cid))
            self._usage_at = time.monotonic()
        return self._usage_bytes

    def save(self) -> None:
        atomic_json(self.config_path, self.config)

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        await self.start_relay()
        self.tasks = [asyncio.create_task(self.ha_loop()), asyncio.create_task(self.record_loop()), asyncio.create_task(self.cleanup_loop())]

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.stop_all_recorders()
        await self.stop_relay()
        if self.session:
            await self.session.close()

    async def ha_registry(self) -> list[dict]:
        if not self.token:
            raise RuntimeError("SUPERVISOR_TOKEN mancante: installare come app di Home Assistant")
        assert self.session
        async with self.session.ws_connect(HA_WS, heartbeat=20) as ws:
            hello = await ws.receive_json(timeout=10)
            if hello.get("type") != "auth_required":
                raise RuntimeError("Handshake Home Assistant inatteso")
            await ws.send_json({"type": "auth", "access_token": self.token})
            auth = await ws.receive_json(timeout=10)
            if auth.get("type") != "auth_ok":
                raise RuntimeError("Autorizzazione Home Assistant fallita")
            await ws.send_json({"id": 1, "type": "config/entity_registry/list"})
            result = await ws.receive_json(timeout=15)
            if not result.get("success") or not isinstance(result.get("result"), list):
                raise RuntimeError("Registro entità ONVIF non disponibile")
            return result["result"]

    async def ha_states(self) -> dict[str, dict]:
        if not self.token:
            raise RuntimeError("SUPERVISOR_TOKEN mancante")
        assert self.session
        async with self.session.get(HA_REST + "/states", headers={"Authorization": "Bearer " + self.token}) as response:
            response.raise_for_status()
            rows = await response.json()
        if not isinstance(rows, list):
            raise RuntimeError("Stati Home Assistant non validi")
        return {row["entity_id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("entity_id"), str)}

    def sync_discovery(self) -> bool:
        motion_by_device: dict[str, str] = {}
        for ent in self.registry:
            entity = ent.get("entity_id", "")
            state = self.states.get(entity, {})
            if (ent.get("platform") == "onvif" and entity.startswith("binary_sensor.")
                    and state.get("attributes", {}).get("device_class") == "motion" and ent.get("device_id")):
                motion_by_device.setdefault(ent["device_id"], entity)
        found = []
        changed = False
        for ent in self.registry:
            entity = ent.get("entity_id", "")
            if ent.get("platform") != "onvif" or not entity.startswith("camera.") or entity not in self.states:
                continue
            cid = camera_id(entity)
            state = self.states[entity]
            item = {"id": cid, "entity_id": entity, "name": state.get("attributes", {}).get("friendly_name") or entity,
                    "motion_entity": motion_by_device.get(ent.get("device_id", ""), "")}
            found.append(item)
            if cid not in self.config["cameras"]:
                self.config["cameras"][cid] = {"entity_id": entity, "name": item["name"], "mode": "continuous", "motion_entity": item["motion_entity"]}
                changed = True
                if cid not in self.config["selected"] and len(self.config["selected"]) < MAX_CAMERAS:
                    self.config["selected"].append(cid)
            elif not self.config["cameras"][cid].get("motion_entity") and item["motion_entity"]:
                self.config["cameras"][cid]["motion_entity"] = item["motion_entity"]
                changed = True
        self.discovered = found
        if changed:
            self.save()
        try:
            entries = onvif_config_entries()
        except (OSError, ValueError, TypeError) as exc:
            self.source_error = "Configurazione ONVIF non leggibile: " + type(exc).__name__
            return False
        new_sources = {}
        for ent in self.registry:
            entity = ent.get("entity_id", "")
            if ent.get("platform") != "onvif" or not entity.startswith("camera.") or entity not in self.states:
                continue
            unique_id = ent.get("unique_id", "")
            profile = unique_id.rsplit("#", 1)[-1] if isinstance(unique_id, str) and "#" in unique_id else ""
            entry = entries.get(ent.get("config_entry_id", ""), {})
            source = onvif_source(entry, profile) if entry else None
            if source and profile and len(new_sources) < MAX_CAMERAS:
                new_sources[camera_id(entity)] = source
        self.source_error = ("" if len(new_sources) == len(found) else
                             "Alcune telecamere ONVIF non hanno un profilo utilizzabile; controlla l'integrazione in Home Assistant")
        changed_sources = new_sources != self.sources
        self.sources = new_sources
        return changed_sources

    async def refresh_ha(self, registry: bool = False) -> None:
        if registry:
            self.registry = await self.ha_registry()
        self.states = await self.ha_states()
        changed_sources = self.sync_discovery()
        if changed_sources:
            await self.restart_relay()
        self.ha_error = ""

    async def ha_loop(self) -> None:
        while True:
            try:
                await self.refresh_ha(registry=True)
                assert self.session
                async with self.session.ws_connect(HA_WS, heartbeat=25) as ws:
                    hello = await ws.receive_json(timeout=10)
                    if hello.get("type") != "auth_required":
                        raise RuntimeError("Handshake eventi Home Assistant inatteso")
                    await ws.send_json({"type": "auth", "access_token": self.token})
                    if (await ws.receive_json(timeout=10)).get("type") != "auth_ok":
                        raise RuntimeError("Eventi Home Assistant non autorizzati")
                    await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": "state_changed"})
                    ack = await ws.receive_json(timeout=10)
                    if not ack.get("success"):
                        raise RuntimeError("Abbonamento ai movimenti non riuscito")
                    last_registry = time.monotonic()
                    while True:
                        timeout = max(1, 60 - (time.monotonic() - last_registry))
                        try:
                            message = await ws.receive_json(timeout=timeout)
                        except asyncio.TimeoutError:
                            message = None
                        if message and message.get("type") == "event":
                            data = message.get("event", {}).get("data", {})
                            entity = data.get("entity_id")
                            if entity:
                                if data.get("new_state") is None:
                                    self.states.pop(entity, None)
                                else:
                                    self.states[entity] = data["new_state"]
                        if time.monotonic() - last_registry >= 60:
                            await self.refresh_ha(registry=True)
                            last_registry = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ha_error = str(exc)
                await asyncio.sleep(5)

    def configured(self) -> list[tuple[str, dict]]:
        return [(cid, cam) for cid, cam in self.config["cameras"].items() if cid in self.sources]

    def relay_config(self) -> str:
        lines = ['api:', '  listen: "127.0.0.1:11984"', 'rtsp:', '  listen: "127.0.0.1:18554"',
                 'webrtc:', '  listen: ""', 'srtp:', '  listen: ""', 'log:', '  level: warn', 'streams:']
        for cid, cam in self.configured():
            lines.append("  " + cid + ": " + json.dumps(self.sources[cid]))
        return "\n".join(lines) + "\n"

    async def start_relay(self) -> None:
        config = DATA / "go2rtc.yaml"
        config.write_text(self.relay_config(), encoding="utf-8")
        os.chmod(config, 0o600)
        if not Path(GO2RTC).exists():
            return
        self.relay = await asyncio.create_subprocess_exec(GO2RTC, "-config", str(config),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)

    async def stop_relay(self) -> None:
        if self.relay and self.relay.returncode is None:
            self.relay.terminate()
            try:
                await asyncio.wait_for(self.relay.wait(), 8)
            except asyncio.TimeoutError:
                self.relay.kill()
                await self.relay.wait()
        self.relay = None

    async def restart_relay(self) -> None:
        await self.stop_all_recorders()
        await self.stop_relay()
        self.relay_retry_at = 0
        await self.start_relay()

    def storage_root(self, require_space: bool = True) -> Path | None:
        raw = self.config.get("storage_path", "")
        marker = self.config.get("storage_id", "")
        if not raw or not marker:
            return None
        try:
            root = Path(raw).resolve(strict=True)
            media = MEDIA.resolve(strict=True)
            if not root.is_dir() or root == media or media not in root.parents:
                return None
            if (root / ".eacvision-storage-id").read_text(encoding="ascii").strip() != marker:
                return None
            if require_space and shutil.disk_usage(root).free < 2 * 1024**3:
                return None
            return root
        except (OSError, ValueError):
            return None

    def storage_status(self) -> dict:
        raw = self.config.get("storage_path", "")
        root = self.storage_root(require_space=False)
        valid = self.storage_root()
        disk = shutil.disk_usage(root) if root else None
        try:
            used_gb = round(self.usage_bytes() / 1024**3, 2) if root else None
        except OSError:
            used_gb = None
        reserve = max(2 * 1024**3, int(disk.total * .05)) if disk else 0
        return {"path": raw, "ready": bool(valid), "free_gb": round(disk.free / 1024**3, 1) if disk else None,
                "total_gb": round(disk.total / 1024**3, 1) if disk else None, "used_gb": used_gb,
                "reserve_gb": round(reserve / 1024**3, 1) if disk else None,
                "message": "Pronto" if valid else "Scegli un disco/NAS in /media e verifica almeno 2 GB liberi"}

    def set_storage(self, raw: str, retention_days: int, max_gb: int) -> None:
        candidate = Path(raw)
        root = candidate.resolve(strict=True)
        media = MEDIA.resolve(strict=True)
        if root == media or media not in root.parents or not root.is_dir():
            raise ValueError("Scegli una cartella già esistente dentro /media, sul disco o NAS montato")
        if not 1 <= retention_days <= 365 or not 0 <= max_gb <= 100000:
            raise ValueError("Limiti di conservazione non validi")
        if shutil.disk_usage(root).free < 2 * 1024**3:
            raise ValueError("Sono necessari almeno 2 GB liberi")
        marker_path = root / ".eacvision-storage-id"
        marker = marker_path.read_text(encoding="ascii").strip() if marker_path.exists() else secrets.token_urlsafe(18)
        if not marker_path.exists():
            marker_path.write_text(marker, encoding="ascii")
        self.config.update(storage_path=str(root), storage_id=marker, retention_days=retention_days, max_gb=max_gb)
        self.save()

    def motion_active(self, cid: str, cam: dict) -> bool:
        entity = cam.get("motion_entity", "")
        active = bool(entity and self.states.get(entity, {}).get("state") == "on" and not self.ha_error)
        if active:
            self.motion_until[cid] = time.monotonic() + 20
        return active or time.monotonic() < self.motion_until.get(cid, 0)

    async def start_recorder(self, cid: str, root: Path) -> None:
        folder = root / cid
        if folder.is_symlink():
            raise OSError("Cartella telecamera non valida")
        folder.mkdir(exist_ok=True)
        # A single upstream is shared by go2rtc with all live viewers and this copy-only recorder.
        args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-stats_period", "5",
                "-rtsp_transport", "tcp", "-i", GO_RTSP + "/" + cid,
                "-map", "0:v:0", "-c:v", "copy", "-an", "-f", "segment", "-segment_time", "300",
                "-segment_atclocktime", "1", "-reset_timestamps", "1", "-strftime", "1",
                str(folder / "%Y-%m-%d_%H-%M-%S.mp4")]
        try:
            self.recorders[cid] = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            self.record_progress[cid] = time.monotonic()
            self.record_time[cid] = -1
            self.progress_tasks[cid] = asyncio.create_task(self.watch_progress(cid, self.recorders[cid]))
        except (OSError, ValueError) as exc:
            self.record_errors[cid] = type(exc).__name__
            self.retry_at[cid] = time.monotonic() + 30

    async def watch_progress(self, cid: str, proc: asyncio.subprocess.Process) -> None:
        if not proc.stdout:
            return
        try:
            while line := await proc.stdout.readline():
                if not line.startswith(b"out_time_ms="):
                    continue
                try:
                    position = int(line.split(b"=", 1)[1])
                except ValueError:
                    continue
                if position > self.record_time.get(cid, -1):
                    self.record_time[cid] = position
                    self.record_progress[cid] = time.monotonic()
                    self.record_errors.pop(cid, None)
        except (OSError, asyncio.CancelledError):
            return

    async def stop_recorder(self, cid: str) -> None:
        proc = self.recorders.pop(cid, None)
        if proc and proc.returncode is None:
            proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        task = self.progress_tasks.pop(cid, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.record_progress.pop(cid, None)
        self.record_time.pop(cid, None)

    async def stop_all_recorders(self) -> None:
        for cid in list(self.recorders):
            await self.stop_recorder(cid)

    async def record_loop(self) -> None:
        while True:
            try:
                if self.relay and self.relay.returncode is not None:
                    await self.stop_all_recorders()
                    if time.monotonic() >= self.relay_retry_at:
                        print("EAC Vision: motore video terminato; riavvio tra i tentativi", flush=True)
                        self.relay_retry_at = time.monotonic() + 30
                        await self.start_relay()
                root = self.storage_root()
                available_mb = memory_available_mb()
                if available_mb is not None and available_mb < 256 and not self.memory_paused:
                    self.memory_paused = True
                    print("EAC Vision: memoria bassa; sospendo temporaneamente le registrazioni", flush=True)
                elif self.memory_paused and available_mb is not None and available_mb > 512:
                    self.memory_paused = False
                    print("EAC Vision: memoria recuperata; riprendo le registrazioni", flush=True)
                enabled = self.configured()[:MAX_CAMERAS]
                allowed = {cid for cid, cam in enabled if root and not self.memory_paused and self.relay and self.relay.returncode is None
                           and (cam.get("mode") == "continuous" or (cam.get("mode") == "motion" and self.motion_active(cid, cam)))}
                for cid, proc in list(self.recorders.items()):
                    if proc.returncode is not None:
                        await self.stop_recorder(cid)
                        self.record_errors[cid] = "Stream interrotto: nuovo tentativo"
                        self.retry_at[cid] = time.monotonic() + 20
                    elif cid in allowed and time.monotonic() - self.record_progress.get(cid, 0) > 45:
                        await self.stop_recorder(cid)
                        self.record_errors[cid] = "Stream bloccato: nuovo tentativo"
                        self.retry_at[cid] = time.monotonic() + 15
                        print("EAC Vision: riavvio registrazione bloccata " + cid, flush=True)
                    elif cid not in allowed:
                        await self.stop_recorder(cid)
                for cid in allowed:
                    if cid not in self.recorders and time.monotonic() >= self.retry_at.get(cid, 0):
                        await self.start_recorder(cid, root)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Isolate a bad camera or a disconnected NAS from the rest of Home Assistant.
                detail = type(exc).__name__ + ": " + str(exc)
                if detail != self.last_loop_error:
                    self.last_loop_error = detail
                    print("EAC Vision: controllo registrazione: " + detail, flush=True)
            await asyncio.sleep(5)

    def recording_files(self, cid: str, day: str = "") -> list[Path]:
        root = self.storage_root(require_space=False)
        if cid not in self.config["cameras"] or not root:
            return []
        folder = root / cid
        if not folder.is_dir() or folder.is_symlink():
            return []
        return sorted((p for p in folder.iterdir() if p.is_file() and not p.is_symlink() and FILE_RE.fullmatch(p.name)
                       and (not day or p.name.startswith(day))), key=lambda p: p.name)

    def completed_recording_files(self, cid: str, day: str = "") -> list[Path]:
        files = self.recording_files(cid)
        # Only the newest file of an active recorder can still lack the MP4 index.
        if cid in self.recorders and files:
            files = files[:-1]
        return [path for path in files if not day or path.name.startswith(day)]

    def segments(self, cid: str, day: str = "") -> list[dict]:
        files = self.completed_recording_files(cid, day)
        result = []
        for idx, path in enumerate(files):
            try:
                start = datetime.strptime(path.stem, "%Y-%m-%d_%H-%M-%S").timestamp()
                next_start = (datetime.strptime(files[idx + 1].stem, "%Y-%m-%d_%H-%M-%S").timestamp()
                              if idx + 1 < len(files) else path.stat().st_mtime)
                elapsed = next_start - start
                if elapsed > 600:
                    elapsed = path.stat().st_mtime - start
                dur = max(1, min(600, elapsed))
                result.append({"start": start, "dur": dur, "name": path.name,
                               "url": "/eacnvr/file?cam=" + cid + "&name=" + path.name, "live": False})
            except (OSError, ValueError):
                continue
        return result

    def days(self, cid: str) -> list[dict]:
        return [{"day": day} for day in sorted({p.name[:10] for p in self.completed_recording_files(cid)}, reverse=True)]

    async def cleanup_loop(self) -> None:
        while True:
            try:
                self.cleanup_recordings()
            except OSError as exc:
                print("EAC Vision: pulizia archivio: " + str(exc), flush=True)
            await asyncio.sleep(300)

    def cleanup_recordings(self) -> None:
        root = self.storage_root(require_space=False)
        if not root:
            return
        files = [p for cid in self.config["cameras"] for p in self.recording_files(cid)]
        files.sort(key=lambda p: p.name)
        cutoff = time.time() - int(self.config.get("retention_days", 3)) * 86400
        active = set()
        for cid in self.recorders:
            owned = self.recording_files(cid)
            if owned:
                active.add(owned[-1])
        total = sum(p.stat().st_size for p in files)
        disk = shutil.disk_usage(root)
        configured_limit = int(self.config.get("max_gb", 0)) * 1024**3
        limit = configured_limit or int(disk.total * .8)
        reserve = max(2 * 1024**3, int(disk.total * .05))
        free = disk.free
        for path in files:
            try:
                if path in active:
                    continue
                if path.stat().st_mtime < cutoff or total > limit or free < reserve:
                    size = path.stat().st_size
                    path.unlink()
                    total -= size
                    free += size
            except OSError:
                continue
        self._usage_bytes = total
        self._usage_at = time.monotonic()

    def public_camera(self, cid: str, cam: dict) -> dict:
        return {"id": cid, "entity_id": cam.get("entity_id", ""), "name": cam.get("name", cid),
                "has_source": cid in self.sources, "mode": cam.get("mode", "off"),
                "motion_entity": cam.get("motion_entity", ""), "motion": self.states.get(cam.get("motion_entity", ""), {}).get("state") == "on",
                "recording": cid in self.recorders and self.recorders[cid].returncode is None,
                "record_error": self.record_errors.get(cid, ""),
                "selected": cid in self.config["selected"]}

    def status(self, include_key: bool = False) -> dict:
        cams = [self.public_camera(cid, cam) for cid, cam in self.config["cameras"].items()
                if any(d["id"] == cid for d in self.discovered)]
        resources = container_resources()
        cpu = None
        if resources["cpu_usec"] is not None:
            now = time.monotonic()
            if self._cpu_sample:
                elapsed = now - self._cpu_sample[0]
                if elapsed > 0:
                    cpu = round(max(0, (resources["cpu_usec"] - self._cpu_sample[1]) / (elapsed * 10000)), 1)
            self._cpu_sample = (now, resources["cpu_usec"])
        result = {"name": APP_NAME, "version": APP_VERSION, "ha_error": self.ha_error, "source_error": self.source_error, "cameras": cams,
                  "storage": self.storage_status(), "relay": bool(self.relay and self.relay.returncode is None),
                  "ffmpeg": Path(FFMPEG).exists(),
                  "memory_available_mb": memory_available_mb(), "memory_paused": self.memory_paused,
                  "app_memory_mb": resources["memory_mb"], "app_cpu_percent": cpu,
                  "max_cameras": MAX_CAMERAS, "retention_days": self.config["retention_days"], "max_gb": self.config["max_gb"],
                  "show_names": self.config["show_names"]}
        if include_key:
            result["client_key"] = self.client_key
        return result
