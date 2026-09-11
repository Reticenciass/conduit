"""Operator-configurable resource limits shared by file services."""

from __future__ import annotations

import os

DEFAULT_MAX_FILE_BYTES = 1024 * 1024 * 1024
MIN_MAX_FILE_BYTES = 1024 * 1024
DEFAULT_MAX_WORKSPACE_BYTES: int | None = None
MIN_MAX_WORKSPACE_BYTES = 10 * 1024 * 1024
MIN_RETENTION_DAYS = 1


def max_file_bytes() -> int:
    """Return the configured per-file limit, defaulting to 1 GiB."""

    raw = os.getenv("CTFWS_MAX_FILE_BYTES")
    if raw is None or not raw.strip():
        return DEFAULT_MAX_FILE_BYTES
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError("CTFWS_MAX_FILE_BYTES precisa ser um inteiro em bytes.") from error
    if value < MIN_MAX_FILE_BYTES:
        raise ValueError(f"CTFWS_MAX_FILE_BYTES precisa ser >= {MIN_MAX_FILE_BYTES}.")
    return value


def max_workspace_bytes() -> int | None:
    """Return the optional quota for all regular files in one workspace."""

    raw = os.getenv("CTFWS_MAX_WORKSPACE_BYTES")
    if raw is None or not raw.strip() or raw.strip() == "0":
        return DEFAULT_MAX_WORKSPACE_BYTES
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(
            "CTFWS_MAX_WORKSPACE_BYTES precisa ser um inteiro em bytes (0 desabilita)."
        ) from error
    if value < MIN_MAX_WORKSPACE_BYTES:
        raise ValueError(
            f"CTFWS_MAX_WORKSPACE_BYTES precisa ser 0 ou >= {MIN_MAX_WORKSPACE_BYTES}."
        )
    return value


def retention_days() -> int | None:
    """Return the optional retention policy for logs and temporary files."""

    raw = os.getenv("CTFWS_RETENTION_DAYS")
    if raw is None or not raw.strip() or raw.strip() == "0":
        return None
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(
            "CTFWS_RETENTION_DAYS precisa ser um inteiro em dias (0 desabilita)."
        ) from error
    if value < MIN_RETENTION_DAYS:
        raise ValueError(f"CTFWS_RETENTION_DAYS precisa ser 0 ou >= {MIN_RETENTION_DAYS}.")
    return value
