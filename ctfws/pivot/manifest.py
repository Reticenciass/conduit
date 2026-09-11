"""Compatibility manifest checks for experimental routed transports."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LigoloManifestStatus:
    """Sanitized result of checking the operator-supplied compatibility manifest."""

    path: str | None
    contract: str | None
    configured_version: str | None
    reported_version: str | None
    valid: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "contract": self.contract,
            "configured_version": self.configured_version,
            "reported_version": self.reported_version,
            "valid": self.valid,
            "reason": self.reason,
        }


EXPECTED_CONTRACT = "ctfws-routed-context-v1"


def ligolo_manifest_status(path: Path | None = None) -> LigoloManifestStatus:
    """Require a pinned version and matching runtime declaration.

    The default repository manifest intentionally uses ``operator-selected`` and
    therefore cannot enable the experimental adapter.  An administrator must
    provide a separate pinned manifest and ``CTFWS_LIGOLO_VERSION`` explicitly.
    """

    selected_path = path or _configured_path()
    reported_version = os.getenv("CTFWS_LIGOLO_VERSION") or None
    if selected_path is None or not selected_path.is_file():
        return LigoloManifestStatus(
            str(selected_path) if selected_path else None,
            None,
            None,
            reported_version,
            False,
            "manifesto Ligolo não encontrado",
        )
    try:
        with selected_path.open("rb") as handle:
            document = tomllib.load(handle)
        section = document.get("ligolo_ng", {})
        if not isinstance(section, dict):
            raise ValueError("seção ligolo_ng inválida")
        contract = str(section.get("contract", "")) or None
        configured_version = str(section.get("version", "")) or None
    except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
        return LigoloManifestStatus(
            str(selected_path),
            None,
            None,
            reported_version,
            False,
            f"manifesto inválido: {error}",
        )
    if contract != EXPECTED_CONTRACT:
        return LigoloManifestStatus(
            str(selected_path),
            contract,
            configured_version,
            reported_version,
            False,
            "contrato do adaptador não corresponde ao motor",
        )
    if not configured_version or configured_version == "operator-selected":
        reason = "o manifesto precisa fixar uma versão Ligolo-ng"
        valid = False
    elif reported_version != configured_version:
        reason = "a versão declarada do binário não corresponde ao manifesto"
        valid = False
    else:
        reason = "contrato e versão compatíveis"
        valid = True
    return LigoloManifestStatus(
        str(selected_path),
        contract,
        configured_version,
        reported_version,
        valid,
        reason,
    )


def _configured_path() -> Path | None:
    configured = os.getenv("CTFWS_LIGOLO_MANIFEST")
    if configured:
        return Path(configured).expanduser().resolve()
    candidate = Path(__file__).resolve().parents[2] / "deploy" / "compatibility.toml"
    return candidate if candidate.is_file() else None
