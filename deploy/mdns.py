"""Publish the RasPrint host alias through Avahi on the AP interface only."""

import logging
from time import sleep

import dbus

from deploy.network import AP_ADDRESS, access_point_interface
from rasprint.ap import configured_hostname

BUS_NAME = "org.freedesktop.Avahi"
SERVER = f"{BUS_NAME}.Server"
ENTRY_GROUP = f"{BUS_NAME}.EntryGroup"
IPV4 = 0
NO_REVERSE = 16
COLLISION_OR_FAILURE = {3, 4}
log = logging.getLogger("rasprint-mdns")


def publish(bus: dbus.SystemBus, interface: int, hostname: str) -> None:
    server = dbus.Interface(bus.get_object(BUS_NAME, "/"), SERVER)
    group = dbus.Interface(bus.get_object(BUS_NAME, server.EntryGroupNew(timeout=5)), ENTRY_GROUP)
    alias = f"{hostname}.local"
    try:
        group.AddAddress(interface, IPV4, dbus.UInt32(NO_REVERSE), alias, AP_ADDRESS, timeout=5)
        group.Commit(timeout=5)
        while access_point_interface() == interface and configured_hostname() == hostname:
            if group.GetState(timeout=5) in COLLISION_OR_FAILURE:
                raise RuntimeError(f"Avahi could not advertise {alias}")
            sleep(3)
    finally:
        try:
            group.Free(timeout=5)
        except dbus.DBusException:
            pass


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    while True:
        interface = access_point_interface()
        if interface is not None:
            hostname = configured_hostname()
            try:
                publish(dbus.SystemBus(), interface, hostname)
                continue
            except (dbus.DBusException, RuntimeError) as exc:
                log.warning("%s", exc)
                for _ in range(4):
                    sleep(3)
                    if configured_hostname() != hostname:
                        break
                continue
        sleep(3)


if __name__ == "__main__":
    main()
