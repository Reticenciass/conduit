from pathlib import Path

import pytest
from typer.testing import CliRunner

from ctfws.cli.app import app


def test_cli_create_add_list_and_note(tmp_path: Path) -> None:
    runner = CliRunner()
    base = tmp_path / "ctf"

    created = runner.invoke(app, ["lab", "create", "Demo", "--base-dir", str(base)])
    assert created.exit_code == 0, created.stdout
    workspace = base / "demo"

    added = runner.invoke(
        app,
        ["--workspace", str(workspace), "host", "add", "10.0.0.5", "--name", "web01"],
    )
    assert added.exit_code == 0, added.stdout

    listed = runner.invoke(app, ["--workspace", str(workspace), "host", "list"])
    assert listed.exit_code == 0, listed.stdout
    assert "web01" in listed.stdout
    assert "10.0.0.5" in listed.stdout

    noted = runner.invoke(
        app,
        ["--workspace", str(workspace), "note", "add", "host", "1", "config encontrada"],
    )
    assert noted.exit_code == 0, noted.stdout


def test_cli_import_map_report_and_tags(tmp_path: Path) -> None:
    runner = CliRunner()
    base = tmp_path / "ctf"
    created = runner.invoke(app, ["lab", "create", "Demo", "--base-dir", str(base)])
    assert created.exit_code == 0, created.stdout
    workspace = base / "demo"
    assert (
        runner.invoke(
            app,
            ["--workspace", str(workspace), "host", "add", "10.10.10.20", "--name", "web01"],
        ).exit_code
        == 0
    )

    fixture = Path(__file__).parent / "fixtures" / "ip_addr.txt"
    imported = runner.invoke(
        app,
        ["--workspace", str(workspace), "import", "ip", "web01", "--file", str(fixture)],
    )
    assert imported.exit_code == 0, imported.stdout
    network = runner.invoke(app, ["--workspace", str(workspace), "network", "list"])
    assert network.exit_code == 0, network.stdout
    assert "10.10.10.0/24" in network.stdout
    tagged = runner.invoke(
        app,
        ["--workspace", str(workspace), "host", "tag", "web01", "web"],
    )
    assert tagged.exit_code == 0, tagged.stdout
    report = runner.invoke(
        app,
        ["--workspace", str(workspace), "report", "--format", "all"],
    )
    assert report.exit_code == 0, report.stdout
    assert (workspace / "reports" / "report.md").is_file()


def test_conduit_start_discovers_workspace_in_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("uvicorn")
    runner = CliRunner()
    base = tmp_path / "ctf"
    created = runner.invoke(app, ["lab", "create", "Demo", "--base-dir", str(base)])
    assert created.exit_code == 0, created.stdout
    workspace = base / "demo"
    monkeypatch.chdir(workspace)

    captured: dict[str, object] = {}

    import uvicorn

    def fake_run(application: object, **kwargs: object) -> None:
        captured["application"] = application
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    started = runner.invoke(app, ["start"])

    assert started.exit_code == 0, started.stdout
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8765
    assert "Conduit iniciando" in started.stdout
    assert "workspace=" in started.stdout
    assert workspace.name in started.stdout


def test_conduit_start_uses_workspace_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("uvicorn")
    runner = CliRunner()
    base = tmp_path / "ctf"
    created = runner.invoke(app, ["lab", "create", "Demo", "--base-dir", str(base)])
    assert created.exit_code == 0, created.stdout
    workspace = base / "demo"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONDUIT_WORKSPACE", str(workspace))

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: None)
    started = runner.invoke(app, ["start", "--port", "9876"])

    assert started.exit_code == 0, started.stdout
    assert "Conduit iniciando" in started.stdout
    assert "9876" in started.stdout
