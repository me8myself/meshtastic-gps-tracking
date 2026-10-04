"""Tracker state: everything we know about every node we have heard.

The Meshtastic connection (or the simulator) feeds raw packet dicts into
``Tracker.handle_packet``. The tracker keeps the latest info for each node,
records a breadcrumb trail of positions received since the program started,
derives flight data (speed, climb rate, height, distance...) and notifies
listeners (the web server) about every change.

All public methods are thread safe: packets arrive on the Meshtastic reader
thread while the web server reads state from the asyncio loop.
"""

from __future__ import annotations

import base64
import csv
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

EARTH_RADIUS_M = 6371008.8

# Breadcrumb points kept per node (at one fix every few seconds this is many hours).
MAX_TRACK_POINTS = 50000

# Ignore computed speed between fixes that are too far apart in time to be meaningful.
MAX_DERIVE_GAP_S = 300

# The firmware is not consistent with the protobuf docs on these units, so they are configurable.
GROUND_SPEED_UNITS = {"kmh": 1 / 3.6, "ms": 1.0, "knots": 0.514444, "mph": 0.44704}
GROUND_TRACK_SCALES = {"1e-5": 1e-5, "1e-2": 1e-2, "deg": 1.0}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees true (0-360)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def pressure_altitude_m(pressure_hpa: float, sea_level_hpa: float = 1013.25) -> float:
    """Barometric altitude from station pressure (international standard atmosphere)."""
    return 44330.0 * (1.0 - (pressure_hpa / sea_level_hpa) ** (1 / 5.255))


def sanitize(value: Any) -> Any:
    """Make a meshtastic packet dict JSON serialisable.

    Drops the raw protobuf objects the library attaches and encodes bytes.
    """
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items() if k != "raw"}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    return str(value)


@dataclass
class TrackPoint:
    t: float
    lat: float
    lon: float
    alt: Optional[float] = None
    speed: Optional[float] = None  # m/s
    course: Optional[float] = None  # degrees true

    def to_list(self) -> list:
        return [round(self.t, 3), self.lat, self.lon, self.alt, self.speed, self.course]


@dataclass
class NodeState:
    num: int
    id: str
    is_local: bool = False
    user: Dict[str, Any] = field(default_factory=dict)
    position: Dict[str, Any] = field(default_factory=dict)
    position_from_db: bool = False  # last position came from the radio's node DB, not heard live
    metrics: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # deviceMetrics, environmentMetrics...
    radio: Dict[str, Any] = field(default_factory=dict)
    first_heard: Optional[float] = None
    last_heard: Optional[float] = None
    last_position_time: Optional[float] = None
    packet_count: int = 0
    packet_types: Dict[str, int] = field(default_factory=dict)
    track: Deque[TrackPoint] = field(default_factory=lambda: deque(maxlen=MAX_TRACK_POINTS))
    derived: Dict[str, Any] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)  # firmware version, etc. for the local node

    @property
    def name(self) -> str:
        return self.user.get("longName") or self.user.get("shortName") or self.id

    def summary(self) -> Dict[str, Any]:
        return {
            "num": self.num,
            "id": self.id,
            "isLocal": self.is_local,
            "name": self.name,
            "shortName": self.user.get("shortName"),
            "user": self.user,
            "position": self.position,
            "positionFromDb": self.position_from_db,
            "metrics": self.metrics,
            "radio": self.radio,
            "firstHeard": self.first_heard,
            "lastHeard": self.last_heard,
            "lastPositionTime": self.last_position_time,
            "packetCount": self.packet_count,
            "packetTypes": self.packet_types,
            "derived": self.derived,
            "stats": self.stats,
            "extra": self.extra,
            "trackLength": len(self.track),
        }


Listener = Callable[[Dict[str, Any]], None]


class Tracker:
    def __init__(
        self,
        ground_speed_unit: str = "kmh",
        ground_track_scale: str = "1e-5",
        log_dir: Optional[str] = None,
    ) -> None:
        self._lock = threading.RLock()
        self.nodes: Dict[int, NodeState] = {}
        self.local_num: Optional[int] = None
        self.connection: Dict[str, Any] = {"status": "disconnected", "detail": ""}
        self.manual_home: Optional[Dict[str, float]] = None
        self.started = time.time()
        self._listeners: List[Listener] = []
        self.ground_speed_factor = GROUND_SPEED_UNITS[ground_speed_unit]
        self.ground_track_factor = GROUND_TRACK_SCALES[ground_track_scale]
        self._csv_file = None
        self._csv = None
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, time.strftime("track_%Y%m%d_%H%M%S.csv"))
            self._csv_file = open(path, "w", newline="", encoding="utf-8")
            self._csv = csv.writer(self._csv_file)
            self._csv.writerow(
                ["time_utc", "node_id", "name", "lat", "lon", "alt_m", "speed_ms", "course_deg",
                 "climb_ms", "sats", "battery_pct", "voltage", "snr", "rssi", "hops_away"]
            )
            self.log_path = path
        else:
            self.log_path = None

    # ------------------------------------------------------------------ listeners
    def add_listener(self, fn: Listener) -> None:
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn: Listener) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def _emit(self, msg: Dict[str, Any]) -> None:
        for fn in list(self._listeners):
            try:
                fn(msg)
            except Exception:  # a broken listener must never stop packet processing
                pass

    def _emit_node(self, node: NodeState, new_point: Optional[TrackPoint] = None) -> None:
        msg: Dict[str, Any] = {"type": "node", "node": node.summary()}
        if new_point is not None:
            msg["point"] = new_point.to_list()
        self._emit(msg)

    # ------------------------------------------------------------------ connection
    def set_connection(self, status: str, detail: str = "", **extra: Any) -> None:
        with self._lock:
            self.connection = {"status": status, "detail": detail, "since": time.time(), **extra}
            self._emit({"type": "connection", "connection": self.connection})

    def set_local_node(self, num: int, extra: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self.local_num = num
            for n in self.nodes.values():
                n.is_local = n.num == num
            node = self._get_node(num)
            node.is_local = True
            if extra:
                node.extra.update(sanitize(extra))
            self._emit_node(node)

    # ------------------------------------------------------------------ queries
    def home(self) -> Optional[Dict[str, Any]]:
        """Reference point for distance / height: manual home, else the local node's GPS."""
        with self._lock:
            if self.manual_home:
                return {**self.manual_home, "source": "manual"}
            local = self.nodes.get(self.local_num) if self.local_num is not None else None
            if local and "latitude" in local.position and "longitude" in local.position:
                return {
                    "lat": local.position["latitude"],
                    "lon": local.position["longitude"],
                    "alt": local.position.get("altitude"),
                    "source": "base",
                }
            return None

    def set_home(self, lat: float, lon: float, alt: Optional[float] = None) -> None:
        with self._lock:
            self.manual_home = {"lat": lat, "lon": lon, "alt": alt}
            self._refresh_home_derived()

    def clear_home(self) -> None:
        with self._lock:
            self.manual_home = None
            self._refresh_home_derived()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "type": "snapshot",
                "connection": self.connection,
                "localNum": self.local_num,
                "home": self.home(),
                "started": self.started,
                "logPath": self.log_path,
                "nodes": [n.summary() for n in self.nodes.values()],
                "tracks": {str(n.num): [p.to_list() for p in n.track] for n in self.nodes.values()},
            }

    def find(self, key: str) -> Optional[NodeState]:
        """Look up a node by number or '!hex' id."""
        with self._lock:
            for n in self.nodes.values():
                if str(n.num) == key or n.id == key:
                    return n
            return None

    def track_copy(self, node: NodeState) -> List[TrackPoint]:
        with self._lock:
            return list(node.track)

    # ------------------------------------------------------------------ mutations
    def reset_node(self, num: int) -> None:
        """Clear a node's trail and flight statistics (e.g. before a new flight)."""
        with self._lock:
            node = self.nodes.get(num)
            if not node:
                return
            node.track.clear()
            node.stats = {}
            node.derived = {k: v for k, v in node.derived.items()
                            if k in ("distanceFromHome", "bearingFromHome", "heightAboveHome")}
            self._emit({"type": "reset", "num": num})
            self._emit_node(node)

    def _get_node(self, num: int, node_id: Optional[str] = None) -> NodeState:
        node = self.nodes.get(num)
        if node is None:
            node = NodeState(num=num, id=node_id or f"!{num:08x}", is_local=(num == self.local_num))
            self.nodes[num] = node
        elif node_id and node.id != node_id:
            node.id = node_id
        return node

    def load_node_db(self, nodes: Dict[str, Dict[str, Any]]) -> None:
        """Seed state from the radio's node database (``interface.nodes``).

        Positions from the DB are shown as last-known but are NOT added to the
        breadcrumb trail, which only covers what was heard since we connected.
        """
        with self._lock:
            for info in nodes.values():
                info = sanitize(info)
                num = info.get("num")
                if num is None:
                    continue
                node = self._get_node(num, info.get("user", {}).get("id"))
                if info.get("user"):
                    node.user.update(info["user"])
                pos = info.get("position") or {}
                if "latitude" in pos and "longitude" in pos and not node.track:
                    node.position = pos
                    node.position_from_db = True
                if info.get("deviceMetrics"):
                    node.metrics.setdefault("deviceMetrics", {}).update(info["deviceMetrics"])
                last = info.get("lastHeard")
                if last and not node.last_heard:
                    node.last_heard = float(last)
                for src, dst in (("snr", "snr"), ("hopsAway", "hopsAway"), ("viaMqtt", "viaMqtt")):
                    if info.get(src) is not None:
                        node.radio.setdefault(dst, info[src])
                if info.get("isFavorite") is not None:
                    node.extra["isFavorite"] = info["isFavorite"]
                self._emit_node(node)
            self._refresh_home_derived()

    def handle_packet(self, packet: Dict[str, Any]) -> None:
        """Process one received mesh packet (as produced by meshtastic-python)."""
        packet = sanitize(packet)
        num = packet.get("from")
        if num is None:
            return
        now = time.time()
        with self._lock:
            node = self._get_node(num, packet.get("fromId"))
            rx_time = packet.get("rxTime")
            heard = float(rx_time) if rx_time and abs(float(rx_time) - now) < 3600 else now
            node.last_heard = heard
            if node.first_heard is None:
                node.first_heard = heard
            node.packet_count += 1

            radio = node.radio
            for key in ("rxSnr", "rxRssi", "hopLimit", "hopStart", "viaMqtt", "channel", "relayNode"):
                if packet.get(key) is not None:
                    radio[key] = packet[key]
            if packet.get("hopStart") is not None and packet.get("hopLimit") is not None:
                radio["hopsAway"] = packet["hopStart"] - packet["hopLimit"]
            if packet.get("rxSnr") is not None:
                radio["snr"] = packet["rxSnr"]

            decoded = packet.get("decoded") or {}
            portnum = decoded.get("portnum", "ENCRYPTED" if "encrypted" in packet else "UNKNOWN")
            node.packet_types[portnum] = node.packet_types.get(portnum, 0) + 1

            new_point = None
            if "position" in decoded:
                new_point = self._handle_position(node, decoded["position"], heard)
            if "user" in decoded:
                node.user.update(decoded["user"])
            if "telemetry" in decoded:
                self._handle_telemetry(node, decoded["telemetry"])

            self._emit_node(node, new_point)
            if new_point is not None and node.num == self.local_num and not self.manual_home:
                self._refresh_home_derived()

    # ------------------------------------------------------------------ internals
    def _handle_telemetry(self, node: NodeState, telemetry: Dict[str, Any]) -> None:
        for kind, values in telemetry.items():
            if isinstance(values, dict):
                node.metrics.setdefault(kind, {}).update(values)
                node.metrics[kind]["_updated"] = time.time()
        env = node.metrics.get("environmentMetrics", {})
        if env.get("barometricPressure"):
            node.derived["pressureAltitude"] = pressure_altitude_m(env["barometricPressure"])

    def _handle_position(self, node: NodeState, pos: Dict[str, Any], heard: float) -> Optional[TrackPoint]:
        pos = dict(pos)
        if "latitude" not in pos and "latitudeI" in pos:
            pos["latitude"] = pos["latitudeI"] * 1e-7
        if "longitude" not in pos and "longitudeI" in pos:
            pos["longitude"] = pos["longitudeI"] * 1e-7
        lat, lon = pos.get("latitude"), pos.get("longitude")
        if lat is None or lon is None or (lat == 0 and lon == 0):
            # Position packets without a fix still carry useful info (sats etc.)
            node.position.update({k: v for k, v in pos.items() if k not in ("latitude", "longitude")})
            return None

        # GPS time is the best clock for the fix; fall back to when we heard it.
        t = heard
        gps_time = pos.get("timestamp") or pos.get("time")
        if gps_time and abs(gps_time - heard) < 600:
            t = float(gps_time) + (pos.get("timestampMillisAdjust", 0) or 0) / 1000.0

        prev = node.track[-1] if node.track else None
        if prev is not None and t <= prev.t:
            if prev.lat == lat and prev.lon == lon:
                return None  # duplicate (same fix relayed twice)
            t = max(heard, prev.t + 0.001)

        node.position = pos
        node.position_from_db = False
        node.last_position_time = heard
        alt = pos.get("altitude")
        if alt is None and pos.get("altitudeHae") is not None:
            alt = pos["altitudeHae"] - (pos.get("altitudeGeoidalSeparation") or 0)

        d = node.derived
        reported_speed = None
        if pos.get("groundSpeed") is not None:
            reported_speed = pos["groundSpeed"] * self.ground_speed_factor
        reported_course = None
        if pos.get("groundTrack") is not None:
            reported_course = (pos["groundTrack"] * self.ground_track_factor) % 360
        d["reportedSpeed"] = reported_speed
        d["reportedCourse"] = reported_course
        d["altitude"] = alt

        computed_speed = computed_course = climb = None
        if prev is not None:
            dt = t - prev.t
            dist = haversine_m(prev.lat, prev.lon, lat, lon)
            if 0 < dt <= MAX_DERIVE_GAP_S:
                computed_speed = dist / dt
                if dist > 2:
                    computed_course = bearing_deg(prev.lat, prev.lon, lat, lon)
                if alt is not None and prev.alt is not None:
                    climb = (alt - prev.alt) / dt
            st = node.stats
            st["distanceTravelled"] = st.get("distanceTravelled", 0.0) + dist
        d["computedSpeed"] = computed_speed
        d["computedCourse"] = computed_course
        d["climbRate"] = climb

        speed = reported_speed if reported_speed is not None else computed_speed
        course = reported_course if reported_course is not None and (speed or 0) > 0.5 else computed_course
        d["speed"] = speed
        d["course"] = course

        point = TrackPoint(t=t, lat=lat, lon=lon, alt=alt, speed=speed, course=course)
        node.track.append(point)

        st = node.stats
        st.setdefault("firstFixTime", t)
        st["lastFixTime"] = t
        st["fixCount"] = st.get("fixCount", 0) + 1
        st["trackDuration"] = t - st["firstFixTime"]
        if alt is not None:
            st.setdefault("launchAltitude", alt)
            st["maxAltitude"] = max(st.get("maxAltitude", alt), alt)
            st["minAltitude"] = min(st.get("minAltitude", alt), alt)
            d["heightAboveLaunch"] = alt - st["launchAltitude"]
            st["maxHeightAboveLaunch"] = st["maxAltitude"] - st["launchAltitude"]
        if speed is not None:
            st["maxSpeed"] = max(st.get("maxSpeed", 0.0), speed)
        if climb is not None:
            st["maxClimb"] = max(st.get("maxClimb", climb), climb)
            st["maxSink"] = min(st.get("maxSink", climb), climb)

        self._update_home_derived(node)
        if "distanceFromHome" in d and d["distanceFromHome"] is not None:
            st["maxDistanceFromHome"] = max(st.get("maxDistanceFromHome", 0.0), d["distanceFromHome"])

        self._log_point(node, point, climb)
        return point

    def _update_home_derived(self, node: NodeState) -> None:
        home = self.home()
        d = node.derived
        pos = node.position
        if not home or node.num == self.local_num and not self.manual_home \
                or "latitude" not in pos or "longitude" not in pos:
            d["distanceFromHome"] = d["bearingFromHome"] = d["heightAboveHome"] = None
            return
        d["distanceFromHome"] = haversine_m(home["lat"], home["lon"], pos["latitude"], pos["longitude"])
        d["bearingFromHome"] = bearing_deg(home["lat"], home["lon"], pos["latitude"], pos["longitude"])
        alt = d.get("altitude", pos.get("altitude"))
        d["heightAboveHome"] = alt - home["alt"] if alt is not None and home.get("alt") is not None else None

    def _refresh_home_derived(self) -> None:
        for n in self.nodes.values():
            self._update_home_derived(n)
        self._emit({"type": "home", "home": self.home()})
        for n in self.nodes.values():
            if n.num != self.local_num or self.manual_home:
                self._emit_node(n)

    def _log_point(self, node: NodeState, p: TrackPoint, climb: Optional[float]) -> None:
        if not self._csv:
            return
        dm = node.metrics.get("deviceMetrics", {})
        self._csv.writerow([
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(p.t)) + f".{int((p.t % 1) * 1000):03d}Z",
            node.id, node.name, f"{p.lat:.7f}", f"{p.lon:.7f}", p.alt,
            None if p.speed is None else round(p.speed, 2),
            None if p.course is None else round(p.course, 1),
            None if climb is None else round(climb, 2),
            node.position.get("satsInView"), dm.get("batteryLevel"), dm.get("voltage"),
            node.radio.get("rxSnr"), node.radio.get("rxRssi"), node.radio.get("hopsAway"),
        ])
        self._csv_file.flush()

    def close(self) -> None:
        with self._lock:
            if self._csv_file:
                self._csv_file.close()
                self._csv_file = self._csv = None


def track_to_gpx(node: NodeState, points: List[TrackPoint]) -> str:
    from xml.sax.saxutils import escape

    def ts(t: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="meshtastic-gps-tracking" xmlns="http://www.topografix.com/GPX/1/1">',
        f"<trk><name>{escape(node.name)} ({escape(node.id)})</name><trkseg>",
    ]
    for p in points:
        ele = f"<ele>{p.alt}</ele>" if p.alt is not None else ""
        lines.append(f'<trkpt lat="{p.lat:.7f}" lon="{p.lon:.7f}">{ele}<time>{ts(p.t)}</time></trkpt>')
    lines.append("</trkseg></trk></gpx>")
    return "\n".join(lines)


def track_to_csv(points: List[TrackPoint]) -> str:
    out = ["time_utc,lat,lon,alt_m,speed_ms,course_deg"]
    for p in points:
        out.append(",".join(str(v) if v is not None else "" for v in (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(p.t)), f"{p.lat:.7f}", f"{p.lon:.7f}",
            p.alt, None if p.speed is None else round(p.speed, 2),
            None if p.course is None else round(p.course, 1))))
    return "\n".join(out) + "\n"
