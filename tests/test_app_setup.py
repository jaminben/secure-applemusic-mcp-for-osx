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
    monkeypatch.setenv("APPLEMUSIC_APP_BUNDLE", str(tmp_path / "UnofficialAppleMusicMCP.app"))
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
    monkeypatch.setenv("APPLEMUSIC_APP_BUNDLE", str(tmp_path / "UnofficialAppleMusicMCP.app"))
    monkeypatch.setattr(app_setup, "LAUNCH_AGENT", tmp_path / "agent.plist")
    monkeypatch.setattr(app_setup, "LOG_DIR", tmp_path / "logs")

    app_setup.install_launch_agent()
    data = plistlib.loads((tmp_path / "agent.plist").read_bytes())

    assert data["Label"] == app_setup.BUNDLE_ID
    # The agent must launch the HELPER (the half that holds the grant).
    assert data["ProgramArguments"][1] == "helper"
    assert data["ProgramArguments"][0].endswith("Contents/MacOS/UnofficialAppleMusicMCP")
    assert data["RunAtLoad"] is True
    assert stat.S_IMODE((tmp_path / "logs").stat().st_mode) == 0o700


def test_bundle_path_is_derived_from_the_app(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLEMUSIC_APP_BUNDLE", str(tmp_path / "Foo.app"))
    assert app_setup.app_bundle_path() == tmp_path / "Foo.app"
    assert app_setup.helper_executable().parts[-2:] == ("MacOS", "UnofficialAppleMusicMCP")


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


# ============================================================================
# Setup logging and environment diagnostics
# ============================================================================


def test_log_writes_to_a_file_not_only_stderr(tmp_path, monkeypatch):
    """Double-clicked, stderr goes nowhere the user can reach."""
    log_dir = tmp_path / "Logs"
    monkeypatch.setattr(app_setup, "LOG_DIR", log_dir)
    monkeypatch.setattr(app_setup, "SETUP_LOG", log_dir / "setup.log")
    app_setup._log("hello")
    assert (log_dir / "setup.log").exists()
    assert "hello" in (log_dir / "setup.log").read_text()


def test_log_appends_across_runs(tmp_path, monkeypatch):
    """A second run after a failed first is the history worth keeping."""
    log_dir = tmp_path / "Logs"
    monkeypatch.setattr(app_setup, "LOG_DIR", log_dir)
    monkeypatch.setattr(app_setup, "SETUP_LOG", log_dir / "setup.log")
    app_setup._log("first")
    app_setup._log("second")
    text = (log_dir / "setup.log").read_text()
    assert "first" in text and "second" in text


def test_log_survives_an_unwritable_directory(tmp_path, monkeypatch, capsys):
    """Setup must not die because logging failed."""
    blocked = tmp_path / "nope"
    blocked.write_text("I am a file, not a directory")
    monkeypatch.setattr(app_setup, "LOG_DIR", blocked)
    monkeypatch.setattr(app_setup, "SETUP_LOG", blocked / "setup.log")
    app_setup._log("still fine")          # must not raise
    assert "still fine" in capsys.readouterr().err


def test_log_splits_multiline_messages(tmp_path, monkeypatch):
    """The summary is written as one multi-line blob; stamp every line."""
    log_dir = tmp_path / "Logs"
    monkeypatch.setattr(app_setup, "LOG_DIR", log_dir)
    monkeypatch.setattr(app_setup, "SETUP_LOG", log_dir / "setup.log")
    app_setup._log("one\ntwo")
    lines = [ln for ln in (log_dir / "setup.log").read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert all(ln[:4].isdigit() for ln in lines)


def test_is_translocated_detects_the_randomized_mount(tmp_path):
    """A quarantined app opened from Downloads runs from a path that vanishes."""
    from pathlib import Path

    assert app_setup.is_translocated(
        Path("/private/var/folders/ab/AppTranslocation/DEAD-BEEF/d/UnofficialAppleMusicMCP.app")
    )
    assert not app_setup.is_translocated(Path("/Applications/UnofficialAppleMusicMCP.app"))
    assert not app_setup.is_translocated(tmp_path / "UnofficialAppleMusicMCP.app")


def test_log_environment_warns_when_translocated(tmp_path, monkeypatch):
    """The warning is the whole point: it names the fix, in the log."""
    log_dir = tmp_path / "Logs"
    monkeypatch.setattr(app_setup, "LOG_DIR", log_dir)
    monkeypatch.setattr(app_setup, "SETUP_LOG", log_dir / "setup.log")
    monkeypatch.setattr(
        app_setup,
        "app_bundle_path",
        lambda: __import__("pathlib").Path(
            "/private/var/folders/ab/AppTranslocation/X/d/UnofficialAppleMusicMCP.app"
        ),
    )
    app_setup._log_environment()
    text = (log_dir / "setup.log").read_text()
    assert "translocated" in text
    assert "/Applications" in text


def test_log_environment_records_the_basics(tmp_path, monkeypatch):
    log_dir = tmp_path / "Logs"
    monkeypatch.setattr(app_setup, "LOG_DIR", log_dir)
    monkeypatch.setattr(app_setup, "SETUP_LOG", log_dir / "setup.log")
    monkeypatch.setattr(
        app_setup, "app_bundle_path",
        lambda: __import__("pathlib").Path("/Applications/UnofficialAppleMusicMCP.app"),
    )
    app_setup._log_environment()
    text = (log_dir / "setup.log").read_text()
    assert app_setup.BUNDLE_ID in text
    assert "bundle:" in text
    assert "wizard:" in text


# ============================================================================
# prime_permission must send a REAL Apple Event
# ============================================================================


def test_primer_does_not_use_a_locally_answered_property():
    """The regression this guard exists for.

    AppleScript answers an application's name/version/running/frontmost from
    the app bundle, with no Apple Event and therefore no TCC check. The primer
    used `get name`, so it exited 0 on a machine with no permission at all and
    reported success -- no prompt ever appeared, on any machine.
    """
    import inspect

    src = inspect.getsource(app_setup.prime_permission)
    script_line = next(
        ln for ln in src.splitlines() if "tell application" in ln and "script =" in ln
    )
    for prop in app_setup.PRIMER_LOCAL_PROPS:
        assert f"get {prop}'" not in script_line, (
            f"primer reads '{prop}', which AppleScript answers without an "
            "Apple Event -- it can never trigger the permission prompt"
        )
    assert "player state" in script_line


def test_primer_reports_success_only_on_exit_zero(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(
        app_setup.subprocess, "run",
        lambda *a, **k: sp.CompletedProcess(a[0] if a else [], 0, "playing", ""),
    )
    ok, msg = app_setup.prime_permission()
    assert ok and "granted" in msg.lower()


def test_primer_recognises_the_declined_error(monkeypatch):
    """-1743 was unreachable while the primer used `get name`."""
    import subprocess as sp

    monkeypatch.setattr(
        app_setup.subprocess, "run",
        lambda *a, **k: sp.CompletedProcess(
            a[0] if a else [], 1, "",
            "execution error: Not authorized to send Apple events to Music. (-1743)",
        ),
    )
    ok, msg = app_setup.prime_permission()
    assert not ok
    assert "System Settings" in msg


def test_primer_surfaces_an_unexpected_error(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(
        app_setup.subprocess, "run",
        lambda *a, **k: sp.CompletedProcess(a[0] if a else [], 1, "", "something else broke"),
    )
    ok, msg = app_setup.prime_permission()
    assert not ok and "something else broke" in msg


def test_primer_survives_a_timeout(monkeypatch):
    import subprocess as sp

    def boom(*a, **k):
        raise sp.TimeoutExpired(cmd="osascript", timeout=120)

    monkeypatch.setattr(app_setup.subprocess, "run", boom)
    ok, _ = app_setup.prime_permission()
    assert not ok


# ============================================================================
# The wizard path must leave the same trail the dialog fallback does
# ============================================================================


def _wizard_log(monkeypatch, tmp_path, run_wizard_impl):
    log_dir = tmp_path / "Logs"
    monkeypatch.setattr(app_setup, "LOG_DIR", log_dir)
    monkeypatch.setattr(app_setup, "SETUP_LOG", log_dir / "setup.log")
    monkeypatch.setattr(app_setup, "_build_plan", lambda: {"pages": []})
    monkeypatch.setattr(app_setup.setup_ui, "run_wizard", run_wizard_impl)
    app_setup._run_with_window()
    f = log_dir / "setup.log"
    return f.read_text() if f.exists() else ""


def test_wizard_logs_each_step_result(monkeypatch, tmp_path):
    """A windowed run used to record the LaunchAgent write and nothing else."""
    monkeypatch.setattr(
        app_setup, "_run_step", lambda page, sel: (True, ["✓ Background helper installed"])
    )

    def fake(plan, handler):
        handler("helper", [])
        handler("permission", [])
        return True

    text = _wizard_log(monkeypatch, tmp_path, fake)
    assert "step helper: running" in text
    assert "step helper: ✓ Background helper installed" in text
    assert "step helper: ok" in text
    assert "step permission: running" in text
    assert "wizard finished" in text


def test_wizard_logs_a_failed_step_distinctly(monkeypatch, tmp_path):
    monkeypatch.setattr(app_setup, "_run_step", lambda page, sel: (False, ["✗ nope"]))
    text = _wizard_log(monkeypatch, tmp_path, lambda plan, h: (h("permission", []), True)[1])
    assert "step permission: FAILED" in text
    assert "step permission: ✗ nope" in text


def test_wizard_logs_the_client_selection(monkeypatch, tmp_path):
    """Which clients were chosen is the other half of 'why is nothing configured'."""
    monkeypatch.setattr(app_setup, "_run_step", lambda page, sel: (True, []))
    text = _wizard_log(
        monkeypatch, tmp_path,
        lambda plan, h: (h("clients", ["claude-desktop", "cursor"]), True)[1],
    )
    assert "selected: claude-desktop, cursor" in text


def test_a_step_never_reached_leaves_no_line(monkeypatch, tmp_path):
    """'Not reached' and 'declined' must not look the same in the log."""
    monkeypatch.setattr(app_setup, "_run_step", lambda page, sel: (True, []))
    text = _wizard_log(monkeypatch, tmp_path, lambda plan, h: (h("helper", []), True)[1])
    assert "step helper:" in text
    assert "step permission:" not in text


def test_wizard_logs_cancellation_and_fallback(monkeypatch, tmp_path):
    assert "cancelled by user" in _wizard_log(monkeypatch, tmp_path, lambda plan, h: False)
    assert "falling back to dialogs" in _wizard_log(monkeypatch, tmp_path, lambda plan, h: None)


# --- unstable install locations ----------------------------------------------
#
# Setup writes an absolute path into the LaunchAgent and into every client
# config. A bundle in `dist/` or `~/Downloads` is one rebuild away from moving,
# and what it leaves behind is a client spawning a command that is not there.


def test_is_stable_location_accepts_only_the_applications_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(
        app_setup, "INSTALL_DIRS", (tmp_path / "Applications", tmp_path / "home-apps")
    )
    (tmp_path / "Applications").mkdir()
    (tmp_path / "home-apps").mkdir()

    for good in ("Applications", "home-apps"):
        assert app_setup.is_stable_location(tmp_path / good / "UnofficialAppleMusicMCP.app")

    for bad in ("dist", "Downloads", "dist/stage-arm64"):
        p = tmp_path / bad / "UnofficialAppleMusicMCP.app"
        assert not app_setup.is_stable_location(p), f"{bad} must not count as installed"


def test_install_copy_lands_in_the_first_writable_dir(tmp_path, monkeypatch):
    unwritable = tmp_path / "no-such-root" / "Applications"
    target = tmp_path / "home-apps"
    monkeypatch.setattr(app_setup, "INSTALL_DIRS", (unwritable, target))
    monkeypatch.setattr(app_setup, "_log", lambda *a, **k: None)

    src = tmp_path / "dist" / "UnofficialAppleMusicMCP.app"
    (src / "Contents" / "MacOS").mkdir(parents=True)
    (src / "Contents" / "MacOS" / "UnofficialAppleMusicMCP").write_text("#!/bin/sh\n")

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can write anywhere, so the fallback cannot be exercised")
    unwritable.parent.mkdir()
    unwritable.parent.chmod(0o500)
    try:
        dest = app_setup.install_copy(src)
    finally:
        unwritable.parent.chmod(0o700)

    assert dest == target / "UnofficialAppleMusicMCP.app"
    assert (dest / "Contents" / "MacOS" / "UnofficialAppleMusicMCP").exists()
    assert src.exists(), "the original must be left alone"


def test_install_copy_is_idempotent(tmp_path, monkeypatch):
    target = tmp_path / "Applications"
    target.mkdir()
    monkeypatch.setattr(app_setup, "INSTALL_DIRS", (target,))
    monkeypatch.setattr(app_setup, "_log", lambda *a, **k: None)

    app = target / "UnofficialAppleMusicMCP.app"
    (app / "Contents").mkdir(parents=True)
    # Already installed: return it rather than deleting and re-copying itself.
    assert app_setup.install_copy(app) == app
    assert (app / "Contents").exists()


def test_install_copy_refuses_a_non_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(app_setup, "INSTALL_DIRS", (tmp_path / "Applications",))
    plain = tmp_path / "checkout"
    plain.mkdir()
    assert app_setup.install_copy(plain) is None


def test_main_offers_to_install_and_registers_the_copy(tmp_path, monkeypatch):
    """The whole point: what gets written down is the stable path, not this one."""
    target = tmp_path / "Applications"
    target.mkdir()
    monkeypatch.setattr(app_setup, "INSTALL_DIRS", (target,))
    monkeypatch.setattr(app_setup, "_log", lambda *a, **k: None)
    monkeypatch.setattr(app_setup, "_log_environment", lambda: None)

    src = tmp_path / "dist" / "UnofficialAppleMusicMCP.app"
    (src / "Contents" / "MacOS").mkdir(parents=True)
    monkeypatch.setattr(app_setup, "app_bundle_path", lambda: src)
    monkeypatch.delenv("APPLEMUSIC_APP_BUNDLE", raising=False)

    asked = []
    monkeypatch.setattr(
        app_setup, "_dialog",
        lambda text, **kw: (asked.append(text), "Install to Applications")[1],
    )
    monkeypatch.setattr(app_setup, "_run_with_window", lambda: 0)
    monkeypatch.setattr(sys, "platform", "darwin")

    assert app_setup.main() == 0
    assert asked, "the user must be asked before anything is copied"
    assert str(tmp_path / "dist") in asked[0], "the dialog must name the offending path"
    assert os.environ["APPLEMUSIC_APP_BUNDLE"] == str(target / "UnofficialAppleMusicMCP.app")


def test_main_leaves_a_bundle_in_applications_alone(tmp_path, monkeypatch):
    target = tmp_path / "Applications"
    target.mkdir()
    monkeypatch.setattr(app_setup, "INSTALL_DIRS", (target,))
    monkeypatch.setattr(app_setup, "_log_environment", lambda: None)

    app = target / "UnofficialAppleMusicMCP.app"
    app.mkdir()
    monkeypatch.setattr(app_setup, "app_bundle_path", lambda: app)

    def refuse(*a, **k):
        raise AssertionError("an installed bundle must not be questioned")

    monkeypatch.setattr(app_setup, "_dialog", refuse)
    monkeypatch.setattr(app_setup, "_run_with_window", lambda: 0)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert app_setup.main() == 0


def test_main_does_not_double_warn_when_translocated(tmp_path, monkeypatch):
    """Translocation has its own path through the log; don't ask twice."""
    monkeypatch.setattr(app_setup, "INSTALL_DIRS", (tmp_path / "Applications",))
    monkeypatch.setattr(app_setup, "_log_environment", lambda: None)
    app = tmp_path / "AppTranslocation" / "abc" / "d" / "UnofficialAppleMusicMCP.app"
    app.mkdir(parents=True)
    monkeypatch.setattr(app_setup, "app_bundle_path", lambda: app)

    def refuse(*a, **k):
        raise AssertionError("translocated bundles are handled by the log, not a dialog")

    monkeypatch.setattr(app_setup, "_dialog", refuse)
    monkeypatch.setattr(app_setup, "_run_with_window", lambda: 0)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert app_setup.main() == 0


def test_install_copy_keeps_the_old_app_when_the_copy_fails(tmp_path, monkeypatch):
    """A half-finished copy must not take a working install down with it."""
    target = tmp_path / "Applications"
    target.mkdir()
    monkeypatch.setattr(app_setup, "INSTALL_DIRS", (target,))
    monkeypatch.setattr(app_setup, "_log", lambda *a, **k: None)

    existing = target / "UnofficialAppleMusicMCP.app"
    (existing / "Contents").mkdir(parents=True)
    (existing / "Contents" / "keep-me").write_text("the working install")

    src = tmp_path / "dist" / "UnofficialAppleMusicMCP.app"
    (src / "Contents").mkdir(parents=True)

    def boom(*a, **k):
        raise OSError("No space left on device")

    monkeypatch.setattr(app_setup.shutil, "copytree", boom)
    assert app_setup.install_copy(src) is None
    assert (existing / "Contents" / "keep-me").read_text() == "the working install"
    assert not list(target.glob("*.installing")), "the staging dir must be cleaned up"
