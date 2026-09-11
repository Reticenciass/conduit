"""Parser for `ip route` output."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_network


@dataclass(frozen=True, slots=True)
class RouteRecord:
    """A route observed in a routing table."""

    destination: str
    via: str | None
    dev: str | None
    metric: int | None
    source: str | None


def parse_ip_route(text: str) -> list[RouteRecord]:
    """Parse common Linux `ip route` lines."""

    records: list[RouteRecord] = []
    for line in text.splitlines():
        tokens = line.split()
        if not tokens:
            continue
        destination_token = "0.0.0.0/0" if tokens[0] == "default" else tokens[0]
        try:
            destination = str(ip_network(destination_token, strict=False))
        except ValueError:
            continue
        values: dict[str, str] = {}
        index = 1
        while index + 1 < len(tokens):
            if tokens[index] in {"via", "dev", "src", "metric"}:
                values[tokens[index]] = tokens[index + 1]
                index += 2
            else:
                index += 1
        records.append(
            RouteRecord(
                destination=destination,
                via=values.get("via"),
                dev=values.get("dev"),
                metric=int(values["metric"]) if values.get("metric", "").isdigit() else None,
                source=values.get("src"),
            )
        )
    return records
