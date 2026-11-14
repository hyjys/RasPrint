"""Root-side bridge restricted to the installed RasPrint AP and HTTP socket."""

from base64 import b64decode, b64encode
from configparser import ConfigParser
from hashlib import pbkdf2_hmac
from io import StringIO
import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
from tempfile import NamedTemporaryFile
from threading import Lock, Thread
from time import sleep
from uuid import UUID

from rasprint.ap import (
    AP_ADDRESS, DEFAULT_NETWORK, AccessPointError, apply_result, network_values,
    receive_packet, send_packet, status_result,
)

PROFILE = Path("/etc/NetworkManager/system-connections/rasprint-ap.nmconnection")
DROP_IN = Path("/etc/systemd/system/rasprint.socket.d/50-rasprint-ap.conf")
JOURNAL = Path("/var/lib/rasprint-ap/rollback.json")
NMCLI = "/usr/bin/nmcli"
SYSTEMCTL = "/usr/bin/systemctl"
WEB_UNITS = ("rasprint.socket", "rasprint.service")
log = logging.getLogger("rasprint-ap")


def command(*arguments: str) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=35, check=True)
    return result.stdout


def write_file(path: Path, value: bytes | None, mode: int = 0o600) -> None:
    if value is None:
        path.unlink(missing_ok=True)
    else:
        with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(value)
                stream.flush()
                os.fchmod(stream.fileno(), mode)
                os.fsync(stream.fileno())
                stream.close()
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_drop_in() -> bytes | None:
    try:
        return DROP_IN.read_bytes()
    except FileNotFoundError:
        return None


def read_profile():
    try:
        contents = PROFILE.read_bytes()
    except FileNotFoundError as exc:
        raise AccessPointError("unconfigured") from exc
    config = ConfigParser(interpolation=None)
    config.read_string(contents.decode("utf-8"))
    connection = config["connection"]
    wifi = config["wifi"]
    if connection["id"] != "RasPrint-AP" or connection["interface-name"] != "wlan0" or wifi["mode"] != "ap":
        raise ValueError("Unexpected NetworkManager profile")
    uuid = str(UUID(connection["uuid"]))
    if config["ipv4"]["address1"] != f"{AP_ADDRESS}/24":
        raise ValueError("Unexpected address in NetworkManager profile")
    raw_ssid = wifi["ssid"]
    if re.fullmatch(r"(?:\d{1,3};)+", raw_ssid):
        ssid = bytes(int(value) for value in raw_ssid.rstrip(";").split(";")).decode("utf-8")
    else:
        ssid = raw_ssid
    secured = config.get("wifi-security", "key-mgmt", fallback="none") == "wpa-psk"
    drop_in = read_drop_in()
    match = re.search(rb"^ListenStream=10\.42\.0\.1:(\d{1,5})$", drop_in or b"", re.MULTILINE)
    if drop_in and not match:
        raise ValueError("Unexpected socket configuration")
    port = int(match.group(1)) if match else DEFAULT_NETWORK["port"]
    return contents, config, uuid, network_values(ssid, secured, None, port), drop_in


def modify_profile(config, network: dict, password: str | None) -> bytes:
    ssid = network["ssid"].encode("utf-8")
    config["wifi"]["ssid"] = "".join(f"{byte};" for byte in ssid)
    if network["secured"]:
        if password:
            key = pbkdf2_hmac("sha1", password.encode("ascii"), ssid, 4096, 32).hex()
        else:
            key = config.get("wifi-security", "psk", fallback="")
            if not key:
                raise AccessPointError("missing_password")
        config["wifi-security"] = {
            "key-mgmt": "wpa-psk", "proto": "rsn;", "pairwise": "ccmp;",
            # Disabling PMF avoids a brcmfmac activation failure on the Pi Zero W.
            "group": "ccmp;", "pmf": "1", "psk": key,
        }
    else:
        config.remove_section("wifi-security")
    output = StringIO()
    config.write(output, space_around_delimiters=False)
    return output.getvalue().encode("utf-8")


def port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_FREEBIND", 15), 1)
        try:
            probe.bind((AP_ADDRESS, port))
        except OSError as exc:
            raise AccessPointError("occupied") from exc


def active_on_wlan(uuid: str) -> bool:
    active = command(NMCLI, "-t", "-f", "UUID,DEVICE", "connection", "show", "--active")
    return f"{uuid}:wlan0" in active.splitlines()


def reload_profile(uuid: str, was_active: bool) -> None:
    subprocess.run(
        (NMCLI, "connection", "load", str(PROFILE)),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=True,
    )
    if was_active:
        if active_on_wlan(uuid):
            command(NMCLI, "--wait", "15", "connection", "down", "uuid", uuid)
        command(NMCLI, "--wait", "25", "connection", "up", "uuid", uuid, "ifname", "wlan0")


def replace_web_port(drop_in: bytes | None) -> None:
    write_file(DROP_IN, drop_in, 0o644)
    command(SYSTEMCTL, "daemon-reload")
    command(SYSTEMCTL, "stop", *WEB_UNITS)
    command(SYSTEMCTL, "start", "rasprint.socket")
    command(SYSTEMCTL, "restart", "rasprint-proxy.service")


def restore(backup: dict) -> None:
    failures = []
    if backup["port_changed"]:
        try:
            replace_web_port(b64decode(backup["drop_in"]) if backup["drop_in"] is not None else None)
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(exc)
    if backup["wifi_changed"]:
        try:
            write_file(PROFILE, b64decode(backup["profile"]))
            reload_profile(backup["uuid"], backup["was_active"])
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(exc)
    if failures:
        raise RuntimeError("The RasPrint AP could not be rolled back") from failures[0]
    write_file(JOURNAL, None)


class Helper:
    def __init__(self):
        self.lock = Lock()
        self.pending = False
        self.error = None
        if JOURNAL.exists():
            self.recover()

    def recover(self):
        try:
            restore(json.loads(JOURNAL.read_bytes()))
            self.error = "failed"
        except Exception:
            log.exception("Could not recover the previous AP configuration")
            self.error = "rollback"

    def status(self) -> dict:
        try:
            _, _, _, network, _ = read_profile()
        except AccessPointError as exc:
            return status_result(DEFAULT_NETWORK, available=False, error=exc.code)
        except (OSError, ValueError, KeyError):
            return status_result(DEFAULT_NETWORK, available=False, error="read")
        with self.lock:
            return status_result(network, pending=self.pending, error=self.error)

    def prepare(self, request: dict):
        ssid, secured, password, port = (request.get(key) for key in ("ssid", "secured", "password", "port"))
        network = network_values(ssid, secured, password, port)
        with self.lock:
            if self.pending:
                raise AccessPointError("busy")
            if JOURNAL.exists():
                raise AccessPointError("rollback")
            original, config, uuid, previous, previous_drop_in = read_profile()
            ssid_changed = previous["ssid"] != ssid
            if secured and not password and (ssid_changed or not previous["secured"]):
                raise AccessPointError("missing_password")
            wifi_changed = ssid_changed or previous["secured"] != secured or bool(secured and password)
            port_changed = previous["port"] != port
            if not (wifi_changed or port_changed):
                return apply_result(network), None
            if port_changed:
                port_available(port)
            new_profile = modify_profile(config, network, password) if wifi_changed else None
            drop_in = f"[Socket]\nListenStream=\nListenStream={AP_ADDRESS}:{port}\n".encode("ascii")
            backup = {
                "profile": b64encode(original).decode("ascii"),
                "drop_in": b64encode(previous_drop_in).decode("ascii") if previous_drop_in is not None else None,
                "uuid": uuid, "was_active": bool(wifi_changed and active_on_wlan(uuid)),
                "port_changed": port_changed, "wifi_changed": wifi_changed,
            }
            self.pending = True
            self.error = None
        worker = Thread(target=self.commit, args=(new_profile, drop_in, backup), daemon=True)
        return apply_result(network), worker

    def commit(self, new_profile: bytes | None, drop_in: bytes, backup: dict) -> None:
        # Give the web server time to deliver the destination URL before disrupting clients.
        sleep(2)
        try:
            write_file(JOURNAL, json.dumps(backup).encode("ascii"))
            if backup["port_changed"]:
                replace_web_port(drop_in)
            if new_profile is not None:
                write_file(PROFILE, new_profile)
                reload_profile(backup["uuid"], backup["was_active"])
            write_file(JOURNAL, None)
        except Exception:
            log.exception("Could not apply the AP configuration")
            if JOURNAL.exists():
                self.recover()
            else:
                self.error = "failed"
        finally:
            with self.lock:
                self.pending = False

    def handle(self, stream: socket.socket):
        stream.settimeout(4)
        worker = None
        try:
            request = receive_packet(stream)
            if request.get("action") == "status":
                response = {"result": self.status()}
            elif request.get("action") == "apply":
                result, worker = self.prepare(request)
                response = {"result": result}
            else:
                response = {"error": "invalid_response"}
        except AccessPointError as exc:
            response = {"error": exc.code}
        except (OSError, ValueError, KeyError, RecursionError, subprocess.SubprocessError):
            response = {"error": "read"}
        try:
            send_packet(stream, response)
        except OSError:
            if worker:
                with self.lock:
                    self.pending = False
        else:
            if worker:
                worker.start()


def main():
    logging.basicConfig(level=logging.INFO)
    helper = Helper()
    with socket.socket(fileno=3) as listener:
        while True:
            connection, _ = listener.accept()
            with connection:
                helper.handle(connection)


if __name__ == "__main__":
    main()
