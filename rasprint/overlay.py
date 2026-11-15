import socket
from threading import Lock

from .ap import receive_packet, send_packet


SOCKET_PATH = "/run/rasprint-overlay.sock"
ERRORS = {
    "unavailable": (
        503, "OverlayFS 설정 서비스에 연결할 수 없습니다.", "The OverlayFS setup service is unavailable.",
    ),
    "unsupported": (
        503, "Raspberry Pi OS의 OverlayFS 부팅 설정을 찾을 수 없습니다.",
        "Raspberry Pi OS OverlayFS boot settings are unavailable.",
    ),
    "prerequisite": (
        503, "OverlayFS를 켜려면 Raspberry Pi OS에 overlayroot 패키지를 설치해야 합니다.",
        "Install the overlayroot package on Raspberry Pi OS to enable OverlayFS.",
    ),
    "memory": (
        503, "Raspberry Pi OS가 이 기기에서 OverlayFS를 켜기에 메모리가 부족하다고 보고했습니다.",
        "Raspberry Pi OS reports insufficient memory to enable OverlayFS on this device.",
    ),
    "read": (
        503, "OverlayFS 상태를 확인할 수 없습니다.", "The OverlayFS state could not be read.",
    ),
    "change": (
        500, "OverlayFS 부팅 설정을 변경할 수 없습니다.", "The OverlayFS boot setting could not be changed.",
    ),
    "invalid": (
        400, "OverlayFS 설정 요청이 올바르지 않습니다.", "Invalid OverlayFS setup request.",
    ),
    "invalid_response": (
        503, "OverlayFS 설정 서비스가 잘못된 응답을 반환했습니다.",
        "The OverlayFS setup service returned an invalid response.",
    ),
    "no_reboot": (
        409, "재부팅이 필요한 OverlayFS 변경이 없습니다.", "No pending OverlayFS change requires a reboot.",
    ),
    "reboot_pending": (
        409, "OverlayFS 변경을 적용할 재부팅이 이미 예약되었습니다.", "The OverlayFS reboot is already scheduled.",
    ),
    "reboot_failed": (
        500, "OverlayFS 변경을 적용할 재부팅을 예약할 수 없습니다.", "The OverlayFS reboot could not be scheduled.",
    ),
}


class OverlaySetupError(Exception):
    def __init__(self, code: str):
        self.code = code
        self.status_code, korean, self.english = ERRORS[code]
        super().__init__(korean)


def overlay_status(
    *, available: bool, enabled: bool | None = None, configured: bool | None = None,
    can_enable: bool = False, error: str | None = None,
) -> dict:
    failure = OverlaySetupError(error) if error else None
    return {
        "available": available,
        "enabled": enabled,
        "configured": configured,
        "reboot_required": enabled is not None and configured is not None and enabled != configured,
        "can_enable": can_enable,
        "error": str(failure) if failure else None,
        "error_english": failure.english if failure else None,
    }


class OverlaySetup:
    def _request(self, request: dict) -> dict:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(55)
                stream.connect(SOCKET_PATH)
                send_packet(stream, request)
                response = receive_packet(stream)
        except OSError as exc:
            raise OverlaySetupError("unavailable") from exc
        except (ValueError, RecursionError) as exc:
            raise OverlaySetupError("invalid_response") from exc
        error = response.get("error")
        if isinstance(error, str) and error in ERRORS:
            raise OverlaySetupError(error)
        result = response.get("result")
        if not isinstance(result, dict):
            raise OverlaySetupError("invalid_response")
        return result

    def status(self) -> dict:
        try:
            return self._request({"action": "status"})
        except OverlaySetupError as exc:
            return overlay_status(available=False, error=exc.code)

    def set_enabled(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise OverlaySetupError("invalid")
        return self._request({"action": "set_enabled", "enabled": enabled})

    def reboot(self) -> dict:
        return self._request({"action": "reboot"})


class DemoOverlaySetup:
    def __init__(self):
        self.enabled = False
        self.configured = False
        self.lock = Lock()

    def _status(self) -> dict:
        return overlay_status(available=True, enabled=self.enabled, configured=self.configured, can_enable=True)

    def status(self) -> dict:
        with self.lock:
            return self._status()

    def set_enabled(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise OverlaySetupError("invalid")
        with self.lock:
            self.configured = enabled
            return self._status()

    def reboot(self) -> dict:
        with self.lock:
            if self.enabled == self.configured:
                raise OverlaySetupError("no_reboot")
            self.enabled = self.configured
            return {"scheduled": True, "demo": True}
