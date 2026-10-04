"""Offline map tiles: storage, serving and downloading.

Tiles are stored as plain files ``<tiles_dir>/{z}/{x}/{y}.jpg`` (XYZ / "slippy
map" scheme). Any ``*.mbtiles`` file dropped into the tiles directory is also
used, so tiles made with other tools (QGIS, MOBAC, TileMill...) work too.

When a tile is missing the server builds one from the nearest lower zoom level
it does have (scaled up), so zooming in past the downloaded detail - or panning
outside a small high-detail area - still shows imagery instead of a blank map.

Run as a script to pre-download an area while you have internet:

    python -m tracker.tiles --lat 45.42 --lon -75.69 --radius-km 3 --max-zoom 18
"""

from __future__ import annotations

import argparse
import io
import math
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterator, List, Optional, Tuple

# Esri World Imagery. Check the provider's terms of use before downloading large areas.
DEFAULT_TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
USER_AGENT = "meshtastic-gps-tracking/1.0 (offline tile cache)"
EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
MAX_ZOOM = 22
MAX_FALLBACK_LEVELS = 8


# ---------------------------------------------------------------------- tile math
def lonlat_to_tile(lon: float, lat: float, z: int) -> Tuple[int, int]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_to_lonlat(x: int, y: int, z: int) -> Tuple[float, float]:
    n = 2 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon, lat


def bbox_around(lat: float, lon: float, radius_km: float) -> Tuple[float, float, float, float]:
    """(west, south, east, north) of a square around a point."""
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(math.cos(math.radians(lat)), 0.01))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def tiles_in_bbox(bbox: Tuple[float, float, float, float], zooms: range) -> Iterator[Tuple[int, int, int]]:
    west, south, east, north = bbox
    for z in zooms:
        x0, y0 = lonlat_to_tile(west, north, z)
        x1, y1 = lonlat_to_tile(east, south, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield z, x, y


def count_tiles(bbox: Tuple[float, float, float, float], zooms: range) -> int:
    west, south, east, north = bbox
    total = 0
    for z in zooms:
        x0, y0 = lonlat_to_tile(west, north, z)
        x1, y1 = lonlat_to_tile(east, south, z)
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    return total


# ---------------------------------------------------------------------- storage
class TileStore:
    def __init__(self, tiles_dir: str, fetch_missing: bool = False, url: str = DEFAULT_TILE_URL) -> None:
        self.dir = tiles_dir
        self.fetch_missing = fetch_missing
        self.url = url
        os.makedirs(self.dir, exist_ok=True)
        self._cache: "OrderedDict[Tuple[int, int, int], Optional[Tuple[bytes, str]]]" = OrderedDict()
        self._cache_lock = threading.Lock()
        self._mbtiles: List[str] = sorted(
            os.path.join(self.dir, f) for f in os.listdir(self.dir) if f.endswith(".mbtiles")
        )
        self._local = threading.local()
        self.download_job: Optional["DownloadJob"] = None
        self._fetch_failed_until = 0.0

    # --- raw lookups
    def path_for(self, z: int, x: int, y: int, ext: str = ".jpg") -> str:
        return os.path.join(self.dir, str(z), str(x), f"{y}{ext}")

    def has_tile(self, z: int, x: int, y: int) -> bool:
        return any(os.path.exists(self.path_for(z, x, y, e)) for e in EXTENSIONS)

    def _mb_conns(self) -> List[sqlite3.Connection]:
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = [sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
                     for p in self._mbtiles]
            self._local.conns = conns
        return conns

    def _read_exact(self, z: int, x: int, y: int) -> Optional[Tuple[bytes, str]]:
        for ext in EXTENSIONS:
            p = self.path_for(z, x, y, ext)
            if os.path.exists(p):
                with open(p, "rb") as f:
                    return f.read(), CONTENT_TYPES[ext]
        tms_y = (2 ** z - 1) - y
        for conn in self._mb_conns():
            try:
                row = conn.execute(
                    "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                    (z, x, tms_y),
                ).fetchone()
            except sqlite3.Error:
                continue
            if row:
                data = bytes(row[0])
                return data, "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
        return None

    def _fetch(self, z: int, x: int, y: int) -> Optional[Tuple[bytes, str]]:
        if time.time() < self._fetch_failed_until:
            return None
        try:
            data = download_tile(self.url, z, x, y, timeout=4)
        except Exception:
            self._fetch_failed_until = time.time() + 30  # probably offline; don't stall every request
            return None
        if data:
            save_tile(self, z, x, y, data)
            return data, "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
        return None

    def get(self, z: int, x: int, y: int) -> Optional[Tuple[bytes, str]]:
        """Tile bytes and content type; synthesised from a lower zoom if necessary."""
        key = (z, x, y)
        with self._cache_lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        result = self._read_exact(z, x, y)
        if result is None and self.fetch_missing:
            result = self._fetch(z, x, y)
        if result is None:
            result = self._from_parent(z, x, y)
            if not self.fetch_missing:  # when fetching, a later request may get the real tile
                with self._cache_lock:
                    self._cache[key] = result
                    while len(self._cache) > 512:
                        self._cache.popitem(last=False)
        return result

    def _from_parent(self, z: int, x: int, y: int) -> Optional[Tuple[bytes, str]]:
        for levels in range(1, min(MAX_FALLBACK_LEVELS, z) + 1):
            pz, px, py = z - levels, x >> levels, y >> levels
            parent = self._read_exact(pz, px, py)
            if parent is None:
                continue
            try:
                from PIL import Image
            except ImportError:
                return None
            try:
                img = Image.open(io.BytesIO(parent[0])).convert("RGB")
                size = img.width
                n = 2 ** levels
                sub = size / n
                ox, oy = (x - (px << levels)) * sub, (y - (py << levels)) * sub
                crop = img.crop((int(ox), int(oy), int(ox + sub), int(oy + sub))).resize(
                    (size, size), Image.BILINEAR
                )
                out = io.BytesIO()
                crop.save(out, "JPEG", quality=85)
                return out.getvalue(), "image/jpeg"
            except Exception:
                return None
        return None

    def info(self) -> Dict:
        zooms = set()
        count = 0
        try:
            for name in os.listdir(self.dir):
                if name.isdigit():
                    zdir = os.path.join(self.dir, name)
                    n = sum(len(files) for _, _, files in os.walk(zdir))
                    if n:
                        zooms.add(int(name))
                        count += n
        except OSError:
            pass
        bounds = None
        for conn in self._mb_conns():
            try:
                for (z,) in conn.execute("SELECT DISTINCT zoom_level FROM tiles"):
                    zooms.add(int(z))
                count += conn.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
                row = conn.execute("SELECT value FROM metadata WHERE name='bounds'").fetchone()
                if row and not bounds:
                    bounds = [float(v) for v in row[0].split(",")]
            except sqlite3.Error:
                continue
        if not bounds and zooms:
            bounds = self._bounds_of_zoom(min(zooms))
        return {
            "tileCount": count,
            "minZoom": min(zooms) if zooms else None,
            "maxZoom": max(zooms) if zooms else None,
            "bounds": bounds,  # [west, south, east, north]
            "mbtiles": [os.path.basename(p) for p in self._mbtiles],
            "fetchMissing": self.fetch_missing,
            "dir": os.path.abspath(self.dir),
        }

    def _bounds_of_zoom(self, z: int) -> Optional[List[float]]:
        zdir = os.path.join(self.dir, str(z))
        xs = [int(d) for d in os.listdir(zdir) if d.isdigit()]
        if not xs:
            return None
        ys = []
        for x in xs:
            for f in os.listdir(os.path.join(zdir, str(x))):
                stem = os.path.splitext(f)[0]
                if stem.isdigit():
                    ys.append(int(stem))
        if not ys:
            return None
        west, north = tile_to_lonlat(min(xs), min(ys), z)
        east, south = tile_to_lonlat(max(xs) + 1, max(ys) + 1, z)
        return [west, south, east, north]

    def start_download(self, bbox, min_zoom: int, max_zoom: int) -> "DownloadJob":
        if self.download_job and self.download_job.running:
            raise RuntimeError("A download is already running")
        self.download_job = DownloadJob(self, bbox, min_zoom, max_zoom)
        self.download_job.start()
        return self.download_job


def save_tile(store: TileStore, z: int, x: int, y: int, data: bytes) -> None:
    ext = ".png" if data[:4] == b"\x89PNG" else ".jpg"
    path = store.path_for(z, x, y, ext)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    with store._cache_lock:
        store._cache.pop((z, x, y), None)


def download_tile(url: str, z: int, x: int, y: int, timeout: float = 15) -> Optional[bytes]:
    import requests

    r = requests.get(url.format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT}, timeout=timeout)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "")
    if not ctype.startswith("image/"):
        raise RuntimeError(f"unexpected content type {ctype!r}")
    return r.content


# ---------------------------------------------------------------------- downloading
class DownloadJob:
    def __init__(self, store: TileStore, bbox, min_zoom: int, max_zoom: int, workers: int = 4) -> None:
        self.store = store
        self.bbox = tuple(bbox)
        self.zooms = range(max(0, min_zoom), min(MAX_ZOOM, max_zoom) + 1)
        self.total = count_tiles(self.bbox, self.zooms)
        self.done = self.skipped = self.failed = self.downloaded = 0
        self.bytes = 0
        self.running = False
        self.cancelled = False
        self.error: Optional[str] = None
        self.workers = workers
        self.started = None
        self.finished = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self.running = True
        self.started = time.time()
        threading.Thread(target=self.run, name="tile-download", daemon=True).start()

    def cancel(self) -> None:
        self.cancelled = True

    def _one(self, zxy) -> None:
        if self.cancelled:
            return
        z, x, y = zxy
        status = "skipped"
        if not self.store.has_tile(z, x, y):
            try:
                data = download_tile(self.store.url, z, x, y)
                if data:
                    save_tile(self.store, z, x, y, data)
                    status = "downloaded"
                    with self._lock:
                        self.bytes += len(data)
                else:
                    status = "failed"
            except Exception as ex:
                status = "failed"
                self.error = str(ex)
        with self._lock:
            self.done += 1
            if status == "skipped":
                self.skipped += 1
            elif status == "downloaded":
                self.downloaded += 1
            else:
                self.failed += 1

    def run(self) -> None:
        try:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for _ in pool.map(self._one, tiles_in_bbox(self.bbox, self.zooms)):
                    pass
        except Exception as ex:
            self.error = str(ex)
        finally:
            self.running = False
            self.finished = time.time()

    def status(self) -> Dict:
        return {
            "running": self.running,
            "cancelled": self.cancelled,
            "total": self.total,
            "done": self.done,
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "failed": self.failed,
            "bytes": self.bytes,
            "error": self.error,
            "minZoom": self.zooms.start,
            "maxZoom": self.zooms.stop - 1,
        }


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Download satellite map tiles for offline use.",
        epilog="Example: python -m tracker.tiles --lat 45.42 --lon -75.69 --radius-km 2 --max-zoom 18",
    )
    area = ap.add_mutually_exclusive_group(required=True)
    area.add_argument("--lat", type=float, help="centre latitude (use with --lon and --radius-km)")
    area.add_argument("--bbox", type=float, nargs=4, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    ap.add_argument("--lon", type=float)
    ap.add_argument("--radius-km", type=float, default=2.0)
    ap.add_argument("--min-zoom", type=int, default=10)
    ap.add_argument("--max-zoom", type=int, default=18)
    ap.add_argument("--tiles-dir", default="tiles")
    ap.add_argument("--url", default=DEFAULT_TILE_URL, help="tile URL template with {z} {x} {y}")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args(argv)

    if args.lat is not None:
        if args.lon is None:
            ap.error("--lon is required with --lat")
        bbox = bbox_around(args.lat, args.lon, args.radius_km)
    else:
        bbox = tuple(args.bbox)
    zooms = range(args.min_zoom, args.max_zoom + 1)
    total = count_tiles(bbox, zooms)
    print(f"Area W{bbox[0]:.5f} S{bbox[1]:.5f} E{bbox[2]:.5f} N{bbox[3]:.5f}, zoom {args.min_zoom}-{args.max_zoom}")
    print(f"{total} tiles (roughly {total * 25 / 1024:.0f} MB)")
    if total > 50000 and not args.yes:
        if input("That's a lot of tiles. Continue? [y/N] ").strip().lower() != "y":
            return
    store = TileStore(args.tiles_dir, url=args.url)
    job = DownloadJob(store, bbox, args.min_zoom, args.max_zoom, workers=args.workers)
    job.start()
    try:
        while job.running:
            time.sleep(0.5)
            s = job.status()
            print(f"\r{s['done']}/{s['total']}  new {s['downloaded']}  existing {s['skipped']}  "
                  f"failed {s['failed']}  {s['bytes'] / 1e6:.1f} MB", end="", flush=True)
    except KeyboardInterrupt:
        job.cancel()
        print("\nCancelling...")
        while job.running:
            time.sleep(0.2)
    s = job.status()
    print(f"\nDone: {s['downloaded']} downloaded, {s['skipped']} already present, {s['failed']} failed.")
    if s["error"]:
        print(f"Last error: {s['error']}")


if __name__ == "__main__":
    main()
