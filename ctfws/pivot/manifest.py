"""Compatibility and integrity checks for routed transports."""

from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LigoloManifestStatus:
    """Sanitized result of checking one pinned Ligolo installation."""

    path: str | None
    contract: str | None
    configured_version: str | None
    reported_version: str | None
    valid: bool
    reason: str
    agent_path: str | None = None
    proxy_path: str | None = None
    agent_sha256: str | None = None
    proxy_sha256: str | None = None
    binary_verified: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "contract": self.contract,
            "configured_version": self.configured_version,
            "reported_version": self.reported_version,
            "valid": self.valid,
            "reason": self.reason,
            "agent_path": self.agent_path,
            "proxy_path": self.proxy_path,
            "agent_sha256": self.agent_sha256,
            "proxy_sha256": self.proxy_sha256,
            "binary_verified": self.binary_verified,
        }


EXPECTED_CONTRACT = "ctfws-routed-context-v1"


def ligolo_manifest_status(path: Path | None = None) -> LigoloManifestStatus:
    """Require a pinned version and, for managed installs, verified binaries.

    The runtime version is supplied by the installer/service environment, but
    it is never accepted as the only proof. When paths and SHA-256 values are
    present in the manifest, both executable files are read and verified on
    every capability check. This deliberately rejects distro binaries which
    report ``dev`` or have not been installed from the pinned release.
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

    agent_path: str | None = None
    proxy_path: str | None = None
    agent_sha256: str | None = None
    proxy_sha256: str | None = None
    try:
        with selected_path.open("rb") as handle:
            document = tomllib.load(handle)
        section = document.get("ligolo_ng", {})
        if not isinstance(section, dict):
            raise ValueError("seção ligolo_ng inválida")
        contract = str(section.get("contract", "")) or None
        configured_version = str(section.get("version", "")) or None
        agent_path = _configured_binary_path(section, "agent_path", "CTFWS_LIGOLO_AGENT")
        proxy_path = _configured_binary_path(section, "proxy_path", "CTFWS_LIGOLO_PROXY")
        agent_sha256 = _configured_hash(section, "agent_sha256", "CTFWS_LIGOLO_AGENT_SHA256")
        proxy_sha256 = _configured_hash(section, "proxy_sha256", "CTFWS_LIGOLO_PROXY_SHA256")
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
        return _status(
            selected_path,
            contract,
            configured_version,
            reported_version,
            False,
            "contrato do adaptador não corresponde ao motor",
            agent_path,
            proxy_path,
            agent_sha256,
            proxy_sha256,
        )
    if not configured_version or configured_version == "operator-selected":
        return _status(
            selected_path,
            contract,
            configured_version,
            reported_version,
            False,
            "o manifesto precisa fixar uma versão Ligolo-ng",
            agent_path,
            proxy_path,
            agent_sha256,
            proxy_sha256,
        )
    if reported_version != configured_version:
        return _status(
            selected_path,
            contract,
            configured_version,
            reported_version,
            False,
            "a versão declarada do binário não corresponde ao manifesto",
            agent_path,
            proxy_path,
            agent_sha256,
            proxy_sha256,
        )

    binary_reason = _verify_binaries(agent_path, proxy_path, agent_sha256, proxy_sha256)
    reason = (
        "contrato e versão compatíveis"
        if binary_reason is None and not any((agent_path, proxy_path, agent_sha256, proxy_sha256))
        else binary_reason or "contrato, versão e binários compatíveis"
    )
    return _status(
        selected_path,
        contract,
        configured_version,
        reported_version,
        binary_reason is None,
        reason,
        agent_path,
        proxy_path,
        agent_sha256,
        proxy_sha256,
        binary_verified=binary_reason is None and agent_path is not None,
    )


def _status(
    path: Path,
    contract: str | None,
    configured_version: str | None,
    reported_version: str | None,
    valid: bool,
    reason: str,
    agent_path: str | None,
    proxy_path: str | None,
    agent_sha256: str | None,
    proxy_sha256: str | None,
    *,
    binary_verified: bool = False,
) -> LigoloManifestStatus:
    return LigoloManifestStatus(
        str(path),
        contract,
        configured_version,
        reported_version,
        valid,
        reason,
        agent_path,
        proxy_path,
        agent_sha256,
        proxy_sha256,
        binary_verified,
    )


def _configured_binary_path(section: dict[str, object], key: str, env_name: str) -> str | None:
    configured = os.getenv(env_name) or section.get(key)
    if not isinstance(configured, str) or not configured.strip():
        return None
    return str(Path(configured).expanduser().resolve())


def _configured_hash(section: dict[str, object], key: str, env_name: str) -> str | None:
    configured = os.getenv(env_name) or section.get(key)
    if not isinstance(configured, str) or not configured.strip():
        return None
    return configured.strip().lower()


def _verify_binaries(
    agent_path: str | None,
    proxy_path: str | None,
    agent_sha256: str | None,
    proxy_sha256: str | None,
) -> str | None:
    if not any((agent_path, proxy_path, agent_sha256, proxy_sha256)):
        # Keep minimal third-party manifests compatible with the old contract;
        # the shipped Conduit manifest includes all four values.
        return None
    values = (("agent", agent_path, agent_sha256), ("proxy", proxy_path, proxy_sha256))
    for role, path, expected in values:
        if not path or not expected:
            return f"o manifesto não fixa o caminho e o checksum do {role} Ligolo"
        binary = Path(path)
        if not binary.is_file() or not os.access(binary, os.X_OK):
            return f"binário {role} Ligolo ausente ou não executável"
        if len(expected) != 64 or any(
            character not in "0123456789abcdef" for character in expected
        ):
            return f"checksum do {role} Ligolo inválido no manifesto"
        digest = hashlib.sha256()
        try:
            with binary.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as error:
            return f"não foi possível ler o binário {role} Ligolo: {error}"
        if digest.hexdigest() != expected:
            return f"checksum do {role} Ligolo não corresponde ao manifesto"
    return None


def _configured_path() -> Path | None:
    configured = os.getenv("CTFWS_LIGOLO_MANIFEST")
    if configured:
        return Path(configured).expanduser().resolve()
    candidate = Path(__file__).resolve().parents[2] / "deploy" / "compatibility.toml"
    return candidate if candidate.is_file() else None
