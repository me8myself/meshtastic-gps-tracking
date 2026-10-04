"""Start the packaged executable in demo mode and check the web UI and live data work."""

import json
import subprocess
import sys
import tempfile
import time
import urllib.request

exe = sys.argv[1]
port = 18080
base = f"http://127.0.0.1:{port}"

out = subprocess.run([exe, "tiles", "--help"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
assert out.returncode == 0 and "--radius-km" in out.stdout, out.stdout + out.stderr


def stop(proc: subprocess.Popen) -> None:
    """Stop the executable. A one-file build on Windows is a launcher plus a child process,
    and terminate() only reaches the launcher, so kill the whole process tree there."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
    proc.wait(timeout=30)


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    proc = subprocess.Popen(
        [exe, "--demo", "--no-browser", "--http-port", str(port), "--tiles-dir", f"{tmp}/tiles", "--log-dir", f"{tmp}/logs"],
        stdin=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 90
        state = None
        while time.time() < deadline:
            try:
                state = json.load(urllib.request.urlopen(f"{base}/api/state", timeout=5))
                if any(n.get("position") for n in state["nodes"]):
                    break
            except OSError:
                pass
            if proc.poll() is not None:
                sys.exit(f"executable exited early with code {proc.returncode}")
            time.sleep(1)
        assert state and len(state["nodes"]) >= 2, f"no demo nodes: {state}"
        for path in ("/", "/static/app.js", "/static/vendor/leaflet/leaflet.js"):
            assert urllib.request.urlopen(base + path, timeout=5).status == 200, path
        print(f"OK: {len(state['nodes'])} nodes, connection {state['connection']['status']}")
    finally:
        stop(proc)
