from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest


REPO = pathlib.Path(__file__).resolve().parents[1]


def test_update_channel_mapping_is_closed_and_defaults_to_stable():
    from ouroboros import config
    from ouroboros import update_channels

    assert config.SETTINGS_DEFAULTS["OUROBOROS_UPDATE_CHANNEL"] == "stable"
    assert update_channels.UPDATE_CHANNEL_BRANCHES == {
        "stable": "main",
        "qa": "ouroboros-stable",
        "development": "ouroboros",
    }
    assert update_channels.get_update_branch({"OUROBOROS_UPDATE_CHANNEL": "stable"}) == "main"
    assert update_channels.get_update_branch({"OUROBOROS_UPDATE_CHANNEL": "qa"}) == "ouroboros-stable"
    assert update_channels.get_update_branch({"OUROBOROS_UPDATE_CHANNEL": "development"}) == "ouroboros"
    assert update_channels.get_update_channel({"OUROBOROS_UPDATE_CHANNEL": " NIGHTLY "}) == "stable"


def test_git_timeout_settings_share_defaults_clamps_and_env_projection(monkeypatch):
    from ouroboros import config
    from ouroboros import update_channels

    getters = {
        "OUROBOROS_MANAGED_UPDATE_FETCH_TIMEOUT_SEC": (
            update_channels.get_managed_update_fetch_timeout_sec
        ),
        "OUROBOROS_RESCUE_GIT_TIMEOUT_SEC": update_channels.get_rescue_git_timeout_sec,
    }
    for name, getter in getters.items():
        assert config.SETTINGS_DEFAULTS[name] == 300
        assert name in config.settings_env_keys()
        monkeypatch.delenv(name, raising=False)
        assert getter() == 300
        monkeypatch.setenv(name, "invalid")
        assert getter() == 300
        monkeypatch.setenv(name, "1")
        assert getter() == 30
        monkeypatch.setenv(name, "9999")
        assert getter() == 1800


def test_update_channel_setting_is_exposed_and_round_trips_in_ui():
    settings_js = (REPO / "web" / "modules" / "settings.js").read_text(encoding="utf-8")
    settings_ui = (REPO / "web" / "modules" / "settings_ui.js").read_text(encoding="utf-8")
    settings_css = (REPO / "web" / "settings.css").read_text(encoding="utf-8")

    assert "['s-update-channel', 'OUROBOROS_UPDATE_CHANNEL', 'stable']" in settings_js
    assert 'id="s-update-channel"' in settings_ui
    assert "{ value: 'stable', label: 'Stable' }" in settings_ui
    assert "{ value: 'qa', label: 'QA' }" in settings_ui
    assert "{ value: 'development', label: 'Development' }" in settings_ui
    assert "[data-update-channel-group].settings-effort-group" in settings_css


def test_update_source_can_be_imported_first_in_a_clean_process():
    result = subprocess.run(
        [sys.executable, "-c", "import supervisor.update_source"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_stable_target_is_latest_release_present_in_main_and_qa():
    from supervisor.update_source import resolve_managed_update_target

    def capture(cmd):
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            ref = cmd[3]
            values = {
                "managed/main^{commit}": "main-sha",
                "managed/ouroboros-stable^{commit}": "qa-sha",
                "refs/ouroboros-managed/tags/v6.88.0^{commit}": "sha-6880",
                "refs/ouroboros-managed/tags/v6.87.5^{commit}": "sha-6875",
            }
            return (0, values[ref], "") if ref in values else (1, "", "missing")
        if cmd == [
            "git",
            "for-each-ref",
            "--format=%(refname:strip=3)",
            "refs/ouroboros-managed/tags/v*",
        ]:
            return 0, "v6.87.5\nv6.88.0-rc.1\nv6.88.0", ""
        if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
            sha, branch = cmd[3], cmd[4]
            if sha == "sha-6880" and branch == "managed/ouroboros-stable":
                return 1, "", ""
            return 0, "", ""
        raise AssertionError(cmd)

    target_ref, target_sha, error = resolve_managed_update_target(
        "managed",
        "main",
        "managed/main",
        update_channel="stable",
        capture=capture,
    )

    assert error == ""
    assert target_ref == "refs/ouroboros-managed/tags/v6.87.5"
    assert target_sha == "sha-6875"


def test_non_stable_target_follows_selected_branch_tip():
    from supervisor.update_source import resolve_managed_update_target

    seen = []

    def capture(cmd):
        seen.append(cmd)
        return 0, "qa-tip", ""

    target_ref, target_sha, error = resolve_managed_update_target(
        "managed",
        "ouroboros-stable",
        "managed/ouroboros-stable",
        update_channel="qa",
        capture=capture,
    )

    assert (target_ref, target_sha, error) == (
        "managed/ouroboros-stable",
        "qa-tip",
        "",
    )
    assert seen == [["git", "rev-parse", "--verify", "managed/ouroboros-stable^{commit}"]]


def _release_feed(ancestors=None):
    """Fake feed where v6.88.0 is newer than v6.87.5 and both can be shared."""
    on_both = {"managed/main", "managed/ouroboros-stable"}
    ancestry = {"sha-6880": set(on_both), "sha-6875": set(on_both)}
    if ancestors:
        ancestry.update(ancestors)
    peeled = {
        "managed/main^{commit}": "main-sha",
        "managed/ouroboros-stable^{commit}": "qa-sha",
        "refs/ouroboros-managed/tags/v6.88.0^{commit}": "sha-6880",
        "refs/ouroboros-managed/tags/v6.87.5^{commit}": "sha-6875",
    }
    calls = []

    def capture(cmd):
        calls.append(list(cmd))
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            ref = cmd[3]
            if ref in peeled:
                return 0, peeled[ref], ""
            return 1, "", "missing"
        if cmd == [
            "git",
            "for-each-ref",
            "--format=%(refname:strip=3)",
            "refs/ouroboros-managed/tags/v*",
        ]:
            return 0, "v6.87.5\nv6.88.0-rc.1\nv6.88.0", ""
        if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
            sha, branch = cmd[3], cmd[4]
            return (0, "", "") if branch in ancestry.get(sha, ()) else (1, "", "")
        raise AssertionError(cmd)

    return capture, calls


def _listed(calls):
    return any(len(cmd) > 1 and cmd[1] == "for-each-ref" for cmd in calls)


def test_explicit_older_release_wins_under_a_newer_shared_tip():
    from supervisor.update_source import resolve_managed_update_target

    capture, calls = _release_feed()
    newest_ref, newest_sha, newest_error = resolve_managed_update_target(
        "managed", "main", "managed/main", update_channel="stable", capture=capture,
    )
    assert (newest_ref, newest_sha, newest_error) == (
        "refs/ouroboros-managed/tags/v6.88.0", "sha-6880", "",
    )
    assert _listed(calls)

    calls.clear()
    older_ref, older_sha, older_error = resolve_managed_update_target(
        "managed", "main", "managed/main",
        update_channel="stable", capture=capture, official_release="  v6.87.5 ",
    )
    assert older_error == ""
    assert older_ref == "refs/ouroboros-managed/tags/v6.87.5"
    assert older_sha == "sha-6875"
    assert ["git", "rev-parse", "--verify", "refs/ouroboros-managed/tags/v6.87.5^{commit}"] in calls
    assert not _listed(calls)

    calls.clear()
    blank_ref, blank_sha, blank_error = resolve_managed_update_target(
        "managed", "main", "managed/main",
        update_channel="stable", capture=capture, official_release="   ",
    )
    assert (blank_ref, blank_sha, blank_error) == (
        "refs/ouroboros-managed/tags/v6.88.0", "sha-6880", "",
    )
    assert _listed(calls)


def test_explicit_release_refusals_do_not_select_another_tag():
    from supervisor.update_source import resolve_managed_update_target

    def refuse(release, channel="stable", ancestors=None):
        capture, calls = _release_feed(ancestors)
        ref, sha, error = resolve_managed_update_target(
            "managed", "main", "managed/main",
            update_channel=channel, capture=capture, official_release=release,
        )
        assert ref == "" and sha == "", (release, ref, sha, error)
        assert error
        assert sha != "sha-6880"
        return error, calls

    for bad in (
        None, 7, 7.7, ["v6.87.5"], {"tag": "v6.87.5"}, b"v6.87.5", True,
    ):
        error, calls = refuse(bad)
        assert error == "official_release must be a plain vX.Y.Z tag"
        assert calls == []

    for bad in (
        "v6.88.0-rc.1",
        "e41596c389c3c33c3155637aaf67f8bebbf4934d",
        "refs/ouroboros-managed/tags/v6.87.5",
        "refs/tags/v6.87.5",
        "v6.87",
        "V6.87.5",
    ):
        error, calls = refuse(bad)
        assert error == "official_release must be a plain vX.Y.Z tag"
        assert calls == []

    error, calls = refuse("v6.87.5", channel="qa")
    assert error == "official_release is only valid on the stable channel"
    assert calls == []
    assert error != "qa-tip"

    error, calls = refuse("v9.9.9")
    assert "not in the fetched managed namespace: v9.9.9" in error
    assert not _listed(calls)

    error, calls = refuse("v6.87.5", ancestors={"sha-6875": {"managed/main"}})
    assert "not reachable from both main and ouroboros-stable" in error
    assert not _listed(calls)


@pytest.mark.serial
def test_explicit_release_uses_real_managed_tag_peeling(tmp_path):
    """A newer shared release and a misleading local tag cannot replace the pin."""
    from supervisor.update_source import resolve_managed_update_target

    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            cwd=tmp_path, text=True, capture_output=True, check=True,
        ).stdout.strip()

    git("init", "-q")
    (tmp_path / "BIBLE.md").write_text("constitution\n")
    git("add", "BIBLE.md")
    git("commit", "-qm", "older")
    older = git("rev-parse", "HEAD")
    git("tag", "-a", "v6.87.5", "-m", "older release")
    tag_object = git("rev-parse", "v6.87.5")
    git("update-ref", "refs/ouroboros-managed/tags/v6.87.5", tag_object)
    git("commit", "--allow-empty", "-qm", "newer")
    newer = git("rev-parse", "HEAD")
    git("update-ref", "refs/ouroboros-managed/tags/v6.88.0", newer)
    for branch in ("main", "ouroboros-stable"):
        git("update-ref", f"refs/remotes/managed/{branch}", newer)
    git("tag", "-f", "v6.87.5", newer)

    def capture(cmd):
        result = subprocess.run(cmd, cwd=tmp_path, text=True, capture_output=True)
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    common = dict(update_channel="stable", capture=capture)
    selected = resolve_managed_update_target(
        "managed", "main", "managed/main", official_release="v6.87.5", **common,
    )
    assert selected == ("refs/ouroboros-managed/tags/v6.87.5", older, "")
    assert older != tag_object and older != newer
    assert resolve_managed_update_target("managed", "main", "managed/main", **common)[1] == newer
