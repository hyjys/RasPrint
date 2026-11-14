"""Read the IPv4 address of the interface used by the deployed access point."""

import fcntl
import socket
import struct

AP_ADDRESS = "10.42.0.1"
AP_INTERFACE = "wlan0"
SIOCGIFADDR = 0x8915


def access_point_interface() -> int | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stream:
            request = struct.pack("256s", AP_INTERFACE.encode("ascii"))
            result = fcntl.ioctl(stream.fileno(), SIOCGIFADDR, request)
        if socket.inet_ntoa(result[20:24]) == AP_ADDRESS:
            return socket.if_nametoindex(AP_INTERFACE)
    except OSError:
        pass
    return None
