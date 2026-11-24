"""EAC Vision Home Assistant: ingress UI and EAC Vision LAN protocol."""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import signal
from pathlib import Path
from urllib.parse import quote, urlencode

import aiohttp
from aiohttp import WSMsgType, web

from core import APP_NAME, APP_VERSION, FILE_RE, HA_REST, GO_API, MAX_CAMERAS, Vision
from ai import VisionAI

HERE = Path(__file__).resolve().parent
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VISION_KEY = web.AppKey("vision", Vision)


def vision(request: web.Request) -> Vision:
    return request.app[VISION_KEY]


def ai_of(request: web.Request) -> VisionAI:
    ai = getattr(vision(request), "ai", None)
    if ai is None:
        raise web.HTTPServiceUnavailable(text="AI in avvio")
    return ai


def cid_of(request: web.Request) -> str:
    cid = request.match_info.get("cid") or request.query.get("cam", "")
    if cid not in vision(request).config["cameras"]:
        raise web.HTTPNotFound(text="Telecamera non trovata")
    return cid


@web.middleware
async def ingress_only(request: web.Request, handler):
    if os.environ.get("EAC_DEV") != "1" and request.remote not in {"172.30.32.2", "::ffff:172.30.32.2"}:
        raise web.HTTPForbidden(text="Accesso solo tramite Home Assistant")
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@web.middleware
async def client_auth(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    elif request.path == "/eacnvr/info":
        response = await handler(request)
    else:
        key = request.query.get("key", "")
        if not secrets.compare_digest(key, vision(request).client_key):
            response = web.Response(status=403, text="Codice di accesso errato")
        else:
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                response = exc
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


async def index(_: web.Request) -> web.Response:
    return web.FileResponse(HERE / "web" / "index.html")


async def status(request: web.Request) -> web.Response:
    return web.json_response(vision(request).status(include_key=True))


async def refresh(request: web.Request) -> web.Response:
    try:
        await vision(request).refresh_ha(registry=True)
    except Exception as exc:
        raise web.HTTPBadGateway(text=str(exc)) from exc
    return web.json_response(vision(request).status(include_key=True))


async def set_camera(request: web.Request) -> web.Response:
    cid = cid_of(request)
    v = vision(request)
    if not any(d["id"] == cid for d in v.discovered):
        raise web.HTTPBadRequest(text="Questa entità non è disponibile nell'integrazione ONVIF")
    body = await request.json()
    cam = v.config["cameras"][cid]
    if "source" in body or "clear_source" in body:
        raise web.HTTPBadRequest(text="Il flusso viene letto dall'integrazione ONVIF di Home Assistant")
    mode = body.get("mode", cam.get("mode", "continuous"))
    if mode not in {"off", "continuous", "motion"}:
        raise web.HTTPBadRequest(text="Modalità di registrazione non valida")
    motion_entity = str(body.get("motion_entity", cam.get("motion_entity", ""))).strip()
    if motion_entity:
        reg = next((e for e in v.registry if e.get("entity_id") == motion_entity and e.get("platform") == "onvif"
                    and motion_entity.startswith("binary_sensor.")), None)
        current = next((e for e in v.registry if e.get("entity_id") == cam["entity_id"]), None)
        if not reg or not current or reg.get("device_id") != current.get("device_id"):
            raise web.HTTPBadRequest(text="Scegli un sensore ONVIF della stessa telecamera")
    if mode == "motion" and not motion_entity:
        raise web.HTTPBadRequest(text="Per registrare al movimento serve un sensore ONVIF")
    if "name" in body:
        cam["name"] = str(body["name"]).strip()[:80] or cam["name"]
    cam["mode"] = mode
    cam["motion_entity"] = motion_entity
    if "selected" in body:
        selected = v.config["selected"]
        if body["selected"] is True and cid not in selected:
            selected.append(cid)
        elif body["selected"] is False and cid in selected:
            selected.remove(cid)
    v.save()
    return web.json_response(v.public_camera(cid, cam))


async def set_storage(request: web.Request) -> web.Response:
    v = vision(request)
    body = await request.json()
    try:
        v.set_storage(str(body.get("path", "")), int(body.get("retention_days", 7)), int(body.get("max_gb", 0)))
    except (OSError, ValueError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    return web.json_response(v.storage_status())


async def set_client_key(request: web.Request) -> web.Response:
    body = await request.json()
    try:
        vision(request).set_client_key(str(body.get("key", "")).strip())
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    return web.json_response({"client_key": vision(request).client_key})


async def set_view(request: web.Request) -> web.Response:
    body = await request.json()
    if not isinstance(body.get("show_names"), bool):
        raise web.HTTPBadRequest(text="Scelta della vista non valida")
    v = vision(request)
    v.config["show_names"] = body["show_names"]
    v.save()
    return web.json_response({"show_names": v.config["show_names"]})


async def snapshot(request: web.Request) -> web.Response:
    cid = cid_of(request)
    v = vision(request)
    entity = v.config["cameras"][cid]["entity_id"]
    if not v.token:
        raise web.HTTPBadGateway(text="Home Assistant non collegato")
    assert v.session
    try:
        async with v.session.get(HA_REST + "/camera_proxy/" + quote(entity, safe=""),
                                 headers={"Authorization": "Bearer " + v.token}) as upstream:
            if upstream.status != 200:
                raise web.HTTPBadGateway(text="Istantanea non disponibile")
            data = await upstream.read()
            if len(data) > 8 * 1024 * 1024:
                raise web.HTTPBadGateway(text="Istantanea troppo grande")
            return web.Response(body=data, content_type=upstream.content_type or "image/jpeg")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise web.HTTPBadGateway(text="Home Assistant non risponde") from exc


async def relay_live(request: web.Request) -> web.WebSocketResponse:
    cid = cid_of(request) if "cid" in request.match_info else request.query.get("src", "")
    v = vision(request)
    if cid not in v.config["cameras"] or cid not in v.sources:
        raise web.HTTPNotFound(text="Stream non configurato")
    if not v.relay or v.relay.returncode is not None:
        raise web.HTTPBadGateway(text="Motore video non disponibile")
    assert v.session
    try:
        upstream = await v.session.ws_connect(GO_API.replace("http://", "ws://") + "/api/ws?src=" + quote(cid),
                                               heartbeat=25)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise web.HTTPBadGateway(text="Stream non disponibile") from exc
    client = web.WebSocketResponse(heartbeat=25, max_msg_size=8 * 1024 * 1024)
    await client.prepare(request)

    async def pump(source, target):
        async for msg in source:
            if msg.type == WSMsgType.TEXT:
                await target.send_str(msg.data)
            elif msg.type == WSMsgType.BINARY:
                await target.send_bytes(msg.data)
            elif msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                break

    tasks = [asyncio.create_task(pump(client, upstream)), asyncio.create_task(pump(upstream, client))]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await upstream.close()
    await client.close()
    return client


async def recording_file(request: web.Request) -> web.FileResponse:
    cid = cid_of(request)
    filename = request.match_info.get("filename") or request.query.get("name", "")
    if not FILE_RE.fullmatch(filename):
        raise web.HTTPBadRequest(text="Nome registrazione non valido")
    v = vision(request)
    root = v.storage_root(require_space=False)
    if not root:
        raise web.HTTPServiceUnavailable(text="Disco/NAS non disponibile")
    path = root / cid / filename
    if not path.is_file() or path.is_symlink():
        raise web.HTTPNotFound(text="Registrazione non trovata")
    if path not in v.completed_recording_files(cid, filename[:10]):
        raise web.HTTPNotFound(text="Registrazione ancora in corso")
    return web.FileResponse(path, headers={"Content-Type": "video/mp4", "Accept-Ranges": "bytes"})


async def recordings(request: web.Request) -> web.Response:
    cid = cid_of(request)
    day = request.query.get("day", "")
    if day and not DAY_RE.fullmatch(day):
        raise web.HTTPBadRequest(text="Giorno non valido")
    return web.json_response(vision(request).segments(cid, day))


async def recording_days(request: web.Request) -> web.Response:
    return web.json_response(vision(request).days(cid_of(request)))


async def nvr_info(_: web.Request) -> web.Response:
    return web.json_response({"app": "EAC Vision NVR", "name": APP_NAME, "port": 8767, "version": APP_VERSION, "hls": True})


def rewrite_hls_playlist(source: str, key: str) -> str:
    suffix = "&key=" + quote(key, safe="")
    source = re.sub(r'URI="([^"]+\?[^\"]+)"', lambda match: 'URI="' + match.group(1) + suffix + '"', source)
    return re.sub(r'(?m)^([^#\r\n][^\r\n]*\?[^\r\n]+)$', lambda match: match.group(1) + suffix, source)


async def hls(request: web.Request) -> web.Response:
    v = vision(request)
    if not v.relay or v.relay.returncode is not None:
        raise web.HTTPBadGateway(text="Motore video non disponibile")
    asset = request.match_info.get("asset", "")
    if asset:
        if asset not in {"playlist.m3u8", "segment.ts", "init.mp4", "segment.m4s"}:
            raise web.HTTPNotFound()
        session_id = request.query.get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9]{8}", session_id):
            raise web.HTTPBadRequest(text="Sessione HLS non valida")
        query = {"id": session_id}
        if asset.startswith("segment") and request.query.get("n", "").isdigit():
            query["n"] = request.query["n"]
        upstream_path = "/api/hls/" + asset + "?" + urlencode(query)
    else:
        cid = request.query.get("src", "")
        if cid not in v.sources:
            raise web.HTTPNotFound(text="Telecamera non trovata")
        upstream_path = "/api/stream.m3u8?" + urlencode({"src": cid}) + "&mp4"
    assert v.session
    try:
        async with v.session.get(GO_API + upstream_path) as upstream:
            if upstream.status != 200:
                raise web.HTTPBadGateway(text="HLS non disponibile")
            data = await upstream.read()
            if len(data) > 16 * 1024 * 1024:
                raise web.HTTPBadGateway(text="Risposta HLS troppo grande")
            if not asset or asset.endswith(".m3u8"):
                data = rewrite_hls_playlist(data.decode("utf-8"), v.client_key).encode("utf-8")
            return web.Response(body=data, headers={"Content-Type": upstream.headers.get("Content-Type", "application/octet-stream")})
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise web.HTTPBadGateway(text="HLS non raggiungibile") from exc


async def nvr_cameras(request: web.Request) -> web.Response:
    v = vision(request)
    cams = [{"id": cid, "name": cam.get("name", cid), "sub": False, "snapshot": True, "mjpeg": False,
             "convert": False, "recorded": cam.get("mode") != "off" and bool(v.storage_root(require_space=False)),
             "recording": cid in v.recorders and v.recorders[cid].returncode is None}
            for cid, cam in v.configured()]
    return web.json_response({"name": APP_NAME, "cams": cams, "mosaic": False})


async def nvr_views(request: web.Request) -> web.Response:
    v = vision(request)
    available = {cid for cid, _ in v.configured()}
    selected = [cid for cid in v.config["selected"] if cid in available][:MAX_CAMERAS]
    layout = "L4" if len(selected) <= 4 else "L9" if len(selected) <= 9 else "L16"
    return web.json_response([{"id": "principale", "name": "Principale", "layout": layout, "slots": selected}])


async def nvr_days(request: web.Request) -> web.Response:
    return web.json_response(vision(request).days(cid_of(request)))


async def nvr_segments(request: web.Request) -> web.Response:
    return await recordings(request)


async def nvr_recent(request: web.Request) -> web.Response:
    cid = cid_of(request)
    items = vision(request).segments(cid)
    if not items:
        raise web.HTTPNotFound(text="Nessuna registrazione completata")
    item = items[-1]
    return web.json_response({"url": item["url"], "start": item["start"], "dur": item["dur"]})


async def nvr_state(request: web.Request) -> web.Response:
    try:
        rev = int(request.query.get("rev", "0"))
        wait = int(request.query.get("wait", "0"))
    except ValueError:
        raise web.HTTPBadRequest(text="Parametri AI non validi")
    return web.json_response(await ai_of(request).wait_state(rev, wait))


async def ai_set(request: web.Request) -> web.Response:
    try:
        return web.json_response(ai_of(request).set(await request.json()))
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc


async def ai_events(request: web.Request) -> web.Response:
    try:
        before = int(request.query.get("before", "0"))
        limit = int(request.query.get("limit", "60"))
    except ValueError:
        raise web.HTTPBadRequest(text="Filtro eventi non valido")
    cam, label = request.query.get("cam", ""), request.query.get("label", "")
    if label and label not in {"persona", "veicolo", "animale"}:
        raise web.HTTPBadRequest(text="Tipo di evento non valido")
    return web.json_response(ai_of(request).events(cam, label, before, limit))


async def ai_image(request: web.Request) -> web.FileResponse:
    path = ai_of(request).image(request.match_info.get("event_id") or request.query.get("id", ""))
    if not path:
        raise web.HTTPNotFound(text="Istantanea non trovata")
    return web.FileResponse(path, headers={"Content-Type": "image/jpeg"})


def make_ingress(v: Vision) -> web.Application:
    app = web.Application(middlewares=[ingress_only])
    app[VISION_KEY] = v
    app.router.add_get("/", index)
    app.router.add_static("/web", HERE / "web", show_index=False)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/refresh", refresh)
    app.router.add_post("/api/cameras/{cid}", set_camera)
    app.router.add_post("/api/storage", set_storage)
    app.router.add_post("/api/client-key", set_client_key)
    app.router.add_post("/api/view", set_view)
    app.router.add_get("/api/snapshot/{cid}", snapshot)
    app.router.add_get("/api/live/{cid}", relay_live)
    app.router.add_get("/api/recordings/{cid}", recordings)
    app.router.add_get("/api/days/{cid}", recording_days)
    app.router.add_get("/api/recording/{cid}/{filename}", recording_file)
    app.router.add_get("/api/ai/state", nvr_state)
    app.router.add_post("/api/ai/config", ai_set)
    app.router.add_get("/api/ai/events", ai_events)
    app.router.add_get("/api/ai/event/{event_id}.jpg", ai_image)
    return app


def make_lan(v: Vision) -> web.Application:
    app = web.Application(middlewares=[client_auth])
    app[VISION_KEY] = v
    app.router.add_get("/eacnvr/info", nvr_info)
    app.router.add_get("/eacnvr/cameras", nvr_cameras)
    app.router.add_get("/eacnvr/views", nvr_views)
    app.router.add_get("/eacnvr/live", relay_live)
    app.router.add_get("/eacnvr/hls.m3u8", hls)
    app.router.add_get("/eacnvr/hls/{asset}", hls)
    app.router.add_get("/eacnvr/snapshot", snapshot)
    app.router.add_get("/eacnvr/days", nvr_days)
    app.router.add_get("/eacnvr/segments", nvr_segments)
    app.router.add_get("/eacnvr/recent", nvr_recent)
    app.router.add_get("/eacnvr/file", recording_file)
    app.router.add_get("/eacnvr/state", nvr_state)
    app.router.add_get("/eacnvr/events", ai_events)
    app.router.add_get("/eacnvr/event.jpg", ai_image)
    app.router.add_route("OPTIONS", "/{path:.*}", nvr_info)
    return app


async def main() -> None:
    v = Vision()
    await v.start()
    v.ai = VisionAI(v)
    await v.ai.start()
    ingress_runner = web.AppRunner(make_ingress(v), access_log=None)
    lan_runner = web.AppRunner(make_lan(v), access_log=None)
    await ingress_runner.setup()
    await lan_runner.setup()
    try:
        await web.TCPSite(ingress_runner, "0.0.0.0", 8099).start()
        await web.TCPSite(lan_runner, "0.0.0.0", 8767).start()
        stopper = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stopper.set)
            except NotImplementedError:
                pass
        print("EAC Vision Home Assistant avviato: ingress 8099, client locali 8767", flush=True)
        await stopper.wait()
    finally:
        await v.ai.stop()
        await v.stop()
        await lan_runner.cleanup()
        await ingress_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
