"""Parser for `ip neigh` output."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NeighborRecord:
    """A neighbor observed by the host."""

    ip: str
    dev: str | None
    mac: str | None
    state: str | None


def parse_ip_neigh(text: str) -> list[NeighborRecord]:
    """Parse neighbor lines, including entries without a MAC address."""

    records: list[NeighborRecord] = []
    for line in text.splitlines():
        tokens = line.split()
        if not tokens:
            continue
        values: dict[str, str] = {}
        for index, token in enumerate(tokens[:-1]):
            if token in {"dev", "lladdr"}:
                values[token] = tokens[index + 1]
        state = next(
            (
                token
                for token in reversed(tokens)
                if token.upper()
                in {
                    "REACHABLE",
                    "STALE",
                    "DELAY",
                    "PROBE",
                    "FAILED",
                    "NOARP",
                    "PERMANENT",
                    "INCOMPLETE",
                }
            ),
            None,
        )
        records.append(
            NeighborRecord(
                ip=tokens[0],
                dev=values.get("dev"),
                mac=values.get("lladdr"),
                state=state,
            )
        )
    return records
