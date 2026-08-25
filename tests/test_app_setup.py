"""First-run setup for the standalone app.

The riskiest thing an installer does is edit a file it did not write. Claude
Desktop's config usually holds the user's other MCP servers, so most of these
tests are about NOT damaging it: merge rather than replace, never write over a
file we could not parse, keep the mode, and don't accumulate backups.
"""

from __future__ import annotations

import json
import os
import plistlib
import stat
import sys
from pathlib import Path

import pytest

from applemusic_mcp import app_setup

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS installer")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A Claude Desktop config with a pre-existing server, as a real one has."""
    monkeypatch.setenv("APPLEMUSIC_APP_BUNDLE", str(tmp_path / "AppleMusicMCP.app"))
    d = tmp_path / "Library" / "Application Support" / "Claude"
    d.mkdir(parents=True)
    p = d / "claude_desktop_config.json"
    p.write_text(
        json.dumps(
            {
                "mcpServers": {"existing": {"command": "/usr/bin/true", "args": ["--keep"]}},
                "otherSetting": {"preserve": True},
            }
        ),
        encoding="utf-8",
    )
    os.chmod(p, 0o600)
    return p


def test_merges_without_touching_other_servers(cfg):
    ok, msg = app_setup.configure_claude_desktop(cfg)
    assert ok, msg
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["existing"] == {"command": "/usr/bin/true", "args": ["--keep"]}
    assert data["otherSetting"] == {"preserve": True}
    assert data["mcpServers"]["unofficial-apple-music"]["args"] == ["shim"]


def test_points_at_the_shim_never_the_helper(cfg):
    """The client must spawn the permissionless half. If this ever became
    'helper', the client's TCC identity would own the Automation grant again
    and the whole split would be pointless."""
    app_setup.configure_claude_desktop(cfg)
    entry = json.loads(cfg.read_text())["mcpServers"]["unofficial-apple-music"]
    assert entry["args"] == ["shim"]
    assert "helper" not in entry["args"]


def test_preserves_file_mode(cfg):
    os.chmod(cfg, 0o600)
    app_setup.configure_claude_desktop(cfg)
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


def test_backs_up_before_the_first_write(cfg):
    app_setup.configure_claude_desktop(cfg)
    backups = list(cfg.parent.glob("*.bak-*"))
    assert len(backups) == 1
    restored = json.loads(backups[0].read_text())
    assert "unofficial-apple-music" not in restored["mcpServers"]


def test_second_run_changes_nothing_and_adds_no_backup(cfg):
    app_setup.configure_claude_desktop(cfg)
    first = cfg.read_text()
    ok, msg = app_setup.configure_claude_desktop(cfg)
    assert ok and "already configured" in msg
    assert cfg.read_text() == first
    assert len(list(cfg.parent.glob("*.bak-*"))) == 1, "a backup per launch would pile up"


def test_unparseable_config_is_left_alone(cfg):
    """Never overwrite a config we could not read — it may hold other servers."""
    cfg.write_text("{ not json", encoding="utf-8")
    ok, msg = app_setup.configure_claude_desktop(cfg)
    assert not ok
    assert "untouched" in msg
    assert cfg.read_text() == "{ not json"


def test_non_object_config_is_left_alone(cfg):
    cfg.write_text("[1, 2, 3]", encoding="utf-8")
    ok, _ = app_setup.configure_claude_desktop(cfg)
    assert not ok
    assert cfg.read_text() == "[1, 2, 3]"


def test_non_object_mcpservers_is_left_alone(cfg):
    cfg.write_text(json.dumps({"mcpServers": "nope"}), encoding="utf-8")
    ok, _ = app_setup.configure_claude_desktop(cfg)
    assert not ok
    assert json.loads(cfg.read_text())["mcpServers"] == "nope"


def test_missing_claude_is_reported_not_created(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLEMUSIC_APP_BUNDLE", str(tmp_path / "A.app"))
    missing = tmp_path / "nowhere" / "claude_desktop_config.json"
    ok, msg = app_setup.configure_claude_desktop(missing)
    assert not ok
    assert "doesn't appear to be installed" in msg
    assert not missing.parent.exists(), "must not fabricate a Claude config directory"


def test_creates_config_when_directory_exists_but_file_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLEMUSIC_APP_BUNDLE", str(tmp_path / "A.app"))
    d = tmp_path / "Claude"
    d.mkdir()
    p = d / "claude_desktop_config.json"
    ok, _ = app_setup.configure_claude_desktop(p)
    assert ok
    assert json.loads(p.read_text())["mcpServers"]["unofficial-apple-music"]["args"] == ["shim"]


# --- LaunchAgent ---------------------------------------------------------------


def test_launch_agent_plist_is_valid_and_runs_the_helper(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPLEMUSIC_APP_BUNDLE", str(tmp_path / "AppleMusicMCP.app"))
    monkeypatch.setattr(app_setup, "LAUNCH_AGENT", tmp_path / "agent.plist")
    monkeypatch.setattr(app_setup, "LOG_DIR", tmp_path / "logs")

    app_setup.install_launch_agent()
    data = plistlib.loads((tmp_path / "agent.plist").read_bytes())

    assert data["Label"] == app_setup.BUNDLE_ID
    # The agent must launch the HELPER (the half that holds the grant).
    assert data["ProgramArguments"][1] == "helper"
    assert data["ProgramArguments"][0].endswith("Contents/MacOS/AppleMusicMCP")
    assert data["RunAtLoad"] is True
    assert stat.S_IMODE((tmp_path / "logs").stat().st_mode) == 0o700


def test_bundle_path_is_derived_from_the_app(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLEMUSIC_APP_BUNDLE", str(tmp_path / "Foo.app"))
    assert app_setup.app_bundle_path() == tmp_path / "Foo.app"
    assert app_setup.helper_executable().parts[-2:] == ("MacOS", "AppleMusicMCP")


def test_permission_prime_uses_a_read_only_script():
    """Priming the prompt must not start playback or change anything."""
    import inspect

    src = inspect.getsource(app_setup.prime_permission)
    assert "get name" in src
    for mutating in ("play", "delete", "make new", "set "):
        assert f'"{mutating}' not in src


# --- consent -------------------------------------------------------------------
#
# An installer that acts on a declined prompt is worse than one that does
# nothing, so the gating is tested rather than assumed.


@pytest.fixture
def spy(monkeypatch):
    """Record what setup would do, without doing any of it."""
    calls = []
    monkeypatch.setattr(app_setup, "install_launch_agent", lambda: calls.append("agent"))
    monkeypatch.setattr(app_setup, "load_agent", lambda: calls.append("load") or True)
    monkeypatch.setattr(
        app_setup,
        "configure_detected_clients",
        lambda: (calls.append("clients"), ["✓ configured a client's config"])[1],
    )
    monkeypatch.setattr(
        app_setup, "prime_permission", lambda: (calls.append("permission"), (True, "ok"))[1]
    )
    return calls


def _answers(monkeypatch, *replies):
    """Feed scripted dialog answers in order."""
    queue = list(replies)
    seen = []

    def fake_dialog(text, title="", buttons=("OK",), default=1):
        seen.append((text, buttons))
        return queue.pop(0) if queue else buttons[-1]

    monkeypatch.setattr(app_setup, "_dialog", fake_dialog)
    return seen


def test_quitting_the_intro_changes_nothing(spy, monkeypatch):
    _answers(monkeypatch, "Quit")
    assert app_setup.main() == 1
    assert spy == [], "declining the intro must not touch anything"


def test_each_step_can_be_skipped_independently(spy, monkeypatch):
    # Continue, then Skip all three.
    _answers(monkeypatch, "Continue", "Skip", "Skip", "Skip", "Done")
    assert app_setup.main() == 0
    assert spy == [], "skipped steps must not run"


def test_accepting_only_the_helper_runs_only_the_helper(spy, monkeypatch):
    _answers(monkeypatch, "Continue", "Install", "Skip", "Skip", "Done")
    app_setup.main()
    assert spy == ["agent", "load"]
    assert "clients" not in spy and "permission" not in spy


def test_accepting_everything_runs_everything_in_order(spy, monkeypatch):
    _answers(monkeypatch, "Continue", "Install", "Choose Clients", "Ask macOS", "Done")
    app_setup.main()
    assert spy == ["agent", "load", "clients", "permission"]


def test_prompts_name_the_file_and_the_permission(monkeypatch):
    seen = _answers(monkeypatch, "Continue", "Skip", "Skip", "Skip", "Done")
    app_setup.main()
    texts = "\n".join(t for t, _ in seen)
    # The user should be able to see exactly what is about to change.
    assert "MCP clients" in texts
    assert "backup" in texts
    assert "LaunchAgents" in texts
    assert "wants to control Music" in texts
    assert "never asks for Accessibility" in texts.replace("\n", " ")


def test_no_step_defaults_to_acting(monkeypatch, spy):
    """If a dialog fails to show, _dialog returns "" — that must read as 'skip',
    never as consent."""
    monkeypatch.setattr(app_setup, "_dialog", lambda *a, **k: "")
    assert app_setup.main() == 1
    assert spy == []


def test_temp_file_and_backup_are_never_world_readable(cfg, monkeypatch):
    """The config can hold OTHER servers' API keys in their env blocks.

    A write-then-chmod would expose those at the umask default for the duration
    of the write. Assert the mode at creation time, not just afterwards.
    """
    seen: list[int] = []
    real_open = os.open

    def spy_open(path, flags, mode=0o777, **kw):
        if str(path).endswith((".json.tmp", ".json")) or ".bak-" in str(path):
            if flags & os.O_CREAT:
                seen.append(mode)
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(os, "open", spy_open)
    ok, _ = app_setup.configure_claude_desktop(cfg)
    assert ok
    assert seen, "expected the temp file and backup to be created via os.open with a mode"
    for mode in seen:
        assert mode & 0o077 == 0, f"created with {oct(mode)} — group/other can read it"


def test_written_config_keeps_owner_only_mode(cfg):
    app_setup.configure_claude_desktop(cfg)
    assert stat.S_IMODE(cfg.stat().st_mode) & 0o077 == 0
    for backup in cfg.parent.glob("*.bak-*"):
        assert stat.S_IMODE(backup.stat().st_mode) & 0o077 == 0, "backup is readable by others"


# --- dialogs must actually display ---------------------------------------------


def test_every_dialog_string_is_valid_applescript():
    """Regression: json.dumps escapes non-ASCII to \\uXXXX, which AppleScript
    rejects as a SYNTAX ERROR — osascript exits 1 and no dialog appears.
    Because a failed dialog is deliberately read as "skip", that turned the
    entire installer into a silent no-op. The real strings contain — → ✓ ✗.

    An earlier version of this test checked _as_applescript_string directly
    while _dialog still called json.dumps, so it passed while the installer was
    broken. It therefore compiles the whole `display dialog` command now, built
    the way _dialog builds it.
    """
    import subprocess

    texts = {
        "intro": app_setup._INTRO,
        "helper": app_setup._STEP_HELPER.format(bundle=app_setup.BUNDLE_ID),
        "clients": app_setup._STEP_CLIENTS.format(key=app_setup.SERVER_KEY),
        "permission": app_setup._STEP_PERMISSION,
        "summary": "Setup finished.\n\n✓ a\n✗ b\n• c",
    }
    for name, text in texts.items():
        script = (
            f"display dialog {app_setup._as_applescript_string(text)} "
            f"with title {app_setup._as_applescript_string('Apple Music MCP')} "
            f'buttons {{"OK"}} default button 1 with icon note'
        )
        res = subprocess.run(
            ["osacompile", "-o", "/dev/null", "-e", script],
            capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 0, f"{name} dialog is not valid AppleScript: {res.stderr}"


@pytest.mark.parametrize(
    "hostile",
    ['quote " inside', "back\\slash", "→ ✓ ✗ é 日本語", 'both " and \\ and →', "new\nline"],
)
def test_dialog_quoting_survives_a_round_trip(hostile):
    """The quoting must be correct for text we did not write — error messages
    embed filesystem paths and exception strings."""
    import subprocess

    res = subprocess.run(
        ["osascript", "-e", f"return {app_setup._as_applescript_string(hostile)}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    # osascript renders a literal newline in a returned string as \n; compare
    # on the single-line forms.
    assert res.stdout.strip().replace("\\n", "\n") == hostile.replace("\r", "")


# --- restarting clients after the edit -------------------------------------------


def _client(name="Cursor", restartable=True, caveat=""):
    from applemusic_mcp import clients as _c

    return _c.Client(
        key=name.lower(), name=name, config=Path("/tmp/x.json"),
        restartable=restartable, caveat=caveat,
    )


def test_the_picker_offers_labels_not_bare_names(monkeypatch):
    """Selection is matched back by label, so the two must agree."""
    from applemusic_mcp import clients as _c

    client = _c.Client(
        key="codex", name="Codex", config=Path("/tmp/c.toml"),
        show_app_name=True, aka="ChatGPT",
    )
    monkeypatch.setattr(app_setup.clients, "detected", lambda: [client])
    seen = {}
    monkeypatch.setattr(
        app_setup, "_choose", lambda prompt, items, **k: seen.setdefault("items", items) and []
    )
    app_setup.configure_detected_clients()
    assert seen["items"] == ["Codex (ChatGPT)"]


def test_no_restart_prompt_when_nothing_is_running(monkeypatch):
    monkeypatch.setattr(app_setup.clients, "is_running", lambda c: False)
    called = []
    monkeypatch.setattr(app_setup, "_dialog", lambda *a, **k: called.append(1) or "x")
    assert app_setup._offer_restart([_client()]) == []
    assert called == [], "must not ask about restarting an app that is not running"


def test_declining_the_restart_quits_nothing(monkeypatch):
    monkeypatch.setattr(app_setup.clients, "is_running", lambda c: True)
    monkeypatch.setattr(app_setup, "_dialog", lambda *a, **k: "Not Now")
    quit_calls = []
    monkeypatch.setattr(app_setup.clients, "quit_client", lambda c: quit_calls.append(c))
    lines = app_setup._offer_restart([_client()])
    assert quit_calls == [], "declining must be honoured"
    assert any("restart to pick up" in line for line in lines)


def test_a_failed_dialog_does_not_quit_anything(monkeypatch):
    """_dialog returns "" when it cannot display. That must read as 'no'."""
    monkeypatch.setattr(app_setup.clients, "is_running", lambda c: True)
    monkeypatch.setattr(app_setup, "_dialog", lambda *a, **k: "")
    quit_calls = []
    monkeypatch.setattr(app_setup.clients, "quit_client", lambda c: quit_calls.append(c))
    app_setup._offer_restart([_client()])
    assert quit_calls == [], "an undisplayable dialog must never terminate an app"


def test_accepting_quits_then_relaunches(monkeypatch):
    order = []
    monkeypatch.setattr(app_setup.clients, "is_running", lambda c: True)
    monkeypatch.setattr(app_setup, "_dialog", lambda *a, **k: "Quit & Reopen")
    monkeypatch.setattr(
        app_setup.clients, "quit_client", lambda c: order.append("quit") or True
    )
    monkeypatch.setattr(
        app_setup.clients, "relaunch", lambda c: order.append("relaunch") or True
    )
    lines = app_setup._offer_restart([_client()])
    assert order == ["quit", "relaunch"], "relaunching before the quit completes races"
    assert any("restarted" in line for line in lines)


def test_a_client_that_refuses_to_quit_is_not_relaunched(monkeypatch):
    monkeypatch.setattr(app_setup.clients, "is_running", lambda c: True)
    monkeypatch.setattr(app_setup, "_dialog", lambda *a, **k: "Quit & Reopen")
    monkeypatch.setattr(app_setup.clients, "quit_client", lambda c: False)
    relaunched = []
    monkeypatch.setattr(app_setup.clients, "relaunch", lambda c: relaunched.append(c))
    lines = app_setup._offer_restart([_client()])
    assert relaunched == [], "relaunching a client that never quit would open a second copy"
    assert any("did not quit" in line for line in lines)


def test_a_terminal_client_is_reported_never_restarted(monkeypatch):
    monkeypatch.setattr(app_setup.clients, "is_running", lambda c: True)
    quit_calls = []
    monkeypatch.setattr(app_setup.clients, "quit_client", lambda c: quit_calls.append(c))
    monkeypatch.setattr(app_setup, "_dialog", lambda *a, **k: "Quit & Reopen")
    cc = _client("Claude Code", restartable=False, caveat="Restart your Claude Code session.")
    lines = app_setup._offer_restart([cc])
    assert quit_calls == [], "never kill the user's terminal session"
    assert any("Restart your Claude Code session" in line for line in lines)
