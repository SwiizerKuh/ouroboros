"""Starting one worker from a pinned runtime, and making it look like a slot.

The pool already owns readiness, pid records, health, assignment, retry and
teardown; none of that is re-implemented here. What a pinned worker needs is a
different BIRTH: a plain subprocess whose first instruction comes from the
snapshot (``supervisor/update_runtime_boot``) instead of ``multiprocessing``'s
child entry, which would rebuild the parent's ``__main__`` from ``server.py`` out
of the conflicted tree.

A plain subprocess cannot inherit a ``multiprocessing.Queue``, so the channels are
manager-backed proxies — the input queue from the supervisor's existing event-queue
manager, the output queue the event bus itself — handed over in a private one-time
file together with the authkey that authenticates them. Two narrow adapters make
the result indistinguishable to the pool: ``PinnedWorkerProcess`` answers the
``pid``/``is_alive``/``exitcode``/``join``/``terminate`` calls the lifecycle makes
of a ``Process``, and ``PinnedWorkerQueue`` answers the ``close`` /
``cancel_join_thread`` a proxy has no equivalent for.
"""

from __future__ import annotations

import logging
import os
import pathlib
import pickle
import subprocess
import sys
import time
import uuid
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger(__name__)


def _runtime():
    """The update-domain snapshot store, imported when a pinned worker is born.

    This module is the supervisor's worker-birth adapter. The snapshot store
    stays in the update domain, and the existing direction into it is the
    function-local import the pool already uses in ``start_worker_slot_process``.
    A module-level import here would make that direction strict.
    """
    from supervisor import update_runtime

    return update_runtime

# How long the child may take to consume its handoff. Its first statements read
# and unlink the file, so the normal case is milliseconds; the bound exists so the
# authkey's lifetime on disk is decided here rather than by a child that never ran.
HANDOFF_CONSUME_TIMEOUT_SEC = 15.0


class PinnedWorkerQueue:
    """A manager queue proxy with the two ``multiprocessing.Queue`` calls the pool makes.

    ``close``/``cancel_join_thread`` exist on a real queue because it owns a feeder
    thread and a pipe. A proxy owns neither — the manager process holds the queue —
    so the honest implementation of both is to do nothing. Everything else is the
    proxy's own behaviour."""

    __slots__ = ("_proxy",)

    def __init__(self, proxy: Any) -> None:
        self._proxy = proxy

    @property
    def proxy(self) -> Any:
        return self._proxy

    def close(self) -> None:
        """No-op: the manager owns this queue's buffers, not this process."""

    def cancel_join_thread(self) -> None:
        """No-op: a proxy has no feeder thread to abandon."""

    def __getattr__(self, name: str) -> Any:
        if name == "_proxy":  # never recurse through the slot this delegates to
            raise AttributeError(name)
        return getattr(self._proxy, name)


class PinnedWorkerProcess:
    """``multiprocessing.Process``'s pool-facing surface over a ``Popen``.

    Narrow on purpose: these are the members the queue, health, reaper and
    lifecycle paths actually read. ``daemon`` is accepted and ignored because a
    plain subprocess has no daemon flag — what actually stops an orphan is the
    child's ppid lifeline plus its durable custody record, both of which the
    bootstrap and ``spawn_supervised`` install."""

    def __init__(self, popen: subprocess.Popen, *, wid: int, runtime_sha: str) -> None:
        self._popen = popen
        self.wid = int(wid)
        self.runtime_sha = str(runtime_sha)
        self.name = f"pinned-worker-{wid}"
        # A plain attribute, settable and ignored: see the class docstring.
        self.daemon = True

    @property
    def pid(self) -> int:
        return int(self._popen.pid)

    @property
    def exitcode(self) -> Optional[int]:
        """``None`` while alive, else the status — negative for a signal, as ``Process`` reports."""
        return self._popen.poll()

    def is_alive(self) -> bool:
        return self._popen.poll() is None

    def join(self, timeout: Optional[float] = None) -> None:
        try:
            self._popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass

    def terminate(self) -> None:
        try:
            self._popen.terminate()
        except Exception:
            log.debug("pinned worker terminate failed", exc_info=True)

    def kill(self) -> None:
        try:
            self._popen.kill()
        except Exception:
            log.debug("pinned worker kill failed", exc_info=True)

    def close(self) -> None:
        """Release the Popen's own handles; harmless while the child still runs."""
        try:
            self._popen.poll()
        except Exception:
            log.debug("pinned worker close poll failed", exc_info=True)


def pinned_worker_queue(manager: Any) -> PinnedWorkerQueue:
    """A manager-backed worker input queue a plain subprocess can be handed."""
    if manager is None:
        raise _runtime().RuntimePinError("pinned worker input queue needs the supervisor queue manager")
    return PinnedWorkerQueue(manager.Queue())


def bootstrap_interpreter() -> str:
    """The interpreter that can execute the snapshot's bootstrap module.

    A source install runs on one, so ``sys.executable`` is exact. A frozen install
    does not: its ``sys.executable`` is the bundle, which cannot be told to import
    a module from somewhere else, so the packaged ``python-standalone`` runtime is
    the only candidate — and if it is absent the caller must refuse rather than
    start a worker that would import the conflicted tree."""
    if not getattr(sys, "frozen", False):
        return str(sys.executable)
    from ouroboros.platform_layer import bundled_resource_bases, embedded_python_candidates

    for base in bundled_resource_bases():
        for candidate in embedded_python_candidates(base):
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
    raise _runtime().RuntimePinError(
        "this frozen installation has no embedded interpreter to boot a pinned "
        "assisted-update worker from"
    )


def _write_handoff(drive_root: Any, payload: Dict[str, Any]) -> pathlib.Path:
    """Owner-only one-time file carrying the authkey, the proxies and the worker args.

    Never argv and never a log line: the authkey authenticates every connection to
    the supervisor's queue manager."""
    import multiprocessing

    directory = _runtime().handoff_dir(drive_root)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        log.debug("could not tighten pinned worker handoff directory mode", exc_info=True)
    path = directory / f"{uuid.uuid4().hex}.handoff"
    blob = pickle.dumps({
        # Plain bytes: an AuthenticationString refuses to pickle outside a spawn.
        "authkey": bytes(multiprocessing.current_process().authkey or b""),
        "payload": pickle.dumps(payload),
    })
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(blob)
    return path


def _await_handoff_consumed(path: pathlib.Path, proc: subprocess.Popen) -> None:
    """Block until the child has taken the handoff, so the authkey stops existing.

    Bounded, and a timeout is a refusal: a child that has not read its channels
    cannot become a resolver, and leaving the file would leave the key."""
    deadline = time.monotonic() + HANDOFF_CONSUME_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if not path.exists():
            return
        if proc.poll() is not None:
            raise _runtime().RuntimePinError(
                f"pinned worker exited before reading its handoff (exit {proc.returncode})"
            )
        time.sleep(0.02)
    raise _runtime().RuntimePinError("pinned worker did not read its handoff within the startup bound")


def start_pinned_worker(
    wid: int,
    pin: Dict[str, Any],
    *,
    in_q: Any,
    event_q: Any,
    repo_dir: Any,
    drive_root: Any,
    custody_session_id: str = "",
) -> PinnedWorkerProcess:
    """Start worker ``wid`` from the pinned snapshot, or raise.

    Fails closed in every direction: an unvalidatable snapshot, a missing
    interpreter, a child that dies before reading its channels and a failed
    custody record all raise. There is no path from here to a worker that imports
    the live tree, because that worker is the thing the pin exists to prevent.
    """
    ok, reason = _runtime().validate_runtime_snapshot(pin)
    if not ok:
        raise _runtime().RuntimePinError(reason)
    root = pathlib.Path(str(pin["root"]))
    root_text = str(root)
    interpreter = bootstrap_interpreter()

    from ouroboros.platform_layer import scrub_repo_from_pythonpath
    from ouroboros.process_custody import spawn_supervised

    env = scrub_repo_from_pythonpath(dict(os.environ), repo_dir)
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([root_text] + ([inherited] if inherited else []))
    # Bytecode OUTSIDE the snapshot: a __pycache__ written into a manifested tree
    # would make the next validation (a respawn) read as tampering.
    env["PYTHONPYCACHEPREFIX"] = str(_runtime().bytecode_cache_root(drive_root))
    # The transport policy's worker marker, normally set inside worker_main; set
    # here too so it is already true for the bootstrap's own imports.
    env["OUROBOROS_IN_WORKER"] = "1"
    # Declares the pinned process shape: review_helpers uses this marker to
    # keep governance resources on the configured system repo, not this import
    # snapshot. OUROBOROS_REPO_DIR / OUROBOROS_DATA_DIR remain the mutable target;
    # repo_dir below is the live checkout.
    env["OUROBOROS_ASSISTED_RUNTIME_ROOT"] = root_text

    handoff = _write_handoff(drive_root, {
        "wid": int(wid),
        "in_q": getattr(in_q, "proxy", in_q),
        "out_q": event_q,
        "repo_dir": str(repo_dir),
        "drive_root": str(drive_root),
        "custody_session_id": str(custody_session_id or ""),
        "runtime_root": root_text,
    })
    proc: Optional[subprocess.Popen] = None
    try:
        proc = spawn_supervised(
            [interpreter, "-m", "supervisor.update_runtime_boot", str(handoff)],
            drive_root=pathlib.Path(drive_root),
            purpose=f"worker:{wid}:pinned_runtime",
            scope="session",
            cwd=root_text,
            env=env,
            stdin=subprocess.DEVNULL,
        )
        _await_handoff_consumed(handoff, proc)
    except BaseException:
        if proc is not None:
            from ouroboros.platform_layer import kill_process_tree

            try:
                kill_process_tree(proc)
                proc.wait(timeout=5)
            except Exception:
                log.debug("pinned worker teardown after failed start", exc_info=True)
        raise
    finally:
        try:
            handoff.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("pinned worker handoff file could not be removed", exc_info=True)
    log.info("started pinned worker %d from runtime %s (pid %d)",
             wid, str(pin.get("sha") or "")[:12], proc.pid)
    return PinnedWorkerProcess(proc, wid=wid, runtime_sha=str(pin.get("sha") or ""))
