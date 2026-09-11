"""Regression tests for the operator-facing CLI error boundary."""

import pytest
import typer

from ctfws.cli.app import _handle_error


def test_cli_permission_errors_are_actionable() -> None:
    """A protected system workspace must not expose a traceback to operators."""

    with pytest.raises(typer.Exit) as raised:
        _handle_error(PermissionError(13, "Permission denied"))

    assert raised.value.exit_code == 1
