"""Drop privileges and exec the fixed Ligolo proxy inside a net namespace.

This module is launched only by the root-owned namespace helper. It accepts a
small, typed argument set and never evaluates a shell command. The proxy
binary is verified by the helper before this worker is started.
"""

from __future__ import annotations

import argparse
import os
import pwd
import subprocess
import sys
from pathlib import Path
from typing import Any


def _setns(fd: int) -> None:
    setns = getattr(os, "setns", None)
    if callable(setns):
        setns(fd, 0)
        return
    # Python builds without os.setns are still supported on Linux through the
    # libc syscall. This fallback is deliberately limited to the network
    # namespace opened by the helper.
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.setns(fd, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Conduit internal routed proxy worker")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--proxy", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--selfcert-cache", type=Path, required=True)
    parser.add_argument("--selfcert-domain", required=True)
    parser.add_argument("--listen", required=True)
    parser.add_argument("--api-listen", required=True)
    parser.add_argument("--run-program")
    parser.add_argument("--run-arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if os.name == "nt":
        parser.error("O worker roteado exige Linux.")
    if not args.namespace.startswith("ctfws-") or "/" in args.namespace:
        parser.error("namespace inválido")
    netns_path = Path("/run/netns") / args.namespace
    if not netns_path.is_file():
        parser.error("namespace não encontrado")
    if not args.proxy.is_file() or not args.config.is_file():
        parser.error("artefato do proxy ausente")

    posix_pwd: Any = pwd
    getpwnam = posix_pwd.getpwnam
    account = getpwnam("ctfws")
    cloexec = int(os.O_CLOEXEC) if hasattr(os, "O_CLOEXEC") else 0
    fd = os.open(netns_path, os.O_RDONLY | cloexec)
    try:
        _setns(fd)
    finally:
        os.close(fd)

    # No supplementary groups are needed by the proxy. The managed binary
    # carries only CAP_NET_ADMIN so it can create its TUN in this namespace.
    posix_os: Any = os
    posix_os.setgroups([])
    posix_os.setgid(account.pw_gid)
    posix_os.setuid(account.pw_uid)
    if args.run_program is not None:
        command = [args.run_program, *(args.run_arguments or [])]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        output, _ = process.communicate()
        sys.stdout.buffer.write((output or b"")[: 4 * 1024 * 1024])
        return int(process.returncode or 0)
    command = [
        str(args.proxy),
        "--daemon",
        "--nobanner",
        "--selfcert",
        "--config",
        str(args.config),
        "--selfcert-cache",
        str(args.selfcert_cache),
        "--selfcert-domain",
        args.selfcert_domain,
        "--laddr",
        args.listen,
        "--api-laddr",
        args.api_listen,
    ]
    os.execv(args.proxy, command)
    return 0  # pragma: no cover - execv never returns on success.


if __name__ == "__main__":  # pragma: no cover - exercised on Linux helper hosts.
    raise SystemExit(main())
