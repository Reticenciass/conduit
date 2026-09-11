"""Parser for `/etc/resolv.conf` output."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResolverConfig:
    """Nameservers and search domains observed from resolv.conf."""

    nameservers: tuple[str, ...]
    search_domains: tuple[str, ...]


def parse_resolv_conf(text: str) -> ResolverConfig:
    """Parse nameserver, search and domain directives."""

    nameservers: list[str] = []
    search_domains: list[str] = []
    for line in text.splitlines():
        tokens = line.split("#", 1)[0].split()
        if not tokens:
            continue
        if tokens[0] == "nameserver" and len(tokens) > 1:
            nameservers.append(tokens[1])
        elif tokens[0] in {"search", "domain"}:
            search_domains.extend(tokens[1:])
    return ResolverConfig(tuple(nameservers), tuple(dict.fromkeys(search_domains)))
