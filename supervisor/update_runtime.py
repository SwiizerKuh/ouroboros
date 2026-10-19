"""The immutable runtime an assisted update's resolver imports from.

A managed assisted merge puts conflict markers into the SAME code the resolver
needs to start and to keep running. The incident this module exists to close had
both halves of that bootstrap flaw: the first worker booted clean and then hit
``ImportError`` on a function-local import that resolved out of the half-merged
tree (one process, two code generations), and every replacement died in
``multiprocessing``'s main-module reconstruction with ``SyntaxError`` on
``server.py``'s conflict markers.

The fix is a pin, not a retry. Before the merge is staged — while the tree is
still exactly ``pre_update_sha`` — the runtime code is published as a manifest-verified
copy under ``<drive_root>/state/update_runtime/<sha>/`` with a per-file manifest,
and the transaction records that pin. Assisted workers then import, discover and
resolve ``__file__``-relative resources from the snapshot, while the repository
they EDIT (``git_ops.REPO_DIR``, the agent's ``repo_dir``, tool access roots)
stays the live mutable candidate. One generation of code, one mutable target.

Publication reads the git OBJECT database (``git archive``), never the worktree.
A transaction written by an older updater — one that never recorded a pin — can
still be given that exact base after the conflicts are live, and only when the
snapshot directory is absent and the base contains the pinned-worker bootstrap.
A pin this update already recorded is not reconstructed. A missing or corrupt
snapshot is a refusal, and an existing invalid tree is left where it is: that
tree may be the forensic copy of the runtime the update was pinned to. Nothing
here falls back to the live tree, because importing the conflicted tree is the
failure this module replaces.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import shutil
import subprocess
import tarfile
import tempfile
import threading
import uuid
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

RUNTIME_PIN_SCHEMA_VERSION = 1
MANIFEST_NAME = "runtime_manifest.json"
STORE_DIR_NAME = "update_runtime"
HANDOFF_DIR_NAME = "handoff"

# The import surface plus the resources runtime code resolves relative to its own
# ``__file__`` (``ouroboros/safety.py`` reads ``prompts/SAFETY.md`` that way, and
# ``version.py`` reads ``VERSION``). Deliberately NOT the whole tree: ``web/``,
# ``docs/`` and ``scripts/`` are read through a repo root, and the repo root an
# assisted worker serves is the mutable candidate — the resolver's own test gate
# must run the CANDIDATE suite, not this frozen copy.
SNAPSHOT_PATHS: Tuple[str, ...] = ("VERSION", "server.py", "ouroboros", "prompts", "supervisor")

# The child entry point must exist INSIDE the snapshot: that is what makes a
# pinned worker's first instruction clean code. A base that predates this module
# cannot host a pinned worker, and recovery must refuse rather than improvise.
BOOTSTRAP_REL = "supervisor/update_runtime_boot.py"

_IGNORED_DIR_NAMES = frozenset({"__pycache__"})

_SELECTION_LOCK = threading.RLock()
_SELECTED_PIN: Dict[str, Any] = {}


class RuntimePinError(RuntimeError):
    """A pinned runtime could not be published, validated or selected."""


def runtime_store_root(drive_root: Any) -> pathlib.Path:
    """``<drive_root>/state/update_runtime`` — private installation state."""
    return pathlib.Path(drive_root) / "state" / STORE_DIR_NAME


def snapshot_root(drive_root: Any, sha: str) -> pathlib.Path:
    return runtime_store_root(drive_root) / str(sha)


def handoff_dir(drive_root: Any) -> pathlib.Path:
    return runtime_store_root(drive_root) / HANDOFF_DIR_NAME


def bytecode_cache_root(drive_root: Any) -> pathlib.Path:
    """Where a pinned child writes ``.pyc`` files.

    NOT inside the snapshot: a worker importing from a manifest-bound tree
    would otherwise create ``__pycache__`` entries that the NEXT validation reads
    as unexplained files, turning a healthy respawn into a refusal."""
    return runtime_store_root(drive_root) / "pycache"


def _iter_snapshot_files(root: pathlib.Path) -> Iterator[pathlib.Path]:
    """Every regular file of the snapshot except the manifest and bytecode caches.

    ``os.walk`` does not follow directory symlinks, but a later ``is_file`` or
    open would follow a file symlink and hash whatever it points at. Symlinks
    are not snapshot files; validation rejects them on their own.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = pathlib.Path(dirpath)
        retained: List[str] = []
        for name in sorted(dirnames):
            path = base / name
            if name in _IGNORED_DIR_NAMES or path.is_symlink():
                continue
            retained.append(name)
        dirnames[:] = retained
        for name in sorted(filenames):
            path = base / name
            if path.is_symlink():
                continue
            if path.parent == root and name == MANIFEST_NAME:
                continue
            if not path.is_file():
                continue
            yield path


def _digest_file(path: pathlib.Path) -> Tuple[str, int]:
    """Hash a regular file without following a symlink into another tree."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    digest = hashlib.sha256()
    size = 0
    try:
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest(), size


def _normalized_relative(relpath: str) -> Optional[str]:
    """A manifest path that is already posix-normalized and relative, or None."""
    if not isinstance(relpath, str) or not relpath or "\\" in relpath or "\x00" in relpath:
        return None
    if relpath.startswith("/"):
        return None
    pure = pathlib.PurePosixPath(relpath)
    if pure.is_absolute():
        return None
    parts = pure.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    normalized = "/".join(parts)
    if normalized != relpath:
        return None
    return normalized


def _path_escapes(root: pathlib.Path, relpath: str) -> bool:
    candidate = root.joinpath(*pathlib.PurePosixPath(relpath).parts)
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return True
    return False


def _first_symlink(root: pathlib.Path) -> str:
    """Relative posix path of the first symlink under ``root``, or ``''``.

    Directory symlinks are reported too. ``os.walk`` will not descend into them,
    so a scan that only hashes files it yields would miss the link entirely.
    """
    if root.is_symlink():
        return "."
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = pathlib.Path(dirpath)
        for name in dirnames:
            path = base / name
            if path.is_symlink():
                return path.relative_to(root).as_posix()
        for name in filenames:
            path = base / name
            if path.is_symlink():
                return path.relative_to(root).as_posix()
    return ""


def _snapshot_entry_present(target: pathlib.Path) -> bool:
    """True when ``target`` is any directory entry, including a broken symlink."""
    try:
        target.lstat()
    except FileNotFoundError:
        return False
    return True


def build_manifest(root: pathlib.Path, sha: str) -> Dict[str, Any]:
    """Per-file inventory of a staged snapshot, in stable order."""
    files: List[Dict[str, Any]] = []
    for path in _iter_snapshot_files(root):
        file_sha, size = _digest_file(path)
        files.append({
            "path": path.relative_to(root).as_posix(),
            "sha256": file_sha,
            "size": size,
        })
    return {
        "schema_version": RUNTIME_PIN_SCHEMA_VERSION,
        "sha": str(sha),
        "paths": list(SNAPSHOT_PATHS),
        "created_at": utc_now_iso(),
        "files": files,
    }


def manifest_digest(manifest: Dict[str, Any]) -> str:
    """Digest over the manifest's FILE LIST only.

    ``created_at`` is excluded so a second publication of the same base — a
    retried apply after the directory was removed, or a legacy recovery that is
    allowed to create an absent snapshot — reproduces the digest. A pin this
    update already recorded is never republished to chase it."""
    payload = json.dumps(
        {"sha": str(manifest.get("sha") or ""), "files": manifest.get("files") or []},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _git_archive_into(repo_dir: Any, sha: str, staging: pathlib.Path) -> None:
    """Extract ``sha``'s snapshot paths from the object database into ``staging``."""
    with tempfile.TemporaryDirectory(prefix="ouroboros-runtime-archive-") as tmp:
        archive = pathlib.Path(tmp) / "runtime.tar"
        completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(repo_dir), "archive", "--format=tar",
             "-o", str(archive), str(sha), "--", *SNAPSHOT_PATHS],
            capture_output=True, text=True, timeout=300,
        )
        if completed.returncode != 0:
            raise RuntimePinError(
                f"git archive of {str(sha)[:12]} failed: "
                f"{(completed.stderr or completed.stdout or '').strip()[:300]}"
            )
        with tarfile.open(archive, "r:") as tar:
            members = []
            for member in tar.getmembers():
                name = pathlib.PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts:
                    raise RuntimePinError(f"refusing archive member outside the snapshot: {member.name}")
                if not (member.isfile() or member.isdir()):
                    # git archive emits regular files and directories; a symlink
                    # into the mutable tree would defeat the whole pin.
                    raise RuntimePinError(f"refusing non-regular archive member: {member.name}")
                members.append(member)
            tar.extractall(staging, members=members)  # noqa: S202 — members vetted above


def publish_runtime_snapshot(repo_dir: Any, drive_root: Any, sha: str) -> Dict[str, Any]:
    """Publish ``sha``'s runtime as an immutable snapshot and return its pin.

    A snapshot that is already present and still validates is reused, so a
    retried apply shares one copy. A snapshot that is present but does not
    validate is refused and left untouched: it may be the forensic copy of a
    pin already in use, and replacing it would erase that evidence. Creation
    runs only when the target directory is absent. It is staged in a sibling
    directory and renamed into place only after its own manifest verifies, so a
    crash mid-publication leaves either the previous good snapshot or nothing.
    """
    sha = str(sha or "").strip()
    if not sha:
        raise RuntimePinError("a runtime pin needs an explicit base sha")
    target = snapshot_root(drive_root, sha)
    if _snapshot_entry_present(target):
        if target.is_symlink() or not target.is_dir():
            raise RuntimePinError(f"refusing to replace invalid runtime snapshot at {target}")
        existing = _pin_for(target, sha)
        if existing is None:
            raise RuntimePinError(f"refusing to replace invalid runtime snapshot at {target}")
        ok, reason = validate_runtime_snapshot(existing)
        if not ok:
            raise RuntimePinError(
                f"refusing to replace invalid runtime snapshot at {target}: {reason}"
            )
        return existing
    store = runtime_store_root(drive_root)
    store.mkdir(parents=True, exist_ok=True)
    staging = store / f".staging-{uuid.uuid4().hex[:12]}"
    try:
        staging.mkdir()
        _git_archive_into(repo_dir, sha, staging)
        manifest = build_manifest(staging, sha)
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8",
        )
        # Checked before the generic verify so the legacy-base refusal reads as
        # what it is, rather than as one more failed manifest entry.
        if not (staging / BOOTSTRAP_REL).is_file():
            raise RuntimePinError(
                f"base {sha[:12]} has no pinned-worker bootstrap ({BOOTSTRAP_REL}); "
                "this base cannot host an isolated assisted resolver"
            )
        staged_pin = _pin_from_manifest(staging, manifest)
        ok, reason = validate_runtime_snapshot(staged_pin)
        if not ok:
            raise RuntimePinError(f"staged runtime snapshot did not verify: {reason}")
        if _snapshot_entry_present(target):
            raise RuntimePinError(f"refusing to replace runtime snapshot that appeared at {target}")
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    published = _pin_from_manifest(target, manifest)
    ok, reason = validate_runtime_snapshot(published)
    if not ok:
        raise RuntimePinError(f"published runtime snapshot did not verify: {reason}")
    log.info("published pinned assisted-update runtime %s (%d files)", sha[:12], published["file_count"])
    return published


def _pin_from_manifest(root: pathlib.Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": RUNTIME_PIN_SCHEMA_VERSION,
        "sha": str(manifest.get("sha") or ""),
        "root": str(root),
        "manifest_sha256": manifest_digest(manifest),
        "file_count": len(manifest.get("files") or []),
        "published_at": str(manifest.get("created_at") or utc_now_iso()),
    }


def _pin_for(root: pathlib.Path, sha: str) -> Optional[Dict[str, Any]]:
    """The pin a published snapshot directory describes, or None if unreadable."""
    try:
        manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(manifest, dict) or str(manifest.get("sha") or "") != str(sha):
        return None
    return _pin_from_manifest(root, manifest)


def read_manifest(pin: Dict[str, Any]) -> Dict[str, Any]:
    root = pathlib.Path(str((pin or {}).get("root") or ""))
    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise RuntimePinError("runtime manifest is not an object")
    return manifest


def validate_runtime_snapshot(pin: Dict[str, Any]) -> Tuple[bool, str]:
    """Verify every manifest file byte-for-byte, and that nothing else is present.

    Returns ``(ok, reason)``; the reason is the disclosure a refusal is reported
    with. Both directions matter: a MISSING or altered file means the pinned code
    is not the reviewed base any more, and an EXTRA file means something wrote
    into a tree whose whole value is that nothing does. A symlink is refused
    even when the bytes it points at match, and every manifest path must already
    be a normalized relative path inside the snapshot.
    """
    pin = pin or {}
    root_text = str(pin.get("root") or "")
    if not root_text:
        return False, "runtime pin has no snapshot root"
    root = pathlib.Path(root_text)
    # ``is_dir`` follows symlinks. A root that is itself a link is not a pin,
    # even when the directory it points at would otherwise validate.
    if root.is_symlink():
        return False, "pinned runtime snapshot root is a symlink"
    if not root.is_dir():
        return False, f"pinned runtime snapshot is missing ({root})"
    try:
        manifest = read_manifest(pin)
    except Exception as exc:
        return False, f"pinned runtime manifest is unreadable ({type(exc).__name__})"
    if str(manifest.get("sha") or "") != str(pin.get("sha") or ""):
        return False, "pinned runtime manifest describes a different base sha"
    expected_digest = str(pin.get("manifest_sha256") or "")
    if not expected_digest:
        return False, "runtime pin has no recorded manifest digest"
    actual_digest = manifest_digest(manifest)
    if expected_digest != actual_digest:
        return False, "pinned runtime manifest does not match the recorded digest"
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        return False, "pinned runtime manifest lists no files"
    link = _first_symlink(root)
    if link:
        shown = "root" if link == "." else link
        return False, f"pinned runtime snapshot contains a symlink ({shown})"
    expected: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("path") or ""):
            return False, "pinned runtime manifest has a malformed entry"
        relpath = str(entry["path"])
        if _normalized_relative(relpath) is None or _path_escapes(root, relpath):
            return False, f"pinned runtime manifest path is not a contained relative path ({relpath})"
        expected[relpath] = entry
    for relpath, entry in expected.items():
        path = root.joinpath(*pathlib.PurePosixPath(relpath).parts)
        if path.is_symlink() or not path.is_file():
            return False, f"pinned runtime file is missing ({relpath})"
        try:
            file_sha, size = _digest_file(path)
        except OSError:
            return False, f"pinned runtime file is not a regular file ({relpath})"
        # ``entry["size"]`` is legitimately 0 for the package markers, so the
        # default has to come from the key's absence, not from falsiness.
        try:
            expected_size = int(entry["size"])
        except (KeyError, TypeError, ValueError):
            return False, f"pinned runtime manifest has no size for {relpath}"
        if size != expected_size or file_sha != str(entry.get("sha256") or ""):
            return False, f"pinned runtime file does not match the manifest ({relpath})"
    present = {path.relative_to(root).as_posix() for path in _iter_snapshot_files(root)}
    unexplained = sorted(present - set(expected))
    if unexplained:
        return False, f"pinned runtime snapshot has unmanifested files ({unexplained[0]})"
    if not (root / BOOTSTRAP_REL).is_file():
        return False, f"pinned runtime snapshot has no bootstrap ({BOOTSTRAP_REL})"
    return True, ""


def pin_bootstrap_module(pin: Dict[str, Any]) -> pathlib.Path:
    return pathlib.Path(str((pin or {}).get("root") or "")) / BOOTSTRAP_REL


# ---------------------------------------------------------------------------
# Selection
#
# The pool spawns pinned workers ONLY while an assisted update has explicitly
# selected its runtime. Ordinary multiprocessing outside that selection is
# untouched, which is why selection is a narrow, explicitly cleared switch rather
# than a setting or an inference from the transaction marker.
# ---------------------------------------------------------------------------


def select_assisted_runtime(pin: Dict[str, Any]) -> Dict[str, Any]:
    """Route every subsequent worker spawn through ``pin``; validates first."""
    ok, reason = validate_runtime_snapshot(pin)
    if not ok:
        raise RuntimePinError(reason)
    selected = dict(pin)
    with _SELECTION_LOCK:
        _SELECTED_PIN.clear()
        _SELECTED_PIN.update(selected)
    return selected


def clear_assisted_runtime() -> bool:
    """Return the pool to ordinary multiprocessing. Keeps the snapshot on disk.

    Snapshots are retained deliberately: one update can span a transaction, a
    process restart and several recoveries, and the cost of a stale copy is disk,
    while the cost of a missing one is a refused update."""
    with _SELECTION_LOCK:
        was_selected = bool(_SELECTED_PIN)
        _SELECTED_PIN.clear()
    return was_selected


def active_runtime_pin() -> Dict[str, Any]:
    """The selected pin, or ``{}`` for ordinary multiprocessing workers."""
    with _SELECTION_LOCK:
        return dict(_SELECTED_PIN)
