"""Start the reverse proxy only once its AP address is assigned."""

import os
from pathlib import Path
import re
from time import sleep

from deploy.network import AP_ADDRESS, access_point_interface

PORT_CONF = Path("/etc/systemd/system/rasprint.socket.d/50-rasprint-ap.conf")
MITMDUMP = "/usr/bin/mitmdump"
PYTHON = "/opt/rasprint/.proxy-venv/bin/python"


def direct_port() -> int:
    try:
        config = PORT_CONF.read_text(encoding="ascii")
    except FileNotFoundError:
        return 8000
    match = re.search(r"^ListenStream=10\.42\.0\.1:(\d{1,5})$", config, re.MULTILINE)
    if not match or not 1 <= int(match[1]) <= 65535 or int(match[1]) == 80:
        raise RuntimeError("Invalid RasPrint direct port configuration")
    return int(match[1])


def main() -> None:
    while access_point_interface() is None:
        sleep(2)
    os.execv(PYTHON, [
        PYTHON, MITMDUMP, "--quiet", "--mode", f"reverse:http://{AP_ADDRESS}:{direct_port()}",
        "--listen-host", AP_ADDRESS, "--listen-port", "80",
        "--set", "confdir=/var/lib/rasprint-proxy",
        "--set", "keep_host_header=true", "--set", "stream_large_bodies=256k",
        "--set", "block_global=true", "--set", "http2=false",
        "-s", "/opt/rasprint/deploy/proxy_headers.py",
    ])


if __name__ == "__main__":
    main()
