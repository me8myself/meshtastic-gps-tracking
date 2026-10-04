"""Connection to a Meshtastic radio (USB serial, TCP/WiFi or Bluetooth LE).

Runs in a background thread, feeds every received packet into the Tracker and
reconnects automatically if the radio is unplugged or the link drops.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from .state import Tracker

log = logging.getLogger(__name__)


class MeshConnection:
    def __init__(
        self,
        tracker: Tracker,
        port: Optional[str] = None,
        host: Optional[str] = None,
        ble: Optional[str] = None,
        reconnect_delay: float = 5.0,
    ) -> None:
        self.tracker = tracker
        self.port = port
        self.host = host
        self.ble = ble
        self.reconnect_delay = reconnect_delay
        self.iface = None
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mesh-connection", daemon=True)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        from pubsub import pub

        # Keep references: pypubsub only holds weak references to listeners.
        self._on_receive_ref = self._on_receive
        self._on_lost_ref = self._on_lost
        pub.subscribe(self._on_receive_ref, "meshtastic.receive")
        pub.subscribe(self._on_lost_ref, "meshtastic.connection.lost")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._lost.set()
        self._close()

    @property
    def description(self) -> str:
        if self.host:
            return f"TCP {self.host}"
        if self.ble is not None:
            return f"Bluetooth {self.ble or '(auto)'}"
        return f"USB serial {self.port or '(auto-detect)'}"

    def _open(self):
        if self.host:
            from meshtastic.tcp_interface import TCPInterface

            host, _, port = self.host.partition(":")
            return TCPInterface(hostname=host, portNumber=int(port) if port else 4403)
        if self.ble is not None:
            from meshtastic.ble_interface import BLEInterface

            return BLEInterface(self.ble or None)
        from meshtastic.serial_interface import SerialInterface

        if self.port:
            return SerialInterface(devPath=self.port)
        # Auto-detect ourselves: the library falls back to a half-open interface when
        # nothing is found and exits the program when several ports are present.
        from meshtastic.util import findPorts

        ports = findPorts(True)
        if not ports:
            raise RuntimeError("no Meshtastic USB device found (is it plugged in? try --port)")
        errors = []
        for port in ports:
            try:
                return SerialInterface(devPath=port)
            except Exception as ex:
                errors.append(f"{port}: {ex}")
        raise RuntimeError("; ".join(errors))

    def _close(self) -> None:
        iface, self.iface = self.iface, None
        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tracker.set_connection("connecting", f"Connecting via {self.description}...")
            try:
                self._lost.clear()
                self.iface = self._open()
                self._on_connected(self.iface)
            except (Exception, SystemExit) as ex:  # no radio found, permission denied, BLE pairing failure...
                log.warning("Connection failed: %s", ex)
                self._close()
                self.tracker.set_connection(
                    "error", f"{self.description}: {ex}. Retrying in {self.reconnect_delay:.0f}s"
                )
                self._stop.wait(self.reconnect_delay)
                continue

            # Wait until the link is lost or we're asked to stop.
            while not self._stop.is_set() and not self._lost.is_set():
                self._lost.wait(1.0)
            self._close()
            if not self._stop.is_set():
                self.tracker.set_connection(
                    "error", f"Lost connection to {self.description}. Reconnecting..."
                )
                self._stop.wait(self.reconnect_delay)
        self.tracker.set_connection("disconnected", "Stopped")

    # ------------------------------------------------------------------ events
    def _on_connected(self, iface) -> None:
        info: Dict[str, Any] = {}
        my_num = None
        try:
            my_num = iface.myInfo.my_node_num
        except Exception:
            pass
        try:
            md = iface.metadata
            if md is not None:
                info["firmwareVersion"] = md.firmware_version
                info["hasBluetooth"] = md.hasBluetooth
                info["hasWifi"] = md.hasWifi
        except Exception:
            pass
        try:
            if iface.myInfo is not None:
                info["rebootCount"] = iface.myInfo.reboot_count
        except Exception:
            pass
        try:
            ln = iface.localNode
            if ln is not None and ln.localConfig is not None:
                lc = ln.localConfig
                info["region"] = _enum_name(lc.lora, "region")
                info["modemPreset"] = _enum_name(lc.lora, "modem_preset")
                info["role"] = _enum_name(lc.device, "role")
                info["positionBroadcastSecs"] = lc.position.position_broadcast_secs
                info["gpsMode"] = _enum_name(lc.position, "gps_mode")
        except Exception:
            pass

        if iface.nodes:
            self.tracker.load_node_db(iface.nodes)
        if my_num is not None:
            self.tracker.set_local_node(my_num, info)
        self.tracker.set_connection("connected", f"Connected via {self.description}")

    def _on_receive(self, packet, interface=None) -> None:
        if interface is not None and interface is not self.iface:
            return
        try:
            self.tracker.handle_packet(packet)
        except Exception:
            log.exception("Failed to process packet")

    def _on_lost(self, interface=None) -> None:
        if interface is None or interface is self.iface:
            self._lost.set()


def _enum_name(msg, field_name: str) -> Optional[str]:
    """Readable name of an enum field on a protobuf message."""
    try:
        fd = msg.DESCRIPTOR.fields_by_name[field_name]
        return fd.enum_type.values_by_number[getattr(msg, field_name)].name
    except Exception:
        return None

