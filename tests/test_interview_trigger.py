"""Regression pin for the /interview -> Q00 MCP-lane trigger (owner A/B 2026-09-29)."""
from __future__ import annotations

import ouroboros.server_owner_routing as sor


def test_interview_predicate_matches_exact_and_spaced():
    assert sor.is_interview_command("/interview")
    assert sor.is_interview_command("/interview a small playlist exporter")
    assert sor.is_interview_command("  /INTERVIEW idea  ")


def test_interview_predicate_rejects_prefix_collisions():
    assert not sor.is_interview_command("/interviewing")
    assert not sor.is_interview_command("/interview-foo")
    assert not sor.is_interview_command("/status")


def _incoming(text):
    return {
        "chat_id": 123,
        "text": text,
        "image_caption": "cap",
        "client_message_id": "mid-1",
        "image_data": "img",
        "task_constraint": "tc",
        "task_metadata": "tm",
        "log_text": "log",
        "origin_message_ref": "origin",
        "source": "src",
        "received_at": "ts",
    }


def test_handle_interview_rewrites_and_preserves_fields(monkeypatch):
    seen = {}

    def _fake_route(bridge, ctx, routed):
        seen.update(routed)

    monkeypatch.setattr(sor, "_route_owner_message", _fake_route)
    replies = []
    sor.handle_interview_command(
        "bridge", "ctx", _incoming("/interview a small playlist exporter"),
        replies.append,
    )
    assert replies == []
    assert seen["chat_id"] == 123
    assert seen["client_message_id"] == "mid-1"
    assert seen["text"] == (
        "Use q00-interview-bridge MCP lane: start Q00 interview about "
        "a small playlist exporter"
    )


def test_handle_interview_bare_replies_usage_and_routes_nothing(monkeypatch):
    called = []

    def _fake_route(bridge, ctx, routed):
        called.append(routed)

    monkeypatch.setattr(sor, "_route_owner_message", _fake_route)
    replies = []
    sor.handle_interview_command(
        "bridge", "ctx", _incoming("/interview"), replies.append,
    )
    assert called == []
    assert len(replies) == 1
    assert replies[0].startswith("Usage: /interview")
