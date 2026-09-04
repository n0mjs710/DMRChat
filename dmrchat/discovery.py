"""
Locate the MOTOTRBO link among the host's network adapters.

The radio presents itself over USB as an ordinary network interface holding an
address on 192.168.10.0/24. We scan for that, and everything else the app needs
-- interface name, host address, gateway -- falls out of the match.

psutil is the primary scanner. A stdlib fallback (ifconfig / ip) covers the case
where the venv was skipped, so the app still starts on a bare interpreter.
"""

import ipaddress
import re
import subprocess

RADIO_SUBNET = ipaddress.ip_network("192.168.10.0/24")
GATEWAY_HOST = 1          # the radio itself is always .1 on this subnet
LINK_NOT_FOUND_MESSAGE = "MOTOTRBO link not found. Please connect radio via USB."


class RadioLink:
    """A discovered MOTOTRBO interface and the addressing derived from it."""

    def __init__(self, interface, address, netmask, method):
        self.interface = interface
        self.address = address
        self.netmask = netmask
        self.method = method            # how we found it, for the status line

    @property
    def gateway(self):
        """The radio gateway, calculated from the subnet rather than hardcoded."""
        return str(RADIO_SUBNET.network_address + GATEWAY_HOST)

    @property
    def network(self):
        return str(RADIO_SUBNET)

    def __str__(self):
        return f"{self.interface} ({self.address}) -> gateway {self.gateway}"


def _in_radio_subnet(address):
    try:
        return ipaddress.ip_address(address) in RADIO_SUBNET
    except ValueError:
        return False


def _scan_psutil():
    """Preferred path: psutil.net_if_addrs() gives us names and addresses directly."""
    try:
        import psutil
        import socket
    except ImportError:
        return None

    for name, addresses in psutil.net_if_addrs().items():
        for entry in addresses:
            if entry.family != socket.AF_INET:
                continue
            if _in_radio_subnet(entry.address):
                return RadioLink(name, entry.address, entry.netmask, "psutil")
    return None


def _scan_ifconfig():
    """Fallback for macOS/BSD: parse ``ifconfig`` output."""
    try:
        output = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    interface = None
    for line in output.splitlines():
        header = re.match(r"^([A-Za-z0-9._-]+):\s", line)
        if header:
            interface = header.group(1)
            continue
        match = re.search(r"\binet (\d+\.\d+\.\d+\.\d+).*?\bnetmask (\S+)", line)
        if match and interface and _in_radio_subnet(match.group(1)):
            return RadioLink(interface, match.group(1), match.group(2), "ifconfig")
    return None


def _scan_ip_command():
    """Fallback for Linux: parse ``ip -o -4 addr``."""
    try:
        output = subprocess.run(
            ["ip", "-o", "-4", "addr"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    for line in output.splitlines():
        match = re.search(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
        if match and _in_radio_subnet(match.group(2)):
            prefix = ipaddress.ip_network(f"0.0.0.0/{match.group(3)}")
            return RadioLink(match.group(1), match.group(2), str(prefix.netmask), "ip")
    return None


def find_radio_link():
    """
    Return a :class:`RadioLink` for the first interface on 192.168.10.0/24,
    or ``None`` when the radio is not connected.
    """
    for scanner in (_scan_psutil, _scan_ifconfig, _scan_ip_command):
        link = scanner()
        if link is not None:
            return link
    return None
