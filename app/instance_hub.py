"""Local IPC between InkFall windows (QLocalServer + JSON-line protocol).

Each window runs one :class:`InstanceHub`: a QLocalServer with a unique
name, advertised in the shared instance registry
(``core.instance_registry``) together with the canonical paths it holds
open. A window about to save a PDF sends a ``release-request`` to every
peer editing the same file and waits (bounded) for ``release-response``.
The peer resolves the request on its GUI thread through
``releaseRequested`` and answers exactly once via :meth:`respond`.

Messages are single-line JSON objects:

``{"type": "release-request", "request_id": ..., "path": <normalized>}``
``{"type": "release-response", "request_id": ..., "status":
"released"|"refused"}``

Connection failures and timeouts are reported as ``None`` — never an
exception — so the caller can fall back to the ordinary destination-lock
error path.
"""

from __future__ import annotations

import atexit
import json
import os
import time

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from core.instance_registry import (
    instances_dir,
    remove_instance_file,
    write_instance,
)

#: How long a saver waits for one peer to answer a release request.
DEFAULT_RELEASE_TIMEOUT_S = 60.0
#: QLocalServer names map to named pipes; keep them well under the cap.
_MAX_SERVER_NAME = 200


class InstanceHub(QObject):
    """One window's presence in the cross-instance coordination protocol."""

    #: Emitted on the GUI thread when a peer asks us to release a file.
    releaseRequested = Signal(str, str)
    #: Emitted when the server stopped (tests/teardown).
    closed = Signal()

    def __init__(self, parent: QObject | None = None, *,
                 directory: str | os.PathLike | None = None,
                 token: str | None = None) -> None:
        super().__init__(parent)
        self._directory = instances_dir(directory)
        self._token = token or os.urandom(6).hex()
        self._server_name = f"inkfall-{os.getpid()}-{self._token}"[:_MAX_SERVER_NAME]
        self._file_name: str | None = None
        self._sockets: list[QLocalSocket] = []
        self._buffers: dict[QLocalSocket, bytes] = {}
        self._pending: dict[str, tuple[QLocalSocket, str]] = {}
        self._closed = False

        # A stale pipe from a same-PID reuse is removed before listening.
        QLocalServer.removeServer(self._server_name)
        self._server = QLocalServer(self)
        if not self._server.listen(self._server_name):
            raise RuntimeError(
                f"Cannot listen for instance coordination: "
                f"{self._server.errorString()}")
        self._server.newConnection.connect(self._accept_connection)
        self._file_name = write_instance(
            self._directory, os.getpid(), self._server_name, [],
            token=self._token)
        atexit.register(self._atexit_cleanup)

    # -- introspection ----------------------------------------------------

    @property
    def server_name(self) -> str:
        """This window's QLocalServer name (registry advertisement)."""
        return self._server_name

    @property
    def directory(self) -> str:
        """Shared registry directory (scan target for peers)."""
        return str(self._directory)

    # -- advertisement ------------------------------------------------------

    def publish(self, docs) -> None:
        """(Re)write this window's open-document advertisement."""
        if self._closed:
            return
        self._file_name = write_instance(
            self._directory, os.getpid(), self._server_name, list(docs),
            token=self._token)

    # -- incoming requests --------------------------------------------------

    def _accept_connection(self) -> None:
        while True:
            socket = self._server.nextPendingConnection()
            if socket is None:
                return
            socket.readyRead.connect(lambda s=socket: self._readable(s))
            socket.disconnected.connect(lambda s=socket: self._dropped(s))
            self._sockets.append(socket)
            self._readable(socket)

    def _readable(self, socket: QLocalSocket) -> None:
        buffer = self._buffers.get(socket, b"") + bytes(socket.readAll())
        while b"\n" in buffer:
            line, _, buffer = buffer.partition(b"\n")
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            if (message.get("type") == "release-request"
                    and isinstance(message.get("request_id"), str)
                    and isinstance(message.get("path"), str)):
                request_id = message["request_id"]
                self._pending[request_id] = (socket, message["path"])
                self.releaseRequested.emit(request_id, message["path"])
        if buffer:
            self._buffers[socket] = buffer
        else:
            self._buffers.pop(socket, None)

    def _dropped(self, socket: QLocalSocket) -> None:
        self._buffers.pop(socket, None)
        self._pending = {
            request_id: entry
            for request_id, entry in self._pending.items()
            if entry[0] is not socket
        }
        if socket in self._sockets:
            self._sockets.remove(socket)
        socket.deleteLater()

    def respond(self, request_id: str, status: str) -> None:
        """Answer one release request; exactly one response is sent."""
        entry = self._pending.pop(request_id, None)
        if entry is None:
            return
        socket, _path = entry
        payload = json.dumps({
            "type": "release-response",
            "request_id": request_id,
            "status": "released" if status == "released" else "refused",
        }).encode("utf-8") + b"\n"
        try:
            socket.write(payload)
            socket.flush()
        except Exception:
            pass

    # -- outgoing requests --------------------------------------------------

    def request_release(self, server_name: str, path: str, *,
                        timeout_s: float = DEFAULT_RELEASE_TIMEOUT_S,
                        request_id: str | None = None) -> str | None:
        """Ask a peer to release ``path``; "released"/"refused"/None.

        Bounded by ``timeout_s``. ``None`` covers unreachable peers and
        timeouts — the caller treats both as a refusal-classified save
        failure rather than a crash. Runs a small event-processing slice
        between wait attempts so the modal save dialog keeps painting.
        """
        if self._closed or server_name == self._server_name:
            return None
        request_id = request_id or f"rel-{time.monotonic_ns()}"
        socket = QLocalSocket()
        try:
            socket.connectToServer(server_name)
            if not socket.waitForConnected(2000):
                return None
            payload = json.dumps({
                "type": "release-request",
                "request_id": request_id,
                "path": str(path),
            }).encode("utf-8") + b"\n"
            socket.write(payload)
            socket.flush()
            deadline = time.monotonic() + max(0.0, float(timeout_s))
            buffer = b""
            while time.monotonic() < deadline:
                if socket.waitForReadyRead(100):
                    buffer += bytes(socket.readAll())
                    while b"\n" in buffer:
                        line, _, buffer = buffer.partition(b"\n")
                        response = self._parse_response(line, request_id)
                        if response is not None:
                            return response
                else:
                    if socket.state() != QLocalSocket.LocalSocketState.ConnectedState:
                        return None
                # Deliver the peer side's signals (its server socket lives
                # on this same thread in tests/simulated setups) and keep
                # the modal save dialog painting during the bounded wait.
                QCoreApplication.processEvents(QEventLoop.AllEvents, 20)
            return None
        finally:
            try:
                socket.disconnectFromServer()
            except Exception:
                pass

    @staticmethod
    def _parse_response(line: bytes, request_id: str) -> str | None:
        try:
            message = json.loads(line.decode("utf-8"))
        except Exception:
            return None
        if (message.get("type") == "release-response"
                and message.get("request_id") == request_id):
            status = message.get("status")
            return status if status in ("released", "refused") else None
        return None

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Stop serving, drop connections, and remove the registry file."""
        if self._closed:
            return
        self._closed = True
        try:
            atexit.unregister(self._atexit_cleanup)
        except Exception:
            pass
        for socket in list(self._sockets):
            try:
                socket.disconnectFromServer()
                socket.deleteLater()
            except Exception:
                pass
        self._sockets.clear()
        self._buffers.clear()
        self._pending.clear()
        try:
            self._server.close()
        except Exception:
            pass
        if self._file_name:
            remove_instance_file(self._directory, self._file_name)
        self.closed.emit()

    def _atexit_cleanup(self) -> None:
        try:
            self.close()
        except Exception:
            pass
