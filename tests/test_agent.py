import json
import subprocess
import sys

from ctfws.agent import COMMANDS, collect


def test_agent_uses_fixed_command_allowlist() -> None:
    assert "whoami" in COMMANDS
    assert all(isinstance(args, tuple) for args in COMMANDS.values())


def test_agent_output_has_normalized_sections() -> None:
    payload = collect()

    assert payload["format"] == "ctfws-agent-v1"
    assert set(payload["system"]) >= {"USER", "UID", "HOSTNAME", "ARCHITECTURE"}
    assert "outputs" in payload


def test_agent_cli_emits_parseable_json() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "ctfws", "agent"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout)["format"] == "ctfws-agent-v1"
