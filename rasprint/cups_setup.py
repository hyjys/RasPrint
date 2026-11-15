"""Client for the local, privileged CUPS setup service."""

import json
import re
import socket
from threading import Lock


CUPS_SETUP_SOCKET = "/run/rasprint-cups.sock"
REQUEST_LIMIT = 2048
RESPONSE_LIMIT = 256 * 1024
QUEUE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,126}")
ERRORS = {
    "unavailable": (
        503, "USB 프린터 등록 서비스에 연결할 수 없습니다.",
        "The USB printer setup service is unavailable.",
    ),
    "discovery": (
        503, "CUPS에서 프린터를 찾는 중 오류가 발생했습니다.",
        "CUPS printer discovery failed.",
    ),
    "device": (
        404, "USB 프린터가 연결되어 있지 않습니다. 다시 검색하세요.",
        "The USB printer is no longer available. Scan again.",
    ),
    "model": (
        409, "이 프린터의 드라이버를 사용할 수 없습니다. 다시 검색하세요.",
        "The driver is no longer available for this printer. Scan again.",
    ),
    "queue": (
        422, "대기열 이름은 영문자 또는 숫자로 시작하고 영문자, 숫자, 마침표, 밑줄, 하이픈을 1~127자로 입력하세요.",
        "Use 1–127 letters, numbers, periods, underscores, or hyphens for the queue name. Start with a letter or number.",
    ),
    "duplicate_queue": (
        409, "이 이름의 CUPS 대기열이 이미 있습니다.",
        "A CUPS queue with this name already exists.",
    ),
    "registered": (
        409, "이 USB 프린터는 CUPS 대기열에 이미 등록되어 있습니다.",
        "This USB printer already has a CUPS queue.",
    ),
    "registration": (
        500, "CUPS에 프린터를 등록할 수 없습니다.",
        "CUPS could not register the printer.",
    ),
    "invalid_response": (
        503, "USB 프린터 등록 서비스의 응답이 올바르지 않습니다.",
        "The USB printer setup service returned an invalid response.",
    ),
}


class CupsSetupError(Exception):
    def __init__(self, code: str):
        self.code = code
        self.status_code, korean, self.english = ERRORS[code]
        super().__init__(korean)


def validate_queue(value: str) -> str:
    if not isinstance(value, str) or not QUEUE_NAME.fullmatch(value):
        raise CupsSetupError("queue")
    return value


def send_packet(stream: socket.socket, payload: dict) -> None:
    stream.sendall(json.dumps(payload, ensure_ascii=True).encode("ascii") + b"\n")


def receive_packet(stream: socket.socket, limit: int) -> dict:
    with stream.makefile("rb") as file:
        line = file.readline(limit + 1)
    if len(line) <= limit and line.endswith(b"\n"):
        payload = json.loads(line)
        if isinstance(payload, dict):
            return payload
    raise ValueError("Invalid CUPS setup message")


def discovery_result(devices: list[dict], error: CupsSetupError | None = None) -> dict:
    return {
        "available": not error, "devices": devices, "error": str(error) if error else None,
        "error_english": error.english if error else None,
    }


class CupsSetup:
    def _request(self, payload: dict) -> dict:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(35)
                stream.connect(CUPS_SETUP_SOCKET)
                send_packet(stream, payload)
                response = receive_packet(stream, RESPONSE_LIMIT)
        except (OSError, AttributeError) as exc:
            raise CupsSetupError("unavailable") from exc
        except (ValueError, RecursionError) as exc:
            raise CupsSetupError("invalid_response") from exc
        error = response.get("error")
        if isinstance(error, str) and error in ERRORS:
            raise CupsSetupError(error)
        result = response.get("result")
        if not isinstance(result, dict):
            raise CupsSetupError("invalid_response")
        return result

    def discover(self) -> dict:
        try:
            return self._request({"action": "discover"})
        except CupsSetupError as exc:
            return discovery_result([], exc)

    def register(self, device_id: str, model_id: str, queue: str) -> dict:
        return self._request({
            "action": "register", "device_id": device_id, "model_id": model_id,
            "queue": validate_queue(queue),
        })


class DemoCupsSetup:
    def __init__(self, queue_provider, add_queue):
        self.queue_provider = queue_provider
        self.add_queue = add_queue
        self.registered_queue = None
        self.lock = Lock()

    def discover(self) -> dict:
        with self.lock:
            return discovery_result([{
                "id": "demo-hp8710", "name": "HP OfficeJet Pro 8710 (USB)",
                "models": [{"id": "demo-hpcups", "name": "HP OfficeJet Pro 8710 (hpcups)"}],
                "suggested_queue": "hp8710", "registered_queue": self.registered_queue,
            }])

    def register(self, device_id: str, model_id: str, queue: str) -> dict:
        queue = validate_queue(queue)
        if device_id != "demo-hp8710":
            raise CupsSetupError("device")
        if model_id != "demo-hpcups":
            raise CupsSetupError("model")
        with self.lock:
            if any(existing.casefold() == queue.casefold() for existing in self.queue_provider()):
                raise CupsSetupError("duplicate_queue")
            if self.registered_queue:
                raise CupsSetupError("registered")
            self.add_queue(queue)
            self.registered_queue = queue
        return {"queue": queue}
