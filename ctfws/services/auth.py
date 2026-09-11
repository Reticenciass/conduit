"""Small in-process authentication boundary for the single-motor deployment."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated actor and role used for audit and authorization."""

    subject: str
    role: str


@dataclass(frozen=True, slots=True)
class OIDCConfig:
    """Operator-provided OIDC validation settings.

    The motor validates bearer assertions locally.  It never stores an OIDC
    token, client secret or refresh token in the workspace.
    """

    issuer: str
    audience: str
    jwks_url: str | None = None
    groups_claim: str = "groups"
    role_claim: str | None = None
    admin_group: str | None = None
    operator_group: str | None = None
    observer_group: str | None = None

    @classmethod
    def from_env(cls) -> OIDCConfig | None:
        issuer = os.getenv("CTFWS_OIDC_ISSUER", "").strip().rstrip("/")
        audience = os.getenv("CTFWS_OIDC_AUDIENCE", "").strip()
        if not issuer or not audience:
            return None
        return cls(
            issuer=issuer,
            audience=audience,
            jwks_url=os.getenv("CTFWS_OIDC_JWKS_URL") or None,
            groups_claim=os.getenv("CTFWS_OIDC_GROUPS_CLAIM", "groups") or "groups",
            role_claim=os.getenv("CTFWS_OIDC_ROLE_CLAIM") or None,
            admin_group=os.getenv("CTFWS_OIDC_ADMIN_GROUP") or None,
            operator_group=os.getenv("CTFWS_OIDC_OPERATOR_GROUP") or None,
            observer_group=os.getenv("CTFWS_OIDC_OBSERVER_GROUP") or None,
        )


class OIDCVerifier:
    """Verify signed OIDC JWTs using a cached operator-configured JWKS."""

    _ALGORITHMS: dict[str, Any] = {}

    def __init__(self, config: OIDCConfig | None) -> None:
        self.config = config
        self._keys: dict[str, dict[str, Any]] = {}
        self._keys_expire_at = 0.0
        self._jwks_url: str | None = config.jwks_url if config else None

    @property
    def enabled(self) -> bool:
        return self.config is not None

    def verify(self, token: str) -> Principal:
        config = self.config
        if config is None:
            raise PermissionError("OIDC não está configurado neste motor.")
        header_segment, payload_segment, signature_segment = self._segments(token)
        header = self._json_segment(header_segment, "cabeçalho OIDC")
        claims = self._json_segment(payload_segment, "claims OIDC")
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise PermissionError("Token OIDC inválido.")
        algorithm = str(header.get("alg", ""))
        if algorithm not in {"RS256", "RS384", "RS512"}:
            raise PermissionError("Algoritmo OIDC não permitido.")
        kid = str(header.get("kid", ""))
        if not kid:
            raise PermissionError("Token OIDC sem identificador de chave.")
        key = self._key(kid)
        self._verify_signature(
            algorithm,
            key,
            f"{header_segment}.{payload_segment}".encode("ascii"),
            self._decode(signature_segment, "assinatura OIDC"),
        )
        self._validate_claims(claims, config)
        subject = str(claims["sub"])
        return Principal(subject=f"oidc:{subject}", role=self._role(claims, config))

    def metadata(self) -> dict[str, object]:
        config = self.config
        return {
            "enabled": config is not None,
            "issuer": config.issuer if config else None,
            "audience": config.audience if config else None,
            "role_mapping": bool(config and (config.role_claim or config.admin_group)),
        }

    def _key(self, kid: str) -> dict[str, Any]:
        self._load_keys()
        key = self._keys.get(kid)
        if key is None:
            self._load_keys(force=True)
            key = self._keys.get(kid)
        if key is None:
            raise PermissionError("A chave do token OIDC não está disponível.")
        return key

    def _load_keys(self, *, force: bool = False) -> None:
        if not force and self._keys and time.time() < self._keys_expire_at:
            return
        config = self.config
        if config is None:
            raise PermissionError("OIDC não está configurado neste motor.")
        jwks_url = self._jwks_url or self._discover_jwks_url(config.issuer)
        payload = self._fetch_json(jwks_url, "JWKS OIDC")
        raw_keys = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(raw_keys, list):
            raise PermissionError("JWKS OIDC sem lista de chaves.")
        self._keys = {
            str(item["kid"]): dict(item)
            for item in raw_keys
            if isinstance(item, dict) and item.get("kid") and item.get("kty") == "RSA"
        }
        self._keys_expire_at = time.time() + 300

    def _discover_jwks_url(self, issuer: str) -> str:
        payload = self._fetch_json(
            f"{issuer}/.well-known/openid-configuration", "configuração OIDC"
        )
        value = payload.get("jwks_uri") if isinstance(payload, dict) else None
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            raise PermissionError("A configuração OIDC não informou jwks_uri válido.")
        self._jwks_url = value
        return value

    @staticmethod
    def _fetch_json(url: str, label: str) -> Any:
        if not url.startswith(("http://", "https://")):
            raise PermissionError(f"URL de {label} não permitida.")
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "ctfws/0.28"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read(2_000_000)
        except (OSError, urllib.error.URLError) as error:
            raise PermissionError(f"Não foi possível consultar {label} OIDC.") from error
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PermissionError(f"Resposta de {label} OIDC inválida.") from error

    @staticmethod
    def _segments(token: str) -> tuple[str, str, str]:
        segments = token.split(".")
        if len(segments) != 3 or not all(segments):
            raise PermissionError("Token OIDC malformado.")
        return segments[0], segments[1], segments[2]

    @classmethod
    def _json_segment(cls, segment: str, label: str) -> Any:
        try:
            return json.loads(cls._decode(segment, label).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PermissionError(f"{label.capitalize()} inválido.") from error

    @staticmethod
    def _decode(value: str, label: str) -> bytes:
        try:
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (ValueError, TypeError) as error:
            raise PermissionError(f"{label.capitalize()} inválida.") from error

    @classmethod
    def _verify_signature(
        cls, algorithm: str, jwk: dict[str, Any], message: bytes, signature: bytes
    ) -> None:
        if jwk.get("kty") != "RSA":
            raise PermissionError("A chave OIDC não é RSA.")
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding, rsa

            modulus = int.from_bytes(cls._decode(str(jwk["n"]), "modulus OIDC"), "big")
            exponent = int.from_bytes(cls._decode(str(jwk["e"]), "exponent OIDC"), "big")
            public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
            hash_algorithm = {
                "RS256": hashes.SHA256(),
                "RS384": hashes.SHA384(),
                "RS512": hashes.SHA512(),
            }[algorithm]
            public_key.verify(signature, message, padding.PKCS1v15(), hash_algorithm)
        except (KeyError, ValueError, TypeError, OSError) as error:
            raise PermissionError("Assinatura OIDC inválida.") from error
        except Exception as error:
            raise PermissionError("Assinatura OIDC inválida.") from error

    @staticmethod
    def _validate_claims(claims: dict[str, Any], config: OIDCConfig) -> None:
        if claims.get("iss") != config.issuer:
            raise PermissionError("Issuer OIDC não corresponde à configuração.")
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if config.audience not in audiences:
            raise PermissionError("Audience OIDC não corresponde à configuração.")
        now = time.time()
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or exp < now - 30:
            raise PermissionError("Token OIDC expirado ou sem expiração.")
        nbf = claims.get("nbf")
        if isinstance(nbf, (int, float)) and nbf > now + 30:
            raise PermissionError("Token OIDC ainda não é válido.")
        if not str(claims.get("sub", "")):
            raise PermissionError("Token OIDC sem subject.")

    @staticmethod
    def _claim_values(claims: dict[str, Any], name: str) -> set[str]:
        value = claims.get(name)
        if isinstance(value, list):
            return {str(item) for item in value}
        if value is None:
            return set()
        return {str(value)}

    @classmethod
    def _role(cls, claims: dict[str, Any], config: OIDCConfig) -> str:
        if config.role_claim:
            values = cls._claim_values(claims, config.role_claim)
            for role in ("admin", "operator", "observer"):
                if role in values:
                    return role
        groups = cls._claim_values(claims, config.groups_claim)
        if config.admin_group and config.admin_group in groups:
            return "admin"
        if config.operator_group and config.operator_group in groups:
            return "operator"
        if config.observer_group and config.observer_group in groups:
            return "observer"
        return "observer"


class AuthManager:
    """Use a bootstrap code for individual mode and bearer sessions for APIs."""

    def __init__(self, accounts_path: Path | None = None) -> None:
        self.oidc = OIDCVerifier(OIDCConfig.from_env())
        self.required = os.getenv("CTFWS_AUTH_REQUIRED", "0") == "1" or self.oidc.enabled
        self._bootstrap = os.getenv("CTFWS_BOOTSTRAP_TOKEN") or secrets.token_urlsafe(24)
        self._sessions: dict[str, Principal] = {}
        self._revoked_tokens: dict[str, None] = {}
        configured_accounts_path = os.getenv("CTFWS_ACCOUNTS_PATH")
        self._accounts_path = accounts_path or (
            Path(configured_accounts_path)
            if configured_accounts_path
            else Path.home() / ".config" / "ctfws" / "accounts.json"
        )

    def login(self, bootstrap_token: str) -> str:
        if not hmac.compare_digest(bootstrap_token, self._bootstrap):
            raise PermissionError("Código de bootstrap inválido.")
        token = secrets.token_urlsafe(32)
        self._sessions[token] = Principal(subject="bootstrap-admin", role="admin")
        return token

    def login_account(self, username: str, password: str) -> str:
        record = self._accounts().get(username)
        if record is None or not self._verify(password, record):
            raise PermissionError("Usuário ou senha inválidos.")
        token = secrets.token_urlsafe(32)
        self._sessions[token] = Principal(subject=username, role=str(record["role"]))
        return token

    def login_oidc(self, token: str) -> str:
        """Exchange a verified OIDC assertion for a revocable local session."""

        if token in self._revoked_tokens:
            raise PermissionError("Token OIDC revogado.")
        principal = self.oidc.verify(token)
        session = secrets.token_urlsafe(32)
        self._sessions[session] = principal
        return session

    def create_account(self, username: str, password: str, role: str) -> None:
        username = username.strip()
        if role not in {"admin", "operator", "observer"}:
            raise ValueError("Papel precisa ser admin, operator ou observer.")
        if not username or len(username) > 120 or len(password) < 12:
            raise ValueError(
                "Usuário é obrigatório e a senha precisa ter pelo menos 12 caracteres."
            )
        records = self._accounts()
        if username in records:
            raise ValueError("Já existe uma conta com esse usuário.")
        salt = secrets.token_bytes(16)
        iterations = 600_000
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        records[username] = {
            "role": role,
            "salt": salt.hex(),
            "digest": digest.hex(),
            "iterations": iterations,
        }
        self._accounts_path.parent.mkdir(parents=True, exist_ok=True)
        self._accounts_path.write_text(
            json.dumps(records, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
        )
        try:
            self._accounts_path.chmod(0o600)
        except OSError:
            pass

    def accounts(self) -> list[dict[str, str]]:
        return [
            {"username": username, "role": str(record["role"])}
            for username, record in sorted(self._accounts().items())
        ]

    def authenticate(self, token: str | None) -> Principal | None:
        if not token:
            return None
        if token in self._revoked_tokens:
            return None
        principal = self._sessions.get(token)
        if principal is not None:
            return principal
        if not self.oidc.enabled:
            return None
        try:
            return self.oidc.verify(token)
        except PermissionError:
            return None

    def oidc_metadata(self) -> dict[str, object]:
        return self.oidc.metadata()

    def revoke(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)
            self._revoked_tokens[token] = None
            if len(self._revoked_tokens) > 10_000:
                oldest = next(iter(self._revoked_tokens))
                self._revoked_tokens.pop(oldest, None)

    def bootstrap_configured(self) -> bool:
        return os.getenv("CTFWS_BOOTSTRAP_TOKEN") is not None

    def _accounts(self) -> dict[str, dict[str, object]]:
        if not self._accounts_path.is_file():
            return {}
        payload = json.loads(self._accounts_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("O arquivo de contas é inválido.")
        return {str(key): dict(value) for key, value in payload.items()}

    @staticmethod
    def _verify(password: str, record: dict[str, object]) -> bool:
        salt = bytes.fromhex(str(record["salt"]))
        iterations = int(str(record["iterations"]))
        expected = bytes.fromhex(str(record["digest"]))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        return hmac.compare_digest(actual, expected)

    @staticmethod
    def hash_identifier(value: str) -> str:
        """Create a non-secret correlation identifier for diagnostics."""

        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
