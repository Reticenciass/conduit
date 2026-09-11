"""Parser for normalized system-facts output."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SystemFacts:
    """Basic host facts in the import format emitted by a future agent."""

    user: str | None
    uid: int | None
    groups: tuple[str, ...]
    hostname: str | None
    fqdn: str | None
    kernel: str | None
    architecture: str | None
    distribution: str | None


def parse_system_facts(text: str) -> SystemFacts:
    """Parse `KEY=value` facts without interpreting arbitrary command output."""

    values: dict[str, str] = {}
    for line in text.splitlines():
        content = line.strip()
        if not content or content.startswith("#") or "=" not in content:
            continue
        key, value = content.split("=", 1)
        values[key.strip().upper()] = value.strip().strip('"')
    uid_text = values.get("UID", "")
    uid = int(uid_text) if uid_text.isdigit() else None
    groups = tuple(group for group in values.get("GROUPS", "").split() if group)
    return SystemFacts(
        user=values.get("USER"),
        uid=uid,
        groups=groups,
        hostname=values.get("HOSTNAME"),
        fqdn=values.get("FQDN"),
        kernel=values.get("KERNEL"),
        architecture=values.get("ARCHITECTURE") or values.get("ARCH"),
        distribution=values.get("DISTRIBUTION") or values.get("PRETTY_NAME"),
    )
