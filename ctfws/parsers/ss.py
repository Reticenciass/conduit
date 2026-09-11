"""Parser for `ss -tunap` output."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SocketRecord:
    """A listening service or existing socket connection."""

    protocol: str
    state: str
    local_address: str
    local_port: int | None
    peer_address: str | None
    peer_port: int | None
    process: str | None

    @property
    def is_listener(self) -> bool:
        """Whether this socket represents a local service."""

        return self.state.upper() in {"LISTEN", "UNCONN"}


_PROCESS_RE = re.compile(r'users:\(\("(?P<name>[^"]+)')


def _endpoint(value: str) -> tuple[str, int | None]:
    """Split IPv4, IPv6 and wildcard endpoint forms."""

    value = value.strip()
    if value in {"*", "*-*", "0.0.0.0:*", "[::]:*"}:
        return "0.0.0.0", None
    if value.startswith("[") and "]" in value:
        address, _, port = value[1:].partition("]:")
    else:
        address, separator, port = value.rpartition(":")
        if not separator:
            return value, None
    if address in {"*", ""}:
        address = "0.0.0.0"
    try:
        return address, None if port == "*" else int(port)
    except ValueError:
        return address, None


def parse_ss(text: str) -> list[SocketRecord]:
    """Parse socket rows while tolerating optional process columns."""

    records: list[SocketRecord] = []
    for line in text.splitlines():
        tokens = line.split(maxsplit=6)
        if len(tokens) < 6 or tokens[0].lower() in {"netid", "state"}:
            continue
        protocol, state = tokens[0], tokens[1]
        local_address, local_port = _endpoint(tokens[4])
        parsed_peer_address, parsed_peer_port = _endpoint(tokens[5])
        peer_address: str | None = parsed_peer_address
        peer_port: int | None = parsed_peer_port
        if tokens[5] in {"*", "0.0.0.0:*", "[::]:*"}:
            peer_address, peer_port = None, None
        process_match = _PROCESS_RE.search(tokens[6]) if len(tokens) > 6 else None
        records.append(
            SocketRecord(
                protocol=protocol,
                state=state,
                local_address=local_address,
                local_port=local_port,
                peer_address=peer_address,
                peer_port=peer_port,
                process=process_match.group("name") if process_match else None,
            )
        )
    return records
