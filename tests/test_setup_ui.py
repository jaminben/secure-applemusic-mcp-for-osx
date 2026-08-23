"""The first-run wizard: its protocol, and the copy it shows.

The window itself is Swift and is not exercised here. What is exercised is the
contract between them, because that is where a mistake is silent: a wizard that
cannot be shown must fall back to asking, never to assuming.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from applemusic_mcp import app_setup, setup_ui


# Captured at import, before the conftest guard neutralises them.
_REAL_WINDOW_PATH = setup_ui.window_path
_REAL_RUN_WIZARD = setup_ui.run_wizard


@pytest.fixture
def fake_window(tmp_path, monkeypatch):
    """Install a scripted stand-in for the Swift window.

    The script is handed a list of messages to emit; it reads the plan, writes
    each message, and reads one reply after any "run".
    """

    def build(messages: list[dict], record: str = "record.json") -> "tuple":
        script = tmp_path / "fake-window"
        target = tmp_path / record
        script.write_text(
            "#!" + sys.executable + "\n"
            "import json, sys\n"
            "plan = json.loads(sys.stdin.readline())\n"
            "seen = {'plan': plan, 'replies': []}\n"
            f"for message in {messages!r}:\n"
            "    sys.stdout.write(json.dumps(message) + '\\n'); sys.stdout.flush()\n"
            "    if message.get('type') == 'run':\n"
            "        seen['replies'].append(json.loads(sys.stdin.readline()))\n"
            f"open({str(target)!r}, 'w').write(json.dumps(seen))\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("APPLEMUSIC_SETUP_UI", str(script))
        # The conftest guard stubs both of these so no test can open a real
        # window. This test drives a scripted stand-in instead, so it puts the
        # genuine implementations back.
        monkeypatch.setattr(setup_ui, "window_path", _REAL_WINDOW_PATH)
        monkeypatch.setattr(setup_ui, "run_wizard", _REAL_RUN_WIZARD)
        return script, target

    return build


# --- protocol --------------------------------------------------------------------


def test_a_missing_window_is_not_consent(monkeypatch):
    """None means 'could not ask'. The caller falls back to dialogs."""
    monkeypatch.setattr(setup_ui, "window_path", lambda: None)
    assert _REAL_RUN_WIZARD({"pages": []}, lambda page, sel: (True, [])) is None


def test_finished_is_reported_as_success(fake_window):
    fake_window([{"type": "finished"}])
    assert _REAL_RUN_WIZARD({"pages": []}, lambda p, s: (True, [])) is True


def test_cancel_is_reported_as_refusal(fake_window):
    fake_window([{"type": "cancel"}])
    assert _REAL_RUN_WIZARD({"pages": []}, lambda p, s: (True, [])) is False


def test_a_window_that_dies_without_answering_is_not_success(fake_window):
    """Crashing halfway must not be read as the user having agreed."""
    fake_window([])
    assert _REAL_RUN_WIZARD({"pages": []}, lambda p, s: (True, [])) is None


def test_each_run_reaches_the_handler_and_the_reply_goes_back(fake_window):
    _, record = fake_window(
        [
            {"type": "run", "page": "helper", "selected": []},
            {"type": "run", "page": "clients", "selected": ["cursor", "vscode"]},
            {"type": "finished"},
        ]
    )
    calls = []

    def handler(page, selected):
        calls.append((page, selected))
        return True, [f"✓ did {page}"]

    assert _REAL_RUN_WIZARD({"pages": [], "title": "t"}, handler) is True
    assert calls == [("helper", []), ("clients", ["cursor", "vscode"])]

    seen = json.loads(record.read_text())
    assert seen["plan"]["title"] == "t", "the plan must reach the window"
    assert seen["replies"] == [
        {"ok": True, "lines": ["✓ did helper"]},
        {"ok": True, "lines": ["✓ did clients"]},
    ]


def test_non_string_selections_are_discarded(fake_window):
    """The window is a separate process; its output is parsed, not trusted."""
    fake_window([{"type": "run", "page": "clients", "selected": ["ok", 3, None]},
                 {"type": "finished"}])
    seen = []
    _REAL_RUN_WIZARD({"pages": []}, lambda p, s: (seen.append(s), (True, []))[1])
    assert seen == [["ok"]]


def test_garbage_lines_are_skipped_not_fatal(fake_window):
    fake_window([{"type": "chatter"}, {"type": "finished"}])
    assert _REAL_RUN_WIZARD({"pages": []}, lambda p, s: (True, [])) is True


# --- what the wizard says --------------------------------------------------------


def test_clients_come_after_the_permission():
    """Pointing a client at a server that cannot yet reach Music would have it
    fail on first use, which reads as a broken install."""
    ids = [p["id"] for p in app_setup._build_plan()["pages"]]
    assert ids.index("helper") < ids.index("permission") < ids.index("clients")
    assert ids[0] == "splash" and ids[-1] == "summary"


def test_every_step_page_says_why_it_is_needed():
    for page in app_setup._build_plan()["pages"]:
        if not page.get("action"):
            continue
        assert "WHY" in (page.get("body") or ""), page["id"]


def test_the_permission_page_states_the_limits_not_just_the_ask():
    body = next(
        p["body"] for p in app_setup._build_plan()["pages"] if p["id"] == "permission"
    )
    # What it is not, and how to get rid of it.
    assert "Accessibility" in body
    assert "never asks for it" in body
    assert "REVOKE" in body
    assert "System Settings" in body


def test_the_splash_names_the_single_permission_up_front():
    splash = app_setup._build_plan()["pages"][0]
    assert "one macOS permission" in splash["body"]
    assert splash.get("action") is None, "the splash must not do anything"


def test_the_clients_page_explains_that_the_client_gains_nothing():
    body = next(
        p["body"] for p in app_setup._build_plan()["pages"] if p["id"] == "clients"
    )
    assert "never inherits" in body


# --- steps -----------------------------------------------------------------------


def test_an_exception_in_a_step_is_reported_not_raised(monkeypatch):
    """A failing step must leave the wizard usable and say what went wrong."""

    def boom():
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(app_setup, "install_launch_agent", boom)
    ok, lines = app_setup._run_step("helper", [])
    assert ok is False
    assert any("disk on fire" in line for line in lines)


def test_selecting_no_clients_is_a_normal_outcome(monkeypatch):
    monkeypatch.setattr(app_setup.clients, "detected", lambda: [])
    ok, lines = app_setup._run_step("clients", [])
    assert ok is True
    assert lines == ["• No clients selected"]


def test_an_unknown_page_does_nothing(monkeypatch):
    assert app_setup._run_step("nonsense", []) == (True, [])
