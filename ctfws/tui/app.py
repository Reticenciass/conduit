"""Dashboard Textual for the current workspace."""

from __future__ import annotations

import json

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Input, Label, Static

from ctfws.core.config import load_config
from ctfws.core.paths import WorkspacePaths
from ctfws.database.repositories import ObservationRepository, PivotRepository
from ctfws.services.search import SearchService
from ctfws.services.workspace import WorkspaceService


class WorkspaceTUI(App[None]):
    """A read-focused dashboard backed by the current application services."""

    TITLE = "Conduit"
    SUB_TITLE = "Infrastructure workspace"
    CSS = """
    Screen {
        background: #10141c;
        color: #e5e7eb;
    }
    #sidebar {
        width: 36%;
        min-width: 32;
        border-right: solid #334155;
        padding: 1;
    }
    #main {
        width: 64%;
        padding: 1;
    }
    .section-title {
        color: #7dd3fc;
        text-style: bold;
        margin: 1 0 0 0;
    }
    DataTable {
        height: 1fr;
        margin: 0 0 1 0;
        border: solid #1e293b;
    }
    #detail {
        height: 12;
        border: solid #334155;
        padding: 1;
        margin-bottom: 1;
    }
    #activity {
        height: 1fr;
    }
    #sessions, #paths {
        height: 7;
        min-height: 5;
    }
    .theme-light Screen {
        background: #f8fafc;
        color: #1e293b;
    }
    .theme-light #sidebar {
        border-right: solid #cbd5e1;
    }
    .theme-light DataTable {
        border: solid #cbd5e1;
    }
    .theme-matrix Screen {
        background: #07130b;
        color: #b7f7c0;
    }
    .theme-matrix #sidebar {
        border-right: solid #1d7a42;
    }
    .theme-matrix .section-title {
        color: #86efac;
    }
    .theme-matrix DataTable {
        border: solid #14532d;
    }
    .theme-minimal Screen {
        background: #171717;
        color: #d4d4d4;
    }
    .theme-minimal #sidebar {
        border-right: solid #525252;
    }
    .theme-minimal .section-title {
        color: #d4d4d4;
    }
    .theme-minimal DataTable {
        border: solid #404040;
    }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("h", "focus_hosts", "Hosts"),
        Binding("n", "focus_networks", "Networks"),
        Binding("s", "focus_sessions", "Sessions"),
        Binding("p", "focus_pivots", "Pivots"),
        Binding("m", "focus_networks", "Map"),
        Binding("e", "focus_search", "Enum"),
        Binding("ctrl+p", "focus_search", "Command palette"),
        Binding("ctrl+f", "focus_search", "Search"),
    ]

    def __init__(self, paths: WorkspacePaths) -> None:
        super().__init__()
        self.paths = paths
        self.config = load_config()
        self.add_class(f"theme-{self.config.theme}")
        self.workspace = WorkspaceService(paths)
        self.SUB_TITLE = f"Infrastructure workspace · {self.config.theme}"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Static("", id="summary")
                yield Label("HOSTS", classes="section-title")
                yield DataTable(id="hosts", cursor_type="row")
                yield Label("NETWORKS", classes="section-title")
                yield DataTable(id="networks", cursor_type="row")
            with Vertical(id="main"):
                yield Input(placeholder="Search workspace (Ctrl+P)", id="search")
                yield Static("Selecione um host para ver detalhes.", id="detail")
                yield Label("SESSIONS", classes="section-title")
                yield DataTable(id="sessions", cursor_type="row")
                yield Label("ACCESS PATHS", classes="section-title")
                yield DataTable(id="paths", cursor_type="row")
                yield Label("ACTIVITY", classes="section-title")
                yield DataTable(id="activity", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self._configure_tables()
        self.action_refresh()

    def _configure_tables(self) -> None:
        self.query_one("#hosts", DataTable).add_columns("ID", "NAME", "IP", "USER", "OS", "STATUS")
        self.query_one("#networks", DataTable).add_columns("CIDR", "VIA", "PATH")
        self.query_one("#sessions", DataTable).add_columns(
            "ID", "NAME", "HOST", "TRANSPORT", "STATUS"
        )
        self.query_one("#paths", DataTable).add_columns("TARGET", "HOPS", "STATE", "CONF")
        self.query_one("#activity", DataTable).add_columns("TIME", "TYPE", "MESSAGE")

    def action_refresh(self) -> None:
        """Refresh all read models from SQLite."""

        self.workspace = WorkspaceService(self.paths)
        hosts_table = self.query_one("#hosts", DataTable)
        networks_table = self.query_one("#networks", DataTable)
        sessions_table = self.query_one("#sessions", DataTable)
        paths_table = self.query_one("#paths", DataTable)
        activity_table = self.query_one("#activity", DataTable)
        hosts_table.clear()
        networks_table.clear()
        sessions_table.clear()
        paths_table.clear()
        activity_table.clear()

        for host in self.workspace.hosts.list():
            hosts_table.add_row(
                str(host.id),
                host.name,
                str(host.ip),
                host.user or "-",
                host.os or "-",
                host.status.value,
                key=str(host.id),
            )

        observations = ObservationRepository(self.workspace.database, self.workspace.lab.id)
        for network in observations.list_table("networks"):
            via_ids = json.loads(str(network["reachable_via_json"]))
            via_names: list[str] = []
            for host_id in via_ids:
                host_for_network = self.workspace.hosts.get(str(host_id))
                if host_for_network is not None:
                    via_names.append(host_for_network.name or str(host_id))
            networks_table.add_row(
                network["cidr"],
                network["gateway"] or "direct",
                " → ".join(via_names) or "direct",
            )

        for session in self.workspace.sessions.list():
            sessions_table.add_row(
                str(session.id),
                session.name,
                str(session.host_id or "-"),
                session.transport.value,
                session.status.value,
                key=str(session.id),
            )

        for path in self.workspace.access_paths.list()[:30]:
            paths_table.add_row(
                f"{path.target_address}:{path.target_port or '-'}",
                " -> ".join(str(item) for item in path.hop_host_ids) or "-",
                path.state.value,
                f"{path.confidence}%",
                key=f"path-{path.id}",
            )

        for event in reversed(self.workspace.events.list(limit=50)):
            activity_table.add_row(event["created_at"], event["event_type"], event["message"])

        hosts = self.workspace.hosts.list()
        pivots = PivotRepository(self.workspace.database, self.workspace.lab.id).list()
        active_shells = self.workspace.shells.list(status="active")
        sessions = self.workspace.sessions.list()
        access_paths = self.workspace.access_paths.list()
        self.query_one("#summary", Static).update(
            f"Lab: {self.workspace.lab.name}\n"
            f"Platform: {self.workspace.lab.platform} | Status: {self.workspace.lab.status}\n"
            f"Created: {self.workspace.lab.created_at:%Y-%m-%d}\n"
            f"Hosts: {len(hosts)} | Networks: {len(networks_table.rows)}\n"
            f"Pivots: {len(pivots)} | Active shells: {len(active_shells)}\n"
            f"Sessions: {len(sessions)} | Access paths: {len(access_paths)}"
        )
        if hosts:
            self._show_host(hosts[0].id)

    def action_focus_hosts(self) -> None:
        self.query_one("#hosts", DataTable).focus()

    def action_focus_networks(self) -> None:
        self.query_one("#networks", DataTable).focus()

    def action_focus_activity(self) -> None:
        self.query_one("#activity", DataTable).focus()

    def action_focus_sessions(self) -> None:
        self.query_one("#sessions", DataTable).focus()

    def action_focus_pivots(self) -> None:
        """Surface pivot candidates without starting any tunnel."""

        pivots = PivotRepository(self.workspace.database, self.workspace.lab.id).list()
        self.notify(f"{len(pivots)} pivot candidate(s) registered")

    def action_focus_search(self) -> None:
        """Focus the global search input."""

        self.query_one("#search", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Use the search field as a compact command palette."""

        query = event.value.strip()
        if not query:
            self.action_refresh()
            return
        results = SearchService(self.workspace).search(query)
        detail = self.query_one("#detail", Static)
        if not results:
            detail.update(f"No results for: {query}")
            self.notify("No matching workspace records", severity="warning")
            return
        detail.update(
            "\n".join(
                [
                    f"Search: {query}",
                    *[f"[{item['kind']}] {item['detail']}" for item in results[:12]],
                ]
            )
        )
        self.notify(f"{len(results)} result(s)")

    def _show_host(self, host_id: int) -> None:
        host = self.workspace.hosts.get(str(host_id))
        if host is None:
            return
        observations = ObservationRepository(self.workspace.database, self.workspace.lab.id)
        interfaces = observations.list_table("interfaces", host.id)
        services = observations.list_table("services", host.id)
        neighbors = observations.list_table("neighbors", host.id)
        networks = sorted({str(row["network"]) for row in interfaces})
        notes = self.workspace.notes.list(entity_type="host", entity_id=host.id)
        detail = self.query_one("#detail", Static)
        detail.update(
            "\n".join(
                [
                    f"[b]{host.name}[/b]",
                    f"IP: {host.ip}    User: {host.user or '-'}    OS: {host.os or '-'}",
                    f"Status: {host.status.value}",
                    f"Interfaces: {len(interfaces)}    Networks: {len(networks)}",
                    f"Neighbors: {len(neighbors)}    Services: {len(services)}",
                    f"Notes: {len(notes)}",
                ]
            )
        )

    @staticmethod
    def _row_value(row_key: object) -> int | None:
        value = getattr(row_key, "value", row_key)
        try:
            return int(str(value))
        except ValueError:
            return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Show details when the user selects a host row."""

        if event.data_table.id == "hosts":
            host_id = self._row_value(event.row_key)
            if host_id is not None:
                self._show_host(host_id)
        elif event.data_table.id == "sessions":
            session_id = self._row_value(event.row_key)
            if session_id is not None:
                session = self.workspace.sessions.get(session_id)
                if session is not None:
                    self.query_one("#detail", Static).update(
                        "\n".join(
                            [
                                f"[b]{session.name}[/b]",
                                f"Transport: {session.transport.value} | "
                                f"Status: {session.status.value}",
                                f"Endpoint: {session.endpoint or '-'} | PID: {session.pid or '-'}",
                                f"Health: {session.health or '-'} | Error: {session.error or '-'}",
                            ]
                        )
                    )


def run_tui(paths: WorkspacePaths) -> None:
    """Run the dashboard for one workspace."""

    WorkspaceTUI(paths).run()
