"""Parser for `ip addr` output."""

from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import ip_interface


@dataclass(frozen=True, slots=True)
class IPAddressRecord:
    """An address observed on an interface."""

    address: str
    prefix: int
    family: str
    network: str


@dataclass(frozen=True, slots=True)
class InterfaceRecord:
    """An interface and all addresses observed on it."""

    name: str
    state: str | None
    mac: str | None
    addresses: tuple[IPAddressRecord, ...]


_INTERFACE_RE = re.compile(
    r"^\d+:\s+(?P<name>[^:]+):\s+<(?P<flags>[^>]*)>(?:.*?\sstate\s(?P<state>\S+))?"
)
_ADDRESS_RE = re.compile(r"^\s+inet(?P<family>6)?\s+(?P<address>[^/\s]+)/(?:\s*)(?P<prefix>\d+)")
_MAC_RE = re.compile(r"^\s+link/\S+\s+(?P<mac>[0-9a-fA-F:]{11,17})")


def parse_ip_addr(text: str) -> list[InterfaceRecord]:
    """Parse interfaces while ignoring unrelated continuation lines."""

    records: list[InterfaceRecord] = []
    current_name: str | None = None
    current_state: str | None = None
    current_mac: str | None = None
    addresses: list[IPAddressRecord] = []

    def flush() -> None:
        nonlocal addresses, current_name, current_state, current_mac
        if current_name is not None:
            records.append(
                InterfaceRecord(
                    name=current_name,
                    state=current_state,
                    mac=current_mac,
                    addresses=tuple(addresses),
                )
            )
        addresses = []
        current_name = None
        current_state = None
        current_mac = None

    for line in text.splitlines():
        header = _INTERFACE_RE.match(line)
        if header:
            flush()
            current_name = header.group("name").split("@", 1)[0]
            current_state = header.group("state") or (
                "UP" if "UP" in header.group("flags").split(",") else None
            )
            continue
        if current_name is None:
            continue
        mac = _MAC_RE.match(line)
        if mac:
            current_mac = mac.group("mac").lower()
            continue
        address = _ADDRESS_RE.match(line)
        if address:
            family = "inet6" if address.group("family") else "inet"
            raw = f"{address.group('address')}/{address.group('prefix')}"
            interface = ip_interface(raw)
            addresses.append(
                IPAddressRecord(
                    address=str(interface.ip),
                    prefix=interface.network.prefixlen,
                    family=family,
                    network=str(interface.network),
                )
            )
    flush()
    return records
