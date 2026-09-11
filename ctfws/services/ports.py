"""In-process OS port leases used while a runtime plan is being reviewed.

Selecting an ephemeral port and immediately closing the probe socket leaves a
race between the review screen and the actual listener.  A workspace motor
therefore keeps a bound socket for every planned local listener until the
resource starts, is cancelled, or the motor shuts down.  The start path still
re-checks the exact port after a restart because old leases cannot be
recreated safely from SQLite alone.
"""

from __future__ import annotations

import socket
import uuid
from dataclasses import dataclass


def _socket_address(address: str, port: int, family: int) -> tuple[object, ...]:
    """Return the platform tuple required by an IPv4 or IPv6 socket."""

    if family == socket.AF_INET6:
        return (address, port, 0, 0)
    return (address, port)


def _socket_family(address: str) -> int:
    """Resolve a TCP family without accepting arbitrary protocols."""

    infos = socket.getaddrinfo(address, 0, type=socket.SOCK_STREAM)
    if not infos:
        raise ValueError(f"Endereço local inválido: {address}")
    return int(infos[0][0])


@dataclass
class PortLease:
    """A bound socket that reserves one local TCP endpoint."""

    address: str
    port: int
    _socket: socket.socket | None
    token: str

    def release(self) -> None:
        """Release the OS reservation exactly once."""

        sock, self._socket = self._socket, None
        if sock is not None:
            sock.close()


class PortLeaseRegistry:
    """Keep local listener reservations scoped to one workspace motor."""

    def __init__(self) -> None:
        self._leases: dict[str, PortLease] = {}
        self._resource_tokens: dict[tuple[str, int], str] = {}

    def reserve(self, address: str, requested_port: int | None = 0) -> PortLease:
        """Reserve an exact port, or ask the OS for an ephemeral one.

        The socket remains bound until :meth:`release_for` or :meth:`close`.
        This prevents two plans created by the same motor from receiving the
        same port and closes the review-to-start race for normal OS binds.
        """

        requested_port = int(requested_port or 0)
        if requested_port < 0 or requested_port > 65535:
            raise ValueError("A porta local deve estar entre 0 e 65535.")
        family = _socket_family(address)
        attempts = 32 if requested_port == 0 else 1
        last_error: OSError | None = None
        for _ in range(attempts):
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                sock.bind(_socket_address(address, requested_port, family))
                sock.listen(1)
                actual_port = int(sock.getsockname()[1])
                token = uuid.uuid4().hex
                lease = PortLease(address, actual_port, sock, token)
                self._leases[token] = lease
                return lease
            except OSError as error:
                last_error = error
                sock.close()
                if requested_port != 0:
                    break
        if requested_port:
            raise ValueError(
                f"A porta local {address}:{requested_port} está ocupada ou não está disponível; "
                "escolha outra."
            ) from last_error
        raise RuntimeError(
            f"Não foi possível reservar uma porta local em {address}."
        ) from last_error

    def attach(self, resource_type: str, resource_id: int, lease: PortLease) -> None:
        """Associate a lease with a persisted resource after its insert commits."""

        if lease.token not in self._leases:
            raise ValueError("A reserva de porta já foi liberada.")
        key = (resource_type, resource_id)
        previous = self._resource_tokens.get(key)
        if previous is not None and previous != lease.token:
            self.release(previous)
        self._resource_tokens[key] = lease.token

    def release_for(self, resource_type: str, resource_id: int) -> None:
        """Release the reservation owned by one forward or context."""

        key = (resource_type, resource_id)
        token = self._resource_tokens.pop(key, None)
        if token is not None:
            self.release(token)

    def release(self, token: str) -> None:
        """Release a lease token, ignoring an already released token."""

        lease = self._leases.pop(token, None)
        if lease is None:
            return
        for key, value in tuple(self._resource_tokens.items()):
            if value == token:
                self._resource_tokens.pop(key, None)
        lease.release()

    def ensure_available(self, address: str, port: int) -> None:
        """Fail fast if a persisted plan lost its in-memory reservation."""

        lease = self.reserve(address, port)
        self.release(lease.token)

    def close(self) -> None:
        """Release all leases during motor shutdown."""

        for token in tuple(self._leases):
            self.release(token)
