"""Cross-instance discovery registry (files only; never touches processes).

Every live InkFall window writes one small JSON file describing itself:
its PID, the name of its QLocalServer, and the normalized paths of the
documents it holds open. A window preparing a save scans this directory
to find peers editing the same canonical file and talks to them over
their server (``app.instance_hub``).

Files whose PID is no longer alive are pruned on sight. Nothing here
enumerates, inspects, or kills processes — liveness is only ever *read*.
A crashed instance's helper processes are handled by the job-object
lifetime guard (``core.proc_lifetime``); its stale registry file is
merely ignored until pruned. PID reuse can, in principle, make a dead
window look alive — the IPC handshake is the real liveness test, so a
phantom peer simply fails to answer and the save falls back to the
ordinary destination-lock error path.

This module must stay Qt-free: it lives below ``app/`` (see
``tests/test_import_boundaries.py``).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "InstanceRecord",
    "instances_dir",
    "normalize_key",
    "pid_alive",
    "prune_stale",
    "remove_instance_file",
    "scan_instances",
    "write_instance",
]

#: Registry files are tiny and rewritten rarely (open/close/save); the
#: bound is a corruption guard, not a real limit.
_MAX_INSTANCE_FILE_BYTES = 1 << 20


@dataclass(frozen=True)
class InstanceRecord:
    """One parsed ``instance-*.json`` file."""

    pid: int
    server: str
    started_at: float
    docs: tuple[tuple[str, bool], ...]
    file: Path


def normalize_key(path: str) -> str:
    """Return the canonical cross-instance key for ``path``.

    Same semantics as ``document.manager.normalize_path`` (expanduser +
    resolve + normcase) so both windows key the same file identically;
    duplicated here to keep ``core/`` free of ``document/`` imports.
    """
    resolved = Path(path).expanduser().resolve()
    return os.path.normcase(str(resolved))


def instances_dir(directory: str | os.PathLike | None = None) -> Path:
    """Per-user runtime directory shared by all InkFall windows.

    ``directory`` (tests) wins, then the ``INKFALL_INSTANCES_DIR``
    environment override (test isolation), then the platform default:
    Windows ``%LOCALAPPDATA%\\InkFall\\instances``, other platforms
    following ``XDG_STATE_HOME``.
    """
    if directory is not None:
        base = Path(directory)
    else:
        override = os.environ.get("INKFALL_INSTANCES_DIR")
        if override:
            base = Path(override)
        elif os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData"
                        / "Local") / "InkFall" / "instances"
        else:
            state_home = os.environ.get("XDG_STATE_HOME") or (
                Path.home() / ".local" / "state")
            base = Path(state_home) / "inkfall" / "instances"
    base.mkdir(parents=True, exist_ok=True)
    return base


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` names a live process (read-only; never signals)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # No handle means the process does not exist (a vanished PID,
            # or one so protected it cannot even be queried — the rare
            # false "dead" only skips coordination, and the atomic
            # replace remains the destination arbiter either way).
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _instance_path(directory: Path, filename: str) -> Path:
    return Path(directory) / filename


def write_instance(directory: str | os.PathLike, pid: int, server: str,
                   docs: list[tuple[str, bool]], token: str | None = None,
                   started_at: float | None = None) -> str:
    """Atomically (re)write this window's registry file; return its name."""
    token = token or uuid.uuid4().hex[:12]
    payload = {
        "pid": int(pid),
        "started_at": float(started_at if started_at is not None
                            else time.time()),
        "server": str(server),
        "docs": [[str(path), bool(dirty)] for path, dirty in docs],
    }
    target = _instance_path(Path(directory),
                            f"instance-{int(pid)}-{token}.json")
    fd, tmp = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, target)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return target.name


def _parse_instance_file(path: Path) -> InstanceRecord | None:
    try:
        if path.stat().st_size > _MAX_INSTANCE_FILE_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        pid = int(data["pid"])
        server = str(data["server"])
        docs = tuple((str(p), bool(d)) for p, d in (data.get("docs") or ()))
        return InstanceRecord(
            pid=pid,
            server=server,
            started_at=float(data.get("started_at") or 0.0),
            docs=docs,
            file=path,
        )
    except Exception:
        return None


def scan_instances(directory: str | os.PathLike,
                   *, alive_only: bool = True) -> list[InstanceRecord]:
    """Parse every well-formed registry file, pruning dead peers' files."""
    records: list[InstanceRecord] = []
    try:
        entries = sorted(Path(directory).glob("instance-*.json"))
    except OSError:
        return records
    for path in entries:
        record = _parse_instance_file(path)
        if record is None:
            continue
        if alive_only and not pid_alive(record.pid):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        records.append(record)
    return records


def prune_stale(directory: str | os.PathLike) -> int:
    """Delete registry files of dead PIDs; return how many were removed."""
    removed = 0
    try:
        entries = sorted(Path(directory).glob("instance-*.json"))
    except OSError:
        return 0
    for path in entries:
        record = _parse_instance_file(path)
        if record is not None and not pid_alive(record.pid):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def remove_instance_file(directory: str | os.PathLike,
                         filename: str) -> None:
    """Remove this window's own registry file (best-effort)."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return
    try:
        _instance_path(Path(directory), filename).unlink()
    except OSError:
        pass
