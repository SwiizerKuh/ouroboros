"""The assisted-update resolver must start, and keep running, from code that parses.

The failure these tests exist against had two halves, both observed live:

* the first resolver booted from the CLEAN tree, then died at task time on an
  unguarded function-local import — ``context_health._memory_health_lines`` does
  ``from ouroboros.memory_nomination_receipts import DialogueMetaUnreadable,
  load_meta`` — which resolved out of the half-merged tree: one process, two code
  generations, ``ImportError``;
* every replacement died earlier still, in ``multiprocessing``'s reconstruction of
  the parent's ``__main__`` from ``server.py``, on a conflict marker at line 478:
  ``SyntaxError``.

So the load-bearing cases here run a REAL child process through the production
``worker_main`` and the production bootstrap, against a mutable checkout that
carries both injected faults, and drive it with a hermetic loopback model. A
helper-level mock could not have caught either half: both are properties of how
the child's ``sys.path`` and entry point are built, which only a real process has.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from supervisor import update_runtime

# Real subprocesses, ports and pool globals must not overlap the parallel lane.
pytestmark = pytest.mark.serial

SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[1]
MOCK_SLUG = "openai-compatible::mock-model"

# A conflict block put where it cannot be skipped: the module stops parsing, which
# is what ``server.py`` did to every replacement worker.
CONFLICT_BLOCK = """

<<<<<<< HEAD
LOCAL_MARKER = "ours"
=======
UPSTREAM_MARKER = "theirs"
>>>>>>> upstream/ouroboros
"""

# The other half: a module that PARSES but no longer exports what the resident
# generation imports from it. This is the exact shape of the live ImportError.
INCOMPATIBLE_RECEIPTS = '''"""Stand-in for an incompatible generation of this module.

It parses, so nothing fails at import; it simply does not export the names the
caller's generation expects, which is how a mixed-generation worker dies.
"""

from __future__ import annotations


def load_dialogue_meta(path):  # renamed: the caller imports ``load_meta``
    return {}
'''


def _git(repo: pathlib.Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _copy_runtime(destination: pathlib.Path) -> None:
    for rel in update_runtime.SNAPSHOT_PATHS:
        source = SOURCE_ROOT / rel
        target = destination / rel
        if source.is_dir():
            shutil.copytree(source, target,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


@pytest.fixture
def candidate(tmp_path):
    """A managed checkout committed at a clean base, plus its private data root.

    The base is THIS tree's runtime, because the thing under test is whether the
    production worker entry point can be served out of a snapshot of it.
    """
    repo = tmp_path / "candidate"
    repo.mkdir()
    _copy_runtime(repo)
    _git(repo, "init", "-q", "-b", "ouroboros")
    _git(repo, "config", "user.email", "runtime-pin@example.invalid")
    _git(repo, "config", "user.name", "Runtime Pin Test")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "clean base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    drive = tmp_path / "data"
    (drive / "logs").mkdir(parents=True)
    (drive / "state").mkdir(parents=True)
    return SimpleNamespace(repo=repo, base_sha=base_sha, drive=drive, root=tmp_path)


def test_pinned_review_resources_use_the_configured_candidate(candidate):
    """Exercise the real triad loader from pinned imports, including its archive."""
    import sys

    docs = candidate.repo / "docs"
    docs.mkdir()
    (docs / "CHECKLISTS.md").write_text(
        "## Repo Commit Checklist\nCANDIDATE_CHECKLIST\n", encoding="utf-8")
    (docs / "CHECKLISTS_ARCHIVE.md").write_text(
        "CANDIDATE_STANDING_DISCLOSURE\n", encoding="utf-8")
    pin = update_runtime.publish_runtime_snapshot(
        candidate.repo, candidate.drive, candidate.base_sha)
    root = pathlib.Path(pin["root"])
    assert not (root / "docs").exists()
    _stage_conflicts(candidate.repo)
    env = dict(os.environ, OUROBOROS_REPO_DIR=str(candidate.repo),
               OUROBOROS_DATA_DIR=str(candidate.drive), PYTHONPATH=str(root),
               OUROBOROS_ASSISTED_RUNTIME_ROOT=str(root))
    code = """
import pathlib
from supervisor.update_runtime_boot import install_snapshot_sys_path
install_snapshot_sys_path(str(pathlib.Path.cwd()))
from ouroboros.tools import review, review_helpers
from ouroboros.config import REPO_DIR
assert pathlib.Path(review.__file__).is_relative_to(pathlib.Path.cwd())
assert review_helpers.REPO_ROOT == REPO_DIR
text = review._load_checklist_section()
assert 'CANDIDATE_CHECKLIST' in text
assert 'CANDIDATE_STANDING_DISCLOSURE' in text
override = pathlib.Path(REPO_DIR) / 'override.md'
override.write_text('## Override\\nEXPLICIT_PATH\\n')
assert 'EXPLICIT_PATH' in review_helpers.load_checklist_section('Override', override)
print('PINNED_REVIEW_RESOURCES_OK')
"""
    done = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PINNED_REVIEW_RESOURCES_OK" in done.stdout
    assert update_runtime.validate_runtime_snapshot(pin) == (True, "")


def _stage_conflicts(repo: pathlib.Path) -> None:
    """Put the incident's two faults into the live (uncommitted) worktree."""
    server = repo / "server.py"
    text = server.read_text(encoding="utf-8")
    head, _, tail = text.partition("\n")
    server.write_text(head + CONFLICT_BLOCK + tail, encoding="utf-8")
    (repo / "ouroboros" / "memory_nomination_receipts.py").write_text(
        INCOMPATIBLE_RECEIPTS, encoding="utf-8")


def _probe_tool_module(repo: pathlib.Path) -> str:
    """A tool module that exists ONLY in the mutable tree.

    Tool discovery walks ``ouroboros.tools.__path__``, so this name appearing in
    the catalog the model is offered would prove discovery rooted in the live
    checkout rather than the snapshot."""
    name = "zz_mutable_discovery_probe"
    (repo / "ouroboros" / "tools" / f"{name}.py").write_text(
        '"""Mutable-tree-only tool module (test probe)."""\n'
        "from ouroboros.tools.registry import ToolEntry\n\n\n"
        "def _handler(ctx):\n    return 'probe'\n\n\n"
        "def get_tools():\n"
        "    return [ToolEntry('mutable_discovery_probe', {\n"
        "        'name': 'mutable_discovery_probe',\n"
        "        'description': 'Present only in the mutable checkout.',\n"
        "        'parameters': {'type': 'object', 'properties': {}, 'required': []},\n"
        "    }, _handler)]\n",
        encoding="utf-8")
    return "mutable_discovery_probe"


# ---------------------------------------------------------------------------
# Publication and validation: the pin is byte-exact, or it is refused
# ---------------------------------------------------------------------------

def test_snapshot_is_the_base_commit_even_though_the_worktree_is_conflicted(candidate):
    """Publication reads the object database, not the conflicted worktree.

    A transaction that never recorded a pin can still be given this exact base
    when the snapshot directory is absent. A pin already recorded is not
    reconstructed from that property."""
    clean_server = (candidate.repo / "server.py").read_text(encoding="utf-8")
    _stage_conflicts(candidate.repo)
    assert "<<<<<<< HEAD" in (candidate.repo / "server.py").read_text(encoding="utf-8")

    pin = update_runtime.publish_runtime_snapshot(
        candidate.repo, candidate.drive, candidate.base_sha)

    root = pathlib.Path(pin["root"])
    assert root == candidate.drive / "state" / "update_runtime" / candidate.base_sha
    assert (root / "server.py").read_text(encoding="utf-8") == clean_server
    assert "load_meta" in (root / "ouroboros" / "memory_nomination_receipts.py").read_text(
        encoding="utf-8")
    assert (root / update_runtime.BOOTSTRAP_REL).is_file()
    assert pin["file_count"] > 400
    assert update_runtime.validate_runtime_snapshot(pin) == (True, "")


def test_publication_is_idempotent_and_reproduces_the_recorded_manifest_digest(candidate):
    """A retried apply and a boot recovery must agree on one snapshot."""
    first = update_runtime.publish_runtime_snapshot(
        candidate.repo, candidate.drive, candidate.base_sha)
    second = update_runtime.publish_runtime_snapshot(
        candidate.repo, candidate.drive, candidate.base_sha)
    assert second["manifest_sha256"] == first["manifest_sha256"]

    # The publisher reproduces the digest when the directory is absent. Recovery
    # of a pin the transaction already recorded does not take this path.
    shutil.rmtree(pathlib.Path(first["root"]))
    rebuilt = update_runtime.publish_runtime_snapshot(
        candidate.repo, candidate.drive, candidate.base_sha)
    assert rebuilt["manifest_sha256"] == first["manifest_sha256"]


@pytest.mark.parametrize("damage", ["altered", "removed", "extra", "no_manifest"])
def test_a_damaged_snapshot_is_refused_rather_than_used(candidate, damage):
    pin = update_runtime.publish_runtime_snapshot(
        candidate.repo, candidate.drive, candidate.base_sha)
    root = pathlib.Path(pin["root"])
    if damage == "altered":
        (root / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    elif damage == "removed":
        (root / "ouroboros" / "context_health.py").unlink()
    elif damage == "extra":
        (root / "ouroboros" / "injected.py").write_text("x = 1\n", encoding="utf-8")
    else:
        (root / update_runtime.MANIFEST_NAME).unlink()

    ok, reason = update_runtime.validate_runtime_snapshot(pin)
    assert ok is False and reason
    with pytest.raises(update_runtime.RuntimePinError):
        update_runtime.select_assisted_runtime(pin)
    assert update_runtime.active_runtime_pin() == {}


def test_an_existing_invalid_snapshot_is_refused_and_left_in_place(candidate):
    """A present snapshot that does not validate is forensic evidence.

    Publication must not delete it and write a fresh tree in its place.
    """
    pin = update_runtime.publish_runtime_snapshot(
        candidate.repo, candidate.drive, candidate.base_sha)
    root = pathlib.Path(pin["root"])
    marker = root / "FORENSIC_MARKER"
    marker.write_text("do-not-erase\n", encoding="utf-8")
    (root / "VERSION").write_text("corrupted\n", encoding="utf-8")

    with pytest.raises(update_runtime.RuntimePinError, match="refusing to replace"):
        update_runtime.publish_runtime_snapshot(
            candidate.repo, candidate.drive, candidate.base_sha)

    assert marker.read_text(encoding="utf-8") == "do-not-erase\n"
    assert (root / "VERSION").read_text(encoding="utf-8") == "corrupted\n"
    assert update_runtime.validate_runtime_snapshot(pin)[0] is False


@pytest.mark.parametrize("relpath", ["../escaped.txt", "ouroboros/../VERSION", "/etc/passwd", "ouroboros//VERSION"])
def test_manifest_paths_must_be_normalized_and_contained(candidate, relpath):
    pin = update_runtime.publish_runtime_snapshot(
        candidate.repo, candidate.drive, candidate.base_sha)
    root = pathlib.Path(pin["root"])
    manifest = json.loads((root / update_runtime.MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["files"].append({"path": relpath, "sha256": "0" * 64, "size": 1})
    (root / update_runtime.MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    # The pin digest is recomputed so the refusal is the path check itself.
    tampered = {**pin, "manifest_sha256": update_runtime.manifest_digest(manifest)}

    ok, reason = update_runtime.validate_runtime_snapshot(tampered)

    assert ok is False and "contained relative path" in reason
    with pytest.raises(update_runtime.RuntimePinError, match="refusing to replace"):
        update_runtime.publish_runtime_snapshot(
            candidate.repo, candidate.drive, candidate.base_sha)
    assert (root / update_runtime.MANIFEST_NAME).is_file()
    assert relpath in (root / update_runtime.MANIFEST_NAME).read_text(encoding="utf-8")


def test_a_file_symlink_is_refused_even_when_its_target_matches(candidate):
    """``os.walk`` yields a file symlink, and ``is_file``/open would follow it.

    Matching bytes on the far side of the link must still be a refusal: the
    pin would otherwise track the mutable tree the link points at.
    """
    pin = update_runtime.publish_runtime_snapshot(
        candidate.repo, candidate.drive, candidate.base_sha)
    root = pathlib.Path(pin["root"])
    version = root / "VERSION"
    payload = version.read_bytes()
    outside = candidate.root / "outside_version"
    outside.write_bytes(payload)
    version.unlink()
    version.symlink_to(outside)

    ok, reason = update_runtime.validate_runtime_snapshot(pin)
    assert ok is False and "symlink" in reason
    with pytest.raises(update_runtime.RuntimePinError, match="refusing to replace"):
        update_runtime.publish_runtime_snapshot(
            candidate.repo, candidate.drive, candidate.base_sha)
    assert version.is_symlink()
    assert outside.read_bytes() == payload


def test_a_directory_symlink_is_refused(candidate):
    """A directory symlink is not a file ``os.walk`` yields, so hashing files misses it."""
    pin = update_runtime.publish_runtime_snapshot(
        candidate.repo, candidate.drive, candidate.base_sha)
    root = pathlib.Path(pin["root"])
    link = root / "linked_tree"
    link.symlink_to(candidate.repo / "ouroboros", target_is_directory=True)

    ok, reason = update_runtime.validate_runtime_snapshot(pin)
    assert ok is False and "symlink" in reason and "linked_tree" in reason
    with pytest.raises(update_runtime.RuntimePinError, match="refusing to replace"):
        update_runtime.publish_runtime_snapshot(
            candidate.repo, candidate.drive, candidate.base_sha)
    assert link.is_symlink()


def test_a_missing_snapshot_is_refused(candidate):
    pin = {"schema_version": 1, "sha": candidate.base_sha,
           "root": str(candidate.drive / "state" / "update_runtime" / "nowhere"),
           "manifest_sha256": "0" * 64, "file_count": 1}
    ok, reason = update_runtime.validate_runtime_snapshot(pin)
    assert ok is False and "missing" in reason


def test_a_base_without_the_bootstrap_cannot_be_pinned(candidate):
    """The legacy limit, stated as a refusal.

    A base that predates this mechanism has no module for a pinned worker's first
    instruction to come from, so it cannot host an isolated resolver. Recovery is
    required to block or roll back on this, never to improvise a live-tree worker.
    """
    (candidate.repo / update_runtime.BOOTSTRAP_REL).unlink()
    _git(candidate.repo, "add", "-A")
    _git(candidate.repo, "commit", "-q", "-m", "a base from before pinned runtimes")
    legacy_sha = _git(candidate.repo, "rev-parse", "HEAD")

    with pytest.raises(update_runtime.RuntimePinError) as caught:
        update_runtime.publish_runtime_snapshot(
            candidate.repo, candidate.drive, legacy_sha)
    assert "bootstrap" in str(caught.value)
    assert not (candidate.drive / "state" / "update_runtime" / legacy_sha).exists()


def test_selection_is_explicit_in_both_directions(candidate):
    pin = update_runtime.publish_runtime_snapshot(
        candidate.repo, candidate.drive, candidate.base_sha)
    assert update_runtime.active_runtime_pin() == {}
    update_runtime.select_assisted_runtime(pin)
    try:
        assert update_runtime.active_runtime_pin()["sha"] == candidate.base_sha
    finally:
        assert update_runtime.clear_assisted_runtime() is True
    assert update_runtime.active_runtime_pin() == {}
    # Retained deliberately: one update spans a transaction, a restart and
    # several recoveries, and a refused update costs more than a stale copy.
    assert pathlib.Path(pin["root"]).is_dir()


# ---------------------------------------------------------------------------
# A real pinned worker, a real task, a hermetic model
# ---------------------------------------------------------------------------

RESOLVED_LINE = 'RESOLVED_MARKER = "both sides kept"\n'


class StubModel:
    """A loopback OpenAI-compatible model that resolves one conflict and stops.

    Small on purpose. It answers the safety supervisor's JSON turn, makes exactly
    one real tool call — ``edit_text`` against the MUTABLE checkout — and then
    gives a final answer. Every request body is recorded, which is how the test
    reads what the worker assembled: the prompt proves the context (and therefore
    the unguarded lazy import inside it) was built, and ``tools`` proves which
    tree tool discovery walked.
    """

    def __init__(self, *, edit_path: str = "", old_str: str = "", new_str: str = "",
                 stage: bool = False) -> None:
        self.requests: list = []
        self.tool_calls: list = []
        # Production file-tool write, then production git staging. ``stage`` asks
        # for ``stage_adaptations``, which runs ``git add -A`` in the worker's
        # repo and nothing else. An answer-only stub (no edit) still forces the
        # worker to build a task context before the model is called.
        self._steps: list = []
        if edit_path:
            self._steps.append(("edit_text", {
                "path": edit_path, "old_str": old_str, "new_str": new_str,
                "root": "system_repo",
            }))
        if stage:
            self._steps.append(("stage_adaptations", {}))
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802 - stdlib callback name
                if self.path.rstrip("/").endswith("/models"):
                    return self._send({"data": [{"id": "mock-model", "max_model_len": 400000}]})
                self.send_error(404)

            def do_POST(self):  # noqa: N802 - stdlib callback name
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads((self.rfile.read(length) or b"{}").decode("utf-8"))
                except ValueError:
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                outer.requests.append(body)
                return self._send(outer._completion(body, len(outer.requests)),
                                  stream=bool(body.get("stream")))

            def _send(self, payload, *, stream=False):
                # The agent streams its tool-bearing rounds, so the stub has to
                # answer in SSE frames; a plain body reads as a truncated stream.
                if stream:
                    content_type = "text/event-stream"
                    choices = []
                    for choice in payload["choices"]:
                        delta = dict(choice["message"])
                        if delta.get("tool_calls"):
                            delta["tool_calls"] = [
                                dict(call, index=index)
                                for index, call in enumerate(delta["tool_calls"])
                            ]
                        choices.append({"index": choice["index"], "delta": delta,
                                        "finish_reason": choice["finish_reason"]})
                    common = {"id": payload["id"], "model": payload["model"],
                              "object": "chat.completion.chunk"}
                    frames = [{**common, "choices": choices},
                              {**common, "choices": [], "usage": payload["usage"]}]
                    data = ("".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
                            + "data: [DONE]\n\n").encode("utf-8")
                else:
                    content_type = "application/json"
                    data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                self.wfile.flush()

            def log_message(self, *_args):
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _completion(self, body: dict, seq: int) -> dict:
        fmt = body.get("response_format")
        if isinstance(fmt, dict) and fmt.get("type") == "json_object":
            message = {"role": "assistant",
                       "content": json.dumps({"status": "SAFE", "reason": "stub"})}
        elif body.get("tools") and len(self.tool_calls) < len(self._steps):
            name, arguments = self._steps[len(self.tool_calls)]
            self.tool_calls.append({"name": name, **arguments})
            message = {
                "role": "assistant", "content": "resolving the conflicted file",
                "tool_calls": [{"id": f"call_{seq}", "type": "function", "function": {
                    "name": name, "arguments": json.dumps(arguments)}}],
            }
        else:
            message = {"role": "assistant",
                       "content": "Final answer: the conflict in server.py is resolved."}
        return {
            "id": f"stub-{seq}", "object": "chat.completion",
            "model": str(body.get("model") or "mock-model"),
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def prompts(self) -> str:
        parts = []
        for body in self.requests:
            for message in body.get("messages") or []:
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    parts.extend(str(part.get("text") or "") for part in content
                                 if isinstance(part, dict))
        return "\n".join(parts)

    def offered_tools(self) -> set:
        names = set()
        for body in self.requests:
            for tool in body.get("tools") or []:
                if isinstance(tool, dict):
                    names.add(str((tool.get("function") or {}).get("name") or ""))
        return names

    def __enter__(self) -> "StubModel":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()


def _settings(stub: StubModel) -> dict:
    """Every routed model slot pinned at the loopback stub: no egress is possible."""
    config = {
        "OPENAI_COMPATIBLE_BASE_URL": stub.base_url,
        "OPENAI_COMPATIBLE_API_KEY": "stub-key-not-a-credential",
        "OUROBOROS_MODEL_FALLBACKS": "",
        "OUROBOROS_MODEL_VISION": "",
        "OUROBOROS_MODEL_CONSCIOUSNESS": "",
        "OUROBOROS_MODEL_DEEP_SELF_REVIEW": "",
        "OUROBOROS_WEBSEARCH_MODEL": "",
        "CLAUDE_CODE_MODEL": "",
        "OUROBOROS_SAFETY_MODE": "off",
        "OUROBOROS_CONTEXT_MODE": "low",
        "OUROBOROS_RUNTIME_MODE": "advanced",
        "OUROBOROS_TASK_REVIEW_MODE": "off",
        "OUROBOROS_POST_TASK_EVOLUTION": "false",
        "OUROBOROS_MAX_WORKERS": 1,
        "TOTAL_BUDGET": 10.0,
        "OUROBOROS_PER_TASK_COST_USD": 10.0,
    }
    for slot in ("OUROBOROS_MODEL", "OUROBOROS_MODEL_HEAVY", "OUROBOROS_MODEL_LIGHT",
                 "OUROBOROS_REVIEW_MODELS", "OUROBOROS_SCOPE_REVIEW_MODELS",
                 "OUROBOROS_SCOPE_REVIEW_MODEL"):
        config[slot] = MOCK_SLUG
    return config


def _event_rows(drive: pathlib.Path, event_type: str) -> list:
    path = drive / "logs" / "events.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("type") == event_type:
            rows.append(row)
    return rows


def _drain_until(event_q, predicate, timeout: float = 180.0) -> list:
    """Collect the worker's own events off the bus until ``predicate`` is satisfied.

    ``task_done`` reaches the supervisor over the event queue, not the log file —
    the file copy is written by the server's log handler, which this test does not
    run — so the bus is where a finished task is observed."""
    import queue as _queue

    collected: list = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            event = event_q.get(timeout=0.5)
        except _queue.Empty:
            continue
        except (EOFError, OSError):
            break
        if isinstance(event, dict):
            collected.append(event)
            if predicate(event):
                return collected
    return collected


def _wait_for(predicate, timeout: float = 90.0, interval: float = 0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


@pytest.fixture
def pool(candidate, monkeypatch):
    """The real pool bound to the candidate checkout, with real children allowed.

    Every worker this fixture starts is a genuine OS process running the
    production entry point; nothing about the spawn path is stubbed, because the
    spawn path is the thing under test."""
    from supervisor import worker_pool_lifecycle as lifecycle
    from supervisor import workers

    monkeypatch.setenv("OUROBOROS_REPO_DIR", str(candidate.repo))
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(candidate.drive))
    settings_path = candidate.drive / "settings.json"
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(settings_path))
    monkeypatch.setattr(workers, "REPO_DIR", candidate.repo)
    monkeypatch.setattr(workers, "DRIVE_ROOT", candidate.drive)
    monkeypatch.setattr(workers, "MAX_WORKERS", 1)
    monkeypatch.setattr(workers, "WORKERS", {})
    monkeypatch.setattr(workers, "PENDING", [])
    monkeypatch.setattr(workers, "RUNNING", {})
    monkeypatch.setattr(workers, "_CTX", None)
    monkeypatch.setattr(workers, "_EVENT_Q", None)
    monkeypatch.setattr(workers, "_EVENT_Q_MANAGER", None)
    monkeypatch.setattr(workers, "_EVENT_Q_GENERATION", "")
    monkeypatch.setattr(workers, "_EVENT_Q_SHUTDOWN", False)
    monkeypatch.setattr(workers, "_WORKER_POOL_DISABLED_REASON", "")
    monkeypatch.setattr(workers, "load_state", lambda: {
        "current_sha": candidate.base_sha, "owner_chat_id": 0})
    (candidate.drive / "state" / "state.json").write_text(json.dumps({
        "current_sha": candidate.base_sha, "owner_chat_id": 0}), encoding="utf-8")

    handle = SimpleNamespace(
        workers=workers, lifecycle=lifecycle, candidate=candidate,
        settings_path=settings_path,
    )
    try:
        yield handle
    finally:
        for slot in list(workers.WORKERS.values()):
            try:
                if slot.proc.pid:
                    lifecycle.kill_worker_tree(slot.proc.pid)
                slot.proc.join(timeout=5)
            except Exception:
                pass
        workers.WORKERS.clear()
        update_runtime.clear_assisted_runtime()
        try:
            workers.shutdown_event_q()
        except Exception:
            pass


def test_a_pinned_worker_boots_and_resolves_a_conflict_the_live_tree_could_not_import(pool):
    """The load-bearing case: both halves of the incident present, worker survives both.

    ``server.py`` carries conflict markers (the ``SyntaxError`` that killed every
    replacement) and ``memory_nomination_receipts.py`` is an incompatible
    generation missing ``load_meta`` (the ``ImportError`` that killed the first
    worker at task time). A real child running the production ``worker_main``
    reports ready, assembles a real task context — which reaches that lazy import
    — calls a hermetic model and performs a real ``edit_text`` that lands in the
    MUTABLE checkout. Nothing in this test is a stub except the model.
    """
    workers, candidate = pool.workers, pool.candidate
    probe_tool = _probe_tool_module(candidate.repo)
    _stage_conflicts(candidate.repo)
    conflicted = (candidate.repo / "server.py").read_text(encoding="utf-8")
    assert "<<<<<<< HEAD" in conflicted

    pin = update_runtime.select_assisted_runtime(
        update_runtime.publish_runtime_snapshot(
            candidate.repo, candidate.drive, candidate.base_sha))
    snapshot_server = (pathlib.Path(pin["root"]) / "server.py").read_text(encoding="utf-8")

    # stage_adaptations is the production ``git add`` and only runs on an
    # integration branch. The sha the readiness seam checks is unchanged.
    _git(candidate.repo, "branch", "-M", "integrate/pr-1")
    workspace_index = _git(SOURCE_ROOT, "diff", "--cached", "--name-only")
    with StubModel(edit_path="server.py",
                   old_str=CONFLICT_BLOCK.strip("\n"),
                   new_str=RESOLVED_LINE.strip("\n"),
                   stage=True) as stub:
        pool.settings_path.write_text(json.dumps(_settings(stub)), encoding="utf-8")
        workers.spawn_workers(1)

        ready = _wait_for(lambda: _event_rows(candidate.drive, "worker_ready"))
        assert ready, "the pinned worker never reported ready"
        row = ready[0]
        assert row["pid"] == workers.WORKERS[0].proc.pid
        # The runtime it imported from is the snapshot; the repository it SERVES
        # is the mutable candidate, whose HEAD is still the reviewed base.
        assert row["runtime_root"] == pin["root"]
        assert row["sys_path_head"] == pin["root"]
        assert row["git_sha"] == candidate.base_sha

        task_id = "update_assisted_merge_probe"
        workers.WORKERS[0].in_q.put({
            "id": task_id, "type": "task", "chat_id": 0,
            "text": "Resolve the conflict markers in server.py.",
        })
        events = _drain_until(
            workers.get_event_q(),
            lambda event: event.get("type") == "task_done" and event.get("task_id") == task_id,
        )
        done = [event for event in events
                if event.get("type") == "task_done" and event.get("task_id") == task_id]
        assert done, (
            "the pinned worker never finished its task; model requests="
            f"{len(stub.requests)}, events={[event.get('type') for event in events]}"
        )
        assert done[0]["worker_id"] == 0

    # The context the model was given exists at all, which means the unguarded
    # lazy import inside the health block resolved — from the snapshot. The
    # ``__file__`` half is load-bearing production code rather than a test
    # claim: the bootstrap derives its root from its OWN ``__file__`` and exits
    # on a mismatch with the assigned pin, so a worker that reached ready could
    # only have imported from the snapshot asserted above.
    prompts = stub.prompts()
    # The staged listing names the incompatible module; a mixed-generation import
    # would have been an ImportError/SyntaxError before any tool result existed.
    assert "ImportError" not in prompts
    assert "SyntaxError" not in prompts
    assert stub.tool_calls, "the model was never offered a round that could edit"
    # Discovery walked the snapshot: a tool module that exists only in the
    # mutable checkout was never offered.
    offered = stub.offered_tools()
    assert "edit_text" in offered
    assert probe_tool not in offered
    # The worker's repo is the mutable candidate. The snapshot path can appear
    # in the data-directory listing because that is where the pin is stored;
    # the write and the index below are what prove it was not the target.
    assert str(candidate.repo) in prompts

    # The file-tool write landed on the MUTABLE candidate...
    resolved = (candidate.repo / "server.py").read_text(encoding="utf-8")
    assert "<<<<<<< HEAD" not in resolved
    assert RESOLVED_LINE.strip("\n") in resolved
    # ...git staging (stage_adaptations → git add) did too, and only there.
    assert [call["name"] for call in stub.tool_calls] == ["edit_text", "stage_adaptations"]
    staged_blob = _git(candidate.repo, "show", ":server.py")
    assert "<<<<<<< HEAD" not in staged_blob
    assert RESOLVED_LINE.strip("\n") in staged_blob
    assert "server.py" in _git(candidate.repo, "diff", "--cached", "--name-only").splitlines()
    assert _git(SOURCE_ROOT, "diff", "--cached", "--name-only") == workspace_index
    assert RESOLVED_LINE.strip("\n") not in (SOURCE_ROOT / "server.py").read_text(encoding="utf-8")
    # ...and never on the snapshot, which still verifies byte-for-byte.
    assert (pathlib.Path(pin["root"]) / "server.py").read_text(encoding="utf-8") == snapshot_server
    assert not (pathlib.Path(pin["root"]) / ".git").exists()
    assert update_runtime.validate_runtime_snapshot(pin) == (True, "")


def _run_pinned_task(workers, candidate, stub, task_id: str) -> list:
    """Hand one task to the live slot and return the bus events through task_done."""
    workers.WORKERS[0].in_q.put({
        "id": task_id, "type": "task", "chat_id": 0,
        "text": "Resolve the conflict markers in server.py.",
    })
    events = _drain_until(
        workers.get_event_q(),
        lambda event: event.get("type") == "task_done" and event.get("task_id") == task_id,
    )
    done = [event for event in events
            if event.get("type") == "task_done" and event.get("task_id") == task_id]
    assert done, (
        "the pinned worker never finished its task; model requests="
        f"{len(stub.requests)}, events={[event.get('type') for event in events]}"
    )
    return done


def test_a_replacement_boots_the_same_way_while_the_markers_are_still_there(pool):
    """The half that failed twice in the incident: the REPLACEMENT.

    A resolver can crash mid-merge, and the pool's answer is a respawn. Under
    ``multiprocessing`` that respawn rebuilt ``__main__`` from the conflicted
    ``server.py`` and died on a ``SyntaxError`` before running a line of its own.
    The replacement has to come from the same pin as the original, while the
    conflicts are still live — which is the only state a replacement ever sees —
    and it has to build a real task context against the incompatible lazy module,
    not merely print ready. Recovery after that uses the same spawn factory.
    """
    workers, candidate = pool.workers, pool.candidate
    _stage_conflicts(candidate.repo)
    assert "<<<<<<< HEAD" in (candidate.repo / "server.py").read_text(encoding="utf-8")
    receipts = (candidate.repo / "ouroboros" / "memory_nomination_receipts.py").read_text(
        encoding="utf-8")
    assert "def load_meta" not in receipts
    pin = update_runtime.select_assisted_runtime(
        update_runtime.publish_runtime_snapshot(
            candidate.repo, candidate.drive, candidate.base_sha))
    snapshot_receipts = (
        pathlib.Path(pin["root"]) / "ouroboros" / "memory_nomination_receipts.py"
    ).read_text(encoding="utf-8")
    assert "def load_meta" in snapshot_receipts

    workers.spawn_workers(1)
    first = _wait_for(lambda: _event_rows(candidate.drive, "worker_ready"))
    assert first, "the original pinned worker never reported ready"
    assert first[0]["sys_path_head"] == pin["root"]
    original_pid = workers.WORKERS[0].proc.pid

    os.kill(original_pid, 9)
    workers.WORKERS[0].proc.join(timeout=10)
    assert workers.WORKERS[0].proc.is_alive() is False
    assert workers.WORKERS[0].proc.exitcode == -9

    assert workers.respawn_worker(0) is True
    replacement_pid = workers.WORKERS[0].proc.pid
    assert replacement_pid != original_pid
    ready = _wait_for(
        lambda: [row for row in _event_rows(candidate.drive, "worker_ready")
                 if row["pid"] == replacement_pid])
    assert ready, "the replacement never reported ready from the pinned runtime"
    assert ready[0]["runtime_root"] == pin["root"]
    assert ready[0]["sys_path_head"] == pin["root"]
    # Still conflicted — the replacement was never handed a parseable tree.
    assert "<<<<<<< HEAD" in (candidate.repo / "server.py").read_text(encoding="utf-8")

    with StubModel() as stub:
        pool.settings_path.write_text(json.dumps(_settings(stub)), encoding="utf-8")
        done = _run_pinned_task(workers, candidate, stub, "update_assisted_merge_replacement")
        assert done[0]["worker_id"] == 0
        prompts = stub.prompts()
        assert stub.requests, "the replacement never built a task context"
        assert "ImportError" not in prompts
        assert "SyntaxError" not in prompts
        assert str(candidate.repo) in prompts

    assert "def load_meta" not in (
        candidate.repo / "ouroboros" / "memory_nomination_receipts.py"
    ).read_text(encoding="utf-8")
    assert (
        pathlib.Path(pin["root"]) / "ouroboros" / "memory_nomination_receipts.py"
    ).read_text(encoding="utf-8") == snapshot_receipts
    assert update_runtime.validate_runtime_snapshot(pin) == (True, "")

    # Boot recovery is not a second factory: drop the slot and let the same
    # starter birth the next one from the pin that is still selected.
    pool.lifecycle.kill_worker_tree(replacement_pid)
    workers.WORKERS[0].proc.join(timeout=10)
    workers.WORKERS.clear()
    assert workers.ensure_worker_pool_started(n=1, allow_disabled_restart=True) is True
    recovered_pid = workers.WORKERS[0].proc.pid
    assert recovered_pid not in {original_pid, replacement_pid}
    recovered = _wait_for(
        lambda: [row for row in _event_rows(candidate.drive, "worker_ready")
                 if row["pid"] == recovered_pid])
    assert recovered, "recovery did not start a worker from the pinned runtime"
    assert recovered[0]["runtime_root"] == pin["root"]
    assert recovered[0]["sys_path_head"] == pin["root"]
    assert type(workers.WORKERS[0].proc).__name__ == "PinnedWorkerProcess"


def test_the_ordinary_pool_is_untouched_without_a_selection(pool):
    """Scope pin: this only changes workers an assisted update explicitly selects."""
    import multiprocessing as mp

    workers, candidate = pool.workers, pool.candidate
    assert update_runtime.active_runtime_pin() == {}

    workers.spawn_workers(1)
    slot = workers.WORKERS[0]
    assert isinstance(slot.proc, mp.process.BaseProcess)
    assert hasattr(slot.in_q, "_reader"), "an ordinary slot keeps its multiprocessing queue"

    ready = _wait_for(lambda: _event_rows(candidate.drive, "worker_ready"))
    assert ready, "the ordinary worker never reported ready"
    assert "runtime_root" not in ready[0]
    rows = [row for row in _event_rows(candidate.drive, "worker_starting")]
    assert rows and "runtime_root" not in rows[0]

    # Cancel is the ordinary shutdown message, not a pinned-runtime path.
    slot.in_q.put({"type": "shutdown"})
    assert _wait_for(lambda: not slot.proc.is_alive(), timeout=30.0)
    assert slot.proc.exitcode == 0


def test_a_damaged_pin_refuses_to_start_a_worker_instead_of_using_the_live_tree(pool):
    """Fail closed at the spawn seam: no snapshot, no worker. Never a fallback."""
    from supervisor.update_runtime_worker import start_pinned_worker

    workers, candidate = pool.workers, pool.candidate
    pin = update_runtime.publish_runtime_snapshot(
        candidate.repo, candidate.drive, candidate.base_sha)
    # Select while healthy, then damage it the way a half-written publication or a
    # stray writer would — the state a respawn mid-update can actually meet.
    update_runtime.select_assisted_runtime(pin)
    (pathlib.Path(pin["root"]) / "ouroboros" / "context_health.py").unlink()

    with pytest.raises(update_runtime.RuntimePinError):
        workers.spawn_workers(1)
    assert workers.WORKERS == {}

    shutil.rmtree(pathlib.Path(pin["root"]))
    with pytest.raises(update_runtime.RuntimePinError):
        start_pinned_worker(0, pin, in_q=None, event_q=None,
                            repo_dir=candidate.repo, drive_root=candidate.drive)
    assert not _event_rows(candidate.drive, "worker_starting")


def test_the_resolver_fence_refuses_when_no_runtime_is_pinned(pool, monkeypatch):
    """The apply seam's own fail-closed check, before any conflict is staged."""
    from supervisor import git_ops, update_merge

    candidate = pool.candidate
    monkeypatch.setattr(git_ops, "REPO_DIR", candidate.repo)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", candidate.drive)
    monkeypatch.setattr(
        pool.workers, "ensure_worker_pool_started",
        lambda **_kwargs: pytest.fail("a resolver must not be started without a pinned runtime"))

    assert update_runtime.active_runtime_pin() == {}
    assert update_merge.ensure_assisted_resolver_ready(candidate.base_sha, timeout_sec=0.1) is False

    # A pin for a DIFFERENT base is not this update's pin either.
    other = update_runtime.publish_runtime_snapshot(
        candidate.repo, candidate.drive, candidate.base_sha)
    update_runtime.select_assisted_runtime({**other, "sha": other["sha"]})
    assert update_merge.ensure_assisted_resolver_ready("f" * 40, timeout_sec=0.1) is False


# ---------------------------------------------------------------------------
# Recovery: legacy creation when the directory is absent, or refuse in place
# ---------------------------------------------------------------------------

@pytest.fixture
def pointed_at_candidate(candidate, monkeypatch):
    from supervisor import git_ops

    monkeypatch.setattr(git_ops, "REPO_DIR", candidate.repo)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", candidate.drive)
    yield candidate
    update_runtime.clear_assisted_runtime()


def test_recovery_rebuilds_a_pin_a_legacy_transaction_never_recorded(pointed_at_candidate):
    """A transaction written before runtime pins existed is still recoverable."""
    from supervisor import update_merge

    candidate = pointed_at_candidate
    _stage_conflicts(candidate.repo)
    tx = {"phase": "assisted_resolution", "task_id": "t",
          "pre_update_sha": candidate.base_sha, "target_sha": "b" * 40}

    ok, detail = update_merge.ensure_assisted_runtime_selected(tx)

    assert ok is True, detail
    pin = tx[update_merge.RUNTIME_PIN_FIELD]
    assert pin["sha"] == candidate.base_sha
    assert update_runtime.active_runtime_pin()["root"] == pin["root"]
    # Reconstructed from the object database with the conflicts live.
    assert "<<<<<<< HEAD" in (candidate.repo / "server.py").read_text(encoding="utf-8")
    assert "<<<<<<< HEAD" not in (pathlib.Path(pin["root"]) / "server.py").read_text(encoding="utf-8")


def test_recovery_refuses_a_base_that_cannot_host_an_isolated_resolver(pointed_at_candidate):
    from supervisor import update_merge

    candidate = pointed_at_candidate
    (candidate.repo / update_runtime.BOOTSTRAP_REL).unlink()
    _git(candidate.repo, "add", "-A")
    _git(candidate.repo, "commit", "-q", "-m", "pre-pin base")
    legacy_sha = _git(candidate.repo, "rev-parse", "HEAD")
    tx = {"phase": "assisted_resolution", "pre_update_sha": legacy_sha}

    ok, detail = update_merge.ensure_assisted_runtime_selected(tx)

    assert ok is False and "bootstrap" in detail
    assert update_runtime.active_runtime_pin() == {}


def test_a_rebuild_that_disagrees_with_the_recorded_manifest_is_refused(pointed_at_candidate):
    """A recorded pin is not a request to reconstruct a missing snapshot."""
    from supervisor import update_merge

    candidate = pointed_at_candidate
    root = candidate.drive / "state" / "update_runtime" / candidate.base_sha
    tx = {"phase": "assisted_resolution", "pre_update_sha": candidate.base_sha,
          update_merge.RUNTIME_PIN_FIELD: {
              "schema_version": 1, "sha": candidate.base_sha,
              "root": str(root),
              "manifest_sha256": "f" * 64, "file_count": 1}}

    ok, detail = update_merge.ensure_assisted_runtime_selected(tx)

    assert ok is False and "missing" in detail
    assert update_runtime.active_runtime_pin() == {}
    assert not root.exists()


def test_recovery_does_not_regenerate_a_recorded_corrupt_snapshot(pointed_at_candidate):
    from supervisor import update_merge

    candidate = pointed_at_candidate
    pin = update_runtime.publish_runtime_snapshot(
        candidate.repo, candidate.drive, candidate.base_sha)
    root = pathlib.Path(pin["root"])
    marker = root / "FORENSIC_MARKER"
    marker.write_text("keep\n", encoding="utf-8")
    (root / "VERSION").write_text("forensic-corrupt\n", encoding="utf-8")
    tx = {"phase": "assisted_resolution", "pre_update_sha": candidate.base_sha,
          update_merge.RUNTIME_PIN_FIELD: pin}

    ok, detail = update_merge.ensure_assisted_runtime_selected(tx)

    assert ok is False and detail
    assert update_runtime.active_runtime_pin() == {}
    assert marker.read_text(encoding="utf-8") == "keep\n"
    assert (root / "VERSION").read_text(encoding="utf-8") == "forensic-corrupt\n"


def test_legacy_recovery_does_not_erase_an_existing_invalid_snapshot(pointed_at_candidate):
    """No recorded pin, but the snapshot directory is already there and invalid.

    Legacy creation is allowed only when that directory is absent.
    """
    from supervisor import update_merge

    candidate = pointed_at_candidate
    pin = update_runtime.publish_runtime_snapshot(
        candidate.repo, candidate.drive, candidate.base_sha)
    root = pathlib.Path(pin["root"])
    (root / "VERSION").write_text("forensic\n", encoding="utf-8")
    tx = {"phase": "assisted_resolution", "pre_update_sha": candidate.base_sha}

    ok, detail = update_merge.ensure_assisted_runtime_selected(tx)

    assert ok is False and "refusing to replace" in detail
    assert update_runtime.active_runtime_pin() == {}
    assert (root / "VERSION").read_text(encoding="utf-8") == "forensic\n"


def test_boot_recovery_preserves_a_partial_merge_when_no_runtime_can_be_pinned(
    pointed_at_candidate, monkeypatch,
):
    """The choice this branch makes: block, do not roll the owner's work away.

    A resume whose runtime cannot be pinned has two exits. With resolution work
    already in the tree, rolling back would discard edits nobody has read, so the
    transaction is re-phased to ``gate_blocked`` — boot's own retry-the-rollback
    state — and the merge is left exactly where it is.
    """
    from supervisor import update_merge

    candidate = pointed_at_candidate
    tx = {"phase": "assisted_resolution", "task_id": "t",
          "pre_update_sha": candidate.base_sha, "target_sha": "b" * 40,
          "resolution_attempts": 0}
    update_merge.write_update_tx(tx)
    monkeypatch.setattr(update_merge, "_assisted_head_state", lambda _tx: "in_progress")
    monkeypatch.setattr(update_merge, "_merge_head_sha", lambda: "a" * 40)
    monkeypatch.setattr(update_merge._g, "git_capture",
                        lambda _cmd: (0, "UU server.py\n", ""))
    monkeypatch.setattr(update_merge, "live_unmerged_paths", lambda: ["server.py"])
    monkeypatch.setattr(update_merge, "ensure_assisted_runtime_selected",
                        lambda _tx: (False, "no bootstrap in this base"))
    monkeypatch.setattr(
        update_merge, "rollback_managed_update",
        lambda *_a, **_k: pytest.fail("a partial merge must not be rolled away here"))
    monkeypatch.setattr(
        update_merge, "materialize_assisted_merge_live",
        lambda *_a: pytest.fail("a blocked resume must not re-stage the merge"))
    monkeypatch.setattr(
        update_merge, "enqueue_assisted_resolution_task",
        lambda _tx: pytest.fail("a blocked resume must not enqueue an unpinned resolver"))

    result = update_merge._recover_assisted_on_boot(tx, supervisor_ready=True)

    assert result["blocked"] is True
    assert result["reason"] == "assisted_runtime_pin_unavailable"
    stored = update_merge.read_update_tx()
    assert stored["phase"] == update_merge.GATE_BLOCKED_PHASE
    assert stored["gate_blocked_reason"] == "assisted_runtime_pin_unavailable"
    assert stored["gate_blocked_from_phase"] == "assisted_resolution"


def test_the_enqueue_seam_blocks_rather_than_enqueue_an_unpinned_resolver(
    pointed_at_candidate, monkeypatch,
):
    from supervisor import update_merge, workers

    candidate = pointed_at_candidate
    tx = {"phase": "assisted_resolution", "task_id": "resolver-x",
          "pre_update_sha": candidate.base_sha, "target_sha": "b" * 40}
    update_merge.write_update_tx(tx)
    monkeypatch.setattr(update_merge, "ensure_assisted_runtime_selected",
                        lambda _tx: (False, "snapshot gone"))
    monkeypatch.setattr(
        workers, "ensure_worker_pool_started",
        lambda **_kwargs: pytest.fail("no pool may start without a pinned runtime"))

    assert update_merge.enqueue_assisted_resolution_task(tx) == ""
    assert update_merge.read_update_tx()["phase"] == update_merge.GATE_BLOCKED_PHASE


def test_clearing_the_transaction_returns_the_pool_to_ordinary_workers(pointed_at_candidate):
    """...and keeps the snapshot, which costs disk where a refused update costs the update."""
    from supervisor import update_merge

    candidate = pointed_at_candidate
    tx = {"phase": "assisted_resolution"}
    update_merge.write_update_tx(tx)
    ok, detail = update_merge.pin_assisted_runtime(tx, candidate.base_sha)
    assert ok, detail
    root = pathlib.Path(tx[update_merge.RUNTIME_PIN_FIELD]["root"])

    assert update_merge.clear_update_tx() is True

    assert update_runtime.active_runtime_pin() == {}
    assert root.is_dir()


# ---------------------------------------------------------------------------
# Custody: the handoff stops existing, and nothing survives its parent
# ---------------------------------------------------------------------------

def _ledger_rows(drive: pathlib.Path) -> list:
    path = drive / "state" / "process_ledger.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode/session/process-exit proof; Windows uses Job custody")
def test_a_pinned_worker_is_custodied_and_its_handoff_does_not_outlive_its_start(pool):
    """A plain subprocess has no daemon flag, so custody is what makes it reapable.

    And the handoff is the only place the queue authkey is ever written: it must
    be gone by the time the worker is running, whichever side removed it."""
    workers, candidate = pool.workers, pool.candidate
    update_runtime.select_assisted_runtime(
        update_runtime.publish_runtime_snapshot(
            candidate.repo, candidate.drive, candidate.base_sha))

    workers.spawn_workers(1)
    pid = workers.WORKERS[0].proc.pid
    assert _wait_for(lambda: _event_rows(candidate.drive, "worker_starting"))

    handoffs = list(update_runtime.handoff_dir(candidate.drive).glob("*.handoff"))
    assert handoffs == [], "the authkey handoff outlived the worker's start"
    assert oct(update_runtime.handoff_dir(candidate.drive).stat().st_mode)[-3:] == "700"

    purposes = {row["purpose"] for row in _ledger_rows(candidate.drive)}
    assert "worker:0:pinned_runtime" in purposes
    pinned = [row for row in _ledger_rows(candidate.drive)
              if row["purpose"] == "worker:0:pinned_runtime"][0]
    assert pinned["pid"] == pid
    assert pinned["scope"] == "session"
    # Its own session, so the pool's one process-tree kill reaches the whole tree.
    assert os.getsid(pid) == pid

    pool.lifecycle.kill_worker_tree(pid)
    assert _wait_for(lambda: not _pid_alive(pid), timeout=20.0)
    assert workers.WORKERS[0].proc.is_alive() is False
    assert workers.WORKERS[0].proc.exitcode is not None


ORPHAN_PROBE = '''"""Start one pinned worker, report its pid, then die abruptly."""
import json
import multiprocessing as mp
import os
import sys


def main() -> None:
    root, drive, repo, sha, report = sys.argv[1:6]
    sys.path.insert(0, root)

    from supervisor.update_runtime import publish_runtime_snapshot, select_assisted_runtime
    from supervisor.update_runtime_worker import pinned_worker_queue, start_pinned_worker

    manager = mp.get_context("forkserver").Manager()
    pin = select_assisted_runtime(publish_runtime_snapshot(repo, drive, sha))
    proc = start_pinned_worker(
        0, pin, in_q=pinned_worker_queue(manager), event_q=manager.Queue(),
        repo_dir=repo, drive_root=drive, custody_session_id="",
    )
    # A FILE, not stdout: the orphaned worker and manager inherit this process's
    # stdout, so a pipe the test read to EOF would wait for the very processes
    # the test is about to watch die.
    with open(report, "w", encoding="utf-8") as handle:
        json.dump({"worker_pid": proc.pid, "manager_pid": manager._process.pid}, handle)
    # Abrupt: no teardown, no goodbye — exactly what an abruptly killed
    # supervisor leaves behind for the lifeline to notice.
    os._exit(0)


if __name__ == "__main__":
    main()
'''


@pytest.mark.skipif(os.name == "nt", reason="POSIX forkserver and parent-death lifeline proof")
def test_a_pinned_worker_does_not_outlive_the_supervisor_that_started_it(candidate, tmp_path):
    """The lifeline, on the path a pinned worker actually takes.

    A ``multiprocessing`` child watches the spawner's sentinel pipe. A plain
    subprocess has none, so it falls back to its ppid — and that fallback is now
    load-bearing for the one worker that matters most. Proven by killing the
    spawner outright rather than by reading the code that installs the watch.
    """
    import sys as _sys

    probe = tmp_path / "orphan_probe.py"
    probe.write_text(ORPHAN_PROBE, encoding="utf-8")
    report = tmp_path / "orphan_probe.json"
    output = tmp_path / "orphan_probe.log"
    environment = dict(os.environ)
    environment["OUROBOROS_DATA_DIR"] = str(candidate.drive)
    environment["OUROBOROS_REPO_DIR"] = str(candidate.repo)
    with output.open("wb") as sink:
        done = subprocess.run(
            [_sys.executable, str(probe), str(SOURCE_ROOT), str(candidate.drive),
             str(candidate.repo), candidate.base_sha, str(report)],
            stdin=subprocess.DEVNULL, stdout=sink, stderr=sink,
            timeout=300, env=environment, cwd=str(tmp_path),
        )
    logged = output.read_text(encoding="utf-8", errors="replace")
    assert done.returncode == 0, logged[-3000:]
    assert report.is_file(), logged[-3000:]
    reported = json.loads(report.read_text(encoding="utf-8"))
    worker_pid, manager_pid = reported["worker_pid"], reported["manager_pid"]
    try:
        # The spawner is already gone (os._exit), so the worker is an orphan the
        # moment it notices: the lifeline polls, so allow its window.
        assert _wait_for(lambda: not _pid_alive(worker_pid), timeout=60.0), (
            "the pinned worker survived the death of the process that started it")
    finally:
        for pid in (worker_pid, manager_pid):
            try:
                os.kill(pid, 9)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.mark.parametrize("damage", ["missing_digest", "wrong_digest", "wrong_base"])
def test_recorded_pin_authority_is_checked_even_with_same_sha_active(pointed_at_candidate, damage):
    from supervisor import update_merge

    candidate = pointed_at_candidate
    pin = update_runtime.publish_runtime_snapshot(
        candidate.repo, candidate.drive, candidate.base_sha)
    update_runtime.select_assisted_runtime(pin)
    recorded = dict(pin)
    if damage == "missing_digest":
        recorded.pop("manifest_sha256")
    elif damage == "wrong_digest":
        recorded["manifest_sha256"] = "f" * 64
    else:
        recorded["sha"] = "f" * 40
    tx = {"pre_update_sha": candidate.base_sha, update_merge.RUNTIME_PIN_FIELD: recorded}

    ok, detail = update_merge.ensure_assisted_runtime_selected(tx)

    assert not ok and detail
    assert update_runtime.active_runtime_pin() == {}
    assert update_runtime.validate_runtime_snapshot(pin)[0]


def test_snapshot_validation_requires_a_recorded_digest(candidate):
    pin = update_runtime.publish_runtime_snapshot(
        candidate.repo, candidate.drive, candidate.base_sha)
    pin.pop("manifest_sha256")
    ok, detail = update_runtime.validate_runtime_snapshot(pin)
    assert not ok and "digest" in detail
