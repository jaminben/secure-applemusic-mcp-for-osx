"""Client detection and config merging."""

from __future__ import annotations

import json
import os
import stat
import shutil
import subprocess

import pytest

from applemusic_mcp import app_setup, clients

# These compile real AppleScript, so they need osacompile. Gate on the TOOL
# rather than on sys.platform: the point is "can this machine compile it", and
# that keeps them running on any Mac -- including yours -- while skipping on the
# Linux matrix instead of failing there.
needs_osacompile = pytest.mark.skipif(
    shutil.which("osacompile") is None, reason="needs osacompile (macOS)"
)

ENTRY = {"command": "/x/App.app/Contents/MacOS/App", "args": ["shim"]}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty Mac: no HOME state and no installed applications.

    app_dirs is redirected too -- otherwise detection finds whatever is really
    in /Applications and the result depends on the machine running the tests.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(clients, "app_dirs", lambda: (tmp_path / "Applications",))
    return tmp_path


# --- detection -------------------------------------------------------------------


def test_nothing_is_detected_on_a_bare_home(home):
    assert clients.detected() == []


def test_a_client_is_detected_from_its_config_alone(home):
    cfg = home / ".cursor" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{}", encoding="utf-8")
    assert [c.key for c in clients.detected()] == ["cursor"]


def test_a_client_is_detected_before_it_has_ever_written_a_config(home):
    """Installed but never configured is the common case on a fresh machine."""
    (home / ".claude").mkdir()
    assert [c.key for c in clients.detected()] == ["claude-code"]


def test_every_known_client_has_a_distinct_key_and_config(home):
    known = clients.known_clients()
    assert len({c.key for c in known}) == len(known)
    assert len({str(c.config) for c in known}) == len(known)


def test_vscode_uses_its_own_schema(home):
    vscode = clients.find("vscode")
    assert vscode.servers_key == "servers", "VS Code calls it 'servers', not 'mcpServers'"
    assert vscode.entry("/x", ["shim"])["type"] == "stdio"


def test_deviations_from_the_common_shape_are_only_the_known_two(home):
    """VS Code renames the object and wants a transport; Codex is TOML. Every
    other client is plain {"mcpServers": {name: {command, args}}}."""
    for client in clients.known_clients():
        if client.key in ("vscode", "codex"):
            continue
        assert client.servers_key == "mcpServers", client.key
        assert client.entry_extra == {}, client.key
        assert client.fmt == "json", client.key


# --- writing ---------------------------------------------------------------------


def test_a_missing_client_directory_is_never_created(home):
    client = clients.find("cursor")
    ok, msg = clients.configure(client, ENTRY)
    assert not ok
    assert "doesn't appear to be installed" in msg
    assert not client.config.parent.exists(), "must not fabricate a config directory"


def test_merge_keeps_other_servers_and_unrelated_keys(home):
    client = clients.find("cursor")
    client.config.parent.mkdir(parents=True)
    client.config.write_text(
        json.dumps({"mcpServers": {"other": {"command": "/bin/true"}}, "misc": 1}),
        encoding="utf-8",
    )
    ok, _ = clients.configure(client, ENTRY)
    assert ok
    data = json.loads(client.config.read_text())
    assert data["mcpServers"]["other"] == {"command": "/bin/true"}
    assert data["misc"] == 1
    assert data["mcpServers"]["unofficial-apple-music"] == ENTRY


def test_vscode_entry_lands_under_servers_not_mcpservers(home):
    client = clients.find("vscode")
    client.config.parent.mkdir(parents=True)
    ok, _ = clients.configure(client, client.entry("/x", ["shim"]))
    assert ok
    data = json.loads(client.config.read_text())
    assert "mcpServers" not in data
    assert data["servers"]["unofficial-apple-music"]["type"] == "stdio"


def test_a_new_config_is_private(home):
    client = clients.find("cursor")
    client.config.parent.mkdir(parents=True)
    clients.configure(client, ENTRY)
    assert stat.S_IMODE(client.config.stat().st_mode) == 0o600


def test_an_existing_mode_is_preserved(home):
    client = clients.find("cursor")
    client.config.parent.mkdir(parents=True)
    client.config.write_text("{}", encoding="utf-8")
    os.chmod(client.config, 0o644)
    clients.configure(client, ENTRY)
    assert stat.S_IMODE(client.config.stat().st_mode) == 0o644


def test_backup_is_written_once_not_per_run(home):
    client = clients.find("cursor")
    client.config.parent.mkdir(parents=True)
    client.config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    clients.configure(client, ENTRY)
    ok, msg = clients.configure(client, ENTRY)
    assert ok and "already configured" in msg
    assert len(list(client.config.parent.glob("*.bak-*"))) == 1


def test_an_unreadable_config_is_left_exactly_as_found(home):
    client = clients.find("cursor")
    client.config.parent.mkdir(parents=True)
    client.config.write_text("{ not json", encoding="utf-8")
    ok, msg = clients.configure(client, ENTRY)
    assert not ok and "untouched" in msg
    assert client.config.read_text() == "{ not json"


def test_no_temp_file_is_left_behind(home):
    client = clients.find("cursor")
    client.config.parent.mkdir(parents=True)
    clients.configure(client, ENTRY)
    assert list(client.config.parent.glob("*.tmp")) == []


def test_backup_does_not_shadow_the_config_name(home):
    """with_suffix() would turn mcp.json into mcp.bak-... and, for a client
    whose config is a dotfile, could collide with the real file."""
    client = clients.find("cursor")
    client.config.parent.mkdir(parents=True)
    client.config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    clients.configure(client, ENTRY)
    backups = list(client.config.parent.glob("*.bak-*"))
    assert len(backups) == 1
    assert backups[0].name.startswith("mcp.json.bak-"), backups[0].name


def test_claude_code_config_is_a_dotfile_not_a_directory(home):
    """~/.claude.json, not ~/.claude/. Getting this wrong would write a config
    Claude Code never reads."""
    assert clients.find("claude-code").config.name == ".claude.json"


def test_claude_code_is_not_ours_to_restart(home):
    """It lives in a terminal. We tell the user; we do not kill their shell."""
    client = clients.find("claude-code")
    assert client.restartable is False
    assert "Restart Claude Code" in client.caveat


def test_gui_clients_are_restartable(home):
    for client in clients.known_clients():
        if client.key != "claude-code":
            assert client.restartable is True, client.key


# --- dialog escaping -------------------------------------------------------------
#
# The regression that made every consent step silently decline itself.


@pytest.mark.parametrize(
    "text",
    ["✓ done", "A — B", "Settings → Privacy", 'a "quoted" word', "back\\slash"],
)
@needs_osacompile
def test_dialog_scripts_actually_compile(text):
    """json.dumps' default ensure_ascii=True emits \\u2713, which AppleScript
    rejects as a syntax error -- so osascript exits non-zero, _dialog returns
    "", and _confirm reads that as Skip. Every setup prompt contains an em
    dash, so this silently turned the whole installer into a no-op.

    Compiled, not merely string-matched: this has to be AppleScript's opinion.
    """
    script = (
        f"display dialog {app_setup._as_applescript_string(text)} "
        f"with title {app_setup._as_applescript_string('t')} buttons {{\"OK\"}}"
    )
    proc = subprocess.run(
        ["osacompile", "-o", "/dev/null", "-e", script], capture_output=True, text=True
    )
    assert proc.returncode == 0, f"AppleScript rejected {text!r}: {proc.stderr}"


@needs_osacompile
def test_every_setup_prompt_compiles():
    """The actual strings, not samples of them."""
    prompts = [
        app_setup._INTRO,
        app_setup._STEP_HELPER.format(bundle="x.y.z"),
        app_setup._STEP_CLIENTS.format(key="unofficial-apple-music"),
        app_setup._STEP_PERMISSION,
        "Setup finished.\n\n✓ one\n✗ two\n• three",
    ]
    for text in prompts:
        script = f"display dialog {app_setup._as_applescript_string(text)} buttons {{\"OK\"}}"
        proc = subprocess.run(
            ["osacompile", "-o", "/dev/null", "-e", script], capture_output=True, text=True
        )
        assert proc.returncode == 0, f"rejected: {text[:40]!r} -> {proc.stderr}"


def test_dialog_does_not_use_ensure_ascii_json_dumps():
    """Guard the call site itself, so the fix cannot be undone by tidying."""
    import inspect

    src = inspect.getsource(app_setup._dialog)
    assert "json.dumps(text)" not in src
    assert "_as_applescript_string(text)" in src


def test_an_installed_app_alone_is_enough_to_detect(home):
    """Detection must not depend on the user having launched the client yet."""
    (home / "Applications").mkdir()
    (home / "Applications" / "Cursor.app").mkdir()
    assert [c.key for c in clients.detected()] == ["cursor"]


# --- quit and relaunch -----------------------------------------------------------


def test_running_detection_needs_an_installed_app(home):
    """No .app on disk means nothing to look for, and nothing to kill."""
    (home / ".cursor").mkdir()
    assert clients.running_pids(clients.find("cursor")) == []


def test_quit_is_a_no_op_when_nothing_is_running(home, monkeypatch):
    monkeypatch.setattr(clients, "running_pids", lambda c: [])
    killed = []
    monkeypatch.setattr(os, "kill", lambda p, s: killed.append((p, s)))
    assert clients.quit_client(clients.find("cursor")) is True
    assert killed == []


def test_quit_sends_sigterm_and_never_sigkill(home, monkeypatch):
    """A client that ignores SIGTERM has unsaved state. Escalating to SIGKILL
    to save the user a menu click is a bad trade, so it must never happen."""
    import signal as sig

    sent = []
    monkeypatch.setattr(os, "kill", lambda p, s: sent.append(s))
    # Still running on every poll: the timeout path.
    monkeypatch.setattr(clients, "running_pids", lambda c: [4321])
    assert clients.quit_client(clients.find("cursor"), timeout=0.5) is False
    assert sent and set(sent) == {sig.SIGTERM}
    assert sig.SIGKILL not in sent


def test_quit_reports_success_once_the_process_is_gone(home, monkeypatch):
    calls = {"n": 0}

    def pids(_client):
        calls["n"] += 1
        return [4321] if calls["n"] <= 2 else []

    monkeypatch.setattr(clients, "running_pids", pids)
    monkeypatch.setattr(os, "kill", lambda p, s: None)
    assert clients.quit_client(clients.find("cursor"), timeout=5) is True


def test_quit_survives_a_process_that_exits_between_poll_and_signal(home, monkeypatch):
    """A PID can die in the gap; that is success, not an error."""

    def boom(_pid, _sig):
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", boom)
    seen = {"n": 0}

    def pids(_c):
        seen["n"] += 1
        return [999] if seen["n"] == 1 else []

    monkeypatch.setattr(clients, "running_pids", pids)
    assert clients.quit_client(clients.find("cursor"), timeout=5) is True


def test_relaunch_uses_launchservices_not_an_apple_event(home, monkeypatch):
    """`open` asks LaunchServices to start an app and needs no permission.
    An AppleScript 'activate' would require permission to CONTROL the app."""
    (home / "Applications").mkdir()
    app = home / "Applications" / "Cursor.app"
    app.mkdir()
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(clients.subprocess, "run", fake_run)
    assert clients.relaunch(clients.find("cursor")) is True
    assert seen["argv"][:2] == ["open", "-a"]
    assert "osascript" not in " ".join(seen["argv"])


def test_relaunch_without_an_installed_app_fails_quietly(home):
    (home / ".cursor").mkdir()
    assert clients.relaunch(clients.find("cursor")) is False


# --- Codex / TOML ----------------------------------------------------------------


def test_codex_is_the_only_toml_client(home):
    toml_clients = [c.key for c in clients.known_clients() if c.fmt == "toml"]
    assert toml_clients == ["codex"]
    assert clients.find("codex").servers_key == "mcp_servers"


def test_chatgpt_desktop_is_not_offered(home):
    """It has no local MCP config: its connectors are remote HTTPS servers
    registered server-side, so a local stdio server cannot be added at all.
    Listing it would promise something the installer cannot deliver."""
    assert not any("chatgpt" in c.key.lower() for c in clients.known_clients())


def _codex(home):
    client = clients.find("codex")
    client.config.parent.mkdir(parents=True, exist_ok=True)
    return client


def test_toml_append_preserves_everything_else(home):
    client = _codex(home)
    original = (
        "# a comment the user wrote\n"
        "model = \"gpt-5\"\n"
        "\n"
        "[mcp_servers.node_repl]\n"
        "command = \"/x/node\"\n"
        "args = []\n"
    )
    client.config.write_text(original, encoding="utf-8")
    ok, _ = clients.configure(client, {"command": "/a/b", "args": ["shim"]})
    assert ok

    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    text = client.config.read_text()
    data = tomllib.loads(text)
    assert "# a comment the user wrote" in text, "comments must survive"
    assert data["model"] == "gpt-5"
    assert data["mcp_servers"]["node_repl"] == {"command": "/x/node", "args": []}
    assert data["mcp_servers"]["unofficial-apple-music"] == {"command": "/a/b", "args": ["shim"]}


def test_toml_update_replaces_rather_than_duplicating(home):
    """Two tables with the same name is a TOML parse error, so a naive append
    on the second run would corrupt the file."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    client = _codex(home)
    clients.configure(client, {"command": "/first", "args": ["shim"]})
    clients.configure(client, {"command": "/second", "args": ["shim"]})
    text = client.config.read_text()
    assert text.count("[mcp_servers.unofficial-apple-music]") == 1
    assert tomllib.loads(text)["mcp_servers"]["unofficial-apple-music"]["command"] == "/second"


def test_toml_replacement_keeps_the_following_table(home):
    """The span to replace ends at the next table header, not at end of file."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    client = _codex(home)
    client.config.write_text(
        "[mcp_servers.unofficial-apple-music]\ncommand = \"/old\"\nargs = []\n\n"
        "[mcp_servers.keepme]\ncommand = \"/keep\"\nargs = []\n",
        encoding="utf-8",
    )
    clients.configure(client, {"command": "/new", "args": ["shim"]})
    data = tomllib.loads(client.config.read_text())
    assert data["mcp_servers"]["keepme"] == {"command": "/keep", "args": []}
    assert data["mcp_servers"]["unofficial-apple-music"]["command"] == "/new"


def test_toml_second_run_is_a_no_op(home):
    client = _codex(home)
    # Start from an existing file, so the first write is an edit and therefore
    # backs up; creating a file from nothing has nothing to back up.
    client.config.write_text('model = "gpt-5"\n', encoding="utf-8")
    entry = {"command": "/a/b", "args": ["shim"]}
    clients.configure(client, entry)
    first = client.config.read_text()
    ok, msg = clients.configure(client, entry)
    assert ok and "already configured" in msg
    assert client.config.read_text() == first
    assert len(list(client.config.parent.glob("*.bak-*"))) == 1


def test_unparseable_toml_is_left_alone(home):
    client = _codex(home)
    client.config.write_text("[[[ not toml", encoding="utf-8")
    ok, msg = clients.configure(client, {"command": "/a", "args": []})
    assert not ok and "untouched" in msg
    assert client.config.read_text() == "[[[ not toml"


def test_a_new_toml_config_is_private(home):
    client = _codex(home)
    clients.configure(client, {"command": "/a/b", "args": ["shim"]})
    assert stat.S_IMODE(client.config.stat().st_mode) == 0o600


def test_toml_append_to_a_file_without_a_trailing_newline(home):
    """Appending directly onto the last line would fuse our header onto it."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    client = _codex(home)
    client.config.write_text('model = "gpt-5"', encoding="utf-8")
    ok, _ = clients.configure(client, {"command": "/a", "args": ["shim"]})
    assert ok
    data = tomllib.loads(client.config.read_text())
    assert data["model"] == "gpt-5"
    assert data["mcp_servers"]["unofficial-apple-music"]["command"] == "/a"


@pytest.mark.parametrize(
    "value", ['/has "quotes"/x', "/back\\slash/x", "/tab\there/x", "/plain/x"]
)
def test_toml_strings_round_trip_hostile_paths(home, value):
    """A path is not ours to sanitise -- it has to survive verbatim."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    client = _codex(home)
    clients.configure(client, {"command": value, "args": ["shim"]})
    data = tomllib.loads(client.config.read_text())
    assert data["mcp_servers"]["unofficial-apple-music"]["command"] == value


def test_creating_a_config_from_nothing_writes_no_backup(home):
    """There is no prior content to preserve, so a backup would just be noise."""
    client = _codex(home)
    clients.configure(client, {"command": "/a", "args": ["shim"]})
    assert list(client.config.parent.glob("*.bak-*")) == []


def _install_app(home, name: str, bundle_id: str):
    """Put a minimal .app on the fake disk."""
    import plistlib

    app = home / "Applications" / f"{name}.app"
    (app / "Contents").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as handle:
        plistlib.dump({"CFBundleIdentifier": bundle_id}, handle)
    return app


def test_codex_is_labelled_with_the_name_on_disk(home):
    """Someone looking for their ChatGPT app must not have to know it is Codex
    underneath. The name is read from the installed bundle, so a rename fixes
    itself -- ChatGPT Classic became ChatGPT in a point release."""
    _install_app(home, "ChatGPT", "com.openai.codex")
    assert clients.find("codex").label == "Codex (ChatGPT)"

    # And again under the older name, with no code change.
    import shutil

    shutil.rmtree(home / "Applications")
    _install_app(home, "ChatGPT Classic", "com.openai.codex")
    assert clients.find("codex").label == "Codex (ChatGPT Classic)"


def test_an_app_is_found_by_bundle_id_not_by_file_name(home):
    """The whole point: a renamed app must still be found."""
    app = _install_app(home, "Something Else Entirely", "com.openai.codex")
    assert clients._app_path(clients.find("codex")) == app


def test_a_client_with_no_app_installed_falls_back_to_its_alias(home):
    (home / ".codex").mkdir()
    assert clients.find("codex").label == "Codex (ChatGPT)"


def test_an_unreadable_bundle_does_not_stop_the_scan(home):
    """Third-party bundles contain anything, including broken plists."""
    broken = home / "Applications" / "Broken.app" / "Contents"
    broken.mkdir(parents=True)
    (broken / "Info.plist").write_bytes(b"<plist><not well formed")
    wanted = _install_app(home, "ChatGPT", "com.openai.codex")
    assert clients._app_path(clients.find("codex")) == wanted


def test_clients_without_an_alias_are_labelled_plainly(home):
    for client in clients.known_clients():
        if client.key != "codex":
            assert client.label == client.name, client.key


def test_labels_are_unique(home):
    """The picker matches the user's selection back by label, so a duplicate
    would configure the wrong client."""
    labels = [c.label for c in clients.known_clients()]
    assert len(set(labels)) == len(labels)


# ============================================================================
# Migration off the old "apple-music" key
# ============================================================================


def test_json_rename_drops_the_superseded_entry(home):
    """The old key pointed at the same binary, so leaving it is a duplicate."""
    client = clients.find("claude-desktop")
    client.config.parent.mkdir(parents=True, exist_ok=True)
    client.config.write_text(
        json.dumps({"mcpServers": {"apple-music": {"command": "/a/b", "args": ["shim"]}}})
    )
    ok, msg = clients.configure(client, {"command": "/a/b", "args": ["shim"]})
    assert ok
    data = json.loads(client.config.read_text())
    assert "apple-music" not in data["mcpServers"]
    assert data["mcpServers"]["unofficial-apple-music"]["command"] == "/a/b"
    assert "superseded" in msg


def test_json_rename_keeps_someone_elses_apple_music_server(home):
    """Another project may legitimately use that name. Not ours to delete."""
    client = clients.find("claude-desktop")
    client.config.parent.mkdir(parents=True, exist_ok=True)
    theirs = {"command": "/usr/local/bin/other-server", "args": []}
    client.config.write_text(json.dumps({"mcpServers": {"apple-music": theirs}}))
    ok, _ = clients.configure(client, {"command": "/a/b", "args": ["shim"]})
    assert ok
    data = json.loads(client.config.read_text())
    assert data["mcpServers"]["apple-music"] == theirs
    assert data["mcpServers"]["unofficial-apple-music"]["command"] == "/a/b"


def test_json_migration_runs_even_when_new_entry_already_correct(home):
    """A half-migrated config -- both keys present -- must still converge."""
    client = clients.find("claude-desktop")
    client.config.parent.mkdir(parents=True, exist_ok=True)
    entry = {"command": "/a/b", "args": ["shim"]}
    client.config.write_text(
        json.dumps({"mcpServers": {"apple-music": dict(entry), "unofficial-apple-music": dict(entry)}})
    )
    ok, _ = clients.configure(client, entry)
    assert ok
    data = json.loads(client.config.read_text())
    assert "apple-music" not in data["mcpServers"]
    assert data["mcpServers"]["unofficial-apple-music"] == entry


def test_toml_rename_drops_the_superseded_table(home):
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    client = _codex(home)
    client.config.parent.mkdir(parents=True, exist_ok=True)
    client.config.write_text('[mcp_servers.apple-music]\ncommand = "/a/b"\nargs = ["shim"]\n')
    ok, msg = clients.configure(client, {"command": "/a/b", "args": ["shim"]})
    assert ok
    data = tomllib.loads(client.config.read_text())
    assert "apple-music" not in data["mcp_servers"]
    assert data["mcp_servers"]["unofficial-apple-music"]["command"] == "/a/b"
