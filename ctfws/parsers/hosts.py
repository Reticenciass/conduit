"""Parser for `/etc/hosts` output."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HostsEntry:
    """An address and the aliases found on one hosts-file line."""

    address: str
    names: tuple[str, ...]


def parse_hosts(text: str) -> list[HostsEntry]:
    """Parse non-comment hosts-file rows."""

    records: list[HostsEntry] = []
    for line in text.splitlines():
        content = line.split("#", 1)[0].strip()
        tokens = content.split()
        if len(tokens) >= 2:
            records.append(HostsEntry(address=tokens[0], names=tuple(tokens[1:])))
    return records
