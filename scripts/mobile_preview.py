"""Time-limited cloud development preview; never imports local game data.

The workflow keeps this process alive on a disposable runner for a manual
phone/browser integration test. Only the public URL and expiry are published.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
OPENER = build_opener(ProxyHandler({}))


def healthy(url):
    try:
        with OPENER.open(url + "/healthz", timeout=5) as response:
            return response.status == 200 and json.load(response).get("status") == "ok"
    except (OSError, ValueError, URLError):
        return False


def stop(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=60, choices=range(5, 61))
    parser.add_argument("--cloudflared", required=True)
    parser.add_argument("--metadata-file", type=Path, required=True)
    args = parser.parse_args()
    args.metadata_file.unlink(missing_ok=True)
    stopping = False

    def request_stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    server = tunnel = None
    # Child services do not receive the workflow's GitHub/ACTIONS credentials.
    child_env = {key: value for key, value in os.environ.items()
                 if key in ("PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR")}
    child_env["PYTHONUNBUFFERED"] = "1"
    try:
        with tempfile.TemporaryDirectory(prefix="after-hours-preview-") as folder:
            work = Path(folder)
            child_env["ROOMS_DATABASE"] = str(work / "rooms.sqlite3")
            with (work / "server.log").open("wb") as server_log, (work / "tunnel.log").open("wb") as tunnel_log:
                server = subprocess.Popen(
                    [sys.executable, "-m", "gunicorn", "--bind", "127.0.0.1:8780",
                     "--workers", "1", "--threads", "8", "--timeout", "60",
                     "--access-logfile", "/dev/null", "multiplayer_app:app"],
                    cwd=ROOT, env=child_env, stdout=server_log, stderr=subprocess.STDOUT,
                )
                for _ in range(30):
                    if healthy("http://127.0.0.1:8780"):
                        break
                    if stopping or server.poll() is not None:
                        raise RuntimeError("Preview server did not start.")
                    time.sleep(1)
                else:
                    raise RuntimeError("Preview server health check timed out.")
                tunnel = subprocess.Popen(
                    [args.cloudflared, "tunnel", "--no-autoupdate", "--protocol", "http2",
                     "--url", "http://127.0.0.1:8780"],
                    env=child_env, stdout=tunnel_log, stderr=subprocess.STDOUT,
                )
                public_url = None
                for _ in range(90):
                    if stopping or tunnel.poll() is not None or server.poll() is not None:
                        raise RuntimeError("Preview services stopped before becoming ready.")
                    match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", (work / "tunnel.log").read_text())
                    if match and healthy(match[0]):
                        public_url = match[0]
                        break
                    time.sleep(1)
                if public_url is None:
                    raise RuntimeError("Cloudflare public health check timed out.")
                expires = datetime.now(timezone.utc) + timedelta(minutes=args.minutes)
                metadata = dict(url=public_url, expires_at=expires.isoformat(), minutes=args.minutes)
                temporary = args.metadata_file.with_suffix(".tmp")
                temporary.write_text(json.dumps(metadata))
                temporary.replace(args.metadata_file)
                print(json.dumps(metadata), flush=True)
                deadline = time.monotonic() + args.minutes * 60
                while not stopping and time.monotonic() < deadline:
                    if server.poll() is not None or tunnel.poll() is not None:
                        raise RuntimeError("A preview service exited unexpectedly.")
                    time.sleep(2)
                # Stop before TemporaryDirectory removes the live SQLite files.
                stop(tunnel)
                stop(server)
    finally:
        stop(tunnel)
        stop(server)


if __name__ == "__main__":
    main()
