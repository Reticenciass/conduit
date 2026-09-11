"""Local-first web dashboard and operational API."""

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from ctfws.core.errors import EntityNotFoundError, IdempotencyConflictError
from ctfws.core.limits import max_file_bytes, max_workspace_bytes
from ctfws.core.paths import WorkspacePaths
from ctfws.database.repositories import ObservationRepository
from ctfws.models.audit import AuditCreate
from ctfws.models.connection import ConnectionProfileCreate, ConnectionProfileUpdate
from ctfws.models.context import NetworkContextCreate
from ctfws.models.evidence import EvidenceCreate
from ctfws.models.forward import ForwardCreate
from ctfws.models.membership import MembershipCreate
from ctfws.models.snapshot import SnapshotCreate
from ctfws.models.task import TaskCreate
from ctfws.models.terminal import TerminalCreate
from ctfws.models.tool import ToolCreate
from ctfws.models.transfer import ConflictPolicy
from ctfws.pivot.adapters import transport_capabilities
from ctfws.pivot.tools import detect_pivot_tools
from ctfws.services.access_paths import AccessPathService
from ctfws.services.connections import ConnectionService
from ctfws.services.contexts import NetworkContextService
from ctfws.services.engine import EngineLock
from ctfws.services.inspection import RemoteInspectionService
from ctfws.services.maintenance import WorkspaceMaintenanceService, workspace_usage
from ctfws.services.pivot import PivotService
from ctfws.services.processes import ForwardProcessService
from ctfws.services.resume import ResumeService
from ctfws.services.search import DiffService, SearchService
from ctfws.services.ssh import AsyncSSHConnectionManager
from ctfws.services.tasks import TaskManager, TaskProgress
from ctfws.services.terminals import TerminalManager
from ctfws.services.tools import ToolCatalogService
from ctfws.services.topology import TopologyService
from ctfws.services.transfers import SFTPTransferService
from ctfws.services.workspace import WorkspaceService


class RemoteUploadRequest(BaseModel):
    """Workspace-relative source and explicit remote destination."""

    local_path: str = Field(min_length=1, max_length=500)
    remote_path: str = Field(min_length=1, max_length=1000)
    overwrite: bool = False
    conflict: ConflictPolicy = ConflictPolicy.CANCEL


class RemoteDownloadRequest(BaseModel):
    """Explicit remote source and workspace-relative destination."""

    remote_path: str = Field(min_length=1, max_length=1000)
    local_path: str = Field(min_length=1, max_length=500)
    overwrite: bool = False
    conflict: ConflictPolicy = ConflictPolicy.CANCEL


class ToolTransferRequest(BaseModel):
    """Destination for an explicitly selected cataloged tool."""

    remote_path: str = Field(min_length=1, max_length=1000)
    overwrite: bool = False
    conflict: ConflictPolicy = ConflictPolicy.CANCEL


class ContextLauncherRequest(BaseModel):
    """Program and arguments for a non-executing SOCKS launcher preview."""

    program: str = Field(min_length=1, max_length=4096)
    arguments: tuple[str, ...] = Field(default=(), max_length=64)
    launcher: Literal["environment", "proxychains", "namespace"] = "environment"


class ContextExecuteRequest(ContextLauncherRequest):
    """Explicit contextual execution request; it never accepts shell text."""

    timeout_seconds: int = Field(default=60, ge=1, le=3600)


class TerminalShareRequest(BaseModel):
    """Explicitly publish or hide terminal output for observers."""

    shared: bool


class TerminalRenameRequest(BaseModel):
    """New human-readable label for a managed terminal."""

    name: str = Field(min_length=1, max_length=120)


class LoginRequest(BaseModel):
    """Bootstrap, local account or verified OIDC assertion."""

    bootstrap_token: str | None = Field(default=None, max_length=500)
    username: str | None = Field(default=None, max_length=120)
    password: str | None = Field(default=None, max_length=500)
    oidc_token: str | None = Field(default=None, max_length=20000)


def _effective_conflict(overwrite: bool, policy: ConflictPolicy) -> ConflictPolicy:
    """Keep the v1 boolean compatible while allowing explicit policies."""

    return ConflictPolicy.REPLACE if overwrite and policy == ConflictPolicy.CANCEL else policy


def _keep_both_path(target: Path) -> Path:
    """Return a non-existing sibling with a deterministic human-readable suffix."""

    stem = target.stem
    suffix = target.suffix
    for index in range(1, 10_000):
        candidate = target.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise ValueError("Não foi possível encontrar um nome livre para manter ambos os arquivos.")


class AccountRequest(BaseModel):
    """Local team account input; only hashes are persisted."""

    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=500)
    role: str = Field(default="operator", max_length=20)


class HostKeyTrustRequest(BaseModel):
    """Fingerprint explicitly confirmed by the operator for a pending host key."""

    fingerprint: str = Field(min_length=10, max_length=200)


class SecretRequest(BaseModel):
    """Secret input kept out of profile responses and process arguments."""

    name: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=10000)


class ResumeRequest(BaseModel):
    """Explicit selection for the post-restart resume action."""

    resource_ids: list[int] = Field(default_factory=list)
    resource_refs: list[str] = Field(default_factory=list, max_length=100)


class TemporaryCredentialRequest(BaseModel):
    """Credential kept only for one connection attempt."""

    password: str | None = Field(default=None, max_length=500)
    kind: Literal["password", "key_passphrase"] | None = None


class OperationConfirmation(BaseModel):
    """Explicit confirmation required before a plan changes runtime state."""

    confirm: bool = False


def create_app(paths: WorkspacePaths) -> Any:
    """Build the dashboard/API and attach one managed-process runtime."""

    try:
        from fastapi import (
            FastAPI,
            File,
            Header,
            HTTPException,
            Request,
            UploadFile,
            WebSocket,
            WebSocketDisconnect,
        )
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as error:  # pragma: no cover
        raise RuntimeError(
            "A interface web exige as dependências opcionais 'fastapi' e 'uvicorn'."
        ) from error

    workspace = WorkspaceService(paths)
    connections = ConnectionService(workspace)
    tasks = TaskManager(workspace)
    from ctfws.services.auth import AuthManager
    from ctfws.services.vault import SecretVault

    vault = SecretVault()
    ssh = AsyncSSHConnectionManager(workspace, secret_resolver=vault.get)
    terminals = TerminalManager(workspace, ssh)
    forward_processes = ForwardProcessService(workspace, ssh)
    contexts = NetworkContextService(workspace, ssh)
    tool_catalog = ToolCatalogService(workspace)
    resume = ResumeService(workspace, forward_processes, contexts)
    topology = TopologyService(workspace)
    search = SearchService(workspace)
    auth = AuthManager()
    membership_required = os.getenv("CTFWS_REQUIRE_MEMBERSHIP", "0") == "1"
    # Workspace membership is an authorization mode and therefore also
    # requires an authenticated principal, even when local auth was otherwise
    # left optional for single-user development.
    auth.required = auth.required or membership_required
    transfers = SFTPTransferService(workspace, ssh)
    inspection = RemoteInspectionService(workspace, ssh)
    maintenance = WorkspaceMaintenanceService(workspace)

    @asynccontextmanager
    async def lifespan(_app: Any) -> Any:
        engine_lock = EngineLock(workspace.paths.root / "motor.lock", workspace.engine_id)
        engine_lock.acquire()
        try:
            tasks.recover()
            terminals.recover()
            yield
        finally:
            await contexts.close_all()
            await forward_processes.close_all()
            await ssh.close_all()
            workspace.port_leases.close()
            tasks.close()
            engine_lock.release()

    app = FastAPI(title=f"Conduit — {workspace.lab.name}", version="0.28", lifespan=lifespan)
    app.state.workspace = workspace
    app.state.terminals = terminals
    app.state.tasks = tasks
    app.state.ssh = ssh
    app.state.forward_processes = forward_processes
    app.state.contexts = contexts
    app.state.tools = tool_catalog
    app.state.resume = resume
    app.state.auth = auth
    app.state.transfers = transfers
    app.state.inspection = inspection
    app.state.maintenance = maintenance

    @app.exception_handler(HTTPException)
    async def api_http_error(_request: Request, error: HTTPException) -> Any:
        """Return a stable, UI-friendly envelope while keeping ``detail`` compatible."""

        detail = error.detail
        if isinstance(detail, dict):
            message = str(detail.get("message", detail.get("detail", "Erro HTTP.")))
            code = str(detail.get("code", f"http_{error.status_code}"))
        else:
            message = str(detail)
            code = f"http_{error.status_code}"
        retryable = error.status_code in {408, 409, 425, 429} or error.status_code >= 500
        return JSONResponse(
            {
                "code": code,
                "message": message,
                "detail": detail,
                "retryable": retryable,
                "diagnostic_id": secrets.token_hex(8),
            },
            status_code=error.status_code,
            headers=error.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def api_validation_error(_request: Request, error: RequestValidationError) -> Any:
        """Normalize Pydantic input failures without exposing internal exception objects."""

        detail = [
            {
                "location": [str(part) for part in item.get("loc", ())],
                "message": str(item.get("msg", "Entrada inválida.")),
                "type": str(item.get("type", "validation_error")),
            }
            for item in error.errors()
        ]
        return JSONResponse(
            {
                "code": "validation_error",
                "message": "Os dados enviados são inválidos.",
                "detail": detail,
                "retryable": False,
                "diagnostic_id": secrets.token_hex(8),
            },
            status_code=422,
        )

    @app.exception_handler(IdempotencyConflictError)
    async def api_idempotency_error(_request: Request, error: IdempotencyConflictError) -> Any:
        """Make duplicate-key content conflicts explicit and retry-safe."""

        return JSONResponse(
            {
                "code": "idempotency_conflict",
                "message": str(error),
                "detail": str(error),
                "retryable": False,
                "diagnostic_id": secrets.token_hex(8),
            },
            status_code=409,
        )

    @app.exception_handler(Exception)
    async def api_internal_error(_request: Request, error: Exception) -> Any:
        """Keep unexpected failures machine-readable without exposing internals."""

        diagnostic_id = secrets.token_hex(8)
        workspace.logger.exception("unhandled web error diagnostic_id=%s", diagnostic_id)
        del error
        return JSONResponse(
            {
                "code": "internal_error",
                "message": "O motor não conseguiu concluir a operação.",
                "detail": "Consulte o identificador de diagnóstico nos logs do motor.",
                "retryable": True,
                "diagnostic_id": diagnostic_id,
            },
            status_code=500,
        )

    frontend_dist = Path(__file__).resolve().parent / "frontend" / "dist"
    if (frontend_dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="assets")

    def require_workspace(workspace_id: int | None) -> None:
        if workspace_id is not None and workspace_id != workspace.lab.id:
            raise HTTPException(status_code=404, detail="Workspace não encontrado.")

    def principal_for(request: Request) -> Any:
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip() if header else None
        return auth.authenticate(token)

    def workspace_role(principal: Any, workspace_id: int | None) -> str:
        """Return the role effective for this workspace, not globally."""

        if principal is None:
            raise HTTPException(status_code=401, detail="Sessão necessária.")
        if not membership_required:
            return str(principal.role)
        if workspace_id is not None and workspace_id != workspace.lab.id:
            raise HTTPException(status_code=404, detail="Workspace não encontrado.")
        membership = workspace.memberships.get(str(principal.subject))
        # The one-time bootstrap administrator must be able to provision the
        # first membership; afterwards all normal subjects need an explicit row.
        if membership is None:
            if principal.subject == "bootstrap-admin" and principal.role == "admin":
                return "admin"
            raise HTTPException(status_code=403, detail="Usuário não pertence a este workspace.")
        ranks = {"observer": 0, "operator": 1, "admin": 2}
        principal_role = str(principal.role)
        member_role = membership.role.value
        return (
            member_role
            if ranks.get(member_role, -1) < ranks.get(principal_role, -1)
            else principal_role
        )

    def live_session_allowed(token: str | None, workspace_id: int | None) -> bool:
        """Revalidate long-lived SSE/WebSocket sessions after revocation."""

        principal = auth.authenticate(token)
        if token is not None and principal is None:
            return False
        if auth.required and principal is None:
            return False
        if membership_required:
            try:
                workspace_role(principal, workspace_id)
            except HTTPException:
                return False
        return True

    def workspace_id_from_path(path: str) -> int | None:
        match = re.match(r"^/api/v2/workspaces/(\d+)(?:/|$)", path)
        return int(match.group(1)) if match else None

    def actor_for(request: Request) -> str:
        principal = principal_for(request)
        return principal.subject if principal is not None else "anonymous"

    def operation_hash(
        kind: str,
        resource_id: int | None,
        actor: str,
        payload: object,
    ) -> str:
        """Fingerprint the scoped request used with an idempotency key."""

        canonical = json.dumps(
            {
                "actor": actor,
                "kind": kind,
                "resource_id": resource_id,
                "payload": payload,
                "workspace_id": workspace.lab.id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def require_confirmation(confirmation: OperationConfirmation | None, operation: str) -> None:
        """Keep the safety confirmation on the API boundary, not only in the UI."""

        if confirmation is None or not confirmation.confirm:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "confirmation_required",
                    "message": f"Confirme explicitamente a operação: {operation}.",
                    "retryable": False,
                },
            )

    @app.middleware("http")
    async def same_origin_write_guard(request: Any, call_next: Any) -> Any:
        """Reject browser writes originating from another site."""

        if auth.required and request.url.path.startswith("/api/"):
            if request.url.path not in {
                "/api/v2/auth/login",
                "/api/v2/auth/session",
                "/api/v2/auth/oidc",
            }:
                header = request.headers.get("authorization", "")
                token = header.removeprefix("Bearer ").strip() if header else None
                principal = auth.authenticate(token)
                if principal is None:
                    from fastapi.responses import JSONResponse

                    return JSONResponse(
                        {"code": "authentication_required", "message": "Sessão necessária."},
                        status_code=401,
                    )
                requested_workspace = workspace_id_from_path(request.url.path)
                if requested_workspace is not None:
                    try:
                        role = workspace_role(principal, requested_workspace)
                    except HTTPException as error:
                        return JSONResponse(
                            {"code": "workspace_access_denied", "message": str(error.detail)},
                            status_code=error.status_code,
                        )
                else:
                    role = (
                        workspace_role(principal, None)
                        if membership_required
                        else str(principal.role)
                    )
                if request.method not in {"GET", "HEAD", "OPTIONS"} and role == "observer":
                    from fastapi.responses import JSONResponse

                    return JSONResponse(
                        {
                            "code": "insufficient_role",
                            "message": "Observadores não alteram recursos.",
                        },
                        status_code=403,
                    )

        origin = request.headers.get("origin")
        if origin and request.method not in {"GET", "HEAD", "OPTIONS"}:
            if urlparse(origin).netloc != request.url.netloc:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    {"detail": "Origem não autorizada para esta operação."}, status_code=403
                )
        actor = actor_for(request)
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip() if header else None
        principal = auth.authenticate(token)
        response = await call_next(request)
        if request.url.path.startswith("/api/") and request.method not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }:
            try:
                workspace.audit.create(
                    AuditCreate(
                        actor=actor,
                        action=f"{request.method} {request.url.path}",
                        resource_type="http_request",
                        result="succeeded" if response.status_code < 400 else "failed",
                        correlation_id=(request.headers.get("x-request-id") or uuid.uuid4().hex)[
                            :120
                        ],
                        details={
                            "status_code": response.status_code,
                            "role": principal.role if principal is not None else None,
                        },
                    )
                )
            except Exception:
                workspace.logger.exception("audit request failed")
        return response

    @app.post("/api/v2/auth/login")
    def auth_login(data: LoginRequest) -> dict[str, str]:
        try:
            if data.bootstrap_token:
                token = auth.login(data.bootstrap_token)
            elif data.username and data.password:
                token = auth.login_account(data.username, data.password)
            elif data.oidc_token:
                token = auth.login_oidc(data.oidc_token)
            else:
                raise PermissionError("Informe bootstrap_token, username/password ou oidc_token.")
            return {"token": token, "token_type": "bearer"}
        except PermissionError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error

    @app.get("/api/v2/auth/accounts")
    def auth_accounts(request: Request) -> list[dict[str, str]]:
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip() if header else None
        principal = auth.authenticate(token)
        if principal is None or principal.role != "admin":
            raise HTTPException(status_code=403, detail="Somente administradores listam contas.")
        return auth.accounts()

    @app.post("/api/v2/auth/accounts", status_code=201)
    def auth_account_create(data: AccountRequest, request: Request) -> dict[str, str]:
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip() if header else None
        principal = auth.authenticate(token)
        if principal is None or principal.role != "admin":
            raise HTTPException(status_code=403, detail="Somente administradores criam contas.")
        try:
            auth.create_account(data.username, data.password, data.role)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"username": data.username, "role": data.role}

    @app.get("/api/v2/auth/session")
    def auth_session(request: Request) -> dict[str, Any]:
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip() if header else None
        principal = auth.authenticate(token)
        if principal is None:
            if not auth.required:
                return {"authenticated": False, "mode": "local-optional"}
            raise HTTPException(status_code=401, detail="Sessão necessária.")
        return {"authenticated": True, "subject": principal.subject, "role": principal.role}

    @app.get("/api/v2/auth/oidc")
    def auth_oidc() -> dict[str, object]:
        """Expose non-secret OIDC capability metadata for enterprise gateways."""

        return auth.oidc_metadata()

    @app.post("/api/v2/auth/logout")
    def auth_logout(request: Request) -> dict[str, bool]:
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip() if header else None
        auth.revoke(token)
        return {"logged_out": True}

    @app.post("/api/v2/auth/secrets")
    def secret_create(data: SecretRequest, request: Request) -> dict[str, str]:
        principal = principal_for(request)
        if membership_required:
            if workspace_role(principal, None) != "admin":
                raise HTTPException(
                    status_code=403,
                    detail="Somente administradores criam credenciais compartilhadas.",
                )
        elif auth.required and (principal is None or str(principal.role) != "admin"):
            raise HTTPException(
                status_code=403,
                detail="Somente administradores criam credenciais compartilhadas.",
            )
        try:
            return {"auth_ref": vault.put(data.name, data.value)}
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        built_index = frontend_dist / "index.html"
        if built_index.is_file():
            return built_index.read_text(encoding="utf-8")
        return _dashboard_html(workspace.lab.name)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "workspace": str(paths.root)}

    @app.get("/api/v2/workspaces/{workspace_id}/maintenance")
    def maintenance_inspect(workspace_id: int) -> dict[str, object]:
        require_workspace(workspace_id)
        return maintenance.inspect().as_dict()

    @app.post("/api/v2/workspaces/{workspace_id}/maintenance/prune")
    def maintenance_prune(workspace_id: int) -> dict[str, object]:
        require_workspace(workspace_id)
        return maintenance.prune().as_dict()

    @app.get("/api/v2/workspaces")
    def workspace_list(request: Request) -> list[dict[str, Any]]:
        if membership_required:
            workspace_role(principal_for(request), None)
        return [workspace.lab.model_dump(mode="json")]

    @app.get("/api/v2/workspaces/{workspace_id}")
    def workspace_get(workspace_id: int) -> dict[str, Any]:
        require_workspace(workspace_id)
        return workspace.lab.model_dump(mode="json")

    @app.get("/api/v2/workspaces/{workspace_id}/members")
    def workspace_member_list(workspace_id: int, request: Request) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        if workspace_role(principal_for(request), workspace_id) != "admin":
            raise HTTPException(
                status_code=403, detail="Somente administradores gerenciam membros."
            )
        return [member.model_dump(mode="json") for member in workspace.memberships.list()]

    @app.post("/api/v2/workspaces/{workspace_id}/members", status_code=201)
    def workspace_member_upsert(
        workspace_id: int, data: MembershipCreate, request: Request
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        if workspace_role(principal_for(request), workspace_id) != "admin":
            raise HTTPException(
                status_code=403, detail="Somente administradores gerenciam membros."
            )
        return workspace.memberships.upsert(data).model_dump(mode="json")

    @app.delete("/api/v2/workspaces/{workspace_id}/members/{subject}")
    def workspace_member_delete(
        workspace_id: int, subject: str, request: Request
    ) -> dict[str, bool]:
        require_workspace(workspace_id)
        if workspace_role(principal_for(request), workspace_id) != "admin":
            raise HTTPException(
                status_code=403, detail="Somente administradores gerenciam membros."
            )
        member = workspace.memberships.get(subject)
        if (
            member is not None
            and member.role.value == "admin"
            and workspace.memberships.count_role("admin") <= 1
        ):
            raise HTTPException(
                status_code=409,
                detail="O último administrador do workspace não pode ser removido.",
            )
        if not workspace.memberships.delete(subject):
            raise HTTPException(status_code=404, detail="Membro não encontrado.")
        return {"deleted": True}

    @app.get("/api/summary")
    @app.get("/api/v1/summary")
    @app.get("/api/v2/workspaces/{workspace_id}/summary")
    def summary(workspace_id: int | None = None) -> dict[str, int]:
        require_workspace(workspace_id)
        return _summary(workspace, terminals)

    @app.get("/api/hosts")
    @app.get("/api/v1/hosts")
    @app.get("/api/v2/workspaces/{workspace_id}/hosts")
    def hosts(workspace_id: int | None = None) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        return [host.model_dump(mode="json") for host in workspace.hosts.list()]

    @app.get("/api/networks")
    @app.get("/api/v2/workspaces/{workspace_id}/networks")
    def networks(workspace_id: int | None = None) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        from ctfws.database.repositories import ObservationRepository

        return ObservationRepository(workspace.database, workspace.lab.id).list_table("networks")

    @app.get("/api/v1/snapshots")
    @app.get("/api/v2/workspaces/{workspace_id}/snapshots")
    def snapshot_list(workspace_id: int | None = None) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        return [snapshot.model_dump(mode="json") for snapshot in workspace.snapshots.list()]

    @app.post("/api/v1/snapshots", status_code=201)
    @app.post("/api/v2/workspaces/{workspace_id}/snapshots", status_code=201)
    def snapshot_create(data: SnapshotCreate, workspace_id: int | None = None) -> dict[str, Any]:
        require_workspace(workspace_id)
        return workspace.snapshots.create(data).model_dump(mode="json")

    @app.get("/api/v1/evidence")
    @app.get("/api/v2/workspaces/{workspace_id}/evidence")
    def evidence_list(workspace_id: int | None = None) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        return [item.model_dump(mode="json") for item in workspace.evidence.list()]

    @app.post("/api/v1/evidence", status_code=201)
    @app.post("/api/v2/workspaces/{workspace_id}/evidence", status_code=201)
    def evidence_create(data: EvidenceCreate, workspace_id: int | None = None) -> dict[str, Any]:
        require_workspace(workspace_id)
        if data.host_id is not None and workspace.hosts.get(str(data.host_id)) is None:
            raise HTTPException(status_code=404, detail="Host da evidência não encontrado.")
        return workspace.evidence.create(data).model_dump(mode="json")

    @app.get("/api/v1/search")
    @app.get("/api/v2/workspaces/{workspace_id}/search")
    def workspace_search(query: str = "", workspace_id: int | None = None) -> list[dict[str, str]]:
        require_workspace(workspace_id)
        if not query.strip():
            return []
        return search.search(query.strip())

    @app.get("/api/v1/audit")
    @app.get("/api/v2/workspaces/{workspace_id}/audit")
    def audit_list(limit: int = 100, workspace_id: int | None = None) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        return [item.model_dump(mode="json") for item in workspace.audit.list(limit)]

    @app.get("/api/v1/diff/commands")
    @app.get("/api/v2/workspaces/{workspace_id}/diff/commands")
    def command_diff(
        host_id: int, command: str, workspace_id: int | None = None
    ) -> dict[str, list[str]]:
        require_workspace(workspace_id)
        if workspace.hosts.get(str(host_id)) is None:
            raise HTTPException(status_code=404, detail="Host não encontrado.")
        return DiffService(workspace).command_diff(host_id, command)

    @app.get("/api/v1/topology")
    @app.get("/api/v2/workspaces/{workspace_id}/topology")
    def topology_view(
        format_name: str = "text",
        format: str | None = None,
        workspace_id: int | None = None,
    ) -> dict[str, str]:
        require_workspace(workspace_id)
        selected_format = format or format_name
        if selected_format not in {"text", "dot"}:
            raise HTTPException(status_code=422, detail="Formato precisa ser text ou dot.")
        content = topology.render_dot() if selected_format == "dot" else topology.render_text()
        return {"format": selected_format, "content": content}

    @app.get("/api/v1/topology/graph")
    @app.get("/api/v2/workspaces/{workspace_id}/topology/graph")
    def topology_graph(workspace_id: int | None = None) -> dict[str, object]:
        require_workspace(workspace_id)
        return topology.graph()

    @app.post("/api/v1/topology/detect-pivots")
    @app.post("/api/v2/workspaces/{workspace_id}/topology/detect-pivots")
    def topology_detect_pivots(workspace_id: int | None = None) -> dict[str, int]:
        require_workspace(workspace_id)
        return {"detected": topology.detect_pivots()}

    @app.get("/api/paths")
    @app.get("/api/v1/paths")
    @app.get("/api/v2/workspaces/{workspace_id}/paths")
    def paths_api(
        target_host_id: int | None = None, workspace_id: int | None = None
    ) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        return [
            path.model_dump(mode="json") for path in workspace.access_paths.list(target_host_id)
        ]

    @app.post("/api/paths/rebuild")
    @app.post("/api/v2/workspaces/{workspace_id}/paths/rebuild")
    def rebuild_paths(workspace_id: int | None = None) -> dict[str, int]:
        require_workspace(workspace_id)
        result = AccessPathService(workspace).rebuild()
        return {"paths": result.paths, "targets": result.targets, "verified": result.verified}

    @app.post("/api/v1/paths/{path_id}/verify")
    @app.post("/api/v2/workspaces/{workspace_id}/paths/{path_id}/verify")
    def verify_path(path_id: int, workspace_id: int | None = None) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            result = AccessPathService(workspace).verify(path_id)
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return result.model_dump(mode="json")

    @app.get("/api/v1/paths/{path_id}/checks")
    @app.get("/api/v2/workspaces/{workspace_id}/paths/{path_id}/checks")
    def path_checks(path_id: int, workspace_id: int | None = None) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        if workspace.access_paths.get(path_id) is None:
            raise HTTPException(status_code=404, detail="Caminho não encontrado.")
        return [
            check.model_dump(mode="json")
            for check in workspace.access_verifications.list_for_path(path_id)
        ]

    @app.get("/api/sessions")
    @app.get("/api/v2/workspaces/{workspace_id}/sessions")
    def sessions(workspace_id: int | None = None) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        return [session.model_dump(mode="json") for session in workspace.sessions.list()]

    @app.get("/api/forwards")
    @app.get("/api/v1/forwards")
    @app.get("/api/v2/workspaces/{workspace_id}/tunnels")
    def forwards(workspace_id: int | None = None) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        return [forward.model_dump(mode="json") for forward in workspace.forwards.list()]

    @app.post("/api/v1/forwards", status_code=201)
    @app.post("/api/v2/workspaces/{workspace_id}/tunnels", status_code=201)
    def forward_create(data: ForwardCreate, workspace_id: int | None = None) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            return PivotService(workspace).add_forward(data).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/forwards/preview")
    @app.post("/api/v2/workspaces/{workspace_id}/tunnels/preview")
    def forward_preview(data: ForwardCreate, workspace_id: int | None = None) -> dict[str, Any]:
        """Return a reviewed adapter plan without creating a persistent resource."""

        require_workspace(workspace_id)
        try:
            return asdict(PivotService(workspace).preview(data))
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/transfers")
    @app.get("/api/v2/workspaces/{workspace_id}/transfers")
    def transfer_list(limit: int = 100, workspace_id: int | None = None) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        return [transfer.model_dump(mode="json") for transfer in workspace.transfers.list(limit)]

    @app.get("/api/v1/connections/{connection_id}/files")
    @app.get("/api/v2/workspaces/{workspace_id}/connections/{connection_id}/files")
    async def remote_file_list(
        connection_id: int,
        remote_path: str = ".",
        workspace_id: int | None = None,
    ) -> list[dict[str, str]]:
        require_workspace(workspace_id)
        try:
            return await transfers.list_remote_async(connection_id, remote_path)
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/capabilities")
    @app.get("/api/v2/workspaces/{workspace_id}/capabilities")
    def capabilities(workspace_id: int | None = None) -> dict[str, Any]:
        require_workspace(workspace_id)
        names = ("ssh", "sftp", "tmux", "chisel", "ligolo-agent", "gs-netcat", "nc")
        detected = detect_pivot_tools()
        return {
            "runtime": "windows" if shutil.which("powershell.exe") else "posix",
            "binaries": {name: shutil.which(name) is not None for name in names},
            "tools": [asdict(tool) for tool in detected],
            "managed_terminal": True,
            "ssh_profiles": True,
            "network_contexts": contexts.capabilities(),
            "transport_adapters": [asdict(item) for item in transport_capabilities()],
            "transports": {
                "ssh": ["local-forward", "remote-forward", "dynamic-socks"],
                "chisel": ["tcp-forward", "dynamic-socks"],
                "ligolo-ng": ["control-channel", "routed-context"],
                "nc": ["single-tcp-flow"],
                "gsocket": ["single-tcp-flow"],
            },
        }

    @app.get("/api/v1/resources/{resource_type}/{resource_id}/actions")
    @app.get("/api/v2/workspaces/{workspace_id}/resources/{resource_type}/{resource_id}/actions")
    def resource_actions(
        resource_type: str,
        resource_id: int,
        workspace_id: int | None = None,
    ) -> dict[str, object]:
        """Describe button actions without exposing internal IDs as workflow input."""

        require_workspace(workspace_id)
        normalized = resource_type.strip().lower().replace("-", "_")

        def action(
            action_id: str,
            label: str,
            available: bool,
            execution: str,
            *requirements: str,
        ) -> dict[str, object]:
            return {
                "id": action_id,
                "label": label,
                "available": available,
                "execution": execution,
                "requirements": list(requirements),
            }

        actions: list[dict[str, object]]
        if normalized in {"connection", "profile", "connection_profile"}:
            profile = workspace.connections.get(resource_id)
            if profile is None:
                raise HTTPException(
                    status_code=404, detail="Conexão não encontrada neste workspace."
                )
            actions = [
                action("test_connection", "Testar conexão", True, "motor Kali"),
                action("inspect_machine", "Inspecionar máquina", True, "máquina remota"),
                action("open_terminal", "Abrir terminal", True, "máquina remota"),
                action("browse_files", "Abrir arquivos", True, "máquina remota", "SFTP"),
            ]
            resource = {"type": "connection", "id": profile.id, "label": profile.name}
        elif normalized in {"host", "machine", "machine_host"}:
            host = workspace.hosts.get(str(resource_id))
            if host is None:
                raise HTTPException(
                    status_code=404, detail="Máquina não encontrada neste workspace."
                )
            profiles_for_host = [
                profile for profile in workspace.connections.list() if profile.host_id == host.id
            ]
            actions = [
                action(
                    "inspect_machine",
                    "Atualizar inventário",
                    bool(profiles_for_host),
                    "máquina remota",
                    "perfil SSH associado",
                ),
                action(
                    "open_terminal",
                    "Abrir terminal",
                    bool(profiles_for_host),
                    "máquina remota",
                    "perfil SSH associado",
                ),
                action(
                    "access_network",
                    "Acessar rede",
                    bool(profiles_for_host),
                    "contexto de rede",
                    "perfil SSH associado",
                ),
                action("register_evidence", "Registrar evidência", True, "motor Kali"),
            ]
            resource = {"type": "host", "id": host.id, "label": host.name, "address": str(host.ip)}
        elif normalized in {"context", "network_context"}:
            context = workspace.contexts.get(resource_id)
            if context is None:
                raise HTTPException(
                    status_code=404, detail="Contexto não encontrado neste workspace."
                )
            raw_routed = contexts.capabilities().get("routed")
            routed: dict[str, object] = raw_routed if isinstance(raw_routed, dict) else {}
            can_start = context.transport.value != "routed" or routed.get("enabled") is True
            actions = [
                action(
                    "start_context",
                    "Iniciar contexto",
                    context.status.value != "active" and can_start,
                    "motor Kali",
                ),
                action(
                    "stop_context",
                    "Encerrar contexto",
                    context.status.value == "active",
                    "motor Kali",
                ),
                action(
                    "execute_tool",
                    "Executar ferramenta",
                    context.status.value == "active",
                    "contexto de rede",
                ),
                action("diagnose_context", "Diagnosticar", True, "motor Kali"),
            ]
            resource = {"type": "network_context", "id": context.id, "label": context.name}
        elif normalized in {"terminal", "terminal_session"}:
            try:
                terminal = terminals.get(resource_id)
            except EntityNotFoundError as error:
                raise HTTPException(
                    status_code=404, detail="Terminal não encontrado neste workspace."
                ) from error
            actions = [
                action(
                    "attach",
                    "Abrir visualização",
                    terminals.runtime_available(terminal.id),
                    "navegador",
                ),
                action(
                    "new_session",
                    "Abrir nova sessão",
                    terminal.connection_id is not None,
                    "máquina remota",
                ),
                action(
                    "share",
                    "Compartilhar visualização",
                    terminal.status.value == "active",
                    "navegador",
                ),
                action("close", "Encerrar terminal", True, "motor Kali"),
            ]
            resource = {
                "type": "terminal",
                "id": terminal.id,
                "label": terminal.name or terminal.context_label,
            }
        elif normalized in {"service", "observed_service"}:
            rows = ObservationRepository(workspace.database, workspace.lab.id).list_table(
                "services"
            )
            service = next((row for row in rows if int(row["id"]) == resource_id), None)
            if service is None:
                raise HTTPException(
                    status_code=404, detail="Serviço não encontrado neste workspace."
                )
            has_port = isinstance(service.get("port"), int)
            actions = [
                action("test_tcp", "Testar serviço TCP", has_port, "motor Kali", "porta observada"),
                action("inspect_http", "Inspecionar HTTP", has_port, "motor Kali", "endpoint TCP"),
                action("create_forward", "Criar acesso", has_port, "motor Kali", "perfil SSH"),
                action("register_evidence", "Registrar evidência", True, "motor Kali"),
            ]
            resource = {
                "type": "service",
                "id": resource_id,
                "label": service.get("description") or service.get("port"),
            }
        elif normalized in {"forward", "tunnel"}:
            forward = workspace.forwards.get(resource_id)
            if forward is None:
                raise HTTPException(
                    status_code=404, detail="Acesso não encontrado neste workspace."
                )
            active = forward.status.value in {"active", "starting", "degraded"}
            actions = [
                action("verify", "Testar acesso", active, "motor Kali"),
                action("start", "Revisar e iniciar", not active, "motor Kali"),
                action("stop", "Encerrar acesso", active, "motor Kali"),
            ]
            resource = {"type": "forward", "id": forward.id, "label": forward.name}
        else:
            raise HTTPException(
                status_code=404, detail="Tipo de recurso não suportado neste workspace."
            )
        return {"resource": resource, "actions": actions}

    @app.get("/api/v1/contexts")
    @app.get("/api/v2/workspaces/{workspace_id}/contexts")
    def context_list(workspace_id: int | None = None) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        return [context.model_dump(mode="json") for context in workspace.contexts.list()]

    @app.post("/api/v1/contexts/{context_id}/launcher")
    @app.post("/api/v2/workspaces/{workspace_id}/contexts/{context_id}/launcher")
    def context_launcher(
        context_id: int,
        data: ContextLauncherRequest,
        workspace_id: int | None = None,
    ) -> dict[str, object]:
        require_workspace(workspace_id)
        try:
            return contexts.launcher_plan(
                context_id,
                data.program,
                data.arguments,
                launcher=data.launcher,
            )
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/contexts/{context_id}/execute", status_code=202)
    @app.post("/api/v2/workspaces/{workspace_id}/contexts/{context_id}/execute", status_code=202)
    def context_execute(
        context_id: int,
        data: ContextExecuteRequest,
        request: Request,
        workspace_id: int | None = None,
        idempotency_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        actor = actor_for(request)
        task = tasks.submit(
            TaskCreate(
                kind="context_execution",
                resource_type="network_context",
                resource_id=context_id,
                total_steps=1,
                idempotency_key=idempotency_key,
                idempotency_hash=operation_hash(
                    "context_execution",
                    context_id,
                    actor,
                    data.model_dump(mode="json"),
                ),
                requested_by=actor,
            ),
            lambda update: _context_execution_task(contexts, context_id, data, update),
        )
        return {"job_id": task.id, "status": task.status.value}

    @app.get("/api/v1/contexts/{context_id}/namespace-plan")
    @app.get("/api/v2/workspaces/{workspace_id}/contexts/{context_id}/namespace-plan")
    def context_namespace_plan(
        context_id: int,
        device: str | None = None,
        workspace_id: int | None = None,
    ) -> list[dict[str, object]]:
        require_workspace(workspace_id)
        try:
            return contexts.namespace_plan(context_id, device=device)
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/contexts/{context_id}/namespace-cleanup-plan")
    @app.get("/api/v2/workspaces/{workspace_id}/contexts/{context_id}/namespace-cleanup-plan")
    def context_namespace_cleanup_plan(
        context_id: int,
        device: str | None = None,
        workspace_id: int | None = None,
    ) -> list[dict[str, object]]:
        require_workspace(workspace_id)
        try:
            return contexts.namespace_cleanup_plan(context_id, device=device)
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/events")
    def event_list(limit: int = 100) -> list[dict[str, Any]]:
        return workspace.events.list(max(1, min(limit, 500)))

    @app.get("/api/v1/resume")
    @app.get("/api/v2/workspaces/{workspace_id}/resume")
    def resume_plan(workspace_id: int | None = None) -> list[dict[str, object]]:
        require_workspace(workspace_id)
        return resume.as_dicts()

    @app.post("/api/v1/resume")
    @app.post("/api/v2/workspaces/{workspace_id}/resume")
    async def resume_apply(
        data: ResumeRequest, workspace_id: int | None = None
    ) -> list[dict[str, object]]:
        require_workspace(workspace_id)
        try:
            return await resume.apply_async(
                tuple(data.resource_ids) if data.resource_ids else None,
                tuple(data.resource_refs) if data.resource_refs else None,
            )
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v2/workspaces/{workspace_id}/events")
    async def event_stream(
        workspace_id: int,
        request: Request,
        last_event_id: int = 0,
    ) -> Any:
        require_workspace(workspace_id)
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip() if header else None
        if not live_session_allowed(token, workspace_id):
            raise HTTPException(status_code=401, detail="Sessão expirada ou revogada.")
        header_cursor = request.headers.get("last-event-id")
        if header_cursor and header_cursor.isdigit():
            last_event_id = max(last_event_id, int(header_cursor))

        async def stream() -> Any:
            cursor = last_event_id
            yield ": ctfws event stream\n\n"
            while True:
                if not live_session_allowed(token, workspace_id):
                    return
                events = workspace.events.list_after(cursor)
                for event in events:
                    cursor = int(event["id"])
                    yield (
                        f"id: {cursor}\n"
                        f"event: {event['event_type']}\n"
                        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    )
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/contexts", status_code=201)
    @app.post("/api/v2/workspaces/{workspace_id}/contexts", status_code=201)
    def context_create(
        data: NetworkContextCreate, workspace_id: int | None = None
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            return contexts.plan(data).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/contexts/{context_id}/start")
    @app.post("/api/v2/workspaces/{workspace_id}/contexts/{context_id}/start")
    async def context_start(
        context_id: int,
        confirmation: OperationConfirmation | None = None,
        workspace_id: int | None = None,
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        require_confirmation(confirmation, "iniciar o contexto")
        try:
            return (await contexts.start_async(context_id)).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/contexts/{context_id}/stop")
    @app.post("/api/v2/workspaces/{workspace_id}/contexts/{context_id}/stop")
    async def context_stop(context_id: int, workspace_id: int | None = None) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            return (await contexts.stop_async(context_id)).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/contexts/{context_id}/namespace/prepare")
    @app.post("/api/v2/workspaces/{workspace_id}/contexts/{context_id}/namespace/prepare")
    def context_namespace_prepare(
        context_id: int,
        confirmation: OperationConfirmation | None = None,
        device: str | None = None,
        workspace_id: int | None = None,
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        require_confirmation(confirmation, "preparar o namespace")
        try:
            return contexts.prepare_namespace(context_id, device=device).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/contexts/{context_id}/namespace/remove")
    @app.post("/api/v2/workspaces/{workspace_id}/contexts/{context_id}/namespace/remove")
    def context_namespace_remove(
        context_id: int, workspace_id: int | None = None
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            return contexts.remove_namespace(context_id).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/jobs")
    @app.get("/api/v2/jobs")
    @app.get("/api/v2/workspaces/{workspace_id}/jobs")
    def job_list(limit: int = 100, workspace_id: int | None = None) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        return [job.model_dump(mode="json") for job in tasks.list(max(1, min(limit, 500)))]

    @app.get("/api/v1/jobs/{job_id}")
    @app.get("/api/v2/jobs/{job_id}")
    @app.get("/api/v2/workspaces/{workspace_id}/jobs/{job_id}")
    def job_get(job_id: int, workspace_id: int | None = None) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            return tasks.get(job_id).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/api/v1/collections")
    @app.get("/api/v2/workspaces/{workspace_id}/collections")
    def collection_list(
        host_id: int | None = None,
        limit: int = 50,
        workspace_id: int | None = None,
    ) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        return [run.model_dump(mode="json") for run in workspace.collections.list(host_id, limit)]

    @app.get("/api/v1/collections/{collection_id}")
    @app.get("/api/v2/workspaces/{workspace_id}/collections/{collection_id}")
    def collection_get(collection_id: int, workspace_id: int | None = None) -> dict[str, Any]:
        """Return one inspection run, including its persisted command outputs."""

        require_workspace(workspace_id)
        run = workspace.collections.get(collection_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Coleta não encontrada neste workspace.")
        return run.model_dump(mode="json")

    @app.post("/api/v1/jobs/{job_id}/cancel")
    @app.post("/api/v2/jobs/{job_id}/cancel")
    @app.post("/api/v2/workspaces/{workspace_id}/jobs/{job_id}/cancel")
    def job_cancel(job_id: int, workspace_id: int | None = None) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            return tasks.cancel(job_id).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/v1/jobs/{job_id}/retry", status_code=202)
    @app.post("/api/v2/workspaces/{workspace_id}/jobs/{job_id}/retry", status_code=202)
    def job_retry(job_id: int, request: Request, workspace_id: int | None = None) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            original = tasks.get(job_id)
            if original.kind != "remote_inspection" or original.resource_id is None:
                raise ValueError("Somente inspeções remotas podem ser repetidas por este endpoint.")
            resource_id = original.resource_id
            task = tasks.retry(
                job_id,
                TaskCreate(
                    kind=original.kind,
                    resource_type=original.resource_type,
                    resource_id=original.resource_id,
                    total_steps=original.total_steps,
                    requested_by=actor_for(request),
                ),
                lambda update: _inspect_task(inspection, resource_id, update),
            )
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"job_id": task.id, "status": task.status.value}

    @app.get("/api/v1/files")
    @app.get("/api/v2/workspaces/{workspace_id}/files")
    def file_list(relative: str = "loot", workspace_id: int | None = None) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        try:
            directory = _safe_workspace_path(paths, relative)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if not directory.is_dir():
            raise HTTPException(status_code=404, detail="Diretório não encontrado.")
        return [
            {
                "name": item.name,
                "path": item.relative_to(paths.root).as_posix(),
                "kind": "directory" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else None,
            }
            for item in sorted(directory.iterdir(), key=lambda candidate: candidate.name.lower())
        ]

    @app.get("/api/v1/tools")
    @app.get("/api/v2/workspaces/{workspace_id}/tools")
    def tool_list(workspace_id: int | None = None) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        return [tool.model_dump(mode="json") for tool in workspace.tools.list()]

    @app.post("/api/v1/tools", status_code=201)
    @app.post("/api/v2/workspaces/{workspace_id}/tools", status_code=201)
    def tool_register(data: ToolCreate, workspace_id: int | None = None) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            return tool_catalog.register(data).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/tools/{tool_id}/transfer/{connection_id}", status_code=202)
    @app.post(
        "/api/v2/workspaces/{workspace_id}/tools/{tool_id}/transfer/{connection_id}",
        status_code=202,
    )
    async def tool_transfer_job(
        tool_id: int,
        connection_id: int,
        transfer_request: ToolTransferRequest,
        http_request: Request,
        workspace_id: int | None = None,
        idempotency_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Queue a cataloged-tool upload without ever executing the tool."""

        require_workspace(workspace_id)
        try:
            tool_catalog.get(tool_id)
            connections.get(connection_id)
        except EntityNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

        task_data = TaskCreate(
            kind="tool_transfer",
            resource_type="tool",
            resource_id=tool_id,
            total_steps=1,
            idempotency_key=idempotency_key,
            requested_by=actor_for(http_request),
        )
        task_data = task_data.model_copy(
            update={
                "idempotency_hash": operation_hash(
                    "tool_transfer",
                    tool_id,
                    task_data.requested_by or "anonymous",
                    {
                        "connection_id": connection_id,
                        "request": transfer_request.model_dump(mode="json"),
                    },
                )
            }
        )
        if transfers.async_available:

            async def upload_tool_task(_update: Any) -> dict[str, object]:
                result = await transfers.upload_tool_async(
                    connection_id,
                    tool_id,
                    transfer_request.remote_path,
                    overwrite=transfer_request.overwrite,
                    conflict=transfer_request.conflict,
                )
                return asdict(result)

            task = tasks.submit_async(
                task_data,
                upload_tool_task,
            )
        else:
            task = tasks.submit(
                task_data,
                lambda _update: asdict(
                    transfers.upload_tool(
                        connection_id,
                        tool_id,
                        transfer_request.remote_path,
                        overwrite=transfer_request.overwrite,
                        conflict=transfer_request.conflict,
                    )
                ),
            )
        return {"job_id": task.id, "status": task.status.value}

    @app.post("/api/v1/files/upload")
    @app.post("/api/v2/workspaces/{workspace_id}/files/upload")
    async def file_upload(
        file: UploadFile = File(...),  # noqa: B008
        destination: str = "loot/inbox",
        overwrite: bool = False,
        conflict: ConflictPolicy = ConflictPolicy.CANCEL,
        workspace_id: int | None = None,
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            directory = _safe_workspace_path(paths, destination)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        directory.mkdir(parents=True, exist_ok=True)
        filename = Path(file.filename or "upload.bin").name
        if filename in {"", ".", ".."}:
            raise HTTPException(status_code=422, detail="Nome de arquivo inválido.")
        target = (directory / filename).resolve()
        if directory not in target.parents:
            raise HTTPException(status_code=422, detail="Destino de arquivo inválido.")
        if target.name == "workspace.db" or target.name.startswith("workspace.db-"):
            raise HTTPException(status_code=422, detail="Arquivo interno não pode ser sobrescrito.")
        policy = _effective_conflict(overwrite, conflict)
        if target.exists():
            if policy == ConflictPolicy.CANCEL:
                raise HTTPException(
                    status_code=409,
                    detail="O arquivo já existe; escolha conflict=keep_both ou conflict=replace.",
                )
            if policy == ConflictPolicy.KEEP_BOTH:
                target = _keep_both_path(target)
        temporary = target.with_name(
            f".{target.name}.ctfws-{hashlib.sha256(os.urandom(16)).hexdigest()[:16]}.part"
        )
        digest = hashlib.sha256()
        size = 0
        quota = max_workspace_bytes()
        existing_size = target.stat().st_size if target.exists() else 0
        quota_base = workspace_usage(paths.root) - existing_size
        try:
            with temporary.open("wb") as handle:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_file_bytes():
                        raise HTTPException(
                            status_code=413,
                            detail="Arquivo maior que o limite configurado do workspace.",
                        )
                    if quota is not None and quota_base + size > quota:
                        raise HTTPException(
                            status_code=413,
                            detail="A quota de armazenamento do workspace seria excedida.",
                        )
                    digest.update(chunk)
                    handle.write(chunk)
            os.replace(temporary, target)
        except HTTPException:
            temporary.unlink(missing_ok=True)
            raise
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise HTTPException(
                status_code=500, detail=f"Falha ao finalizar arquivo: {error}"
            ) from error
        return {
            "path": target.relative_to(paths.root).as_posix(),
            "size": size,
            "sha256": digest.hexdigest(),
        }

    @app.get("/api/v1/files/download")
    @app.get("/api/v2/workspaces/{workspace_id}/files/download")
    def file_download(relative: str, workspace_id: int | None = None) -> Any:
        require_workspace(workspace_id)
        try:
            target = _safe_workspace_path(paths, relative)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
        return FileResponse(target, filename=target.name)

    @app.post("/api/v1/connections/{connection_id}/upload")
    @app.post("/api/v2/workspaces/{workspace_id}/connections/{connection_id}/upload")
    async def remote_upload(
        connection_id: int, request: RemoteUploadRequest, workspace_id: int | None = None
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            result = await transfers.upload_async(
                connection_id,
                request.local_path,
                request.remote_path,
                overwrite=request.overwrite,
                conflict=request.conflict,
            )
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(result)

    @app.post("/api/v1/connections/{connection_id}/download")
    @app.post("/api/v2/workspaces/{workspace_id}/connections/{connection_id}/download")
    async def remote_download(
        connection_id: int, request: RemoteDownloadRequest, workspace_id: int | None = None
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            result = await transfers.download_async(
                connection_id,
                request.remote_path,
                request.local_path,
                overwrite=request.overwrite,
                conflict=request.conflict,
            )
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(result)

    @app.post("/api/v2/workspaces/{workspace_id}/transfers/upload/{connection_id}", status_code=202)
    def remote_upload_job(
        connection_id: int,
        request: RemoteUploadRequest,
        http_request: Request,
        workspace_id: int | None = None,
        idempotency_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        actor = actor_for(http_request)
        task = tasks.submit(
            TaskCreate(
                kind="file_upload",
                resource_type="connection",
                resource_id=connection_id,
                total_steps=1,
                idempotency_key=idempotency_key,
                idempotency_hash=operation_hash(
                    "file_upload",
                    connection_id,
                    actor,
                    request.model_dump(mode="json"),
                ),
                requested_by=actor,
            ),
            lambda _update: asdict(
                transfers.upload(
                    connection_id,
                    request.local_path,
                    request.remote_path,
                    overwrite=request.overwrite,
                    conflict=request.conflict,
                )
            ),
        )
        return {"job_id": task.id, "status": task.status.value}

    @app.post(
        "/api/v2/workspaces/{workspace_id}/transfers/download/{connection_id}",
        status_code=202,
    )
    def remote_download_job(
        connection_id: int,
        request: RemoteDownloadRequest,
        http_request: Request,
        workspace_id: int | None = None,
        idempotency_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        actor = actor_for(http_request)
        task = tasks.submit(
            TaskCreate(
                kind="file_download",
                resource_type="connection",
                resource_id=connection_id,
                total_steps=1,
                idempotency_key=idempotency_key,
                idempotency_hash=operation_hash(
                    "file_download",
                    connection_id,
                    actor,
                    request.model_dump(mode="json"),
                ),
                requested_by=actor,
            ),
            lambda _update: asdict(
                transfers.download(
                    connection_id,
                    request.remote_path,
                    request.local_path,
                    overwrite=request.overwrite,
                    conflict=request.conflict,
                )
            ),
        )
        return {"job_id": task.id, "status": task.status.value}

    @app.get("/api/v1/connections")
    @app.get("/api/v2/workspaces/{workspace_id}/connections")
    def connection_list(workspace_id: int | None = None) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        return [profile.model_dump(mode="json") for profile in workspace.connections.list()]

    @app.post("/api/v1/connections", status_code=201)
    @app.post("/api/v2/workspaces/{workspace_id}/connections", status_code=201)
    def connection_create(
        data: ConnectionProfileCreate, workspace_id: int | None = None
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            return connections.add(data).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.put("/api/v1/connections/{connection_id}")
    @app.put("/api/v2/workspaces/{workspace_id}/connections/{connection_id}")
    def connection_update(
        connection_id: int,
        data: ConnectionProfileUpdate,
        if_match: str | None = Header(default=None),
        workspace_id: int | None = None,
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        if if_match is None or not if_match.isdigit():
            raise HTTPException(status_code=428, detail="Informe a revisão em If-Match.")
        try:
            return connections.update(
                connection_id, data, expected_revision=int(if_match)
            ).model_dump(mode="json")
        except ValueError as error:
            if str(error) == "revision_conflict":
                raise HTTPException(
                    status_code=409, detail="O perfil foi alterado por outro operador."
                ) from error
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/connections/parse")
    @app.post("/api/v2/connections/parse")
    def connection_parse(payload: dict[str, str]) -> dict[str, Any]:
        try:
            parsed = connections.parse_ssh(payload.get("ssh", ""))
            jump_profile_ids, unresolved_jumps = connections.resolve_jump_profile_ids(parsed)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "host": parsed.host,
            "user": parsed.user,
            "port": parsed.port,
            "identity_file": parsed.identity_file,
            "known_hosts_file": parsed.known_hosts_file,
            "jump_targets": [asdict(item) for item in parsed.jump_targets],
            "jump_profile_ids": list(jump_profile_ids),
            "unresolved_jump_targets": [asdict(item) for item in unresolved_jumps],
        }

    @app.post("/api/v1/connections/{connection_id}/test")
    @app.post("/api/v2/workspaces/{workspace_id}/connections/{connection_id}/test")
    async def connection_test(
        connection_id: int,
        data: TemporaryCredentialRequest | None = None,
        workspace_id: int | None = None,
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        profile = connections.get(connection_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Conexão não encontrada.")
        try:
            await ssh.connect(
                connection_id,
                data.password if data else None,
                credential_kind=data.kind if data else None,
            )
        except Exception:
            current = connections.get(connection_id)
            result: dict[str, Any] = {
                "id": profile.id,
                "state": current.state.value if current is not None else "error",
                "error": (
                    current.last_error
                    if current is not None and current.last_error
                    else "A conexão SSH não pôde ser estabelecida."
                ),
            }
            pending_host_key = ssh.pending_host_key(connection_id)
            if pending_host_key is not None:
                result["host_key"] = {
                    key: value for key, value in pending_host_key.items() if key != "public_key"
                }
            return result
        current = connections.get(connection_id)
        return {
            "id": profile.id,
            "state": current.state.value if current is not None else "ready",
            "error": None,
        }

    @app.post("/api/v1/connections/{connection_id}/trust-host-key")
    @app.post("/api/v2/workspaces/{workspace_id}/connections/{connection_id}/trust-host-key")
    def connection_trust_host_key(
        connection_id: int,
        data: HostKeyTrustRequest,
        workspace_id: int | None = None,
    ) -> dict[str, str | int]:
        """Persist a host key only after matching the pending fingerprint."""

        require_workspace(workspace_id)
        try:
            return ssh.trust_host_key(connection_id, data.fingerprint)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v1/connections/{connection_id}/disconnect")
    @app.post("/api/v2/workspaces/{workspace_id}/connections/{connection_id}/disconnect")
    async def connection_disconnect(
        connection_id: int, workspace_id: int | None = None
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            await ssh.close(connection_id)
        except Exception as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"id": connection_id, "state": "disconnected"}

    @app.post("/api/v1/connections/{connection_id}/inspect", status_code=202)
    @app.post(
        "/api/v2/workspaces/{workspace_id}/connections/{connection_id}/inspect", status_code=202
    )
    async def connection_inspect(
        connection_id: int,
        request: Request,
        workspace_id: int | None = None,
        idempotency_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            connections.get(connection_id)
            actor = actor_for(request)
            task_data = TaskCreate(
                kind="remote_inspection",
                resource_type="connection",
                resource_id=connection_id,
                total_steps=len(inspection.COMMANDS),
                idempotency_key=idempotency_key,
                idempotency_hash=operation_hash(
                    "remote_inspection",
                    connection_id,
                    actor,
                    {"commands": list(inspection.COMMANDS)},
                ),
                requested_by=actor,
            )
            if inspection.async_available:
                task = tasks.submit_async(
                    task_data,
                    lambda update: _inspect_async_task(inspection, connection_id, update),
                )
            else:
                task = tasks.submit(
                    task_data,
                    lambda update: _inspect_task(inspection, connection_id, update),
                )
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"job_id": task.id, "status": task.status.value}

    @app.get("/api/v1/terminals")
    @app.get("/api/v2/workspaces/{workspace_id}/terminals")
    def terminal_list(workspace_id: int | None = None) -> list[dict[str, Any]]:
        require_workspace(workspace_id)
        result = []
        for terminal in terminals.list():
            payload = terminal.model_dump(mode="json")
            available = terminals.runtime_available(terminal.id)
            payload["runtime_available"] = available
            payload["reconnectable"] = not available and terminal.connection_id is not None
            if available:
                payload["availability_reason"] = None
            elif terminal.status.value == "closed":
                payload["availability_reason"] = "Terminal encerrado pelo operador."
            elif terminal.status.value == "exited":
                payload["availability_reason"] = "A sessão terminou ou o motor foi reiniciado."
            else:
                payload["availability_reason"] = "A sessão não está presente no motor atual."
            result.append(payload)
        return result

    @app.post("/api/v1/terminals", status_code=201)
    @app.post("/api/v2/workspaces/{workspace_id}/terminals", status_code=201)
    async def terminal_create(
        data: TerminalCreate, request: Request, workspace_id: int | None = None
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            principal = principal_for(request)
            owner = principal.subject if principal is not None else None
            return (await terminals.start_async(data, owner_subject=owner)).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/terminals/{terminal_id}")
    @app.get("/api/v2/workspaces/{workspace_id}/terminals/{terminal_id}")
    def terminal_get(terminal_id: int, workspace_id: int | None = None) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            terminal = terminals.get(terminal_id)
            payload = terminal.model_dump(mode="json")
            payload["runtime_available"] = terminals.runtime_available(terminal.id)
            payload["reconnectable"] = (
                not payload["runtime_available"] and terminal.connection_id is not None
            )
            payload["availability_reason"] = (
                None
                if payload["runtime_available"]
                else "A sessão não está presente no motor atual."
            )
            return payload
        except Exception as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.delete("/api/v1/terminals/{terminal_id}")
    @app.delete("/api/v2/workspaces/{workspace_id}/terminals/{terminal_id}")
    def terminal_close(terminal_id: int, workspace_id: int | None = None) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            return terminals.close(terminal_id).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/v1/terminals/{terminal_id}/share")
    @app.post("/api/v2/workspaces/{workspace_id}/terminals/{terminal_id}/share")
    def terminal_share(
        data: TerminalShareRequest,
        terminal_id: int,
        request: Request,
        workspace_id: int | None = None,
    ) -> dict[str, Any]:
        """Publish only the selected terminal; observer access stays read-only."""

        require_workspace(workspace_id)
        principal = principal_for(request)
        role = (
            workspace_role(principal, workspace_id)
            if membership_required
            else (str(principal.role) if principal is not None else "admin")
        )
        if role == "observer":
            raise HTTPException(status_code=403, detail="Observadores não compartilham terminais.")
        try:
            return terminals.set_sharing(terminal_id, data.shared).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.patch("/api/v1/terminals/{terminal_id}/rename")
    @app.patch("/api/v2/workspaces/{workspace_id}/terminals/{terminal_id}/rename")
    def terminal_rename(
        data: TerminalRenameRequest,
        terminal_id: int,
        workspace_id: int | None = None,
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            return terminals.rename(terminal_id, data.name).model_dump(mode="json")
        except ValueError as error:
            message = str(error)
            status = 409 if "outro terminal" in message else 404
            raise HTTPException(status_code=status, detail=message) from error

    @app.websocket("/api/v1/terminals/{terminal_id}/stream")
    @app.websocket("/api/v2/workspaces/{workspace_id}/terminals/{terminal_id}/stream")
    async def terminal_stream(
        websocket: WebSocket, terminal_id: int, workspace_id: int | None = None
    ) -> None:
        if workspace_id is not None and workspace_id != workspace.lab.id:
            await websocket.close(code=4404)
            return
        header = websocket.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip() if header else None
        protocols = [
            item.strip()
            for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if item.strip()
        ]
        if token is None and len(protocols) >= 2 and protocols[0] == "ctfws":
            token = protocols[1]
        principal = auth.authenticate(token)
        if token is not None and principal is None:
            await websocket.close(code=4401)
            return
        if auth.required and principal is None:
            await websocket.close(code=4401)
            return
        try:
            role = (
                workspace_role(principal, workspace_id)
                if membership_required
                else (str(principal.role) if principal is not None else "admin")
            )
        except HTTPException as error:
            await websocket.close(code=4404 if error.status_code == 404 else 4403)
            return
        origin = websocket.headers.get("origin")
        if origin and urlparse(origin).netloc != websocket.headers.get("host"):
            await websocket.close(code=4403)
            return
        try:
            terminal = terminals.get(terminal_id)
        except Exception:
            await websocket.close(code=4404)
            return
        if not terminals.runtime_available(terminal_id):
            await websocket.accept(subprotocol="ctfws" if protocols[:1] == ["ctfws"] else None)
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "terminal_not_active",
                    "message": (
                        terminal.availability_reason
                        or "Este terminal não possui uma sessão ativa no motor."
                    ),
                    "reconnectable": terminal.connection_id is not None,
                    "connection_id": terminal.connection_id,
                }
            )
            await websocket.close(code=4409, reason="terminal_not_active")
            return
        if role == "observer" and terminal.sharing.value != "shared":
            await websocket.close(code=4403)
            return
        await websocket.accept(subprotocol="ctfws" if protocols[:1] == ["ctfws"] else None)
        raw_sequence = websocket.query_params.get("after_sequence", "0")
        after_sequence = int(raw_sequence) if raw_sequence.isdigit() else 0
        read_only = websocket.query_params.get("readonly", "0") == "1"
        try:
            viewer_token = terminals.subscribe(terminal_id, after_sequence=after_sequence)
        except EntityNotFoundError:
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "terminal_not_active",
                    "message": "A sessão deixou de existir no motor antes da visualização iniciar.",
                    "reconnectable": terminal.connection_id is not None,
                    "connection_id": terminal.connection_id,
                }
            )
            await websocket.close(code=4409, reason="terminal_not_active")
            return
        has_control = False
        if role != "observer" and not read_only:
            has_control = terminals.acquire_control(terminal_id, viewer_token)
        if workspace_id is not None:
            await websocket.send_json(
                {
                    "type": "ready",
                    "control": has_control,
                    "readonly": read_only or role == "observer",
                }
            )
        receive_task: asyncio.Task[Any] | None = None
        output_task: asyncio.Task[Any] | None = None
        try:
            receive_task = asyncio.create_task(websocket.receive())
            output_task = asyncio.create_task(
                asyncio.to_thread(terminals.read_subscriber_frame, terminal_id, viewer_token, 0.25)
            )
            while True:
                done, _ = await asyncio.wait(
                    {receive_task, output_task},
                    timeout=0.5,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not live_session_allowed(token, workspace_id):
                    await websocket.close(code=4401)
                    return
                try:
                    principal = auth.authenticate(token)
                    role = (
                        workspace_role(principal, workspace_id)
                        if membership_required
                        else (str(principal.role) if principal is not None else "admin")
                    )
                    current = terminals.get(terminal_id)
                except Exception:
                    await websocket.close(code=4401)
                    return
                if role == "observer" and current.sharing.value != "shared":
                    await websocket.close(code=4403)
                    return
                if not done:
                    continue
                if receive_task in done:
                    message = receive_task.result()
                    if message.get("type") == "websocket.disconnect":
                        return
                    raw = message.get("bytes")
                    text = message.get("text")
                    if raw is None and text is not None:
                        if text.startswith("{"):
                            try:
                                payload = json.loads(text)
                                if not isinstance(payload, dict):
                                    raise ValueError("Mensagem de terminal inválida.")
                                if payload.get("action") == "resize":
                                    if (
                                        role == "observer"
                                        or read_only
                                        or not terminals.has_control(terminal_id, viewer_token)
                                    ):
                                        await websocket.send_json(
                                            {"type": "error", "code": "control_held"}
                                        )
                                    else:
                                        terminals.resize(
                                            terminal_id,
                                            int(payload.get("columns", 80)),
                                            int(payload.get("rows", 24)),
                                        )
                                    text = ""
                                elif payload.get("action") == "control":
                                    acquired = (
                                        role != "observer"
                                        and not read_only
                                        and terminals.acquire_control(terminal_id, viewer_token)
                                    )
                                    if not acquired:
                                        await websocket.send_json(
                                            {"type": "error", "code": "control_held"}
                                        )
                                    else:
                                        await websocket.send_json(
                                            {"type": "control", "granted": True}
                                        )
                                    text = ""
                                else:
                                    text = str(payload.get("data", ""))
                            except json.JSONDecodeError:
                                # A malformed JSON-looking payload is treated as
                                # terminal input, preserving normal shell behavior.
                                pass
                            except (TypeError, ValueError, RuntimeError, OSError) as error:
                                await websocket.send_json(
                                    {
                                        "type": "error",
                                        "code": "terminal_operation_failed",
                                        "message": str(error)[:500],
                                    }
                                )
                                text = ""
                        raw = text.encode("utf-8")
                    if raw and role != "observer" and not read_only:
                        try:
                            await terminals.write_async(terminal_id, raw, owner=viewer_token)
                        except PermissionError:
                            await websocket.send_json({"type": "error", "code": "control_held"})
                        except Exception as error:
                            await websocket.send_json(
                                {
                                    "type": "error",
                                    "code": "terminal_input_failed",
                                    "message": str(error)[:500],
                                }
                            )
                    receive_task = asyncio.create_task(websocket.receive())
                if output_task in done:
                    frame = output_task.result()
                    if frame:
                        if workspace_id is not None:
                            await websocket.send_json(
                                {
                                    "type": "output",
                                    "sequence": frame.sequence,
                                    "gap": frame.gap,
                                }
                            )
                        await websocket.send_bytes(frame.data)
                    current = terminals.get(terminal_id)
                    if current.status.value in {
                        "exited",
                        "error",
                        "closed",
                    } and terminals.is_drained(terminal_id):
                        await websocket.send_json({"type": "exit", "code": current.exit_code})
                        return
                    output_task = asyncio.create_task(
                        asyncio.to_thread(
                            terminals.read_subscriber_frame, terminal_id, viewer_token, 0.25
                        )
                    )
        except (WebSocketDisconnect, RuntimeError, OSError, ValueError):
            return
        finally:
            try:
                terminals.unsubscribe(terminal_id, viewer_token)
            except Exception:
                pass
            for task in (receive_task, output_task):
                if task is not None:
                    task.cancel()

    @app.post("/api/v1/forwards/{forward_id}/start")
    @app.post("/api/v2/workspaces/{workspace_id}/tunnels/{forward_id}/start")
    async def forward_start(
        forward_id: int,
        confirmation: OperationConfirmation | None = None,
        workspace_id: int | None = None,
    ) -> dict[str, Any]:
        require_workspace(workspace_id)
        require_confirmation(confirmation, "iniciar o túnel")
        try:
            return (await forward_processes.start_async(forward_id)).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/forwards/{forward_id}/check")
    @app.post("/api/v2/workspaces/{workspace_id}/tunnels/{forward_id}/check")
    async def forward_check(forward_id: int, workspace_id: int | None = None) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            return (await forward_processes.check_async(forward_id)).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/v1/forwards/{forward_id}/stop")
    @app.post("/api/v2/workspaces/{workspace_id}/tunnels/{forward_id}/stop")
    async def forward_stop(forward_id: int, workspace_id: int | None = None) -> dict[str, Any]:
        require_workspace(workspace_id)
        try:
            return (await forward_processes.stop_async(forward_id)).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    return app


def _summary(workspace: WorkspaceService, terminals: TerminalManager) -> dict[str, int]:
    sessions = workspace.sessions.list()
    terminal_rows = terminals.list()
    return {
        "hosts": len(workspace.hosts.list()),
        "connections": len(workspace.connections.list()),
        "terminals": len(terminal_rows),
        "active_terminals": sum(row.status.value == "active" for row in terminal_rows),
        "sessions": len(sessions),
        "active_sessions": sum(
            session.status.value in {"active", "degraded"} for session in sessions
        ),
        "paths": len(workspace.access_paths.list()),
        "forwards": len(workspace.forwards.list()),
    }


def _safe_workspace_path(paths: WorkspacePaths, relative: str) -> Path:
    """Resolve a workspace-relative path without allowing traversal."""

    candidate = (paths.root / relative).resolve()
    root = paths.root.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("O caminho precisa permanecer dentro do workspace.")
    return candidate


def _inspect_task(
    inspection: RemoteInspectionService,
    connection_id: int,
    update: Any,
) -> dict[str, object]:
    """Adapt inspection progress to the durable task contract."""

    def progress(completed_steps: int, current_step: str) -> None:
        update(
            TaskProgress(
                completed_steps=completed_steps,
                total_steps=len(inspection.COMMANDS),
                current_step=current_step,
            )
        )

    result = inspection.inspect(connection_id, progress)
    return {
        "host_id": result.host_id,
        "host_name": result.host_name,
        "networks": result.networks,
        "services": result.services,
        "connections": result.connections,
        "completed_steps": result.completed_steps,
        "failed_steps": list(result.failed_steps),
        "snapshot_id": result.snapshot_id,
        "collection_id": result.collection_id,
    }


def _context_execution_task(
    contexts: NetworkContextService,
    context_id: int,
    request: ContextExecuteRequest,
    update: Any,
) -> dict[str, object]:
    """Run a contextual launcher as one durable task step."""

    update(TaskProgress(completed_steps=0, total_steps=1, current_step="executando ferramenta"))
    result = contexts.execute_launcher(
        context_id,
        request.program,
        request.arguments,
        launcher=request.launcher,
        timeout_seconds=request.timeout_seconds,
    )
    update(TaskProgress(completed_steps=1, total_steps=1, current_step="ferramenta concluída"))
    return result


async def _inspect_async_task(
    inspection: RemoteInspectionService,
    connection_id: int,
    update: Any,
) -> dict[str, object]:
    """Adapt shared AsyncSSH inspection progress to the durable task contract."""

    def progress(completed_steps: int, current_step: str) -> None:
        update(
            TaskProgress(
                completed_steps=completed_steps,
                total_steps=len(inspection.COMMANDS),
                current_step=current_step,
            )
        )

    result = await inspection.inspect_async(connection_id, progress)
    return {
        "host_id": result.host_id,
        "host_name": result.host_name,
        "networks": result.networks,
        "services": result.services,
        "connections": result.connections,
        "completed_steps": result.completed_steps,
        "failed_steps": list(result.failed_steps),
        "snapshot_id": result.snapshot_id,
        "collection_id": result.collection_id,
    }


def _dashboard_html(lab_name: str) -> str:
    """Return a self-contained operator dashboard with no CDN dependency."""

    safe_name = (
        lab_name.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    html = r"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Conduit — __LAB_NAME__</title>
<style>
:root{--bg:#0b1120;--panel:#111c31;--line:#263653;--text:#e5eefc;--muted:#8ea3c0;--accent:#60a5fa;--good:#34d399}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(140deg,#08101e,#101b31 60%,#13213b);color:var(--text);font:14px system-ui,-apple-system,Segoe UI,sans-serif}
header{height:64px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 24px;background:#0c1527cc}
.brand{font-weight:700;font-size:18px}.layout{display:grid;grid-template-columns:230px 1fr;min-height:calc(100vh - 64px)}aside{border-right:1px solid var(--line);padding:18px 12px}main{padding:26px;max-width:1400px;width:100%;margin:auto}
.nav{display:flex;flex-direction:column;gap:6px}.nav button,.ghost{background:transparent;border:0;color:var(--muted);text-align:left;padding:11px 12px;border-radius:8px;cursor:pointer;font:inherit}.nav button:hover,.nav button.active{background:#1a2b48;color:var(--text)}
.hero{display:flex;justify-content:space-between;gap:16px;margin-bottom:22px}h1{font-size:28px;margin:0 0 6px}h2{font-size:16px;margin:0 0 14px}p{color:var(--muted);margin:0}.actions{display:flex;gap:10px;flex-wrap:wrap}
button.primary{border:0;color:#07111f;font-weight:700;border-radius:8px;padding:10px 14px;cursor:pointer;background:var(--accent)}.metrics{display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:12px;margin-bottom:22px}.card{background:linear-gradient(145deg,var(--panel),#0f1a2d);border:1px solid var(--line);border-radius:12px;padding:16px;box-shadow:0 10px 30px #0002}.metric{font-size:28px;font-weight:700;margin-top:6px}.label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.wide{grid-column:1/-1}table{width:100%;border-collapse:collapse}th,td{padding:11px 8px;border-bottom:1px solid var(--line);text-align:left}th{color:var(--muted);font-size:12px;text-transform:uppercase}.status{color:var(--good)}.muted{color:var(--muted)}.empty{padding:20px;color:var(--muted);text-align:center}
.form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}input{width:100%;background:#0b1425;border:1px solid var(--line);border-radius:7px;padding:10px;color:var(--text);font:inherit}label{color:var(--muted);font-size:12px}label span{display:block;margin-bottom:5px}.terminal{background:#050912;border:1px solid #263653;border-radius:10px;overflow:hidden}.term-head{display:flex;justify-content:space-between;padding:9px 12px;background:#0e192b;color:var(--muted)}#terminal-output{height:340px;overflow:auto;padding:14px;white-space:pre-wrap;font:13px ui-monospace,Consolas,monospace;color:#c7f9dd}#terminal-input{border:0;border-top:1px solid #263653;border-radius:0;background:#050912;font:13px ui-monospace,Consolas,monospace}
@media(max-width:900px){.layout{grid-template-columns:1fr}aside{display:none}.metrics{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}main{padding:16px}}
</style></head><body>
<header><div class="brand">⌁ Conduit</div><div class="muted">__LAB_NAME__ · <span id="health">conectando…</span></div></header>
<div class="layout"><aside><nav class="nav"><button class="active">◈ Visão geral</button><button onclick="openConnection()">⇄ Conexões</button><button onclick="inspectPrompt()">⌁ Inspecionar SSH</button><button onclick="openLocal()">▣ Novo terminal local</button><button onclick="openTunnelPrompt()">⌁ Tunnelar destino</button><button onclick="openFiles()">▧ Arquivos</button><button onclick="refresh()">◷ Atualizar</button></nav></aside>
<main><section class="hero"><div><h1>Visão geral</h1><p>Seu ambiente de infraestrutura em um só lugar.</p></div><div class="actions"><button class="primary" onclick="openConnection()">+ Conectar máquina</button><button class="primary" onclick="openTunnelPrompt()">⌁ Tunnelar</button></div></section>
<section class="metrics" id="metrics"></section><section class="grid" id="content"></section>
<section class="card" id="connection-form" hidden><h2>Adicionar conexão SSH</h2><p style="margin-bottom:14px">Cole o comando SSH. Senhas não são armazenadas pelo workspace.</p><div class="form"><label><span>Nome</span><input id="c-name" placeholder="jump-01"></label><label><span>Comando SSH</span><input id="c-ssh" placeholder="ssh kali@10.0.0.5 -p 22"></label></div><div class="actions" style="margin-top:14px"><button class="primary" onclick="saveConnection()">Salvar conexão</button><button class="ghost" onclick="closeConnection()">Cancelar</button></div><div id="form-error" class="muted" style="margin-top:10px"></div></section>
<section class="card" id="terminal-panel" hidden style="margin-top:18px"><div class="term-head"><span id="term-label">Terminal</span><button class="ghost" onclick="closeTerminal()">Fechar</button></div><div class="terminal"><div id="terminal-output"></div><input id="terminal-input" placeholder="Digite e pressione Enter…"></div></section>
</main></div>
<script>
const api='/api/v1';let ws=null;let currentTerminal=null;const notify=s=>document.querySelector('#health').textContent=String(s);
const esc=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
async function get(path,opts){const r=await fetch(api+path,opts);if(!r.ok)throw new Error((await r.json().catch(()=>({detail:r.statusText}))).detail||r.statusText);return r.json()}
async function refresh(){try{const[sum,hosts,cons,terms,paths]=await Promise.all([get('/summary'),fetch('/api/hosts').then(r=>r.json()),get('/connections'),get('/terminals'),fetch('/api/paths').then(r=>r.json())]);document.querySelector('#health').textContent='motor online';renderMetrics(sum);renderOverview(hosts,cons,terms,paths)}catch(e){document.querySelector('#health').textContent='erro: '+e.message}}
function renderMetrics(s){document.querySelector('#metrics').innerHTML=Object.entries({hosts:s.hosts,conexões:s.connections,'terminais ativos':s.active_terminals,forwards:s.forwards}).map(([k,v])=>`<div class="card"><div class="label">${esc(k)}</div><div class="metric">${v}</div></div>`).join('')}
function renderOverview(hosts,cons,terms,paths){document.querySelector('#content').innerHTML=`<section class="card"><h2>Conexões salvas</h2>${cons.length?`<table><tr><th>Nome</th><th>Destino</th><th>Ações</th></tr>${cons.map(c=>`<tr><td>${esc(c.name)}</td><td>${esc(c.user+'@'+c.host+':'+c.port)}</td><td><button class="primary" onclick="openSSH(${c.id})">Abrir terminal</button></td></tr>`).join('')}</table>`:'<div class="empty">Adicione sua primeira conexão SSH.</div>'}</section><section class="card"><h2>Terminais</h2>${terms.length?`<table><tr><th>Contexto</th><th>Estado</th><th></th></tr>${terms.map(t=>`<tr><td>${esc(t.context_label)}</td><td class="status">${esc(t.status)}</td><td><button class="ghost" onclick="attach(${t.id})">Abrir</button></td></tr>`).join('')}</table>`:'<div class="empty">Nenhum terminal gerenciado.</div>'}</section><section class="card wide"><h2>Caminhos de acesso</h2>${paths.length?`<table><tr><th>Destino</th><th>Saltos</th><th>Estado</th><th>Confiança</th></tr>${paths.slice(0,12).map(p=>`<tr><td>${esc(p.target_address)}${p.target_port?':'+p.target_port:''}</td><td>${esc((p.hop_host_ids||[]).join(' → '))}</td><td>${esc(p.state)}</td><td>${p.confidence}%</td></tr>`).join('')}</table>`:'<div class="empty">Importe observações para construir caminhos explicáveis.</div>'}</section>`}
function openConnection(){document.querySelector('#connection-form').hidden=false;document.querySelector('#c-name').focus()}function closeConnection(){document.querySelector('#connection-form').hidden=true}
async function saveConnection(){const err=document.querySelector('#form-error');err.textContent='';try{const p=await get('/connections/parse',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ssh:document.querySelector('#c-ssh').value})});await get('/connections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:document.querySelector('#c-name').value,host:p.host,user:p.user,port:p.port,identity_file:p.identity_file,known_hosts_file:p.known_hosts_file})});closeConnection();await refresh()}catch(e){err.textContent=e.message}}
async function openSSH(id){try{const t=await get('/terminals',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'ssh-'+id+'-'+Date.now(),kind:'ssh',connection_id:id})});attach(t.id)}catch(e){notify(e.message)}}async function openLocal(){try{const t=await get('/terminals',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'local-'+Date.now(),kind:'local'})});attach(t.id)}catch(e){notify(e.message)}}
async function attach(id){try{const t=await get('/terminals/'+id);currentTerminal=id;document.querySelector('#terminal-panel').hidden=false;document.querySelector('#term-label').textContent=t.context_label;document.querySelector('#terminal-output').textContent='';if(ws)ws.close();ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+api+'/terminals/'+id+'/stream');ws.binaryType='arraybuffer';ws.onmessage=e=>{if(typeof e.data==='string')return;const o=document.querySelector('#terminal-output');o.textContent+=new TextDecoder().decode(e.data);o.scrollTop=o.scrollHeight};ws.onclose=()=>{document.querySelector('#terminal-output').textContent+='\n[conexão encerrada]\n'}}catch(e){notify(e.message)}}
function openTunnelPrompt(){notify('O assistente de túneis está disponível na interface React compilada. Recarregue a página ou execute o frontend de desenvolvimento.')}
async function openFiles(){try{const files=await get('/files?relative=loot/inbox');document.querySelector('#content').innerHTML=`<section class="card wide"><h2>Arquivos do workspace</h2><p style="margin-bottom:14px">Envie ferramentas e evidências para a Kali. Cada arquivo recebe SHA-256.</p><div class="actions"><input id="file-pick" type="file"><button class="primary" onclick="uploadFile()">Enviar arquivo</button></div><table style="margin-top:16px"><tr><th>Arquivo</th><th>Tamanho</th><th>SHA</th></tr>${files.map(f=>`<tr><td>${esc(f.name)}</td><td>${f.size??'-'}</td><td class="muted">disponível após upload</td></tr>`).join('')}</table></section>`}catch(e){notify(e.message)}}
function inspectPrompt(){notify('A inspeção guiada está disponível na interface React compilada. Recarregue a página para abrir o assistente.')}
async function uploadFile(){const input=document.querySelector('#file-pick');if(!input.files.length)return;const data=new FormData();data.append('file',input.files[0]);const response=await fetch(api+'/files/upload?destination=loot/inbox',{method:'POST',body:data});if(!response.ok){notify((await response.json()).detail);return}notify('Arquivo enviado com SHA-256: '+(await response.json()).sha256);openFiles()}
function closeTerminal(){if(ws)ws.close();document.querySelector('#terminal-panel').hidden=true;currentTerminal=null;refresh()}document.querySelector('#terminal-input').addEventListener('keydown',e=>{if(e.key==='Enter'&&ws&&e.target.value){ws.send(e.target.value+'\n');e.target.value=''}});refresh();
</script></body></html>"""
    return html.replace("__LAB_NAME__", safe_name)


def workspace_from_root(root: Path) -> WorkspacePaths:
    """Resolve a root for embedders that start from a filesystem path."""

    return WorkspacePaths.from_value(root)
