from ctfws.models.host import HostCreate
from ctfws.models.shell import ShellCreate, ShellStatus, ShellType
from ctfws.services.shells import ShellService
from ctfws.shell.helper import ToolAvailability, suggestions
from ctfws.shell.tmux import TmuxManager


def test_shell_registry_and_status(workspace) -> None:
    host = workspace.add_host(HostCreate(name="web01", ip="10.10.10.20"))
    service = ShellService(workspace)
    shell = service.add(
        ShellCreate(host_id=host.id, name="web-main", type=ShellType.REVERSE, user="www-data")
    )

    assert shell.status == ShellStatus.ACTIVE
    assert service.update_status(shell.id, ShellStatus.DETACHED).status == ShellStatus.DETACHED
    assert service.rename(shell.id, "web-detached").name == "web-detached"
    assert workspace.shells.list()[0].name == "web-detached"


def test_helper_only_returns_suggestions_for_available_tools() -> None:
    available = [
        ToolAvailability("python3", "python3"),
        ToolAvailability("script", "script"),
        ToolAvailability("socat", None),
    ]

    result = suggestions(available)

    assert {item.tool for item in result} == {"python3", "script"}
    assert all(item.command for item in result)


def test_tmux_plan_is_explicit_and_deterministic() -> None:
    manager = TmuxManager("ctf-pivot-lab")

    commands = manager.commands()

    assert commands[0] == ["tmux", "new-session", "-d", "-s", "ctf-pivot-lab", "-n", "dashboard"]
    assert commands[1][-1] == "pivot"
    assert all(command[0] == "tmux" for command in commands)


def test_tmux_host_windows_are_unique_and_deterministic() -> None:
    windows = TmuxManager.windows_for_hosts(["db01", "web01", "web01"])

    names = [window.name for window in windows]
    assert names == [
        "dashboard",
        "shell-db01",
        "enum-db01",
        "shell-web01",
        "enum-web01",
        "pivot",
        "notes",
        "loot",
        "logs",
    ]


def test_tmux_plan_skips_existing_windows() -> None:
    manager = TmuxManager("ctf-pivot-lab")

    commands = manager.commands_for_state(
        manager.windows_for_hosts(["web01"]),
        existing_windows={"dashboard", "shell-web01", "enum-web01"},
        session_exists=True,
    )

    assert [command[-1] for command in commands] == ["pivot", "notes", "loot", "logs"]
