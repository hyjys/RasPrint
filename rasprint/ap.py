import json
from pathlib import Path
import re
import socket
from threading import Lock


AP_ADDRESS = "10.42.0.1"
AP_SOCKET = "/run/rasprint-ap.sock"
HOSTNAME_FILE = Path("/etc/systemd/system/rasprint.socket.d/hostname")
DEFAULT_HOSTNAME = "rasprint"
DEFAULT_NETWORK = {"ssid": "RasPrint", "secured": True, "port": 8000, "hostname": DEFAULT_HOSTNAME}
HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
ERRORS = {
    "ssid": (422, "AP 이름은 제어 문자 없이 UTF-8 기준 1~32바이트여야 합니다.",
             "The network name must contain 1–32 UTF-8 bytes and no control characters."),
    "secured": (422, "올바른 Wi-Fi 보안 설정을 선택하세요.", "Enter a valid Wi-Fi security setting."),
    "port": (422, "HTTP 포트는 1~65535 사이여야 합니다.", "The HTTP port must be between 1 and 65535."),
    "proxy_port": (422, "80번 포트는 .local 주소에 사용합니다.", "Port 80 is reserved for the .local address."),
    "hostname": (
        422,
        ".local 앞 이름은 영문, 숫자, 하이픈으로 1~63자여야 하며 하이픈으로 시작하거나 끝날 수 없습니다.",
        "Use 1–63 ASCII letters, digits, or hyphens, with no hyphen at either end.",
    ),
    "password": (422, "Wi-Fi 비밀번호는 출력 가능한 ASCII 문자 8~63자여야 합니다.",
                 "The Wi-Fi password must contain 8–63 printable ASCII characters."),
    "missing_password": (422, "WPA2를 켜거나 보호된 AP 이름을 바꾸려면 Wi-Fi 비밀번호를 입력하세요.",
                         "Enter the Wi-Fi password to enable WPA2 or rename a secured network."),
    "unavailable": (503, "AP 설정 서비스에 연결할 수 없습니다. 시스템 서비스를 먼저 설치하세요.",
                    "The access point helper is unavailable. Install the system service first."),
    "unconfigured": (503, "Pi 콘솔에서 먼저 RasPrint AP를 설정하세요.",
                     "Set up the RasPrint access point from the Pi console first."),
    "occupied": (409, "해당 HTTP 포트는 이미 사용 중입니다.", "The requested HTTP port is already in use."),
    "busy": (409, "AP 설정을 변경하는 중입니다.", "An access point change is already in progress."),
    "rollback": (503, "이전 AP 설정을 복구하지 못했습니다. 서버 로그를 확인하세요.",
                 "The previous access point configuration could not be restored. Check the server logs."),
    "failed": (503, "AP 변경에 실패했습니다. 이전 설정으로 복구했습니다.",
               "The access point change failed. The previous settings were restored."),
    "invalid_response": (503, "AP 설정 서비스의 응답이 올바르지 않습니다.",
                         "The access point helper returned an invalid response."),
    "read": (503, "AP 설정을 읽을 수 없습니다.", "The access point configuration could not be read."),
}


class AccessPointError(Exception):
    def __init__(self, code: str):
        self.code = code
        self.status_code, korean, self.english = ERRORS[code]
        super().__init__(korean)


def normalize_hostname(value: str) -> str:
    if not isinstance(value, str) or not HOST_LABEL.fullmatch(value):
        raise AccessPointError("hostname")
    return value.lower()


def configured_hostname() -> str:
    try:
        return normalize_hostname(HOSTNAME_FILE.read_text(encoding="ascii").strip())
    except (AccessPointError, OSError, UnicodeError):
        return DEFAULT_HOSTNAME


def network_values(
    ssid: str, secured: bool, password: str | None, port: int, hostname: str | None = None,
) -> dict:
    if not isinstance(ssid, str) or not ssid.isprintable():
        raise AccessPointError("ssid")
    if not ssid.strip() or len(ssid.encode("utf-8")) > 32:
        raise AccessPointError("ssid")
    if type(secured) is not bool:
        raise AccessPointError("secured")
    if type(port) is not int or not 1 <= port <= 65535:
        raise AccessPointError("port")
    if port == 80:
        raise AccessPointError("proxy_port")
    if password is not None and (not isinstance(password, str) or (password and not re.fullmatch(r"[ -~]{8,63}", password))):
        raise AccessPointError("password")
    values = {"ssid": ssid, "secured": secured, "port": port}
    if hostname is not None:
        values["hostname"] = normalize_hostname(hostname)
    return values


def local_url(network: dict) -> str:
    return f"http://{network['hostname']}.local/"


def apply_result(network: dict) -> dict:
    port = network["port"]
    return {**network, "url": f"http://{AP_ADDRESS}:{port}/", "local_url": local_url(network)}


def status_result(network: dict, *, available: bool = True, pending: bool = False, error: str | None = None) -> dict:
    failure = AccessPointError(error) if error else None
    return {
        **apply_result(network), "available": available, "pending": pending,
        "error": str(failure) if failure else None,
        "error_english": failure.english if failure else None,
    }


def send_packet(stream: socket.socket, payload: dict):
    stream.sendall(json.dumps(payload, ensure_ascii=True).encode("ascii") + b"\n")


def receive_packet(stream: socket.socket) -> dict:
    buffer = bytearray()
    while len(buffer) < 2048:
        chunk = stream.recv(1024)
        if not chunk:
            break
        buffer.extend(chunk)
        if buffer.endswith(b"\n") and len(buffer) <= 2048:
            payload = json.loads(buffer)
            if isinstance(payload, dict):
                return payload
            break
    raise ValueError("Invalid AP helper message")


class AccessPoint:
    def _request(self, payload: dict) -> dict:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(8)
                stream.connect(AP_SOCKET)
                send_packet(stream, payload)
                response = receive_packet(stream)
        except (OSError, AttributeError) as exc:
            raise AccessPointError("unavailable") from exc
        except (ValueError, RecursionError) as exc:
            raise AccessPointError("invalid_response") from exc
        error = response.get("error")
        result = response.get("result")
        if isinstance(error, str) and error in ERRORS:
            raise AccessPointError(error)
        if not isinstance(result, dict):
            raise AccessPointError("invalid_response")
        return result

    def status(self) -> dict:
        try:
            return self._request({"action": "status"})
        except AccessPointError as exc:
            return status_result(
                {**DEFAULT_NETWORK, "hostname": configured_hostname()}, available=False, error=exc.code,
            )

    def apply(self, ssid: str, secured: bool, password: str | None, port: int, hostname: str | None = None) -> dict:
        values = network_values(ssid, secured, password, port, hostname)
        return self._request({"action": "apply", **values, "password": password})


class DemoAccessPoint:
    def __init__(self):
        self.network = DEFAULT_NETWORK.copy()
        self.lock = Lock()

    def status(self) -> dict:
        with self.lock:
            return status_result(self.network)

    def apply(self, ssid: str, secured: bool, password: str | None, port: int, hostname: str | None = None) -> dict:
        values = network_values(ssid, secured, password, port, hostname)
        with self.lock:
            if secured and not password and (not self.network["secured"] or ssid != self.network["ssid"]):
                raise AccessPointError("missing_password")
            self.network.update(values)
            return apply_result(self.network)
