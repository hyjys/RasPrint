"""Register discovered USB printers through a local CUPS socket."""

import json
import logging
import re
import socket
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import cups

from rasprint.cups_setup import (
    REQUEST_LIMIT, CupsSetupError, discovery_result, receive_packet, send_packet, validate_queue,
)


CUPS_SERVER = "/run/cups/cups.sock"
LPADMIN = "/usr/sbin/lpadmin"
IPP_USB = "/usr/sbin/ipp-usb"
DEVICE_LIMIT = 32
MODEL_LIMIT = 12
COMMAND_ENV = {"LANG": "C.UTF-8", "PATH": "/usr/bin:/usr/sbin", "CUPS_SERVER": CUPS_SERVER}
IPP_USB_DEVICE = re.compile(
    r"^\s*\d+\. Bus \d+ Device \d+\s+[a-fA-F0-9]{4}:[a-fA-F0-9]{4}\s+(\d{1,5})\s+(\x22.*\x22)\n"
    r"[ \t]+status: OK[ \t]*$",
    re.MULTILINE,
)
log = logging.getLogger("rasprint-cups")


def opaque_id(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def display_name(value: str, fallback: str = "USB printer") -> str:
    label = "".join(char for char in value if char.isprintable()).strip() or fallback
    return label.encode("utf-8")[:127].decode("utf-8", "ignore")


def canonical_model(value: str) -> str:
    model = re.sub(r"[^a-z0-9]", "", value.lower())
    model = re.sub(r"^(hewlettpackard|hp(?=officejet|laserjet|deskjet|photosmart|designjet|envy|smarttank))", "", model)
    return model.removesuffix("series")


def device_model(device_id: str) -> str:
    for field in device_id.split(";"):
        key, _, value = field.partition(":")
        if key.strip().upper() in {"MDL", "MODEL"} and value:
            return value
    return ""


def usb_uri(uri: str) -> bool:
    return bool(re.fullmatch(r"(?:usb://[^\s]+|usb:/dev/usb/lp\d+|hp:/usb/[^\s]+)", uri))


def identity(uri: str) -> tuple:
    try:
        parts = urlsplit(uri)
        if parts.scheme == "ipp" and parts.hostname in {"localhost", "127.0.0.1", "::1"}:
            return ("ipp-usb", parts.port, parts.path)
    except ValueError:
        return (uri,)
    if usb_uri(uri):
        serial = parse_qs(parts.query).get("serial")
        if serial:
            return ("usb", canonical_model(unquote(parts.path.rsplit("/", 1)[-1])), serial[0])
    return (uri,)


def driver_matches(reference: set[str], name: str, attributes: dict) -> bool:
    relevant = reference - {""}
    model = attributes.get("ppd-make-and-model", "")
    models = {
        canonical_model(device_model(attributes.get("ppd-device-id", ""))),
        canonical_model(model.split(",", 1)[0]),
    }
    if relevant & models:
        return True
    return (
        "officejetpro8710" in reference
        and bool(re.search(r"(?:^|/)hp-officejet_pro_8710(?:_[a-z0-9]+)?\.ppd(?:\.gz)?$", name, re.IGNORECASE))
    )


def command(path: str, *args: str, timeout: int = 18) -> str:
    return subprocess.run(
        (path, *args), capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, check=True,
        env=COMMAND_ENV, encoding="utf-8", errors="replace",
    ).stdout


def ipp_usb_devices() -> dict[str, str]:
    if not Path(IPP_USB).is_file():
        return {}
    try:
        status = command(IPP_USB, "status", timeout=3)
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for match in IPP_USB_DEVICE.finditer(status):
        port, model = match.groups()
        if not 1 <= int(port) <= 65535:
            continue
        try:
            name = json.loads(model)
        except ValueError:
            name = model.strip('"')
        found[f"ipp://localhost:{int(port)}/ipp/print"] = display_name(name)
    return found


@dataclass
class Device:
    uri: str
    name: str
    models: dict[str, str]
    suggested_queue: str
    registered_queue: str | None

    def as_json(self) -> dict:
        return {
            "id": opaque_id(self.uri), "name": self.name, "suggested_queue": self.suggested_queue,
            "registered_queue": self.registered_queue,
            "models": [{"id": opaque_id(driver), "name": label} for driver, label in self.models.items()],
        }


def printer_inventory(connection) -> list[Device]:
    raw_devices = connection.getDevices(limit=DEVICE_LIMIT, include_schemes=["usb", "hp"], timeout=5)
    installed = connection.getPrinters()
    configured = {
        identity(attrs["device-uri"]): queue
        for queue, attrs in installed.items() if attrs.get("device-uri")
    }
    local = {
        uri: attrs for uri, attrs in raw_devices.items()
        if attrs.get("device-class") == "direct" and usb_uri(uri)
    }
    ppds = connection.getPPDs() if local else {}
    inventory: dict[tuple, Device] = {}
    # Prefer HPLIP's native backend when it and the generic CUPS USB backend see the same device.
    for uri in sorted(local, key=lambda uri: not uri.startswith("hp:/usb/")):
        key = identity(uri)
        if key in inventory:
            continue
        attributes = local[uri]
        model = attributes.get("device-make-and-model") or ""
        model_id = device_model(attributes.get("device-id", ""))
        name = display_name(model if model.lower() != "unknown" else "", attributes.get("device-info") or "USB printer")
        reference = {
            canonical_model(model), canonical_model(model_id),
            canonical_model(unquote(urlsplit(uri).path.rsplit("/", 1)[-1])),
        }
        candidates = {
            driver: display_name(ppd.get("ppd-make-and-model", ""), driver)
            for driver, ppd in ppds.items()
            if not driver.startswith(("driverless:", "ipp:", "ipps:", "http:", "https:", "dnssd:", "file:"))
            and driver not in {"raw", "everywhere"}
            and driver_matches(reference, driver, ppd)
        }
        ordered = sorted(candidates, key=lambda driver: (
            "hpcups" not in candidates[driver].lower(), candidates[driver], not driver.startswith("drv:///"),
        ))
        unique = {}
        for driver in ordered:
            unique.setdefault(candidates[driver], driver)
        models = {driver: label for label, driver in list(unique.items())[:MODEL_LIMIT]}
        queue = suggested_queue(name, reference)
        inventory[key] = Device(uri, name, models, queue, configured.get(key))
    for uri, name in ipp_usb_devices().items():
        key = identity(uri)
        inventory[key] = Device(
            uri, name, {"everywhere": "IPP Everywhere (USB)"},
            suggested_queue(name, {canonical_model(name)}), configured.get(key),
        )
    return list(inventory.values())[:DEVICE_LIMIT]


def suggested_queue(name: str, reference: set[str]) -> str:
    if "officejetpro8710" in reference:
        return "hp8710"
    stripped = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return stripped[:60].rstrip("-") or "usb-printer"


def register(connection, request: dict) -> dict:
    queue = validate_queue(request.get("queue"))
    device_id, model_id = request.get("device_id"), request.get("model_id")
    device = next((item for item in printer_inventory(connection) if opaque_id(item.uri) == device_id), None)
    if device is None:
        raise CupsSetupError("device")
    model = next((driver for driver in device.models if opaque_id(driver) == model_id), None)
    if model is None:
        raise CupsSetupError("model")
    queues = connection.getPrinters()
    if any(existing.casefold() == queue.casefold() for existing in (*queues, *connection.getClasses())):
        raise CupsSetupError("duplicate_queue")
    if any(identity(attrs["device-uri"]) == identity(device.uri) for attrs in queues.values() if attrs.get("device-uri")):
        raise CupsSetupError("registered")
    try:
        command(
            LPADMIN, "-p", queue, "-E", "-v", device.uri, "-m", model, "-D", device.name,
            "-o", "printer-is-shared=false",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("CUPS rejected USB queue registration: %s", getattr(exc, "stderr", None) or type(exc).__name__)
        raise CupsSetupError("registration") from exc
    return {"queue": queue}


def handle(stream: socket.socket) -> None:
    stream.settimeout(5)
    try:
        request = receive_packet(stream, REQUEST_LIMIT)
        action = request.get("action")
        if action not in ("discover", "register"):
            raise CupsSetupError("invalid_response")
        connection = cups.Connection(host=CUPS_SERVER)
        if action == "discover":
            result = discovery_result([device.as_json() for device in printer_inventory(connection)])
        else:
            result = register(connection, request)
        response = {"result": result}
    except CupsSetupError as exc:
        response = {"error": exc.code}
    except (cups.IPPError, cups.HTTPError, OSError, RuntimeError) as exc:
        log.warning("CUPS USB discovery failed: %s", exc)
        response = {"error": "discovery"}
    except (ValueError, RecursionError):
        response = {"error": "invalid_response"}
    try:
        send_packet(stream, response)
    except OSError:
        pass


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cups.setUser("root")
    cups.setPasswordCB(lambda _prompt: None)
    with socket.socket(fileno=3) as listener:
        while True:
            connection, _ = listener.accept()
            with connection:
                handle(connection)


if __name__ == "__main__":
    main()
