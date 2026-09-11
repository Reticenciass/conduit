"""Authentication boundary tests, including a signed OIDC assertion."""

from __future__ import annotations

import base64
import json
import time
from io import BytesIO
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ctfws.services.auth import AuthManager


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def test_oidc_verifies_signature_claims_and_role_mapping(tmp_path: Path, monkeypatch) -> None:
    issuer = "https://idp.example.test"
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "test-key",
        "n": _b64(public_numbers.n.to_bytes((public_numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64(public_numbers.e.to_bytes((public_numbers.e.bit_length() + 7) // 8, "big")),
    }
    header = {"alg": "RS256", "kid": "test-key", "typ": "JWT"}
    claims = {
        "iss": issuer,
        "aud": "ctfws",
        "sub": "alice",
        "groups": ["red-team-admins"],
        "exp": time.time() + 300,
    }
    encoded_header = _b64(json.dumps(header, separators=(",", ":")).encode())
    encoded_claims = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    token = f"{encoded_header}.{encoded_claims}.{_b64(signature)}"

    class Response(BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

    def urlopen(_request: object, timeout: int) -> Response:
        assert timeout == 5
        return Response(json.dumps({"keys": [jwk]}).encode())

    monkeypatch.setenv("CTFWS_OIDC_ISSUER", issuer)
    monkeypatch.setenv("CTFWS_OIDC_AUDIENCE", "ctfws")
    monkeypatch.setenv("CTFWS_OIDC_JWKS_URL", f"{issuer}/keys")
    monkeypatch.setenv("CTFWS_OIDC_ADMIN_GROUP", "red-team-admins")
    monkeypatch.setattr("ctfws.services.auth.urllib.request.urlopen", urlopen)

    auth = AuthManager(tmp_path / "accounts.json")
    assert auth.required
    assert auth.authenticate(token) is not None
    assert auth.authenticate(token).role == "admin"  # type: ignore[union-attr]
    session = auth.login_oidc(token)
    assert auth.authenticate(session).subject == "oidc:alice"  # type: ignore[union-attr]
    auth.revoke(token)
    assert auth.authenticate(token) is None
    with pytest.raises(PermissionError, match="revogado"):
        auth.login_oidc(token)


def test_local_accounts_are_persisted_without_overwriting_existing_users(tmp_path: Path) -> None:
    auth = AuthManager(tmp_path / "accounts.json")
    auth.create_account(" operator ", "operator-password", "operator")

    assert auth.accounts() == [{"username": "operator", "role": "operator"}]
    session = auth.login_account("operator", "operator-password")
    assert auth.authenticate(session).subject == "operator"  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="Já existe"):
        auth.create_account("operator", "another-password", "observer")
