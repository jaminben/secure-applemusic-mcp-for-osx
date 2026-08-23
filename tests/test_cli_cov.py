"""Full-coverage unit tests for applemusic_mcp.cli (the slim 5-verb CLI).

Everything is mocked at the boundary (auth/browser/server/requests/input), so
no real tokens, network, or browser are touched.
"""

import runpy
import sys
import types

import pytest

from applemusic_mcp import cli


def _args(**kw):
    """Build an argparse-like namespace with CLI defaults."""
    base = dict(
        dev=False, team_id=None, key_id=None, key_path=None, days=180, port=8765, force=False
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


# --- login (web) -----------------------------------------------------------


# --- login --dev -----------------------------------------------------------


# --- logout ----------------------------------------------------------------


# --- reset -----------------------------------------------------------------


def test_reset_no_force(capsys):
    assert cli.cmd_reset(_args(force=False)) == 1
    assert "force" in capsys.readouterr().out.lower()


def test_reset_force_with_config(monkeypatch, tmp_path, capsys):
    cfg = tmp_path
    (cfg / "config.json").write_text("{}")
    deleted = []
    fake_browser = types.SimpleNamespace(clear_session=lambda: None)
    fake_auth = types.SimpleNamespace(secret_delete=lambda k: deleted.append(k))
    monkeypatch.setitem(sys.modules, "applemusic_mcp.browser", fake_browser)
    monkeypatch.setitem(sys.modules, "applemusic_mcp.auth", fake_auth)
    monkeypatch.setattr(cli, "get_config_dir", lambda: cfg)
    assert cli.cmd_reset(_args(force=True)) == 0
    assert not (cfg / "config.json").exists()
    assert "developer_token" in deleted
    assert "Reset complete" in capsys.readouterr().out


def test_reset_force_no_config(monkeypatch, tmp_path):
    cfg = tmp_path / "empty"
    cfg.mkdir()
    fake_browser = types.SimpleNamespace(clear_session=lambda: None)
    fake_auth = types.SimpleNamespace(secret_delete=lambda k: None)
    monkeypatch.setitem(sys.modules, "applemusic_mcp.browser", fake_browser)
    monkeypatch.setitem(sys.modules, "applemusic_mcp.auth", fake_auth)
    monkeypatch.setattr(cli, "get_config_dir", lambda: cfg)
    assert cli.cmd_reset(_args(force=True)) == 0  # no unlink branch


# --- status ----------------------------------------------------------------


def _status_auth(dev_info, user):
    return types.SimpleNamespace(developer_token_info=lambda: dev_info, has_user_token=lambda: user)


def test_status_valid_and_api_ok(monkeypatch, tmp_path, capsys):
    import time

    monkeypatch.setattr(cli, "get_config_dir", lambda: tmp_path)
    monkeypatch.setitem(
        sys.modules, "applemusic_mcp.auth", _status_auth({"expires": time.time() + 99999}, True)
    )
    monkeypatch.setattr(cli, "get_developer_token", lambda: "D")
    monkeypatch.setattr(cli, "get_user_token", lambda: "U")
    fake_requests = types.SimpleNamespace(
        get=lambda *a, **k: types.SimpleNamespace(status_code=200)
    )
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    assert cli.cmd_status(_args()) == 0
    out = capsys.readouterr().out
    assert "valid" in out and "present" in out and "API: ok" in out


def test_status_expired_nonok_api(monkeypatch, tmp_path, capsys):
    import time

    monkeypatch.setattr(cli, "get_config_dir", lambda: tmp_path)
    monkeypatch.setitem(
        sys.modules, "applemusic_mcp.auth", _status_auth({"expires": time.time() - 10}, False)
    )
    monkeypatch.setattr(cli, "get_developer_token", lambda: "D")
    monkeypatch.setattr(cli, "get_user_token", lambda: "U")
    fake_requests = types.SimpleNamespace(
        get=lambda *a, **k: types.SimpleNamespace(status_code=403)
    )
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    assert cli.cmd_status(_args()) == 0
    out = capsys.readouterr().out
    assert "expired" in out and "none" in out and "status 403" in out


def test_status_no_token_filenotfound(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "get_config_dir", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "applemusic_mcp.auth", _status_auth(None, False))

    def boom():
        raise FileNotFoundError()

    monkeypatch.setattr(cli, "get_developer_token", boom)
    monkeypatch.setattr(cli, "get_user_token", lambda: "U")
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=lambda *a, **k: None))
    assert cli.cmd_status(_args()) == 0
    assert "not configured" in capsys.readouterr().out


def test_status_api_generic_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "get_config_dir", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "applemusic_mcp.auth", _status_auth(None, True))
    monkeypatch.setattr(cli, "get_developer_token", lambda: "D")
    monkeypatch.setattr(cli, "get_user_token", lambda: "U")

    def boom(*a, **k):
        raise RuntimeError("net down")

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=boom))
    assert cli.cmd_status(_args()) == 0
    assert "error" in capsys.readouterr().out.lower()


# --- serve -----------------------------------------------------------------


def test_serve(monkeypatch):
    called = {}
    fake_server = types.SimpleNamespace(main=lambda: called.setdefault("ran", True))
    monkeypatch.setitem(sys.modules, "applemusic_mcp.server", fake_server)
    cli.cmd_serve(_args())
    assert called["ran"]


# --- main() dispatch -------------------------------------------------------


@pytest.mark.parametrize(
    "argv,fn",
    [
        (["login"], "cmd_login"),
        (["signin"], "cmd_login"),  # hidden alias
        (["logout"], "cmd_logout"),
        (["status"], "cmd_status"),
        (["reset"], "cmd_reset"),
    ],
)
def test_main_dispatch_exits(monkeypatch, argv, fn):
    monkeypatch.setattr(sys, "argv", ["applemusic-mcp"] + argv)
    monkeypatch.setattr(cli, fn, lambda args: 0)
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0


def test_main_serve_no_exit(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["applemusic-mcp", "serve"])
    ran = {}
    monkeypatch.setattr(cli, "cmd_serve", lambda args: ran.setdefault("y", True))
    cli.main()  # serve does not sys.exit
    assert ran["y"]


def test_main_no_command_prints_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["applemusic-mcp"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_dunder_main_entrypoint(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["applemusic-mcp", "status"])
    monkeypatch.setattr(cli, "cmd_status", lambda args: 0)
    with pytest.raises(SystemExit):
        runpy.run_module("applemusic_mcp.cli", run_name="__main__")
