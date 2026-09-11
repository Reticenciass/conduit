"""Optional authenticated local secret vault.

The default connection workflow uses an SSH agent or a temporary secret. This
module is opt-in and keeps the master key outside individual workspaces.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from pathlib import Path
from typing import Any

_AESGCM: Any
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM
except ImportError:  # pragma: no cover - optional feature
    _AESGCM = None
AESGCM: Any = _AESGCM


class SecretVault:
    """Store named secrets with AES-GCM and a separately protected key file."""

    def __init__(self, root: Path | None = None) -> None:
        base = (root or Path.home() / ".config" / "ctfws").expanduser()
        self.root = base
        self.key_path = base / "vault.key"
        self.data_path = base / "vault.json"

    def put(self, name: str, value: str, *, owner: str = "local") -> str:
        if AESGCM is None:
            raise RuntimeError("O cofre exige a dependência opcional cryptography.")
        if not name or not value:
            raise ValueError("Nome e valor do segredo são obrigatórios.")
        key = self._key(create=True)
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(nonce, value.encode("utf-8"), name.encode())
        reference = f"vault:{name}"
        records = self._records()
        records[reference] = {
            "owner": owner,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        self._write_records(records)
        return reference

    def get(self, reference: str, *, owner: str | None = None) -> str:
        if AESGCM is None:
            raise RuntimeError("O cofre exige a dependência opcional cryptography.")
        record = self._records().get(reference)
        if record is None:
            raise KeyError(f"Segredo {reference} não encontrado.")
        if owner is not None and record.get("owner") != owner:
            raise PermissionError("O segredo pertence a outro proprietário.")
        nonce = base64.b64decode(str(record["nonce"]))
        ciphertext = base64.b64decode(str(record["ciphertext"]))
        name = reference.removeprefix("vault:")
        plaintext = AESGCM(self._key()).decrypt(nonce, ciphertext, name.encode())
        return str(plaintext.decode("utf-8"))

    def revoke(self, reference: str) -> None:
        records = self._records()
        if reference not in records:
            raise KeyError(f"Segredo {reference} não encontrado.")
        del records[reference]
        self._write_records(records)

    def list_refs(self, *, owner: str | None = None) -> list[str]:
        records = self._records()
        return sorted(
            reference
            for reference, record in records.items()
            if owner is None or record.get("owner") == owner
        )

    def _key(self, *, create: bool = False) -> bytes:
        if not self.key_path.is_file():
            if not create:
                raise KeyError("A chave mestra do cofre não foi configurada.")
            self.root.mkdir(parents=True, exist_ok=True)
            self.key_path.write_bytes(secrets.token_bytes(32))
            self._restrict(self.key_path)
        key = self.key_path.read_bytes()
        if len(key) != 32:
            raise ValueError("A chave mestra do cofre é inválida.")
        return key

    def _records(self) -> dict[str, dict[str, Any]]:
        if not self.data_path.is_file():
            return {}
        payload = json.loads(self.data_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("O arquivo do cofre é inválido.")
        return {str(key): dict(value) for key, value in payload.items()}

    def _write_records(self, records: dict[str, dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.data_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(records, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
        )
        self._restrict(temporary)
        os.replace(temporary, self.data_path)
        self._restrict(self.data_path)

    @staticmethod
    def _restrict(path: Path) -> None:
        try:
            path.chmod(0o600)
        except OSError:
            pass
