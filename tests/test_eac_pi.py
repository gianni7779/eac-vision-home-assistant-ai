import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eac_vision_ai"))
import core
import server
import ai
import numpy as np


class VisionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        core.DATA = root / "data"
        core.MEDIA = root / "media"
        core.HA_CONFIG = root / "homeassistant"
        (core.HA_CONFIG / ".storage").mkdir(parents=True)
        self.entries_path = core.HA_CONFIG / ".storage" / "core.config_entries"
        self.entry = {"entry_id": "front-entry", "domain": "onvif", "data": {
            "host": "192.168.1.50", "port": 80, "username": "u@ser", "password": "p:a@ss",
        }}
        self.write_entries()
        core.MEDIA.mkdir()
        (core.MEDIA / "nas").mkdir()
        os.environ["EAC_DEV"] = "1"
        self.vision = core.Vision()
        self.vision.registry = [
            {"entity_id": "camera.front", "platform": "onvif", "device_id": "dev1",
             "config_entry_id": "front-entry", "unique_id": "mac#profile_main"},
            {"entity_id": "binary_sensor.front_motion", "platform": "onvif", "device_id": "dev1"},
            {"entity_id": "camera.other", "platform": "generic", "device_id": "dev2"},
        ]
        self.vision.states = {
            "camera.front": {"entity_id": "camera.front", "attributes": {"friendly_name": "Ingresso"}},
            "binary_sensor.front_motion": {"entity_id": "binary_sensor.front_motion", "state": "off", "attributes": {"device_class": "motion"}},
            "camera.other": {"entity_id": "camera.other", "attributes": {}},
        }
        self.vision.sync_discovery()
        self.cid = core.camera_id("camera.front")
        async def no_restart():
            pass
        self.vision.restart_relay = no_restart
        self.ingress = TestClient(TestServer(server.make_ingress(self.vision)))
        self.lan = TestClient(TestServer(server.make_lan(self.vision)))
        await self.ingress.start_server()
        await self.lan.start_server()

    def write_entries(self):
        self.entries_path.write_text(json.dumps({"data": {"entries": [self.entry]}}), encoding="utf-8")

    async def asyncTearDown(self):
        await self.lan.close()
        await self.ingress.close()
        self.temp.cleanup()
        os.environ.pop("EAC_DEV", None)

    async def test_onvif_import_storage_and_nvr_contract(self):
        response = await self.ingress.get("/api/status")
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(len(data["cameras"]), 1)
        self.assertEqual(data["cameras"][0]["motion_entity"], "binary_sensor.front_motion")
        self.assertNotIn("source", data["cameras"][0])
        self.assertEqual(data["source_error"], "")
        self.assertEqual(self.vision.sources[self.cid],
                         "onvif://u%40ser:p%3Aa%40ss@192.168.1.50:80?subtype=profile_main")
        self.assertNotIn("p:a@ss", json.dumps(data))
        self.assertNotIn("source", self.vision.config["cameras"][self.cid])
        response = await self.lan.get("/eacnvr/info")
        info = await response.json()
        self.assertEqual(info["app"], "EAC Vision NVR")
        self.assertEqual(info["name"], "EAC Vision AI Home Assistant")
        self.assertEqual(info["version"], core.APP_VERSION)
        denied = await self.lan.get("/eacnvr/cameras", headers={"Origin": "http://localhost"})
        self.assertEqual(denied.status, 403)
        self.assertEqual(denied.headers.get("Access-Control-Allow-Origin"), "*")
        self.assertEqual((await self.ingress.get("/web/icon.png")).status, 200)
        self.assertEqual((await self.ingress.get("/web/dashboard.css")).status, 200)

        forbidden = await self.ingress.post("/api/cameras/" + self.cid, json={"source": "rtsp://example"})
        self.assertEqual(forbidden.status, 400)
        response = await self.ingress.post("/api/cameras/" + self.cid, json={"selected": True})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["mode"], "continuous")
        key = self.vision.client_key
        cams = await (await self.lan.get("/eacnvr/cameras?key=" + key)).json()
        self.assertEqual(cams["cams"][0]["name"], "Ingresso")
        self.assertFalse(cams["mosaic"])
        views = await (await self.lan.get("/eacnvr/views?key=" + key)).json()
        self.assertEqual(views[0]["slots"], [self.cid])

        response = await self.ingress.post("/api/storage", json={
            "path": str(core.MEDIA / "nas"), "retention_days": 7, "max_gb": 25,
        })
        self.assertEqual(response.status, 200, await response.text())
        self.assertTrue((await response.json())["ready"])
        folder = core.MEDIA / "nas" / self.cid
        folder.mkdir()
        (folder / "2026-11-22_12-00-00.mp4").write_bytes(b"demo")
        days = await (await self.lan.get(f"/eacnvr/days?cam={self.cid}&key={key}")).json()
        self.assertEqual(days, [{"day": "2026-11-22"}])
        segments = await (await self.lan.get(f"/eacnvr/segments?cam={self.cid}&day=2026-11-22&key={key}")).json()
        self.assertEqual(len(segments), 1)
        self.assertTrue(segments[0]["url"].startswith("/eacnvr/file?"))
        file_response = await self.lan.get(segments[0]["url"] + "&key=" + key)
        self.assertEqual(file_response.status, 200)
        self.assertEqual(await file_response.read(), b"demo")
        ranged = await self.lan.get(segments[0]["url"] + "&key=" + key, headers={"Range": "bytes=1-2"})
        self.assertEqual(ranged.status, 206)
        self.assertEqual(await ranged.read(), b"em")
        (core.MEDIA / "nas" / ".eacvision-storage-id").unlink()
        self.assertIsNone(self.vision.storage_root())
        disabled = await self.ingress.post("/api/cameras/" + self.cid, json={"mode": "off", "selected": False})
        self.assertEqual(disabled.status, 200)
        self.assertTrue((await disabled.json())["has_source"])
        self.assertFalse((await disabled.json())["selected"])

    async def test_existing_motion_configuration_moves_to_h24(self):
        cid = self.cid
        core.atomic_json(core.DATA / "vision.json", {"cameras": {cid: {
            "entity_id": "camera.front", "name": "Ingresso", "source": "rtsp://192.168.1.50/stream1", "mode": "motion",
        }}})
        migrated = core.Vision()
        self.assertEqual(migrated.config["cameras"][cid]["mode"], "continuous")
        self.assertNotIn("source", migrated.config["cameras"][cid])

    async def test_client_key_and_live_names(self):
        self.assertEqual(self.vision.config["retention_days"], 3)
        old = self.vision.client_key
        invalid = await self.ingress.post("/api/client-key", json={"key": "abc"})
        self.assertEqual(invalid.status, 400)
        self.assertEqual(self.vision.client_key, old)
        changed = await self.ingress.post("/api/client-key", json={"key": "MioCodice-1234"})
        self.assertEqual(changed.status, 200)
        self.assertEqual(self.vision.secret_path.read_text(), "MioCodice-1234")
        self.assertEqual((await self.lan.get("/eacnvr/cameras?key=" + old)).status, 403)
        self.assertEqual((await self.lan.get("/eacnvr/cameras?key=MioCodice-1234")).status, 200)
        self.assertEqual((await self.ingress.post("/api/view", json={"show_names": False})).status, 200)
        self.assertFalse((await (await self.ingress.get("/api/status")).json())["show_names"])
        self.assertFalse(core.Vision().config["show_names"])

    async def test_hls_playlist_keeps_client_auth_on_all_parts(self):
        playlist = '#EXTM3U\nhls/playlist.m3u8?id=ABCDEFGH\n#EXT-X-MAP:URI="init.mp4?id=ABCDEFGH"\nsegment.m4s?id=ABCDEFGH&n=1'
        result = server.rewrite_hls_playlist(playlist, "a-b_12345678")
        self.assertIn('hls/playlist.m3u8?id=ABCDEFGH&key=a-b_12345678', result)
        self.assertIn('URI="init.mp4?id=ABCDEFGH&key=a-b_12345678"', result)
        self.assertIn('segment.m4s?id=ABCDEFGH&n=1&key=a-b_12345678', result)
        denied = await self.lan.get('/eacnvr/hls.m3u8?src=' + self.cid)
        self.assertEqual(denied.status, 403)

    async def test_hls_supports_ios_mp4_and_ts_fallback(self):
        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/vnd.apple.mpegurl"}
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                return False
            async def read(self):
                return b"#EXTM3U\n"
        class FakeSession:
            def __init__(self):
                self.urls = []
            def get(self, url):
                self.urls.append(url)
                return FakeResponse()
        fake = FakeSession()
        self.vision.session = fake
        self.vision.relay = SimpleNamespace(returncode=None)
        endpoint = f"/eacnvr/hls.m3u8?src={self.cid}&key={self.vision.client_key}"
        self.assertEqual((await self.lan.get(endpoint)).status, 200)
        self.assertTrue(fake.urls[-1].endswith("&mp4"))
        self.assertEqual((await self.lan.get(endpoint + "&mode=ts")).status, 200)
        self.assertTrue(fake.urls[-1].endswith("src=" + self.cid))
        self.assertEqual((await self.lan.get(endpoint + "&mode=invalid")).status, 400)

    async def test_cleanup_removes_expired_recording(self):
        self.vision.set_storage(str(core.MEDIA / "nas"), 3, 0)
        folder = core.MEDIA / "nas" / self.cid
        folder.mkdir()
        old = folder / "2026-11-19_12-00-00.mp4"
        new = folder / "2026-11-24_12-00-00.mp4"
        old.write_bytes(b"old")
        new.write_bytes(b"new")
        os.utime(old, (time.time() - 4 * 86400, time.time() - 4 * 86400))
        self.vision.cleanup_recordings()
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    async def test_ai_config_events_and_snapshot(self):
        self.vision.ai = ai.VisionAI(self.vision)
        self.assertEqual(self.vision.ai.config["max_cameras"], 4)
        result = await self.ingress.post("/api/ai/config", json={"cam": self.cid, "persona": False, "sensitivity": 2})
        self.assertEqual(result.status, 200)
        self.assertFalse((await result.json())["cams"][self.cid]["labels"]["persona"])
        event = self.vision.ai.publish(self.cid, "veicolo", {"score": .89, "box": [.1, .2, .3, .4]},
                                       np.zeros((360, 640, 3), dtype=np.uint8), int(time.time() * 1000))
        listed = await (await self.ingress.get("/api/ai/events?label=veicolo")).json()
        self.assertEqual(listed[0]["id"], event["id"])
        image = await self.ingress.get("/api/ai/event/" + event["id"] + ".jpg")
        self.assertEqual(image.status, 200)
        self.assertTrue((await image.read()).startswith(b"\xff\xd8"))
        remote = await (await self.lan.get("/eacnvr/state?key=" + self.vision.client_key)).json()
        self.assertIn(self.cid, remote["cams"])

    async def test_ai_person_crossing_creates_event_on_two_close_frames(self):
        self.vision.ai = ai.VisionAI(self.vision)
        detector = self.vision.ai
        detector.runtime[self.cid] = {"counts": dict.fromkeys(ai.LABELS, 0), "tracks": {}}
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        motion = np.ones((90, 160), dtype=bool)
        person = [{"label": "persona", "score": .88, "box": [.2, .1, .3, .7]}]
        at = int(time.time() * 1000)
        detector.follow(self.cid, frame, motion, at, person)
        self.assertEqual(detector.events(), [])
        detector.follow(self.cid, frame, motion, at + 400, person)
        self.assertEqual(len(detector.events()), 1)
        self.assertEqual(detector.events()[0]["label"], "persona")

    async def test_ai_timeline_reads_events_for_selected_day(self):
        self.vision.ai = ai.VisionAI(self.vision)
        now = int(time.time() * 1000)
        event = self.vision.ai.publish(self.cid, "persona", {"score": .9, "box": [.1, .1, .3, .6]},
                                       np.zeros((360, 640, 3), dtype=np.uint8), now)
        day = time.strftime("%Y-%m-%d", time.localtime(now / 1000))
        url = f"/api/ai/events?cam={self.cid}&day={day}"
        result = await (await self.ingress.get(url)).json()
        self.assertEqual([item["id"] for item in result], [event["id"]])
        self.assertEqual((await self.ingress.get(url.replace(day, "invalid"))).status, 400)
        path = self.vision.ai.event_root / day / "eventi.jsonl"
        path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        self.vision.ai.recent.clear()
        persisted = await (await self.ingress.get(url)).json()
        self.assertEqual([item["id"] for item in persisted], [event["id"]])

    async def test_onvif_changes_are_detected_without_saving_credentials(self):
        self.entry["data"]["password"] = "updated"
        self.write_entries()
        restarted = []
        async def restart():
            restarted.append(True)
        async def states():
            return self.vision.states
        self.vision.restart_relay = restart
        self.vision.ha_states = states
        await self.vision.refresh_ha()
        self.assertEqual(restarted, [True])
        self.assertIn("updated@", self.vision.sources[self.cid])
        self.assertNotIn("updated", self.vision.config_path.read_text(encoding="utf-8"))
        self.assertFalse(self.vision.sync_discovery())

    async def test_each_onvif_entity_uses_its_own_profile(self):
        self.vision.registry.append({"entity_id": "camera.front_sub", "platform": "onvif",
                                     "device_id": "dev1", "config_entry_id": "front-entry",
                                     "unique_id": "mac#profile_sub"})
        self.vision.states["camera.front_sub"] = {"entity_id": "camera.front_sub", "attributes": {}}
        self.assertTrue(self.vision.sync_discovery())
        other = core.camera_id("camera.front_sub")
        self.assertTrue(self.vision.sources[other].endswith("subtype=profile_sub"))
        self.assertNotEqual(self.vision.sources[other], self.vision.sources[self.cid])

    async def test_stalled_h24_recorder_is_restarted(self):
        self.vision.config["cameras"][self.cid]["mode"] = "continuous"
        self.vision.set_storage(str(core.MEDIA / "nas"), 7, 0)
        self.vision.relay = SimpleNamespace(returncode=None)
        self.vision.recorders[self.cid] = SimpleNamespace(returncode=None)
        self.vision.record_progress[self.cid] = time.monotonic() - 50
        stopped = asyncio.Event()

        async def fake_stop(cid):
            self.vision.recorders.pop(cid, None)
            stopped.set()

        self.vision.stop_recorder = fake_stop
        task = asyncio.create_task(self.vision.record_loop())
        try:
            await asyncio.wait_for(stopped.wait(), 2)
            self.assertEqual(self.vision.record_errors[self.cid], "Stream bloccato: nuovo tentativo")
            self.assertGreater(self.vision.retry_at[self.cid], time.monotonic())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_active_recording_hides_only_current_file(self):
        self.vision.set_storage(str(core.MEDIA / "nas"), 7, 0)
        folder = core.MEDIA / "nas" / self.cid
        folder.mkdir()
        old = folder / "2026-11-21_23-55-00.mp4"
        current = folder / "2026-11-22_00-00-00.mp4"
        old.write_bytes(b"finished")
        current.write_bytes(b"still writing")
        self.vision.recorders[self.cid] = SimpleNamespace(returncode=None)
        self.assertEqual([item["name"] for item in self.vision.segments(self.cid, "2026-11-21")], [old.name])
        self.assertEqual(self.vision.segments(self.cid, "2026-11-22"), [])
        self.assertEqual(self.vision.days(self.cid), [{"day": "2026-11-21"}])
        self.assertEqual((await self.ingress.get(f"/api/recording/{self.cid}/{old.name}")).status, 200)
        self.assertEqual((await self.ingress.get(f"/api/recording/{self.cid}/{current.name}")).status, 404)

    async def test_live_websocket_is_relayed_to_client(self):
        self.vision.relay = SimpleNamespace(returncode=None)
        self.vision.session = aiohttp.ClientSession()

        async def fake_go2rtc(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await ws.send_json({"type": "mse", "value": "video/mp4; codecs=\"avc1.640029\""})
                    await ws.send_bytes(b"fmp4")
            return ws

        app = web.Application()
        app.router.add_get("/api/ws", fake_go2rtc)
        fake = TestServer(app)
        await fake.start_server()
        old_api = server.GO_API
        server.GO_API = str(fake.make_url("/")).rstrip("/")
        try:
            async with self.lan.ws_connect(f"/eacnvr/live?src={self.cid}&key={self.vision.client_key}") as ws:
                await ws.send_json({"type": "mse", "value": "avc1.640029"})
                self.assertEqual((await ws.receive(timeout=3)).json()["type"], "mse")
                self.assertEqual((await ws.receive(timeout=3)).data, b"fmp4")
        finally:
            server.GO_API = old_api
            await self.vision.session.close()
            await fake.close()

    async def test_home_assistant_onvif_and_motion_events(self):
        async def states(_request):
            return web.json_response([
                {"entity_id": "camera.front", "state": "idle", "attributes": {"friendly_name": "Ingresso"}},
                {"entity_id": "binary_sensor.front_motion", "state": "off", "attributes": {"device_class": "motion"}},
            ])

        async def websocket(_request):
            ws = web.WebSocketResponse()
            await ws.prepare(_request)
            await ws.send_json({"type": "auth_required"})
            async for msg in ws:
                data = msg.json()
                if data.get("type") == "auth":
                    await ws.send_json({"type": "auth_ok"})
                elif data.get("type") == "config/entity_registry/list":
                    await ws.send_json({"id": 1, "type": "result", "success": True, "result": self.vision.registry})
                elif data.get("type") == "subscribe_events":
                    await ws.send_json({"id": 1, "type": "result", "success": True, "result": None})
                    await ws.send_json({"id": 1, "type": "event", "event": {"data": {
                        "entity_id": "binary_sensor.front_motion", "new_state": {
                            "entity_id": "binary_sensor.front_motion", "state": "on", "attributes": {"device_class": "motion"},
                        },
                    }}})
            return ws

        app = web.Application()
        app.router.add_get("/core/api/states", states)
        app.router.add_get("/core/websocket", websocket)
        fake = TestServer(app)
        await fake.start_server()
        old_rest, old_ws = core.HA_REST, core.HA_WS
        core.HA_REST = str(fake.make_url("/core/api")).rstrip("/")
        core.HA_WS = str(fake.make_url("/core/websocket")).replace("http://", "ws://")
        self.vision.token = "test-token"
        self.vision.session = aiohttp.ClientSession()
        task = asyncio.create_task(self.vision.ha_loop())
        try:
            async def seen_motion():
                while self.vision.states.get("binary_sensor.front_motion", {}).get("state") != "on":
                    await asyncio.sleep(.01)
            await asyncio.wait_for(seen_motion(), 3)
            self.assertTrue(self.vision.motion_active(self.cid, self.vision.config["cameras"][self.cid]))
            self.assertEqual(self.vision.ha_error, "")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self.vision.session.close()
            await fake.close()
            core.HA_REST, core.HA_WS = old_rest, old_ws


if __name__ == "__main__":
    unittest.main()
