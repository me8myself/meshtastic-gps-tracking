# Meshtastic GPS Tracker

This program tracks Meshtastic nodes on an **offline satellite map**. It was built to follow a small RC
airplane carrying a Meshtastic GPS tracker, but it works with any node that shares its position.

Plug a Meshtastic radio into your laptop (USB, WiFi or Bluetooth). The program listens to the mesh and shows a
live map in your web browser:

- **Every device it hears** appears on the map with its **name and battery level**.
- **A breadcrumb trail** shows where each device has been since the program connected.
- **Click a device** to see everything known about it: position, height, speed, climb rate, course,
  distance and bearing from home, satellites, GPS accuracy, battery and voltage, uptime, signal strength
  (SNR/RSSI), hops, hardware model, firmware, environment sensors and the raw data.
- **Flight stats** since connecting: max height, max speed, max climb and sink, max distance from home,
  total distance flown and flight time.
- **The base station** (the radio plugged into your computer) appears in the top bar with its name and
  battery. Click it to see its details.
- **Follow mode** keeps the map centred on the plane. **Reset trail** clears the trail between flights.
- **Export** any track as **GPX** (Google Earth and most mapping apps) or **CSV**. Every received fix is
  also logged to `logs/track_<date>.csv` automatically.
- **Metric or imperial** units.
- **Phones and tablets** work too: run with `--listen 0.0.0.0` and open the page on another device.

## Quick start

Requires Python 3.9 or newer.

```bash
git clone <this repo> && cd meshtastic-gps-tracking
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Try it without any hardware: a simulated plane flies a figure-eight
python -m tracker --demo

# Real radio, USB auto-detected
python -m tracker
```

A browser tab opens at <http://localhost:8080>.

### Connecting to the radio

| How | Command |
| --- | --- |
| USB (auto-detect) | `python -m tracker` |
| USB, specific port | `python -m tracker --port /dev/ttyUSB0` (Windows: `--port COM3`) |
| WiFi / Ethernet | `python -m tracker --host 192.168.1.50` |
| Bluetooth LE | `python -m tracker --ble` or `--ble "Meshtastic_1234"` |

If the radio is unplugged or the link drops, the program keeps retrying and reconnects on its own. Run
`python -m tracker --help` for every option.

On Linux, add yourself to the `dialout` group if you get a permission error on the serial port:
`sudo usermod -aG dialout $USER`, then log out and back in.

## Offline satellite map

At the flying field you probably won't have internet, so download the imagery at home first. There are two
ways to do it.

**From the app:** with internet access, run the program, pan and zoom the map so your field fills the
screen, then press **Offline map** → **Download**. Zoom 10–18 is a good range. The dialog shows how many
tiles that is.

**From the command line:**

```bash
# 2 km around a point, zoom 10 to 18 (about 1-2 thousand tiles)
python -m tracker.tiles --lat 45.3887 --lon -75.7038 --radius-km 2 --max-zoom 18

# or a bounding box
python -m tracker.tiles --bbox -75.72 45.38 -75.69 45.40 --min-zoom 12 --max-zoom 19
```

Tiles are saved in `tiles/{z}/{x}/{y}.jpg` (change this with `--tiles-dir`). Copy that folder to another
computer and it works there too. You can also drop `.mbtiles` files into the folder, for example ones
exported from QGIS or Mobile Atlas Creator.

If you zoom in past the detail you downloaded, or pan outside the high-detail area, the program enlarges the
nearest lower-zoom imagery so you still see a map instead of a blank screen.

Add `--fetch-missing` to download and keep any missing tile whenever internet happens to be available.

The default imagery source is Esri World Imagery. Please respect its
[terms of use](https://www.esri.com/en-us/legal/terms/full-master-agreement) and download only the areas
you need. Use `--url` with `python -m tracker.tiles` to pick a different XYZ tile source.

## Setting up the tracker on the plane

The plane only reports what its Meshtastic node sends, so configure that node for tracking. Use the
Meshtastic app or the CLI (`pip install meshtastic`) with the plane's radio plugged in:

```bash
# Tracker role prioritises position packets
meshtastic --set device.role TRACKER

# Send position often. Fixed interval, smart broadcast off.
meshtastic --set position.position_broadcast_secs 5 \
           --set position.position_broadcast_smart_enabled false \
           --set position.gps_update_interval 2

# Include altitude, speed, heading, satellites, accuracy and timestamps in each position
#   ALTITUDE(1)+ALTITUDE_MSL(2)+DOP(8)+SATINVIEW(32)+SEQ_NO(64)+TIMESTAMP(128)+HEADING(256)+SPEED(512)
meshtastic --set position.position_flags 1003

# Battery / voltage telemetry every minute
meshtastic --set telemetry.device_update_interval 60
```

Things to check:

- **Position precision:** the channel must share **full precision** positions. A reduced-precision channel
  rounds positions to hundreds of metres, which is useless for a plane. In the app, open the channel
  settings and enable precise location. The details panel shows "Precision: full" when this is right.
- **Update rate vs. airtime:** each packet on the default `LONG_FAST` preset takes about a second of
  airtime, and the firmware may enforce minimum intervals. For updates every few seconds, put the plane and
  the base station on **their own private channel** and a faster modem preset such as `SHORT_FAST`
  (`meshtastic --set lora.modem_preset SHORT_FAST` on **both** radios). Range is shorter, but at RC
  distances that's usually fine. Please don't flood a busy public mesh with fast position updates.
- **Antenna:** mount the plane's antenna vertically and away from carbon fibre, metal and the motor wiring.
- The base station radio needs to be on the same channel and preset as the plane.

## What the numbers mean

| Field | Source |
| --- | --- |
| Altitude (MSL) | GPS altitude above sea level, reported by the tracker |
| Height above launch | Altitude minus the first altitude received since connecting (or since **Reset trail**) |
| Height above home | Altitude minus the home altitude. Home is the base station's GPS position, or a point you set by right-clicking (long-pressing on a phone) the map and choosing **Set home here** |
| Ground speed (GPS) | Speed reported by the tracker's GPS |
| Speed (from fixes) | Distance between the last two fixes divided by the time between them |
| Climb rate | Altitude change between the last two fixes divided by the time between them |
| Course | GPS track, or the bearing between the last two fixes |
| Distance / bearing from home | Straight-line ground distance and direction from home to the device |
| Pressure altitude | From the barometric pressure sensor, if the node has one (e.g. a BME280) |
| SNR / RSSI / hops | Signal quality of the last packet heard from the device |

A dashed outline around a marker means the position came from the radio's memory and nothing new has been
heard since connecting. Markers fade when a device hasn't been heard for 2 minutes.

**Speed units:** current firmware sends GPS ground speed in km/h and track in units of 1e-5 degrees, even
though the protocol documentation says m/s and 1/100 degrees. If the GPS ground speed looks wrong compared
with "Speed (from fixes)", start the program with `--ground-speed-unit ms` (or `knots` / `mph`). Use
`--ground-track-scale` the same way for the course.

## Project layout

```
tracker/
  __main__.py   command line (python -m tracker)
  mesh.py       radio connection: USB / TCP / BLE, auto-reconnect
  state.py      node database, breadcrumb trails, derived flight data, CSV log, GPX export
  server.py     web server: UI, REST API, WebSocket live updates, map tiles
  tiles.py      offline tile store, fallback from lower zoom levels, tile downloader
  simulator.py  --demo mode
  static/       web UI (Leaflet is bundled, so no internet is needed)
tests/          pytest suite
```

Run the tests with `pip install pytest httpx && python -m pytest`.

### API

These endpoints are useful if you want to feed the data into something else:

- `GET /api/state`: every node, with full details and trails
- `WS /ws`: a snapshot, then live `node`, `connection`, `home` and `reset` messages
- `GET /api/nodes/{id}/track.gpx` and `.../track.csv`: track export (`id` is a node number or `!hex` id)
- `POST /api/nodes/{id}/reset`: clear a trail and its flight stats
- `POST /api/home` `{"lat":..,"lon":..,"alt":..}` / `DELETE /api/home`: set or clear the manual home point
