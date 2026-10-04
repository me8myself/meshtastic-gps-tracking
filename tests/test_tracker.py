import io
import json
import time

import pytest

from tracker.state import Tracker, bearing_deg, haversine_m, sanitize, track_to_gpx
from tracker.tiles import TileStore, count_tiles, lonlat_to_tile, save_tile, tile_to_lonlat

LOCAL = 0xA0000001
PLANE = 0xB0000002


def pkt(num, decoded, **extra):
    p = {"from": num, "fromId": f"!{num:08x}", "rxTime": int(time.time()), "rxSnr": 5.5, "rxRssi": -80,
         "hopStart": 3, "hopLimit": 2, "decoded": decoded}
    p.update(extra)
    return p


def pos(lat, lon, alt, t, **extra):
    return {"portnum": "POSITION_APP", "position": {"latitude": lat, "longitude": lon, "altitude": alt, "time": t, **extra}}


def test_geo_math():
    # one degree of latitude is ~111.2 km
    assert haversine_m(0, 0, 1, 0) == pytest.approx(111195, rel=1e-3)
    assert bearing_deg(0, 0, 1, 0) == pytest.approx(0)
    assert bearing_deg(0, 0, 0, 1) == pytest.approx(90)
    assert bearing_deg(0, 0, -1, 0) == pytest.approx(180)


def test_sanitize_drops_protobuf_and_bytes():
    class Proto:
        pass

    out = sanitize({"decoded": {"payload": b"\x01\x02", "position": {"raw": Proto(), "latitude": 1.0}}})
    json.dumps(out)
    assert "raw" not in out["decoded"]["position"]
    assert out["decoded"]["payload"] == "AQI="


def test_flight_tracking_and_derived_values():
    events = []
    tr = Tracker()
    tr.add_listener(events.append)
    tr.set_local_node(LOCAL)
    t0 = int(time.time()) - 20
    tr.handle_packet(pkt(LOCAL, pos(45.0, -75.0, 100, t0)))

    tr.handle_packet(pkt(PLANE, {"portnum": "NODEINFO_APP", "user": {"id": "!b0000002", "longName": "RC Plane",
                                                                        "shortName": "RCP", "hwModel": "TBEAM"}}))
    tr.handle_packet(pkt(PLANE, pos(45.0, -75.0, 100, t0)))
    # 0.001 deg north ~ 111 m in 10 s, climbing 50 m
    tr.handle_packet(pkt(PLANE, pos(45.001, -75.0, 150, t0 + 10, groundSpeed=40, groundTrack=0)))
    # duplicate relay of the same fix must not add a breadcrumb
    tr.handle_packet(pkt(PLANE, pos(45.001, -75.0, 150, t0 + 10)))
    tr.handle_packet(pkt(PLANE, {"portnum": "TELEMETRY_APP", "telemetry": {
        "deviceMetrics": {"batteryLevel": 77, "voltage": 3.9}}}))

    node = tr.find("!b0000002")
    assert node.name == "RC Plane"
    assert len(node.track) == 2
    d = node.derived
    assert d["computedSpeed"] == pytest.approx(11.12, rel=0.01)
    assert d["reportedSpeed"] == pytest.approx(40 / 3.6)  # firmware sends km/h by default
    assert d["climbRate"] == pytest.approx(5.0)
    assert d["heightAboveLaunch"] == 50
    assert d["heightAboveHome"] == 50
    assert d["distanceFromHome"] == pytest.approx(111.2, rel=0.01)
    assert d["bearingFromHome"] == pytest.approx(0, abs=0.01)
    assert node.stats["maxAltitude"] == 150
    assert node.radio["hopsAway"] == 1
    assert node.metrics["deviceMetrics"]["batteryLevel"] == 77

    snap = tr.snapshot()
    json.dumps(snap)
    assert len(snap["tracks"][str(PLANE)]) == 2
    assert any(e.get("point") for e in events if e["type"] == "node")

    gpx = track_to_gpx(node, list(node.track))
    assert gpx.count("<trkpt") == 2

    tr.reset_node(PLANE)
    assert len(node.track) == 0 and node.stats == {}


def test_manual_home_overrides_base():
    tr = Tracker()
    tr.set_local_node(LOCAL)
    t0 = int(time.time())
    tr.handle_packet(pkt(PLANE, pos(45.0, -75.0, 200, t0)))
    assert tr.find(str(PLANE)).derived["distanceFromHome"] is None  # no home known yet
    tr.set_home(45.0, -75.001, 180)
    d = tr.find(str(PLANE)).derived
    assert d["distanceFromHome"] == pytest.approx(78.6, rel=0.02)
    assert d["heightAboveHome"] == 20
    tr.clear_home()
    assert tr.home() is None


def test_node_db_positions_are_not_breadcrumbs():
    tr = Tracker()
    tr.load_node_db({"!b0000002": {"num": PLANE, "user": {"id": "!b0000002", "longName": "Old"},
                                   "position": {"latitude": 1.0, "longitude": 2.0}, "lastHeard": 1000}})
    node = tr.find(str(PLANE))
    assert node.position_from_db and len(node.track) == 0


def test_csv_log(tmp_path):
    tr = Tracker(log_dir=str(tmp_path))
    tr.handle_packet(pkt(PLANE, pos(45.0, -75.0, 100, int(time.time()))))
    tr.close()
    lines = open(tr.log_path).read().strip().splitlines()
    assert len(lines) == 2 and "45.0000000" in lines[1]


def test_tile_math_roundtrip():
    x, y = lonlat_to_tile(-75.7, 45.39, 15)
    lon, lat = tile_to_lonlat(x, y, 15)
    assert lon <= -75.7 and lat >= 45.39
    assert count_tiles((-75.71, 45.38, -75.69, 45.40), range(15, 16)) >= 4


def test_tile_store_parent_fallback(tmp_path):
    from PIL import Image

    store = TileStore(str(tmp_path))
    img = Image.new("RGB", (256, 256), (10, 200, 30))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    save_tile(store, 10, 300, 400, buf.getvalue())

    exact = store.get(10, 300, 400)
    assert exact and exact[1] == "image/jpeg"
    child = store.get(12, 1201, 1602)  # two levels down, inside the parent tile
    assert child is not None
    out = Image.open(io.BytesIO(child[0]))
    assert out.size == (256, 256)
    assert store.get(12, 0, 0) is None  # nothing anywhere above this one
    info = store.info()
    assert info["tileCount"] == 1 and info["minZoom"] == 10


def test_server_endpoints(tmp_path):
    from fastapi.testclient import TestClient

    from tracker.server import create_app

    tr = Tracker()
    tr.handle_packet(pkt(PLANE, pos(45.0, -75.0, 100, int(time.time()))))
    app = create_app(tr, TileStore(str(tmp_path)))
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/api/config").json()["tiles"]["tileCount"] == 0
        assert client.get("/tiles/3/1/1").status_code == 404
        assert len(client.get("/api/state").json()["nodes"]) == 1
        assert "<trkpt" in client.get(f"/api/nodes/{PLANE}/track.gpx").text
        assert client.post("/api/home", json={"lat": 45.0, "lon": -75.0}).json()["home"]["source"] == "manual"
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "snapshot"
            tr.handle_packet(pkt(PLANE, pos(45.0005, -75.0, 120, int(time.time()) + 5)))
            msg = ws.receive_json()
            while msg["type"] != "node":
                msg = ws.receive_json()
            assert msg["point"][1] == 45.0005


def test_mesh_connection_with_fake_radio(monkeypatch):
    """Drive MeshConnection end to end with a stand-in for the meshtastic interface."""
    from types import SimpleNamespace

    from meshtastic.protobuf import config_pb2, localonly_pb2
    from pubsub import pub

    from tracker import mesh

    lc = localonly_pb2.LocalConfig()
    lc.lora.region = config_pb2.Config.LoRaConfig.RegionCode.US
    lc.device.role = config_pb2.Config.DeviceConfig.Role.CLIENT
    lc.position.position_broadcast_secs = 30

    class FakeIface:
        def __init__(self):
            self.myInfo = SimpleNamespace(my_node_num=LOCAL, reboot_count=3)
            self.metadata = SimpleNamespace(firmware_version="2.6.0", hasBluetooth=True, hasWifi=False)
            self.localNode = SimpleNamespace(localConfig=lc)
            self.nodes = {"!b0000002": {"num": PLANE, "user": {"id": "!b0000002", "longName": "Plane"}}}
            self.closed = False

        def close(self):
            self.closed = True

    fake = FakeIface()
    tr = Tracker()
    conn = mesh.MeshConnection(tr, port="/dev/fake", reconnect_delay=0.05)
    monkeypatch.setattr(conn, "_open", lambda: fake)
    conn.start()
    try:
        for _ in range(100):
            if tr.connection["status"] == "connected":
                break
            time.sleep(0.02)
        assert tr.connection["status"] == "connected"
        assert tr.local_num == LOCAL
        local = tr.nodes[LOCAL]
        assert local.extra["region"] == "US" and local.extra["firmwareVersion"] == "2.6.0"
        assert local.extra["positionBroadcastSecs"] == 30

        pub.sendMessage("meshtastic.receive", packet=pkt(PLANE, pos(45.0, -75.0, 100, int(time.time()))),
                        interface=fake)
        assert len(tr.nodes[PLANE].track) == 1

        pub.sendMessage("meshtastic.connection.lost", interface=fake)
        for _ in range(100):
            if fake.closed:
                break
            time.sleep(0.02)
        assert fake.closed
    finally:
        conn.stop()


def test_mesh_connection_reports_missing_radio(monkeypatch):
    from tracker import mesh

    tr = Tracker()
    conn = mesh.MeshConnection(tr, reconnect_delay=0.05)
    monkeypatch.setattr("meshtastic.util.findPorts", lambda *a: [])
    conn.start()
    try:
        for _ in range(100):
            if tr.connection["status"] == "error":
                break
            time.sleep(0.02)
        assert tr.connection["status"] == "error"
        assert "no Meshtastic USB device" in tr.connection["detail"]
    finally:
        conn.stop()


def test_settings_file(tmp_path, monkeypatch):
    from tracker import __main__ as cli

    (tmp_path / "settings.txt").write_text('# comment\n--port COM5\n\n--ble "My Radio"  # trailing\n')
    monkeypatch.setattr(cli, "app_dir", lambda: str(tmp_path))
    assert cli.read_settings_file() == ["--port", "COM5", "--ble", "My Radio"]
