"""Web server: serves the map UI, offline tiles and live tracker updates."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional, Set

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .state import Tracker, track_to_csv, track_to_gpx
from .tiles import TileStore, count_tiles

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class HomeBody(BaseModel):
    lat: float
    lon: float
    alt: Optional[float] = None


class DownloadBody(BaseModel):
    west: float
    south: float
    east: float
    north: float
    minZoom: int = 10
    maxZoom: int = 18


def create_app(tracker: Tracker, tiles: TileStore, initial_view: Optional[Dict[str, Any]] = None) -> FastAPI:
    clients: Set[asyncio.Queue] = set()
    loop_holder: Dict[str, asyncio.AbstractEventLoop] = {}

    def on_update(msg: Dict[str, Any]) -> None:
        # Called from the radio thread: hand the message over to the event loop.
        loop = loop_holder.get("loop")
        if loop is None or loop.is_closed():
            return
        data = json.dumps(msg, default=str)

        def push() -> None:
            for q in list(clients):
                if q.qsize() < 2000:
                    q.put_nowait(data)

        loop.call_soon_threadsafe(push)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        loop_holder["loop"] = asyncio.get_running_loop()
        tracker.add_listener(on_update)
        yield
        tracker.remove_listener(on_update)

    app = FastAPI(title="Meshtastic GPS Tracker", lifespan=lifespan)

    # ---------------------------------------------------------------- pages
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ---------------------------------------------------------------- tiles
    @app.get("/tiles/{z}/{x}/{y}")
    async def tile(z: int, x: int, y: int) -> Response:
        if z < 0 or z > 24 or not (0 <= x < 2 ** z) or not (0 <= y < 2 ** z):
            raise HTTPException(404)
        result = await asyncio.to_thread(tiles.get, z, x, y)
        if result is None:
            return Response(status_code=404)
        data, ctype = result
        return Response(content=data, media_type=ctype, headers={"Cache-Control": "max-age=300"})

    @app.get("/api/config")
    async def config() -> Dict[str, Any]:
        info = await asyncio.to_thread(tiles.info)
        return {"tiles": info, "initialView": initial_view}

    @app.post("/api/tiles/download")
    async def download(body: DownloadBody) -> Dict[str, Any]:
        if body.maxZoom - body.minZoom > 12 or body.maxZoom > 20:
            raise HTTPException(400, "zoom range too large")
        bbox = (body.west, body.south, body.east, body.north)
        total = count_tiles(bbox, range(body.minZoom, body.maxZoom + 1))
        if total > 100000:
            raise HTTPException(400, f"{total} tiles is too many; zoom in or lower the max zoom")
        try:
            job = tiles.start_download(bbox, body.minZoom, body.maxZoom)
        except RuntimeError as ex:
            raise HTTPException(409, str(ex))
        return job.status()

    @app.get("/api/tiles/download")
    async def download_status() -> Dict[str, Any]:
        job = tiles.download_job
        return job.status() if job else {"running": False, "total": 0}

    @app.delete("/api/tiles/download")
    async def download_cancel() -> Dict[str, Any]:
        if tiles.download_job:
            tiles.download_job.cancel()
        return {"ok": True}

    # ---------------------------------------------------------------- tracker
    @app.get("/api/state")
    async def state() -> Dict[str, Any]:
        return tracker.snapshot()

    def _node(key: str):
        node = tracker.find(key)
        if node is None:
            raise HTTPException(404, "unknown node")
        return node

    @app.post("/api/nodes/{key}/reset")
    async def reset(key: str) -> Dict[str, Any]:
        tracker.reset_node(_node(key).num)
        return {"ok": True}

    @app.get("/api/nodes/{key}/track.gpx")
    async def gpx(key: str) -> Response:
        node = _node(key)
        body = track_to_gpx(node, tracker.track_copy(node))
        fname = f"{node.id.strip('!')}_track.gpx"
        return Response(body, media_type="application/gpx+xml",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @app.get("/api/nodes/{key}/track.csv")
    async def csv_track(key: str) -> Response:
        node = _node(key)
        fname = f"{node.id.strip('!')}_track.csv"
        return PlainTextResponse(track_to_csv(tracker.track_copy(node)), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @app.post("/api/home")
    async def set_home(body: HomeBody) -> Dict[str, Any]:
        tracker.set_home(body.lat, body.lon, body.alt)
        return {"home": tracker.home()}

    @app.delete("/api/home")
    async def clear_home() -> Dict[str, Any]:
        tracker.clear_home()
        return {"home": tracker.home()}

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        q: asyncio.Queue = asyncio.Queue()
        clients.add(q)
        try:
            await socket.send_text(json.dumps(tracker.snapshot(), default=str))
            while True:
                data = await q.get()
                await socket.send_text(data)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            clients.discard(q)

    return app
