"""Command-line interface for Conduit V0.28."""

from __future__ import annotations

import json
import os
import sys
from ipaddress import ip_address
from pathlib import Path
from shlex import join as shell_join
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from ctfws import __version__
from ctfws.agent import collect
from ctfws.core.errors import CTFWSError, EntityNotFoundError, WorkspaceNotFoundError
from ctfws.core.logging import configure_logging
from ctfws.core.paths import WorkspacePaths, discover_labs, slugify
from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.database.repositories import ObservationRepository, PivotRepository
from ctfws.events import Event
from ctfws.models.connection import ConnectionProfileCreate
from ctfws.models.context import ContextTransport, NetworkContextCreate
from ctfws.models.evidence import EvidenceCreate
from ctfws.models.forward import ForwardCreate, ForwardKind, ForwardStatus
from ctfws.models.host import HostCreate, HostStatus
from ctfws.models.lab import LabCreate
from ctfws.models.note import NoteCreate, NoteEntityType
from ctfws.models.session import SessionCreate, SessionStatus, SessionTransport
from ctfws.models.shell import ShellCreate, ShellStatus, ShellType
from ctfws.models.snapshot import SnapshotCreate
from ctfws.pivot.tools import detect_pivot_tools
from ctfws.reports.generator import ReportService
from ctfws.services.access_paths import AccessPathService
from ctfws.services.backups import WorkspaceBackupService
from ctfws.services.connections import ConnectionService
from ctfws.services.contexts import NetworkContextService
from ctfws.services.doctor import WorkspaceDoctor
from ctfws.services.enumeration import EnumerationService, ImportResult
from ctfws.services.intelligence import IntelligenceService
from ctfws.services.pivot import PivotService
from ctfws.services.processes import ForwardProcessService
from ctfws.services.resume import ResumeService
from ctfws.services.search import DiffService, SearchService
from ctfws.services.sessions import SessionService
from ctfws.services.shells import ShellService
from ctfws.services.topology import TopologyService
from ctfws.services.workspace import WorkspaceService, create_lab
from ctfws.shell.helper import detect_tools, suggestions
from ctfws.shell.tmux import TmuxManager

console = Console()
app = typer.Typer(
    help="Conduit: organize authorized infrastructure and lab work.",
    invoke_without_command=True,
)
lab_app = typer.Typer(help="Create and inspect lab workspaces.")
host_app = typer.Typer(help="Manage hosts in the current workspace.")
note_app = typer.Typer(help="Manage notes attached to workspace entities.")
event_app = typer.Typer(help="Inspect the workspace timeline.")
import_app = typer.Typer(help="Import operator-provided command output.")
network_app = typer.Typer(help="Inspect discovered networks.")
service_app = typer.Typer(help="Inspect discovered local services.")
connection_app = typer.Typer(help="Inspect observed connections.")
pivot_app = typer.Typer(help="Review possible pivot relationships.")
shell_app = typer.Typer(help="Register and organize existing shells.")
tmux_app = typer.Typer(help="Plan and manage a local tmux workspace.")
forward_app = typer.Typer(help="Manage explicit port-forward plans.")
evidence_app = typer.Typer(help="Register evidence metadata.")
enum_app = typer.Typer(help="Run or import explicit quick enumeration.")
command_app = typer.Typer(help="Inspect recorded commands and outputs.")
identity_app = typer.Typer(help="Synchronize host aliases from imported evidence.")
relationship_app = typer.Typer(help="Rebuild observed host relationships.")
profile_app = typer.Typer(help="Manage saved SSH connection profiles.")
path_app = typer.Typer(help="Infer and inspect explainable access paths.")
context_app = typer.Typer(help="Plan isolated SOCKS and routed network contexts.")
session_app = typer.Typer(help="Register and health-check operator sessions.")
snapshot_app = typer.Typer(help="Create named observation checkpoints.")
source_app = typer.Typer(help="Inspect observation provenance records.")
app.add_typer(lab_app, name="lab")
app.add_typer(host_app, name="host")
app.add_typer(note_app, name="note")
app.add_typer(event_app, name="event")
app.add_typer(import_app, name="import")
app.add_typer(network_app, name="network")
app.add_typer(service_app, name="service")
app.add_typer(connection_app, name="connection")
app.add_typer(pivot_app, name="pivot")
app.add_typer(shell_app, name="shell")
app.add_typer(tmux_app, name="tmux")
app.add_typer(forward_app, name="forward")
app.add_typer(evidence_app, name="evidence")
app.add_typer(enum_app, name="enum")
app.add_typer(command_app, name="command")
app.add_typer(identity_app, name="identity")
app.add_typer(relationship_app, name="relationship")
app.add_typer(profile_app, name="profile")
app.add_typer(path_app, name="path")
app.add_typer(context_app, name="context")
app.add_typer(session_app, name="session")
app.add_typer(snapshot_app, name="snapshot")
app.add_typer(source_app, name="source")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    workspace: Annotated[
        Path | None,
        typer.Option(
            "--workspace",
            "-w",
            help="Pasta do laboratório ou caminho para " "workspace.db.",
        ),
    ] = None,
    version: Annotated[bool, typer.Option("--version", help="Mostrar a versão.")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="Ativar logs detalhados.")] = False,
) -> None:
    """Global options shared by all commands."""

    ctx.ensure_object(dict)
    ctx.obj["workspace"] = workspace
    ctx.obj["debug"] = debug
    if version:
        console.print(f"Conduit {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        try:
            paths = WorkspacePaths.from_value(workspace)
        except CTFWSError:
            console.print(ctx.get_help())
            raise typer.Exit() from None
        from ctfws.tui.app import run_tui

        run_tui(paths)


@app.command("tui")
def tui(ctx: typer.Context) -> None:
    """Open the dashboard explicitly."""

    try:
        paths = WorkspacePaths.from_value(ctx.obj.get("workspace"))
    except Exception as error:
        _handle_error(error)
        return
    from ctfws.tui.app import run_tui

    run_tui(paths)


def _service(ctx: typer.Context) -> WorkspaceService:
    paths = WorkspacePaths.from_value(ctx.obj.get("workspace"))
    configure_logging(paths.log_file, bool(ctx.obj.get("debug", False)))
    return WorkspaceService(paths)


def _resolve_start_workspace(value: Path | None) -> WorkspacePaths:
    """Resolve the workspace used by the one-command Conduit launcher."""

    if value is not None:
        return WorkspacePaths.from_value(value)

    current = Path.cwd()
    if (current / "workspace.db").is_file():
        return WorkspacePaths.from_value(current)

    configured = os.getenv("CONDUIT_WORKSPACE", "").strip()
    if configured:
        return WorkspacePaths.from_value(Path(configured))

    candidates: list[Path] = []
    known_paths = [
        Path.home() / ".config" / "conduit" / "workspace",
        Path.home() / ".local" / "share" / "conduit" / "workspace",
    ]
    if os.name != "nt":
        known_paths.append(Path("/var/lib/ctfws/workspace"))
    for candidate in known_paths:
        if (candidate / "workspace.db").is_file():
            candidates.append(candidate.resolve())

    for base_dir in (Path.home() / "ctf", Path.home() / "conduit"):
        candidates.extend(item.root for item in discover_labs(base_dir))

    unique = sorted({candidate.resolve() for candidate in candidates}, key=str)
    if len(unique) == 1:
        return WorkspacePaths.from_value(unique[0])
    if len(unique) > 1:
        locations = "\n".join(f"  - {item}" for item in unique)
        raise ValueError(
            "Mais de um workspace foi encontrado. Execute no diretório desejado ou use "
            f"--workspace:\n{locations}"
        )
    raise WorkspaceNotFoundError(
        "Nenhum workspace foi encontrado. Execute 'conduit lab create NOME' ou "
        "informe --workspace."
    )


def _handle_error(error: Exception) -> None:
    if isinstance(error, CTFWSError):
        console.print(f"[red]Erro:[/red] {error}")
        raise typer.Exit(code=1)
    if isinstance(error, ValueError):
        console.print(f"[red]Entrada inválida:[/red] {error}")
        raise typer.Exit(code=2)
    raise error


def _read_input(input_file: Path | None) -> str:
    """Read import text from a file or stdin; never execute the text."""

    if input_file is not None:
        try:
            text = input_file.expanduser().read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError(f"Não foi possível ler {input_file}: {error}") from error
    else:
        if sys.stdin.isatty():
            console.print("Cole o output e finalize com EOF (Ctrl+D/Ctrl+Z).")
        text = sys.stdin.read()
    if not text.strip():
        raise ValueError("Nenhum output foi fornecido.")
    return text


def _show_import_result(result: ImportResult) -> None:
    details = ", ".join(f"{key}={value}" for key, value in result.details.items())
    console.print(f"[green]Importado:[/green] {result.parser} ({details})")


def _host_id_filter(workspace: WorkspaceService, identifier: str | None) -> int | None:
    """Resolve an optional host filter and fail loudly for an unknown host."""

    if identifier is None:
        return None
    host = workspace.hosts.get(identifier)
    if host is None:
        raise EntityNotFoundError(f"Host {identifier} não encontrado neste laboratório.")
    return host.id


def _snapshot_id_filter(workspace: WorkspaceService, snapshot_id: int | None) -> int | None:
    """Validate an optional snapshot reference for an import operation."""

    if snapshot_id is None:
        return None
    if not any(snapshot.id == snapshot_id for snapshot in workspace.snapshots.list()):
        raise EntityNotFoundError(f"Snapshot {snapshot_id} não encontrado neste laboratório.")
    return snapshot_id


@lab_app.command("create")
def lab_create(
    name: Annotated[str, typer.Argument(help="Nome do laboratório.")],
    base_dir: Annotated[
        Path,
        typer.Option("--base-dir", "-b", help="Diretório que conterá a pasta do laboratório."),
    ] = Path("~/ctf"),
    platform: Annotated[str, typer.Option(help="Plataforma do laboratório.")] = "unknown",
) -> None:
    """Create an isolated lab directory and its SQLite database."""

    root = base_dir.expanduser() / slugify(name)
    if root.exists() and any(root.iterdir()):
        console.print(f"[red]Erro:[/red] o diretório {root} já existe e não está vazio.")
        raise typer.Exit(code=1)
    try:
        paths, lab = create_lab(root, LabCreate(name=name, platform=platform))
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Lab criado:[/green] {lab.name}")
    console.print(f"Workspace: {paths.root}")


@lab_app.command("list")
def lab_list(
    base_dir: Annotated[Path, typer.Option("--base-dir", "-b")] = Path("~/ctf"),
) -> None:
    """List labs found below a base directory."""

    from ctfws.database.db import Database
    from ctfws.database.repositories import LabRepository

    table = Table("NAME", "PLATFORM", "STATUS", "PATH")
    for paths in discover_labs(base_dir):
        lab = LabRepository(Database(paths.database)).get()
        if lab:
            table.add_row(lab.name, lab.platform, lab.status, str(paths.root))
    console.print(table)


@host_app.command("add")
def host_add(
    ctx: typer.Context,
    ip: Annotated[str, typer.Argument(help="Endereço IP do host.")],
    name: Annotated[str | None, typer.Option("--name", "-n")] = None,
    hostname: Annotated[str | None, typer.Option("--hostname")] = None,
    os_name: Annotated[str | None, typer.Option("--os")] = None,
    user: Annotated[str | None, typer.Option("--user")] = None,
    network_scope: Annotated[
        str, typer.Option("--network-scope", help="Escopo de rede deste host.")
    ] = "default",
    status: Annotated[HostStatus, typer.Option("--status")] = HostStatus.DISCOVERED,
) -> None:
    """Add one manually observed host; no network action is performed."""

    try:
        host = _service(ctx).add_host(
            HostCreate(
                name=name,
                ip=ip_address(ip),
                hostname=hostname,
                os=os_name,
                user=user,
                network_scope=network_scope,
                status=status,
            )
        )
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Host adicionado:[/green] {host.name} ({host.ip}) [id={host.id}]")


@host_app.command("list")
def host_list(
    ctx: typer.Context,
    status: Annotated[
        str | None,
        typer.Option("--status"),
    ] = None,
    tag: Annotated[str | None, typer.Option("--tag")] = None,
) -> None:
    """List hosts in the current workspace."""

    try:
        workspace = _service(ctx)
        hosts = workspace.hosts.list(status=status)
        if tag:
            host_ids = workspace.tags.hosts_with_tag(tag)
            hosts = [host for host in hosts if host.id in host_ids]
    except Exception as error:
        _handle_error(error)
        return
    table = Table("ID", "NAME", "IP", "USER", "OS", "STATUS")
    for host in hosts:
        table.add_row(
            str(host.id),
            host.name,
            str(host.ip),
            host.user or "-",
            host.os or "-",
            host.status.value,
        )
    console.print(table)


@host_app.command("show")
def host_show(
    ctx: typer.Context,
    identifier: Annotated[str, typer.Argument(help="ID, nome ou IP do host.")],
) -> None:
    """Show the details currently known for one host."""

    try:
        service = _service(ctx)
        host = service.hosts.get(identifier)
        if host is None:
            raise EntityNotFoundError(f"Host {identifier} não encontrado.")
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[bold]{host.name}[/bold]")
    console.print(f"ID: {host.id}\nIP: {host.ip}\nHostname: {host.hostname or '-'}")
    tags = service.tags.list_for_host(host.id)
    console.print(f"User: {host.user or '-'}\nOS: {host.os or '-'}\nStatus: {host.status.value}")
    console.print(f"Tags: {', '.join(tags) if tags else '-'}")
    observations = ObservationRepository(service.database, service.lab.id)
    interfaces = observations.list_table("interfaces", host.id)
    services = observations.list_table("services", host.id)
    neighbors = observations.list_table("neighbors", host.id)
    facts = observations.list_table("system_facts", host.id)
    notes = service.notes.list(entity_type="host", entity_id=host.id)
    console.print(
        f"Interfaces: {len(interfaces)} | Services: {len(services)} | "
        f"Neighbors: {len(neighbors)}"
    )
    for row in interfaces:
        console.print(f"  {row['name']} {row['ip']}/{row['prefix']} -> {row['network']}")
    for row in services:
        console.print(f"  service {row['address']}:{row['port'] or '-'} {row['description'] or ''}")
    for row in neighbors:
        console.print(f"  neighbor {row['ip']} via {row['dev'] or '-'} ({row['state'] or '-'})")
    if facts:
        fact = facts[-1]
        console.print(
            f"System: {fact['user'] or '-'} | {fact['distribution'] or '-'} | "
            f"{fact['kernel'] or '-'} | {fact['architecture'] or '-'}"
        )
    for note in notes:
        console.print(f"  note: {note.body}")


@note_app.command("add")
def note_add(
    ctx: typer.Context,
    entity_type: Annotated[
        NoteEntityType,
        typer.Argument(help="lab, host, network, shell, service ou pivot"),
    ],
    entity_id: Annotated[int, typer.Argument(help="ID da entidade.")],
    body: Annotated[str, typer.Argument(help="Texto da nota.")],
) -> None:
    """Attach a note to a lab entity."""

    try:
        note = _service(ctx).add_note(
            NoteCreate(entity_type=entity_type, entity_id=entity_id, body=body)
        )
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Nota adicionada:[/green] id={note.id}")


@note_app.command("list")
def note_list(
    ctx: typer.Context,
    entity_type: Annotated[NoteEntityType | None, typer.Option("--entity-type")] = None,
    entity_id: Annotated[int | None, typer.Option("--entity-id")] = None,
) -> None:
    """List notes, optionally filtered by entity."""

    try:
        notes = _service(ctx).notes.list(
            entity_type=entity_type.value if entity_type else None, entity_id=entity_id
        )
    except Exception as error:
        _handle_error(error)
        return
    table = Table("ID", "ENTITY", "TEXT", "CREATED")
    for note in notes:
        table.add_row(
            str(note.id),
            f"{note.entity_type.value}:{note.entity_id}",
            note.body,
            note.created_at.isoformat(timespec="seconds"),
        )
    console.print(table)


@event_app.command("list")
def event_list(
    ctx: typer.Context,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=1000),
    ] = 100,
) -> None:
    """Show the most recent timeline events."""

    try:
        events = _service(ctx).events.list(limit=limit)
    except Exception as error:
        _handle_error(error)
        return
    table = Table("TIME", "TYPE", "MESSAGE")
    for event in reversed(events):
        table.add_row(event["created_at"], event["event_type"], event["message"])
    console.print(table)


def _run_import(
    ctx: typer.Context,
    host: str,
    input_file: Path | None,
    importer: str,
    snapshot_id: int | None,
    network_scope: str = "default",
) -> None:
    """Run one named parser through the application service."""

    try:
        text = _read_input(input_file)
        workspace = _service(ctx)
        snapshot_id = _snapshot_id_filter(workspace, snapshot_id)
        service = EnumerationService(workspace)
        kwargs: dict[str, object] = {"snapshot_id": snapshot_id}
        if importer in {"import_ip_addr", "import_route"}:
            kwargs["network_scope"] = network_scope
        result = getattr(service, importer)(host, text, **kwargs)
    except Exception as error:
        _handle_error(error)
        return
    _show_import_result(result)


@import_app.command("ip")
def import_ip(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="ID, nome ou IP do host dono do output.")],
    input_file: Annotated[Path | None, typer.Option("--file", "-f")] = None,
    snapshot_id: Annotated[int | None, typer.Option("--snapshot")] = None,
    network_scope: Annotated[
        str,
        typer.Option("--network-scope", help="Escopo da rede para evitar fundir CIDRs isolados."),
    ] = "default",
) -> None:
    """Import `ip addr` output for a host."""

    _run_import(ctx, host, input_file, "import_ip_addr", snapshot_id, network_scope)


@import_app.command("route")
def import_route(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="ID, nome ou IP do host dono do output.")],
    input_file: Annotated[Path | None, typer.Option("--file", "-f")] = None,
    snapshot_id: Annotated[int | None, typer.Option("--snapshot")] = None,
    network_scope: Annotated[
        str,
        typer.Option("--network-scope", help="Escopo da rede para evitar fundir CIDRs isolados."),
    ] = "default",
) -> None:
    """Import `ip route` output for a host."""

    _run_import(ctx, host, input_file, "import_route", snapshot_id, network_scope)


@import_app.command("neigh")
def import_neigh(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="ID, nome ou IP do host dono do output.")],
    input_file: Annotated[Path | None, typer.Option("--file", "-f")] = None,
    snapshot_id: Annotated[int | None, typer.Option("--snapshot")] = None,
) -> None:
    """Import `ip neigh` output for a host."""

    _run_import(ctx, host, input_file, "import_neigh", snapshot_id)


@import_app.command("ss")
def import_ss(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="ID, nome ou IP do host dono do output.")],
    input_file: Annotated[Path | None, typer.Option("--file", "-f")] = None,
    snapshot_id: Annotated[int | None, typer.Option("--snapshot")] = None,
) -> None:
    """Import `ss -tunap` output for a host."""

    _run_import(ctx, host, input_file, "import_ss", snapshot_id)


@import_app.command("hosts")
def import_hosts(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="ID, nome ou IP do host dono do output.")],
    input_file: Annotated[Path | None, typer.Option("--file", "-f")] = None,
    snapshot_id: Annotated[int | None, typer.Option("--snapshot")] = None,
) -> None:
    """Import `/etc/hosts` output for a host."""

    _run_import(ctx, host, input_file, "import_hosts", snapshot_id)


@import_app.command("resolv")
def import_resolv(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="ID, nome ou IP do host dono do output.")],
    input_file: Annotated[Path | None, typer.Option("--file", "-f")] = None,
    snapshot_id: Annotated[int | None, typer.Option("--snapshot")] = None,
) -> None:
    """Import `/etc/resolv.conf` output for a host."""

    _run_import(ctx, host, input_file, "import_resolv", snapshot_id)


@import_app.command("system")
def import_system(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="ID, nome ou IP do host dono do output.")],
    input_file: Annotated[Path | None, typer.Option("--file", "-f")] = None,
    snapshot_id: Annotated[int | None, typer.Option("--snapshot")] = None,
) -> None:
    """Import normalized system facts as `KEY=value` lines."""

    _run_import(ctx, host, input_file, "import_system", snapshot_id)


@network_app.command("list")
def network_list(ctx: typer.Context) -> None:
    """List networks inferred from imported interfaces and routes."""

    try:
        workspace = _service(ctx)
        rows = ObservationRepository(workspace.database, workspace.lab.id).list_table("networks")
    except Exception as error:
        _handle_error(error)
        return
    table = Table("ID", "CIDR", "SCOPE", "VIA", "OBSERVED")
    for row in rows:
        reachable = json.loads(row["reachable_via_json"] or "[]")
        via = ", ".join(str(item) for item in reachable) or "direct"
        table.add_row(
            str(row["id"]),
            row["cidr"],
            str(row.get("scope", "default")),
            via,
            row["observed_at"],
        )
    console.print(table)


@network_app.command("path")
def network_path(
    ctx: typer.Context,
    cidr: Annotated[str, typer.Argument()],
    scope: Annotated[
        str, typer.Option("--scope", help="Escopo da rede; use para CIDRs sobrepostos.")
    ] = "default",
) -> None:
    """Show the currently recorded reachability path for a CIDR."""

    try:
        workspace = _service(ctx)
        rows = ObservationRepository(workspace.database, workspace.lab.id).list_table("networks")
        row = next(
            (
                item
                for item in rows
                if item["cidr"] == cidr and item.get("scope", "default") == scope
            ),
            None,
        )
        if row is None:
            raise EntityNotFoundError(f"Network {cidr} (escopo {scope}) não encontrada.")
        host_names: list[str] = []
        for host_id in json.loads(row["reachable_via_json"] or "[]"):
            host = workspace.hosts.get(str(host_id))
            host_names.append(host.name or str(host_id) if host else str(host_id))
    except Exception as error:
        _handle_error(error)
        return
    label = cidr if scope == "default" else f"{cidr} [{scope}]"
    path = " -> ".join(["ATTACKER", *host_names, label])
    console.print(path)


@service_app.command("list")
def service_list(
    ctx: typer.Context,
    host: Annotated[str | None, typer.Option("--host", "-H")] = None,
) -> None:
    """List listening services imported from `ss`."""

    try:
        workspace = _service(ctx)
        host_id = _host_id_filter(workspace, host)
        rows = ObservationRepository(workspace.database, workspace.lab.id).list_table(
            "services", host_id
        )
    except Exception as error:
        _handle_error(error)
        return
    table = Table("HOST", "ADDRESS", "PORT", "PROTO", "STATE", "SERVICE", "PROCESS")
    for row in rows:
        table.add_row(
            str(row["host_id"]),
            row["address"],
            str(row["port"] or "-"),
            row["protocol"],
            row["state"],
            row["description"] or "-",
            row["process"] or "-",
        )
    console.print(table)


@connection_app.command("list")
def connection_list(
    ctx: typer.Context,
    host: Annotated[str | None, typer.Option("--host", "-H")] = None,
) -> None:
    """List connections imported from `ss`."""

    try:
        workspace = _service(ctx)
        host_id = _host_id_filter(workspace, host)
        rows = ObservationRepository(workspace.database, workspace.lab.id).list_table(
            "connections", host_id
        )
    except Exception as error:
        _handle_error(error)
        return
    table = Table("HOST", "PROTO", "LOCAL", "REMOTE", "STATE", "PROCESS")
    for row in rows:
        local = f"{row['local_address']}:{row['local_port'] or '-'}"
        remote = f"{row['remote_address'] or '-'}:{row['remote_port'] or '-'}"
        table.add_row(
            str(row["host_id"]),
            row["protocol"],
            local,
            remote,
            row["state"],
            row["process"] or "-",
        )
    console.print(table)


@profile_app.command("parse")
def profile_parse(command: Annotated[str, typer.Argument(help="Comando SSH colado")]) -> None:
    """Parse a safe subset of an SSH command without saving it."""

    try:
        parsed = ConnectionService.parse_ssh(command)
    except Exception as error:
        _handle_error(error)
        return
    jumps = ",".join(
        (f"{item.user}@" if item.user else "") + f"{item.host}:{item.port}"
        for item in parsed.jump_targets
    )
    console.print(
        f"host={parsed.host} user={parsed.user} port={parsed.port} "
        f"identity={parsed.identity_file or '-'} jumps={jumps or '-'}"
    )


@profile_app.command("add")
def profile_add(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Nome exibido no workspace")],
    ssh_command: Annotated[str, typer.Argument(help="Destino ou comando SSH")],
    auth_ref: Annotated[str | None, typer.Option("--auth-ref")] = None,
    auth_method: Annotated[
        str,
        typer.Option(
            "--auth-method",
            help="agent_or_key, password ou key_passphrase",
        ),
    ] = "agent_or_key",
    tag: Annotated[list[str] | None, typer.Option("--tag")] = None,
) -> None:
    """Save an SSH profile without storing a password."""

    try:
        parsed = ConnectionService.parse_ssh(ssh_command)
        workspace = _service(ctx)
        connection_service = ConnectionService(workspace)
        jump_profile_ids, unresolved = connection_service.resolve_jump_profile_ids(parsed)
        if unresolved:
            formatted = ", ".join(
                (f"{item.user}@" if item.user else "") + f"{item.host}:{item.port}"
                for item in unresolved
            )
            raise ValueError(
                "Cadastre os perfis dos saltos ProxyJump antes do destino: " + formatted
            )
        profile = connection_service.add(
            ConnectionProfileCreate(
                name=name,
                host=parsed.host,
                user=parsed.user,
                port=parsed.port,
                identity_file=parsed.identity_file,
                known_hosts_file=parsed.known_hosts_file,
                auth_ref=auth_ref,
                auth_method=auth_method,
                jump_profile_ids=jump_profile_ids,
                tags=tuple(tag or ()),
            )
        )
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Perfil salvo:[/green] {profile.name} [id={profile.id}]")


@profile_app.command("list")
def profile_list(ctx: typer.Context) -> None:
    """List saved connection profiles."""

    try:
        profiles = _service(ctx).connections.list()
    except Exception as error:
        _handle_error(error)
        return
    table = Table("ID", "NAME", "DESTINATION", "AUTH", "AUTH REF", "TAGS")
    for profile in profiles:
        table.add_row(
            str(profile.id),
            profile.name,
            f"{profile.user}@{profile.host}:{profile.port}",
            profile.auth_method,
            profile.auth_ref or "ssh-agent/key",
            ", ".join(profile.tags) or "-",
        )
    console.print(table)


@pivot_app.command("detect")
def pivot_detect(ctx: typer.Context) -> None:
    """Analyze imported observations and record pivot candidates."""

    try:
        count = TopologyService(_service(ctx)).detect_pivots()
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Pivot candidates:[/green] {count}")


@pivot_app.command("list")
def pivot_list(ctx: typer.Context) -> None:
    """List inferred pivot candidates; no tunnel is started."""

    try:
        workspace = _service(ctx)
        pivots = PivotRepository(workspace.database, workspace.lab.id).list()
    except Exception as error:
        _handle_error(error)
        return
    table = Table("ID", "HOST", "NETWORK", "CONF", "STATUS", "REASON")
    observations = ObservationRepository(workspace.database, workspace.lab.id)
    networks = {row["id"]: row["cidr"] for row in observations.list_table("networks")}
    for pivot in pivots:
        host = workspace.hosts.get(str(pivot.host_id))
        table.add_row(
            str(pivot.id),
            host.name if host else str(pivot.host_id),
            networks.get(pivot.network_id, str(pivot.network_id)),
            f"{pivot.confidence}%",
            pivot.status.value,
            pivot.reason,
        )
    console.print(table)


@pivot_app.command("tools")
def pivot_tools() -> None:
    """Show locally available pivot helpers without invoking them."""

    table = Table("TOOL", "AVAILABLE", "PATH", "VERSION")
    for tool in detect_pivot_tools():
        table.add_row(
            tool.name,
            "✓" if tool.installed else "✗",
            tool.executable or "-",
            tool.version or "-",
        )
    console.print(table)


@app.command("map")
def map_view(
    ctx: typer.Context,
    output_dir: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    format_name: Annotated[str, typer.Option("--format", help="text, dot ou all")] = "text",
) -> None:
    """Render the observed topology without probing the network."""

    if format_name not in {"text", "dot", "all"}:
        console.print("[red]Formato inválido:[/red] escolha text, dot ou all.")
        raise typer.Exit(code=2)
    try:
        topology = TopologyService(_service(ctx))
        if output_dir is not None:
            paths = topology.export(output_dir)
            console.print("[green]Arquivos exportados:[/green]")
            for path in paths:
                console.print(str(path))
        elif format_name == "dot":
            console.print(topology.render_dot())
        else:
            console.print(topology.render_text())
    except Exception as error:
        _handle_error(error)


@forward_app.command("add")
def forward_add(
    ctx: typer.Context,
    name: Annotated[str, typer.Option("--name", "-n")],
    via: Annotated[str, typer.Option("--via", help="Host ID, nome ou IP do salto.")],
    local_port: Annotated[int, typer.Option("--local-port", min=1, max=65535)],
    kind: Annotated[ForwardKind, typer.Option("--kind")] = ForwardKind.LOCAL,
    local_address: Annotated[str, typer.Option("--local-address")] = "127.0.0.1",
    target_address: Annotated[str | None, typer.Option("--target-address")] = None,
    target_port: Annotated[int | None, typer.Option("--target-port", min=1, max=65535)] = None,
    user: Annotated[str | None, typer.Option("--user")] = None,
    tool: Annotated[str, typer.Option("--tool")] = "ssh",
    endpoint: Annotated[str | None, typer.Option("--endpoint")] = None,
    fingerprint: Annotated[str | None, typer.Option("--fingerprint")] = None,
    auth_ref: Annotated[str | None, typer.Option("--auth-ref")] = None,
    session_id: Annotated[int | None, typer.Option("--session-id", min=1)] = None,
) -> None:
    """Register a forward and print its command; do not execute it."""

    try:
        workspace = _service(ctx)
        via_host_id = _host_id_filter(workspace, via)
        if via_host_id is None:
            raise EntityNotFoundError(f"Host {via} não encontrado neste laboratório.")
        forward = PivotService(workspace).add_forward(
            ForwardCreate(
                name=name,
                via_host_id=via_host_id,
                kind=kind,
                local_address=local_address,
                local_port=local_port,
                target_address=target_address,
                target_port=target_port,
                user=user,
                tool=tool,
                endpoint=endpoint,
                fingerprint=fingerprint,
                auth_ref=auth_ref,
                session_id=session_id,
            )
        )
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Forward planejado:[/green] {forward.name} [id={forward.id}]")
    console.print(f"Comando: {forward.command}")
    console.print("[yellow]Nenhum processo foi iniciado.[/yellow]")


@forward_app.command("list")
def forward_list(ctx: typer.Context) -> None:
    """List stored forward plans."""

    try:
        forwards = _service(ctx).forwards.list()
    except Exception as error:
        _handle_error(error)
        return
    table = Table("ID", "NAME", "VIA", "LOCAL", "TARGET", "TOOL", "STATUS", "HEALTH", "PID")
    for forward in forwards:
        target = (
            f"{forward.target_address}:{forward.target_port}"
            if forward.target_address and forward.target_port
            else "SOCKS"
        )
        table.add_row(
            str(forward.id),
            forward.name,
            str(forward.via_host_id),
            f"{forward.local_address}:{forward.local_port}",
            target,
            forward.tool,
            forward.status.value,
            forward.health or "-",
            str(forward.pid or "-"),
        )
    console.print(table)


@forward_app.command("status")
def forward_status(
    ctx: typer.Context,
    forward_id: Annotated[int, typer.Argument()],
    status: Annotated[ForwardStatus, typer.Argument()],
) -> None:
    """Update forward metadata; it does not start or stop a process."""

    try:
        forward = _service(ctx).forwards.update_status(forward_id, status)
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Status atualizado:[/green] {forward.name} -> {forward.status.value}")


@forward_app.command("start")
def forward_start(
    ctx: typer.Context,
    forward_id: Annotated[int, typer.Argument()],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Start a stored local forward only after showing its command."""

    try:
        workspace = _service(ctx)
        forward = workspace.forwards.get(forward_id)
        if forward is None:
            raise EntityNotFoundError(f"Forward {forward_id} não encontrado.")
        console.print(f"Comando local: {forward.command}")
        if not yes and not typer.confirm("Executar este processo local?"):
            console.print("Operação cancelada.")
            return
        updated = ForwardProcessService(workspace).start(forward_id)
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Forward ativo:[/green] pid={updated.pid}")


@forward_app.command("stop")
def forward_stop(
    ctx: typer.Context,
    forward_id: Annotated[int, typer.Argument()],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Stop a stored local forward after confirmation."""

    if not yes and not typer.confirm("Parar o processo local deste forward?"):
        console.print("Operação cancelada.")
        return
    try:
        updated = ForwardProcessService(_service(ctx)).stop(forward_id)
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Forward parado:[/green] {updated.name}")


@forward_app.command("check")
def forward_check(ctx: typer.Context, forward_id: Annotated[int, typer.Argument()]) -> None:
    """Refresh process and local-listener health for one forward."""

    try:
        updated = ForwardProcessService(_service(ctx)).check(forward_id)
    except Exception as error:
        _handle_error(error)
        return
    console.print(
        f"[green]Forward:[/green] {updated.name} -> {updated.status.value} "
        f"({updated.health or 'unknown'})"
    )


@evidence_app.command("add")
def evidence_add(
    ctx: typer.Context,
    evidence_type: Annotated[str, typer.Option("--type")],
    description: Annotated[str, typer.Option("--description", "-d")],
    path: Annotated[str, typer.Option("--path", "-p")],
    host: Annotated[str | None, typer.Option("--host")] = None,
) -> None:
    """Register evidence metadata without copying or executing the path."""

    try:
        workspace = _service(ctx)
        host_id = _host_id_filter(workspace, host)
        item = workspace.evidence.create(
            EvidenceCreate(
                host_id=host_id,
                type=evidence_type,
                description=description,
                path=path,
            )
        )
        workspace._emit(
            Event(
                event_type="EVIDENCE_ADDED",
                message=f"Evidence registered: {item.description}",
                entity_type="evidence",
                entity_id=item.id,
                payload={"path": item.path, "type": item.type},
            )
        )
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Evidência registrada:[/green] id={item.id}")


@evidence_app.command("list")
def evidence_list(ctx: typer.Context) -> None:
    """List evidence metadata."""

    try:
        items = _service(ctx).evidence.list()
    except Exception as error:
        _handle_error(error)
        return
    table = Table("ID", "HOST", "TYPE", "DESCRIPTION", "PATH", "TIME")
    for item in items:
        table.add_row(
            str(item.id),
            str(item.host_id or "-"),
            item.type,
            item.description,
            item.path,
            item.created_at.isoformat(timespec="seconds"),
        )
    console.print(table)


@app.command("report")
def report(
    ctx: typer.Context,
    output_dir: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    format_name: Annotated[str, typer.Option("--format", help="md, html ou all")] = "md",
) -> None:
    """Generate a report from the persisted workspace state."""

    if format_name not in {"md", "html", "all"}:
        console.print("[red]Formato inválido:[/red] escolha md, html ou all.")
        raise typer.Exit(code=2)
    try:
        workspace = _service(ctx)
        target = output_dir or (workspace.paths.root / "reports")
        formats = {"md", "html"} if format_name == "all" else {format_name}
        paths = ReportService(workspace).write(target, formats)
    except Exception as error:
        _handle_error(error)
        return
    for path in paths:
        console.print(f"[green]Relatório gerado:[/green] {path}")


@host_app.command("tag")
def host_tag(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="ID, nome ou IP do host.")],
    tag: Annotated[str, typer.Argument()],
) -> None:
    """Add a normalized tag to a host."""

    try:
        workspace = _service(ctx)
        host_id = _host_id_filter(workspace, host)
        if host_id is None:
            raise EntityNotFoundError(f"Host {host} não encontrado neste laboratório.")
        workspace.tags.add(host_id, tag)
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Tag adicionada:[/green] {tag.lower()}")


@host_app.command("untag")
def host_untag(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="ID, nome ou IP do host.")],
    tag: Annotated[str, typer.Argument()],
) -> None:
    """Remove a tag from a host."""

    try:
        workspace = _service(ctx)
        host_id = _host_id_filter(workspace, host)
        if host_id is None:
            raise EntityNotFoundError(f"Host {host} não encontrado neste laboratório.")
        workspace.tags.remove(host_id, tag)
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Tag removida:[/green] {tag.lower()}")


@app.command("search")
def search(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument()],
) -> None:
    """Search hosts, networks, notes, events and command history."""

    try:
        results = SearchService(_service(ctx)).search(query)
    except Exception as error:
        _handle_error(error)
        return
    table = Table("TYPE", "ID", "DETAIL")
    for result in results:
        table.add_row(result["kind"], result["label"], result["detail"])
    console.print(table)


@app.command("diff")
def diff(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="ID, nome ou IP do host.")],
    command: Annotated[str, typer.Option("--command", "-c")],
) -> None:
    """Compare the two most recent stored outputs of one command."""

    try:
        workspace = _service(ctx)
        host_id = _host_id_filter(workspace, host)
        if host_id is None:
            raise EntityNotFoundError(f"Host {host} não encontrado neste laboratório.")
        result = DiffService(workspace).command_diff(host_id, command)
    except Exception as error:
        _handle_error(error)
        return
    if result["message"]:
        console.print(result["message"][0])
        return
    console.print("[bold]CHANGES[/bold]")
    for line in result["added"]:
        console.print(f"[green]+ {line}[/green]")
    for line in result["removed"]:
        console.print(f"[red]- {line}[/red]")


@app.command("config")
def config_show() -> None:
    """Show user configuration defaults."""

    from ctfws.core.config import config_path, load_config

    config = load_config()
    console.print(f"Path: {config_path()}")
    console.print(f"theme = {config.theme}\neditor = {config.editor}")
    console.print(f"terminal = {config.terminal}\nauto_save = {str(config.auto_save).lower()}")


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    """Check database integrity, schema and local operator capabilities."""

    try:
        paths = WorkspacePaths.from_value(ctx.obj.get("workspace"))
        report = WorkspaceDoctor().run(paths)
    except Exception as error:
        _handle_error(error)
        return
    table = Table("CHECK", "STATUS", "DETAIL")
    for check in report.checks:
        status = (
            "[green]OK[/green]"
            if check.ok
            else "[yellow]OPTIONAL[/yellow]" if check.optional else "[red]FAIL[/red]"
        )
        table.add_row(check.name, status, check.detail)
    console.print(table)
    if not report.ok:
        raise typer.Exit(code=1)


@app.command("backup")
def backup(
    ctx: typer.Context,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    full: Annotated[
        bool, typer.Option("--full", help="Incluir arquivos e manifesto de hashes.")
    ] = False,
) -> None:
    """Create a consistent database or complete workspace backup."""

    try:
        paths = WorkspacePaths.from_value(ctx.obj.get("workspace"))
        timestamp = utc_now().replace(":", "").replace("+", "-")
        if full:
            target = output or (paths.root / "backups" / f"workspace-{timestamp}.zip")
            result = WorkspaceBackupService().create(WorkspaceService(paths), target)
        else:
            target = output or (paths.root / "backups" / f"workspace-{timestamp}.db")
            result = Database(paths.database).backup_to(target)
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Backup criado:[/green] {result}")


@app.command("restore")
def restore(
    ctx: typer.Context,
    backup_file: Annotated[Path, typer.Argument(help="Arquivo SQLite de backup.")],
    yes: Annotated[
        bool, typer.Option("--yes", help="Confirmar a substituição do banco ativo.")
    ] = False,
    destination: Annotated[
        Path | None,
        typer.Option("--destination", help="Pasta nova para restaurar um backup completo."),
    ] = None,
) -> None:
    """Restore a database or a backup completo with integrity validation."""

    if not yes and not typer.confirm("Substituir o banco atual pelo backup informado?"):
        console.print("Operação cancelada.")
        return
    try:
        paths = WorkspacePaths.from_value(ctx.obj.get("workspace"))
        if backup_file.suffix.lower() == ".zip":
            target_root = destination or (paths.root / "restored-workspace")
            result = WorkspaceBackupService().restore(backup_file, target_root, replace=False)
            console.print(f"[green]Restore completo concluído:[/green] {result}")
            return
        database = Database(paths.database)
        timestamp = utc_now().replace(":", "").replace("+", "-")
        safety_copy = paths.root / "backups" / f"before-restore-{timestamp}.db"
        database.backup_to(safety_copy)
        database.restore_from(backup_file)
        database.initialize()
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Restore concluído:[/green] {paths.database}")
    console.print(f"Backup de segurança: {safety_copy}")


def _run_web(paths: WorkspacePaths, host: str, port: int) -> None:
    """Run the web motor after the caller has resolved its workspace."""

    import uvicorn

    from ctfws.web import create_app

    if host not in {"127.0.0.1", "localhost", "::1"}:
        console.print(
            "[yellow]Aviso:[/yellow] a API será exposta fora do loopback; "
            "use firewall/autenticação do ambiente."
        )
    console.print(f"[bold]Conduit iniciando[/bold] · workspace={paths.root}")
    console.print(f"Acesso local: http://127.0.0.1:{port}")
    console.print("Acesso pelo Windows: " f"ssh -L {port}:127.0.0.1:{port} USER@SERVIDOR_LINUX")
    uvicorn.run(create_app(paths), host=host, port=port, log_level="info")


@app.command("start")
def start(
    ctx: typer.Context,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8765,
) -> None:
    """Start Conduit with workspace discovery and safe local defaults."""

    try:
        _run_web(_resolve_start_workspace(ctx.obj.get("workspace")), host, port)
    except Exception as error:
        _handle_error(error)


@app.command("web")
def web(
    ctx: typer.Context,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8765,
) -> None:
    """Start the dashboard/API for an explicitly selected workspace."""

    try:
        _run_web(WorkspacePaths.from_value(ctx.obj.get("workspace")), host, port)
    except Exception as error:
        _handle_error(error)


@app.command("config-set")
def config_set(
    key: Annotated[str, typer.Argument(help="theme, editor, terminal ou auto_save")],
    value: Annotated[str, typer.Argument()],
) -> None:
    """Update one user setting in ~/.config/ctfws/config.toml."""

    from ctfws.core.config import update_config

    try:
        updated = update_config(key, value)
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Configuração atualizada:[/green] {key}={getattr(updated, key)}")


@app.command("agent")
def agent_command() -> None:
    """Print the fixed-command agent JSON for transport by the operator."""

    typer.echo(json.dumps(collect(), ensure_ascii=False, indent=2))


@enum_app.command("quick")
def enum_quick(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="ID, nome ou IP do host associado ao output.")],
    input_file: Annotated[
        Path | None,
        typer.Option("--file", "-f", help="JSON do ctfws agent."),
    ] = None,
    execute_local: Annotated[
        bool,
        typer.Option("--execute-local", help="Coleta somente esta máquina após confirmação."),
    ] = False,
    snapshot_id: Annotated[int | None, typer.Option("--snapshot")] = None,
) -> None:
    """Import agent JSON or explicitly collect the local fixed allowlist."""

    try:
        if input_file is not None:
            payload = json.loads(input_file.expanduser().read_text(encoding="utf-8"))
        elif execute_local:
            if not typer.confirm("Executar a coleta fixa nesta máquina?"):
                console.print("Operação cancelada.")
                return
            payload = collect()
        else:
            raise ValueError("Forneça --file agent.json ou use --execute-local com confirmação.")
        workspace = _service(ctx)
        snapshot_id = _snapshot_id_filter(workspace, snapshot_id)
        results = EnumerationService(workspace).import_quick(host, payload, snapshot_id=snapshot_id)
    except Exception as error:
        _handle_error(error)
        return
    for result in results:
        _show_import_result(result)


@command_app.command("list")
def command_list(
    ctx: typer.Context,
    host: Annotated[str | None, typer.Option("--host", "-H")] = None,
) -> None:
    """List captured commands and their output timestamps."""

    try:
        workspace = _service(ctx)
        host_id = _host_id_filter(workspace, host)
        rows = ObservationRepository(workspace.database, workspace.lab.id).list_table(
            "commands", host_id
        )
    except Exception as error:
        _handle_error(error)
        return
    table = Table("ID", "HOST", "COMMAND", "TIME")
    for row in rows:
        table.add_row(str(row["id"]), str(row["host_id"] or "-"), row["command"], row["created_at"])
    console.print(table)


@identity_app.command("sync")
def identity_sync(ctx: typer.Context) -> None:
    """Link aliases from imported `/etc/hosts` rows to known IPs."""

    try:
        count = IntelligenceService(_service(ctx)).sync_aliases()
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Aliases sincronizados:[/green] {count}")


@identity_app.command("list")
def identity_list(ctx: typer.Context) -> None:
    """List host aliases with their source."""

    try:
        rows = IntelligenceService(_service(ctx)).repository.aliases()
    except Exception as error:
        _handle_error(error)
        return
    table = Table("HOST", "ALIAS", "SOURCE")
    for row in rows:
        table.add_row(str(row["host_id"]), row["alias"], row["source"])
    console.print(table)


@relationship_app.command("rebuild")
def relationship_rebuild(ctx: typer.Context) -> None:
    """Infer relationships from imported connections to known hosts."""

    try:
        count = IntelligenceService(_service(ctx)).rebuild_relationships()
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Relações atualizadas:[/green] {count}")


@relationship_app.command("list")
def relationship_list(ctx: typer.Context) -> None:
    """List explainable inferred host relationships."""

    try:
        rows = IntelligenceService(_service(ctx)).repository.relationships()
    except Exception as error:
        _handle_error(error)
        return
    table = Table("SOURCE", "TARGET", "TYPE", "LABEL", "CONF")
    for row in rows:
        table.add_row(
            str(row["source_host_id"]),
            str(row["target_host_id"]),
            row["relation_type"],
            row["label"],
            f"{row['confidence']}%",
        )
    console.print(table)


@path_app.command("rebuild")
def path_rebuild(ctx: typer.Context) -> None:
    """Infer candidate access paths from networks, relationships and sessions."""

    try:
        result = AccessPathService(_service(ctx)).rebuild()
    except Exception as error:
        _handle_error(error)
        return
    console.print(
        f"[green]Caminhos reconstruídos:[/green] {result.paths} "
        f"targets={result.targets} verified={result.verified}"
    )


@path_app.command("list")
def path_list(
    ctx: typer.Context,
    target: Annotated[str | None, typer.Option("--target", "-t")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List explainable paths without starting any transport."""

    try:
        workspace = _service(ctx)
        target_id = _host_id_filter(workspace, target)
        paths = AccessPathService(workspace).list_paths(target_id)
    except Exception as error:
        _handle_error(error)
        return
    if json_output:
        console.print_json(json.dumps([path.model_dump(mode="json") for path in paths]))
        return
    table = Table("ID", "TARGET", "HOST HOPS", "SESSION HOPS", "STATE", "CONF", "REASON")
    for path in paths:
        table.add_row(
            str(path.id),
            f"{path.target_address}:{path.target_port or '-'}",
            " -> ".join(str(item) for item in path.hop_host_ids) or "-",
            " -> ".join(str(item) for item in path.hop_session_ids) or "-",
            path.state.value,
            f"{path.confidence}%",
            path.reason,
        )
    console.print(table)


@path_app.command("verify")
def path_verify(ctx: typer.Context, path_id: Annotated[int, typer.Argument()]) -> None:
    """Verify one explicit TCP endpoint and store its five-minute proof."""

    try:
        path = AccessPathService(_service(ctx)).verify(path_id)
    except Exception as error:
        _handle_error(error)
        return
    console.print(
        f"[green]Endpoint verificado:[/green] {path.target_address}:{path.target_port} "
        f"estado={path.state.value} check={path.verification_id}"
    )


@context_app.command("list")
def context_list(ctx: typer.Context) -> None:
    """List planned and active network contexts."""

    try:
        contexts = _service(ctx).contexts.list()
    except Exception as error:
        _handle_error(error)
        return
    table = Table("ID", "NAME", "TRANSPORT", "NETWORKS", "STATUS", "PORT")
    for context in contexts:
        table.add_row(
            str(context.id),
            context.name,
            context.transport.value,
            ", ".join(context.network_cidrs) or "-",
            context.status.value,
            str(context.local_port or "-"),
        )
    console.print(table)


@context_app.command("plan")
def context_plan(
    ctx: typer.Context,
    name: Annotated[str, typer.Option("--name")],
    transport: Annotated[ContextTransport, typer.Option("--transport")] = ContextTransport.SOCKS,
    connection_id: Annotated[int | None, typer.Option("--connection-id")] = None,
    network: Annotated[list[str] | None, typer.Option("--network")] = None,
    local_port: Annotated[int | None, typer.Option("--local-port")] = None,
) -> None:
    """Create a reviewable context plan without starting it."""

    try:
        context = NetworkContextService(_service(ctx)).plan(
            NetworkContextCreate(
                name=name,
                transport=transport,
                connection_id=connection_id,
                network_cidrs=tuple(network or ()),
                local_port=local_port,
            )
        )
    except Exception as error:
        _handle_error(error)
        return
    console.print(
        f"[green]Contexto planejado:[/green] {context.name} [id={context.id}] "
        f"{context.transport.value} port={context.local_port or '-'}"
    )


@context_app.command("start")
def context_start(ctx: typer.Context, context_id: Annotated[int, typer.Argument()]) -> None:
    """Start one previously reviewed context."""

    try:
        context = NetworkContextService(_service(ctx)).start(context_id)
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Contexto:[/green] {context.status.value} {context.error or ''}")


@context_app.command("stop")
def context_stop(ctx: typer.Context, context_id: Annotated[int, typer.Argument()]) -> None:
    """Stop one context and only its owned process."""

    try:
        context = NetworkContextService(_service(ctx)).stop(context_id)
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Contexto:[/green] {context.status.value}")


@app.command("resume-plan")
def resume_plan(ctx: typer.Context) -> None:
    """Show resources that need a manual post-restart resume."""

    try:
        items = ResumeService(_service(ctx)).plan()
    except Exception as error:
        _handle_error(error)
        return
    table = Table("TYPE", "ID", "NAME", "ACTION", "REASON")
    for item in items:
        table.add_row(
            item.resource_type, str(item.resource_id), item.name, item.action, item.reason
        )
    console.print(table)


@app.command("resume")
def resume(
    ctx: typer.Context,
    resource_id: Annotated[list[int] | None, typer.Option("--resource-id")] = None,
    resource_ref: Annotated[list[str] | None, typer.Option("--resource-ref")] = None,
) -> None:
    """Apply a selected resume plan, or all displayed items after confirmation."""

    if not typer.confirm("Iniciar os recursos selecionados explicitamente?", default=False):
        console.print("Operação cancelada.")
        return
    try:
        results = ResumeService(_service(ctx)).apply(
            tuple(resource_id or ()) or None,
            tuple(resource_ref or ()) or None,
        )
    except Exception as error:
        _handle_error(error)
        return
    for result in results:
        console.print_json(json.dumps(result, ensure_ascii=False))


@session_app.command("add")
def session_add(
    ctx: typer.Context,
    name: Annotated[str, typer.Option("--name", "-n")],
    host: Annotated[str | None, typer.Option("--host", "-H")] = None,
    transport: Annotated[SessionTransport, typer.Option("--transport")] = SessionTransport.OTHER,
    user: Annotated[str | None, typer.Option("--user")] = None,
    endpoint: Annotated[str | None, typer.Option("--endpoint")] = None,
    terminal: Annotated[str | None, typer.Option("--terminal")] = None,
    pid: Annotated[int | None, typer.Option("--pid", min=1)] = None,
    tmux_session: Annotated[str | None, typer.Option("--tmux-session")] = None,
) -> None:
    """Register an existing or planned operator session."""

    try:
        workspace = _service(ctx)
        host_id = _host_id_filter(workspace, host)
        session = SessionService(workspace).add(
            SessionCreate(
                name=name,
                host_id=host_id,
                transport=transport,
                user=user,
                endpoint=endpoint,
                terminal=terminal,
                pid=pid,
                tmux_session=tmux_session,
            )
        )
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Sessão registrada:[/green] {session.name} [id={session.id}]")


@session_app.command("list")
def session_list(
    ctx: typer.Context,
    status: Annotated[SessionStatus | None, typer.Option("--status")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List sessions and their last known health."""

    try:
        sessions = SessionService(_service(ctx)).list(status)
    except Exception as error:
        _handle_error(error)
        return
    if json_output:
        console.print_json(json.dumps([session.model_dump(mode="json") for session in sessions]))
        return
    table = Table("ID", "NAME", "HOST", "TRANSPORT", "STATUS", "HEALTH", "PID", "ENDPOINT")
    for session in sessions:
        table.add_row(
            str(session.id),
            session.name,
            str(session.host_id or "-"),
            session.transport.value,
            session.status.value,
            session.health or "-",
            str(session.pid or "-"),
            session.endpoint or "-",
        )
    console.print(table)


@session_app.command("check")
def session_check(ctx: typer.Context, session_id: Annotated[int, typer.Argument()]) -> None:
    """Refresh process liveness for one session."""

    try:
        session = SessionService(_service(ctx)).check(session_id)
    except Exception as error:
        _handle_error(error)
        return
    console.print(
        f"[green]Sessão:[/green] {session.name} -> {session.status.value} ({session.health})"
    )


@session_app.command("close")
def session_close(ctx: typer.Context, session_id: Annotated[int, typer.Argument()]) -> None:
    """Close a session record without killing an external process."""

    try:
        session = SessionService(_service(ctx)).close(session_id)
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Sessão fechada:[/green] {session.name}")


@snapshot_app.command("create")
def snapshot_create(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument()],
    purpose: Annotated[str | None, typer.Option("--purpose")] = None,
) -> None:
    """Create a named checkpoint for the observation history."""

    try:
        snapshot = _service(ctx).snapshots.create(SnapshotCreate(name=name, purpose=purpose))
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Snapshot criado:[/green] {snapshot.name} [id={snapshot.id}]")


@snapshot_app.command("list")
def snapshot_list(ctx: typer.Context) -> None:
    """List named observation checkpoints."""

    try:
        snapshots = _service(ctx).snapshots.list()
    except Exception as error:
        _handle_error(error)
        return
    table = Table("ID", "NAME", "PURPOSE", "CREATED")
    for snapshot in snapshots:
        table.add_row(
            str(snapshot.id),
            snapshot.name,
            snapshot.purpose or "-",
            snapshot.created_at.isoformat(timespec="seconds"),
        )
    console.print(table)


@source_app.command("list")
def source_list(
    ctx: typer.Context,
    host: Annotated[str | None, typer.Option("--host", "-H")] = None,
) -> None:
    """List the command/file identity behind imported observations."""

    try:
        workspace = _service(ctx)
        host_id = _host_id_filter(workspace, host)
        sources = workspace.sources.list(host_id)
    except Exception as error:
        _handle_error(error)
        return
    table = Table("ID", "HOST", "KIND", "COMMAND", "SNAPSHOT", "SHA256", "COLLECTED")
    for source in sources:
        table.add_row(
            str(source.id),
            str(source.host_id or "-"),
            source.kind.value,
            source.command or "-",
            str(source.snapshot_id or "-"),
            source.content_hash[:16] + "…",
            source.collected_at.isoformat(timespec="seconds"),
        )
    console.print(table)


@shell_app.command("add")
def shell_add(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="ID, nome ou IP do host da shell.")],
    name: Annotated[str, typer.Option("--name", "-n")],
    shell_type: Annotated[ShellType, typer.Option("--type")] = ShellType.OTHER,
    user: Annotated[str | None, typer.Option("--user")] = None,
    terminal: Annotated[str | None, typer.Option("--terminal")] = None,
    notes: Annotated[str | None, typer.Option("--notes")] = None,
) -> None:
    """Register an existing shell; this does not open a process."""

    try:
        workspace = _service(ctx)
        host_id = _host_id_filter(workspace, host)
        if host_id is None:
            raise EntityNotFoundError(f"Host {host} não encontrado neste laboratório.")
        shell = ShellService(workspace).add(
            ShellCreate(
                host_id=host_id,
                name=name,
                type=shell_type,
                user=user,
                terminal=terminal,
                notes=notes,
            )
        )
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Shell registrada:[/green] {shell.name} [id={shell.id}]")


@shell_app.command("list")
def shell_list(
    ctx: typer.Context,
    status: Annotated[ShellStatus | None, typer.Option("--status")] = None,
) -> None:
    """List registered shells."""

    try:
        shells = _service(ctx).shells.list(status=status.value if status else None)
    except Exception as error:
        _handle_error(error)
        return
    table = Table("ID", "NAME", "HOST", "USER", "TYPE", "STATUS", "TERMINAL")
    for shell in shells:
        table.add_row(
            str(shell.id),
            shell.name,
            str(shell.host_id),
            shell.user or "-",
            shell.type.value,
            shell.status.value,
            shell.terminal or "-",
        )
    console.print(table)


@shell_app.command("rename")
def shell_rename(
    ctx: typer.Context,
    shell_id: Annotated[int, typer.Argument()],
    name: Annotated[str, typer.Argument()],
) -> None:
    """Rename a registered shell."""

    try:
        shell = ShellService(_service(ctx)).rename(shell_id, name)
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Shell renomeada:[/green] {shell.name}")


@shell_app.command("status")
def shell_status(
    ctx: typer.Context,
    shell_id: Annotated[int, typer.Argument()],
    status: Annotated[ShellStatus, typer.Argument()],
) -> None:
    """Update metadata status; no shell process is touched."""

    try:
        shell = ShellService(_service(ctx)).update_status(shell_id, status)
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Status atualizado:[/green] {shell.name} -> {shell.status.value}")


@shell_app.command("close")
def shell_close(ctx: typer.Context, shell_id: Annotated[int, typer.Argument()]) -> None:
    """Mark a shell closed without touching its underlying process."""

    try:
        shell = ShellService(_service(ctx)).update_status(shell_id, ShellStatus.CLOSED)
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Shell fechada:[/green] {shell.name}")


@shell_app.command("note")
def shell_note(
    ctx: typer.Context,
    shell_id: Annotated[int, typer.Argument()],
    body: Annotated[str, typer.Argument()],
) -> None:
    """Attach a note to a registered shell."""

    try:
        workspace = _service(ctx)
        if workspace.shells.get(str(shell_id)) is None:
            raise EntityNotFoundError(f"Shell {shell_id} não encontrada.")
        workspace.add_note(
            NoteCreate(entity_type=NoteEntityType.SHELL, entity_id=shell_id, body=body)
        )
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Nota adicionada à shell:[/green] {shell_id}")


@shell_app.command("attach")
def shell_attach(ctx: typer.Context, shell_id: Annotated[int, typer.Argument()]) -> None:
    """Print an attach hint for a registered shell without opening it."""

    try:
        shell = _service(ctx).shells.get(str(shell_id))
        if shell is None:
            raise EntityNotFoundError(f"Shell {shell_id} não encontrada.")
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"Shell: {shell.name} (host {shell.host_id})")
    console.print("Attach is operator-controlled; use the terminal/session that owns this shell.")


@shell_app.command("detach")
def shell_detach(ctx: typer.Context, shell_id: Annotated[int, typer.Argument()]) -> None:
    """Mark a shell detached without touching its process."""

    try:
        shell = ShellService(_service(ctx)).update_status(shell_id, ShellStatus.DETACHED)
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]Shell detached:[/green] {shell.name}")


@shell_app.command("helper")
def shell_helper() -> None:
    """Show non-executing interactive-shell suggestions."""

    table = Table("TOOL", "AVAILABLE", "PATH")
    for tool in detect_tools():
        table.add_row(tool.name, "✓" if tool.available else "✗", tool.path or "-")
    console.print(table)
    for suggestion in suggestions():
        console.print(f"\n[bold]{suggestion.tool}[/bold]\n{shell_join([suggestion.command])}")
        console.print(suggestion.instructions)
    console.print("\n[yellow]Nenhuma sugestão foi executada.[/yellow]")


def _tmux_session(ctx: typer.Context, explicit: str | None) -> str:
    if explicit:
        return explicit
    workspace = _service(ctx)
    return f"ctf-{slugify(workspace.lab.name)}"


@tmux_app.command("plan")
def tmux_plan(
    ctx: typer.Context,
    session: Annotated[str | None, typer.Option("--session", "-s")] = None,
) -> None:
    """Print the local tmux commands without executing them."""

    try:
        workspace = _service(ctx)
        manager = TmuxManager(_tmux_session(ctx, session))
        windows = manager.windows_for_hosts(
            [host.name or str(host.id) for host in workspace.hosts.list()]
        )
        for command in manager.planned_commands(windows):
            console.print(shell_join(command))
    except Exception as error:
        _handle_error(error)


@tmux_app.command("start")
def tmux_start(
    ctx: typer.Context,
    session: Annotated[str | None, typer.Option("--session", "-s")] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirma a execução dos comandos exibidos."),
    ] = False,
) -> None:
    """Create the local tmux workspace after explicit confirmation."""

    try:
        workspace = _service(ctx)
        manager = TmuxManager(_tmux_session(ctx, session))
        windows = manager.windows_for_hosts(
            [host.name or str(host.id) for host in workspace.hosts.list()]
        )
        commands = manager.planned_commands(windows)
        console.print("[bold]Comandos que serão executados localmente:[/bold]")
        for command in commands:
            console.print(shell_join(command))
        if not yes and not typer.confirm("Executar agora?"):
            console.print("Operação cancelada.")
            return
        manager.execute(windows)
    except Exception as error:
        _handle_error(error)
        return
    console.print(f"[green]tmux iniciado:[/green] {manager.session_name}")


@tmux_app.command("status")
def tmux_status(
    ctx: typer.Context,
    session: Annotated[str | None, typer.Option("--session", "-s")] = None,
) -> None:
    """List existing tmux windows without modifying the session."""

    try:
        manager = TmuxManager(_tmux_session(ctx, session))
        windows = manager.list_windows()
    except Exception as error:
        _handle_error(error)
        return
    if not manager.available():
        console.print("tmux não está disponível neste sistema.")
        return
    for window in windows:
        console.print(window)
