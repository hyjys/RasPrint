"""Socket-activated bridge to Raspberry Pi OS OverlayFS boot configuration."""
import logging
import os
import socket
import subprocess
from pathlib import Path

from rasprint.ap import receive_packet, send_packet
from rasprint.overlay import OverlaySetupError, overlay_status


RASPI_CONFIG = "/usr/bin/raspi-config"
DPKG_QUERY = "/usr/bin/dpkg-query"
SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMD_RUN = "/usr/bin/systemd-run"
MOUNT = "/usr/bin/mount"
MOUNTINFO = Path("/proc/self/mountinfo")
MEMINFO = Path("/proc/meminfo")
FIRMWARE = Path("/boot/firmware")
REBOOT_UNIT = "rasprint-overlay-reboot"

log = logging.getLogger(__name__)


def run(*arguments: str, timeout: int = 8) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments, capture_output=True, text=True, errors="replace", timeout=timeout,
    )


def mounts() -> dict[str, tuple[str, bool]]:
    result = {}
    for line in MOUNTINFO.read_text(encoding="utf-8").splitlines():
        before, _, after = line.partition(" - ")
        fields, filesystem = before.split(), after.split()
        if len(fields) >= 6 and len(filesystem) >= 3:
            result[fields[4]] = (filesystem[0], "ro" in fields[5].split(","))
    return result


def boot_mount(mount_table: dict[str, tuple[str, bool]]) -> tuple[str, bool]:
    # Match raspi-config's firmware layout and require the real boot partition.
    target = str(FIRMWARE) if (FIRMWARE / "config.txt").is_file() else "/boot"
    if not os.access(RASPI_CONFIG, os.X_OK) or target not in mount_table:
        raise OverlaySetupError("unsupported")
    return target, mount_table[target][1]


def configured() -> bool:
    result = run(RASPI_CONFIG, "nonint", "get_overlay_conf")
    output = result.stdout.strip()
    if result.returncode != 0 or output not in {"0", "1"}:
        log.error("raspi-config could not read OverlayFS boot settings: %s", result.stderr.strip())
        raise OverlaySetupError("read")
    return output == "0"


def enable_error() -> str | None:
    # Apply the upstream RAM minimum and never allow raspi-config to run apt.
    for line in MEMINFO.read_text(encoding="ascii").splitlines():
        if line.startswith("MemTotal:"):
            if int(line.split()[1]) <= 262144:
                return "memory"
            break
    result = run(DPKG_QUERY, "-W", "-f=${db:Status-Abbrev}", "overlayroot")
    return None if result.returncode == 0 and result.stdout.strip() == "ii" else "prerequisite"


def status() -> dict:
    enabled = None
    try:
        mount_table = mounts()
        enabled = mount_table["/"][0] == "overlay"
        boot_mount(mount_table)
        configured_state = configured()
        error = enable_error()
    except OverlaySetupError as exc:
        return overlay_status(available=False, enabled=enabled, error=exc.code)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        log.exception("Could not inspect OverlayFS state")
        return overlay_status(available=False, enabled=enabled, error="read")
    return overlay_status(
        available=True, enabled=enabled, configured=configured_state, can_enable=error is None,
        error=error if not configured_state else None,
    )


def reboot_scheduled() -> bool:
    return run(SYSTEMCTL, "is-active", "--quiet", f"{REBOOT_UNIT}.timer").returncode == 0


def change_setting(enabled: bool) -> dict:
    try:
        if reboot_scheduled():
            raise OverlaySetupError("reboot_pending")
        target, was_read_only = boot_mount(mounts())
        if configured() != enabled:
            if enabled and (error := enable_error()):
                raise OverlaySetupError(error)
            try:
                result = run(RASPI_CONFIG, "nonint", "do_overlayfs", "0" if enabled else "1", timeout=20)
            finally:
                if was_read_only and not mounts().get(target, ("", True))[1]:
                    if run(MOUNT, "-o", "remount,ro", target).returncode:
                        log.error("Could not restore the boot mount's read-only state")
                        raise OverlaySetupError("change")
            # Some versions return success even if they could not update cmdline.txt.
            if configured() != enabled:
                log.error("raspi-config did not update OverlayFS: %s %s", result.stdout.strip(), result.stderr.strip())
                raise OverlaySetupError("change")
        return status()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.exception("Could not change OverlayFS boot settings")
        raise OverlaySetupError("change") from exc


def reboot() -> dict:
    if not status()["reboot_required"]:
        raise OverlaySetupError("no_reboot")
    try:
        if not reboot_scheduled():
            result = run(
                SYSTEMD_RUN, f"--unit={REBOOT_UNIT}", "--on-active=3s", "--timer-property=AccuracySec=1s",
                "--collect", "--", SYSTEMCTL, "reboot",
            )
            if result.returncode:
                log.error("Could not schedule OverlayFS reboot: %s", result.stderr.strip())
                raise OverlaySetupError("reboot_failed")
    except (OSError, subprocess.SubprocessError) as exc:
        log.exception("Could not schedule OverlayFS reboot")
        raise OverlaySetupError("reboot_failed") from exc
    return {"scheduled": True}


def dispatch(request: dict) -> dict:
    action = request.get("action")
    if action == "status":
        return status()
    if action == "reboot":
        return reboot()
    if action == "set_enabled" and type(request.get("enabled")) is bool:
        return change_setting(request["enabled"])
    raise OverlaySetupError("invalid")


def serve_connection(stream: socket.socket) -> None:
    stream.settimeout(2)
    try:
        response = {"result": dispatch(receive_packet(stream))}
    except OverlaySetupError as exc:
        response = {"error": exc.code}
    except (ValueError, RecursionError):
        response = {"error": "invalid"}
    except OSError:
        return
    try:
        send_packet(stream, response)
    except OSError:
        pass


def main() -> None:
    with socket.socket(fileno=3) as listener:
        while True:
            connection, _ = listener.accept()
            with connection:
                serve_connection(connection)


if __name__ == "__main__":
    main()
