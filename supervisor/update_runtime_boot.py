"""The first instruction a pinned assisted-update worker executes.

Run as ``python -m supervisor.update_runtime_boot <handoff-path>`` with the
snapshot as the working directory, so the ONE module that must not come from a
conflicted tree is the one this file lives in. ``multiprocessing``'s own child
entry is deliberately not used: under spawn and forkserver it rebuilds the
parent's ``__main__`` from its path, which for this installation is ``server.py``
— reading it out of a half-merged tree is the ``SyntaxError`` that killed every
replacement resolver in the incident this module closes.

Its argv carries ONE path and no secrets. The queue proxies and the
``multiprocessing`` authkey that authenticates them travel in the owner-only
handoff file, which is consumed (read then unlinked) before anything else runs.
Only the supervisor writes that file, and only this process reads it: the pickle
is trusted host state, not input.
"""

from __future__ import annotations

import pathlib
import pickle
import sys


def install_snapshot_sys_path(root_text: str, repo_dir: str = "") -> None:
    """Put the snapshot first on ``sys.path`` and drop the mutable checkout.

    The bootstrap calls this before it imports the worker, and ``worker_main``
    calls it again before it imports the agent. Both insertions root at the
    snapshot. Leaving the live checkout on the path is how one process ends up
    importing a second, conflicted generation.
    """
    root_text = str(root_text)
    skip = {"", root_text}
    repo_keys: set[str] = set()
    if repo_dir:
        skip.add(str(repo_dir))
        try:
            resolved = str(pathlib.Path(repo_dir).resolve())
        except OSError:
            resolved = ""
        if resolved:
            skip.add(resolved)
            repo_keys.add(resolved)
    kept: list[str] = []
    for entry in sys.path:
        if entry in skip:
            continue
        if repo_keys:
            try:
                if str(pathlib.Path(entry).resolve()) in repo_keys:
                    continue
            except OSError:
                pass
        kept.append(entry)
    sys.path[:] = [root_text, *kept]


def runtime_root() -> pathlib.Path:
    """The snapshot THIS file was imported from.

    Derived from ``__file__`` rather than taken from the handoff: the root that
    must govern imports is the one the running code actually came from, so a
    mismatch between the two is a fault to refuse, not a value to prefer."""
    return pathlib.Path(__file__).resolve().parents[1]


def consume_handoff(path: str) -> dict:
    """Read the one-time handoff and remove it, so the authkey stops existing."""
    handoff = pathlib.Path(path)
    data = handoff.read_bytes()
    try:
        handoff.unlink()
    except OSError:
        # The parent unlinks it as a backstop; losing the race is not a reason
        # to refuse a worker that already holds the bytes.
        pass
    payload = pickle.loads(data)
    if not isinstance(payload, dict) or "authkey" not in payload or "payload" not in payload:
        raise RuntimeError("pinned worker handoff is malformed")
    return payload


def main(argv: "list[str] | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        raise SystemExit("usage: python -m supervisor.update_runtime_boot <handoff-path>")
    root = runtime_root()
    # Imports, tool discovery and ``__file__``-relative resources all root here.
    # The live repository is NOT added: that is the mixed-generation import this
    # whole path exists to prevent. The repository the worker EDITS arrives as
    # ``repo_dir`` below and stays the mutable candidate. This first insertion
    # happens before the handoff names that checkout; the second, just below,
    # drops it once the handoff is known and before the worker module is imported.
    root_text = str(root)
    install_snapshot_sys_path(root_text)

    outer = consume_handoff(argv[0])
    import multiprocessing

    # BEFORE unpickling the proxies: rebuilding a manager proxy opens an
    # authenticated connection using ``current_process().authkey``, which this
    # plain subprocess minted independently of the supervisor's.
    multiprocessing.current_process().authkey = outer["authkey"]
    spec = pickle.loads(outer["payload"])

    pinned_root = str(spec.get("runtime_root") or "")
    if pinned_root and pathlib.Path(pinned_root).resolve() != root:
        raise SystemExit(
            f"pinned worker runtime mismatch: booted from {root}, assigned {pinned_root}"
        )
    install_snapshot_sys_path(root_text, str(spec.get("repo_dir") or ""))

    from supervisor.worker_process import worker_main

    worker_main(
        int(spec["wid"]), spec["in_q"], spec["out_q"],
        str(spec["repo_dir"]), str(spec["drive_root"]),
        str(spec.get("custody_session_id") or ""),
        runtime_root=root_text,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
