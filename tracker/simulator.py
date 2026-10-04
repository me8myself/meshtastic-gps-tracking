"""Demo mode: simulated mesh traffic so the UI can be tried without a radio.

Generates packets shaped exactly like the ones meshtastic-python produces, so
they exercise the same code path as real hardware.
"""

from __future__ import annotations

import math
import random
import threading
import time
from typing import Any, Dict

from .state import Tracker

BASE_NUM = 0xA1B2C3D4
PLANE_NUM = 0x0BADF00D
HIKER_NUM = 0x12345678


class Simulator:
    def __init__(self, tracker: Tracker, lat: float, lon: float, interval: float = 2.0) -> None:
        self.tracker = tracker
        self.lat0 = lat
        self.lon0 = lon
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="simulator", daemon=True)
        self.ground_alt = 95

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _offset(self, east_m: float, north_m: float):
        dlat = north_m / 111320.0
        dlon = east_m / (111320.0 * math.cos(math.radians(self.lat0)))
        return self.lat0 + dlat, self.lon0 + dlon

    def _packet(self, num: int, decoded: Dict[str, Any], snr: float, rssi: int, hops: int = 0) -> Dict[str, Any]:
        return {
            "from": num,
            "fromId": f"!{num:08x}",
            "to": 0xFFFFFFFF,
            "toId": "^all",
            "rxTime": int(time.time()),
            "rxSnr": snr,
            "rxRssi": rssi,
            "hopLimit": 3 - hops,
            "hopStart": 3,
            "decoded": decoded,
        }

    def _run(self) -> None:
        t = self.tracker
        t.set_connection("connecting", "Demo mode: starting simulated radio...")
        time.sleep(0.5)
        t.load_node_db({
            "!a1b2c3d4": {"num": BASE_NUM, "user": {
                "id": "!a1b2c3d4", "longName": "Ground Station", "shortName": "GS",
                "hwModel": "HELTEC_V3", "role": "CLIENT"}},
            "!0badf00d": {"num": PLANE_NUM, "user": {
                "id": "!0badf00d", "longName": "RC Plane", "shortName": "RCP",
                "hwModel": "TBEAM", "role": "TRACKER"}, "deviceMetrics": {"batteryLevel": 100}},
            "!12345678": {"num": HIKER_NUM, "user": {
                "id": "!12345678", "longName": "Spotter Dave", "shortName": "SPOT",
                "hwModel": "T_ECHO", "role": "CLIENT"}},
        })
        t.set_local_node(BASE_NUM, {"firmwareVersion": "2.6.11.demo", "region": "US",
                                    "modemPreset": "LONG_FAST", "role": "CLIENT"})
        t.set_connection("connected", "Demo mode (simulated radio)")

        start = time.time()
        battery = 100.0
        last_base = last_hiker = last_tel = 0.0
        while not self._stop.is_set():
            now = time.time()
            el = now - start
            # --- base station: stationary, reports position + telemetry occasionally
            if now - last_base > 30 or last_base == 0:
                last_base = now
                t.handle_packet(self._packet(BASE_NUM, {"portnum": "POSITION_APP", "position": {
                    "latitude": self.lat0, "longitude": self.lon0, "altitude": self.ground_alt,
                    "time": int(now), "satsInView": 11, "PDOP": 120, "precisionBits": 32}}, 0, 0))
                t.handle_packet(self._packet(BASE_NUM, {"portnum": "TELEMETRY_APP", "telemetry": {
                    "time": int(now), "deviceMetrics": {"batteryLevel": 101, "voltage": 4.2,
                                                        "channelUtilization": 3.1, "airUtilTx": 0.4,
                                                        "uptimeSeconds": int(el) + 3600}}}, 0, 0))

            # --- the plane: takeoff, climb, fly a figure-eight, vary altitude
            if el < 8:
                east, north, agl = 0.0, el * 2, 0.0  # taxi
            else:
                ft = el - 8
                w = 2 * math.pi / 90
                east = 220 * math.sin(w * ft)
                north = 16 + 140 * math.sin(2 * w * ft)
                agl = min(ft * 4, 80 + 25 * math.sin(ft / 15))
            jitter = random.gauss(0, 1.5)
            lat, lon = self._offset(east + jitter, north + jitter)
            # analytic velocity for the reported speed/track
            if el >= 8:
                ft = el - 8
                w = 2 * math.pi / 90
                ve = 220 * w * math.cos(w * ft)
                vn = 140 * 2 * w * math.cos(2 * w * ft)
            else:
                ve, vn = 0.0, 2.0
            speed_ms = math.hypot(ve, vn)
            track = (math.degrees(math.atan2(ve, vn)) + 360) % 360
            dist = math.hypot(east, north)
            rssi = int(-40 - 20 * math.log10(max(dist, 1)))
            t.handle_packet(self._packet(PLANE_NUM, {"portnum": "POSITION_APP", "position": {
                "latitude": lat, "longitude": lon, "latitudeI": int(lat * 1e7), "longitudeI": int(lon * 1e7),
                "altitude": int(self.ground_alt + agl), "time": int(now),
                "groundSpeed": int(round(speed_ms * 3.6)), "groundTrack": int(track * 1e5),
                "satsInView": random.randint(9, 14), "PDOP": random.randint(90, 160),
                "fixQuality": 1, "fixType": 3, "precisionBits": 32, "seqNumber": int(el / self.interval)}},
                snr=round(10 - dist / 40 + random.gauss(0, 1), 2), rssi=rssi))

            if now - last_tel > 15:
                last_tel = now
                battery = max(0.0, battery - 0.4)
                t.handle_packet(self._packet(PLANE_NUM, {"portnum": "TELEMETRY_APP", "telemetry": {
                    "time": int(now), "deviceMetrics": {
                        "batteryLevel": int(battery), "voltage": round(3.3 + 0.9 * battery / 100, 3),
                        "channelUtilization": round(random.uniform(2, 8), 2),
                        "airUtilTx": round(random.uniform(0.5, 2), 2), "uptimeSeconds": int(el) + 120}}},
                    snr=6.0, rssi=-70))
                t.handle_packet(self._packet(PLANE_NUM, {"portnum": "TELEMETRY_APP", "telemetry": {
                    "time": int(now), "environmentMetrics": {
                        "temperature": round(22 - agl * 0.0065 + random.gauss(0, 0.2), 2),
                        "relativeHumidity": round(48 + random.gauss(0, 1), 1),
                        "barometricPressure": round(1013.25 * (1 - (self.ground_alt + agl) / 44330) ** 5.255, 2)}}},
                    snr=6.0, rssi=-70))

            # --- a person walking around at the field, one hop away
            if now - last_hiker > 20 or last_hiker == 0:
                last_hiker = now
                hl, ho = self._offset(-120 + el * 0.4, -60 + 10 * math.sin(el / 60))
                t.handle_packet(self._packet(HIKER_NUM, {"portnum": "POSITION_APP", "position": {
                    "latitude": hl, "longitude": ho, "altitude": self.ground_alt + 1, "time": int(now),
                    "satsInView": 7, "precisionBits": 32}}, snr=-4.5, rssi=-108, hops=1))
                t.handle_packet(self._packet(HIKER_NUM, {"portnum": "TELEMETRY_APP", "telemetry": {
                    "deviceMetrics": {"batteryLevel": 63, "voltage": 3.81, "uptimeSeconds": 50000}}},
                    snr=-4.5, rssi=-108, hops=1))

            self._stop.wait(self.interval)
