"""The first-run wizard: its protocol, and the copy it shows.

The window itself is Swift and is not exercised here. What is exercised is the
contract between them, because that is where a mistake is silent: a wizard that
cannot be shown must fall back to asking, never to assuming.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys

import pytest

from applemusic_mcp import app_setup, setup_ui


def prose(page: dict) -> str:
    """Everything the page says. Which half a sentence sits in is layout, not
    meaning, so tests assert against both."""
    return " ".join(filter(None, [page.get("body"), page.get("footer")]))


def find(page_id: str) -> dict:
    return next(p for p in app_setup._build_plan()["pages"] if p["id"] == page_id)


@pytest.fixture(autouse=True)
def _deterministic_plan(monkeypatch):
    """Pin the optional Apple Music page ON for every test in this file.

    The page appears only when the signed MusicKit helper is present, and that
    helper is a BUILD ARTEFACT -- swift/amcp-musickit/AMCPMusicKit.app is not in
    git. So the plan had four pages on a machine where someone had run the Swift
    build and three everywhere else, and these tests quietly asserted whichever
    the developer happened to have. They passed locally and failed on CI and in
    any fresh clone.

    Tests should not depend on whether a build script has been run, so the
    optional page is forced present here and its absence is covered explicitly
    by test_plan_is_coherent_without_the_musickit_helper.
    """
    monkeypatch.setattr(app_setup.musickit, "is_available", lambda: True)
    monkeypatch.setattr(app_setup.musickit, "authorization_status", lambda: "notDetermined")


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


def test_the_running_order_is_server_then_clients_then_permissions():
    """Set up the thing, connect it, then grant it what it needs. All steps
    finish before the user goes near a client, so nothing depends on the
    permission having been granted first."""
    ids = [p["id"] for p in app_setup._build_plan()["pages"]]
    assert ids.index("helper") < ids.index("clients") < ids.index("permission")
    assert ids[0] == "splash" and ids[-1] == "summary"


def test_the_copy_stays_out_of_jargon():
    """Apple's guidance is plain language. Someone who does not already know
    what a LaunchAgent is must not have to look it up to consent to one."""
    jargon = [
        "LaunchAgent", "Apple Event", "daemon", "TCC", "plist", "shim",
        "object specifier", "stdio", "socket", "argv", "bundle id",
    ]
    for page in app_setup._build_plan()["pages"]:
        body = prose(page) + " " + page["title"]
        for word in jargon:
            assert word.lower() not in body.lower(), f"{page['id']}: '{word}'"


def test_pages_are_short_enough_to_read():
    """A wall of text is not consent; nobody reads it."""
    for page in app_setup._build_plan()["pages"]:
        assert len(prose(page)) < 480, f"{page['id']} is {len(prose(page))} characters"
        for half in ("body", "footer"):
            text = page.get(half) or ""
            paragraphs = [p for p in text.split("\n\n") if p.strip()]
            assert len(paragraphs) <= 3, f"{page['id']} {half}: {len(paragraphs)} paragraphs"


def test_the_splash_is_titled_for_what_the_app_does():
    assert app_setup._build_plan()["pages"][0]["title"] == "Control Apple Music with AI"


def test_the_permission_page_states_the_limit_and_how_to_undo_it():
    """Apple's convention: say what it is for before the system dialog, and
    name where the user can change their mind."""
    body = prose(find("permission"))
    assert "only permission" in body
    assert "can't see your files" in body
    assert "System Settings > Privacy & Security > Automation" in body


def test_the_splash_previews_every_step_and_does_nothing_itself():
    splash = app_setup._build_plan()["pages"][0]
    assert splash.get("action") is None, "the splash must not do anything"
    assert "Nothing changes until you say so" in prose(splash)
    # One bullet per page that follows, summary excluded.
    steps = [p for p in app_setup._build_plan()["pages"][1:] if p["id"] != "summary"]
    assert len(splash["bullets"]) == len(steps)


def test_the_clients_page_promises_the_backup_it_makes():
    body = prose(find("clients"))
    assert "backup" in body
    assert "keeps the settings it already has" in body


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


# --- the Apple Music page --------------------------------------------------------


def test_no_apple_music_page_without_the_helper(monkeypatch):
    """A build with no signed helper cannot offer the permission at all."""
    monkeypatch.setattr(app_setup.musickit, "is_available", lambda: False)
    assert app_setup._musickit_page() is None
    assert "musickit" not in [p["id"] for p in app_setup._build_plan()["pages"]]


def test_the_apple_music_page_can_be_declined_on_its_own(monkeypatch):
    """It is the one optional step, and 'Not Now' is what says so -- Cancel
    abandons the whole wizard, which is a different thing."""
    monkeypatch.setattr(app_setup.musickit, "is_available", lambda: True)
    monkeypatch.setattr(app_setup.musickit, "authorization_status", lambda: "notDetermined")
    page = app_setup._musickit_page()
    # "Continue" rather than "Allow": the system dialog owns that word, and
    # echoing it on our own button pre-empts a choice that is Apple's to offer.
    assert page["action"] == "Continue"
    assert page["skip"] == "Not Now"


def test_an_already_granted_page_offers_no_action(monkeypatch):
    """Re-prompting for something already granted would just be noise."""
    monkeypatch.setattr(app_setup.musickit, "is_available", lambda: True)
    monkeypatch.setattr(app_setup.musickit, "authorization_status", lambda: "authorized")
    page = app_setup._musickit_page()
    assert page.get("action") is None and page.get("skip") is None
    assert "already allowed" in prose(page)


def test_the_page_distinguishes_itself_from_the_automation_grant(monkeypatch):
    """The two permissions sound alike; conflating them is the likely mistake."""
    monkeypatch.setattr(app_setup.musickit, "is_available", lambda: True)
    monkeypatch.setattr(app_setup.musickit, "authorization_status", lambda: "notDetermined")
    body = prose(app_setup._musickit_page())
    # Name the permission the way macOS will, so the system dialog that
    # follows is recognisably the same thing.
    assert "Apple Music" in body
    assert "can't buy anything" in body
    assert "optional" in body.lower()


def test_only_the_apple_music_step_is_skippable(monkeypatch):
    monkeypatch.setattr(app_setup.musickit, "is_available", lambda: True)
    monkeypatch.setattr(app_setup.musickit, "authorization_status", lambda: "notDetermined")
    for page in app_setup._build_plan()["pages"]:
        if page.get("skip"):
            assert page["id"] == "musickit", f"{page['id']} must not be skippable"


def test_declining_apple_music_is_reported_softly(monkeypatch):
    """Declining is an answer, not a failure to be fixed, so it must not be
    rendered as an error."""
    monkeypatch.setattr(
        app_setup.musickit, "request_authorization", lambda: (False, "user did not grant access")
    )
    ok, lines = app_setup._run_step("musickit", [])
    assert ok is False
    assert lines[0].startswith("•"), lines
    assert not lines[0].startswith("✗")


def test_granting_apple_music_is_reported(monkeypatch):
    monkeypatch.setattr(app_setup.musickit, "request_authorization", lambda: (True, "authorized"))
    ok, lines = app_setup._run_step("musickit", [])
    assert ok and lines == ["✓ Apple Music access granted"]


def test_the_plan_is_built_without_re_probing_the_helper(monkeypatch):
    """Reading the status shells out to the helper; doing it per page render
    would spawn processes for nothing."""
    calls = []
    monkeypatch.setattr(app_setup.musickit, "is_available", lambda: True)
    monkeypatch.setattr(
        app_setup.musickit,
        "authorization_status",
        lambda: (calls.append(1), "notDetermined")[1],
    )
    app_setup._build_plan()
    assert len(calls) == 1, f"probed {len(calls)} times"


# --- example prompts -------------------------------------------------------------


def test_every_permission_page_shows_what_it_unlocks():
    """The clearest explanation of a permission is an example of what it lets
    you do. Abstract wording is what makes people click Deny."""
    for page_id in ("permission", "musickit"):
        page = find(page_id)
        assert page.get("examples"), f"{page_id} has no example prompts"


def test_the_splash_leads_with_examples():
    splash = find("splash")
    assert len(splash.get("examples") or []) >= 2


def test_examples_are_things_a_person_would_actually_say():
    """Short, spoken, no jargon -- they are dialogue, not documentation."""
    for page in app_setup._build_plan()["pages"]:
        for example in page.get("examples") or []:
            assert len(example) <= 60, f"too long to read at a glance: {example!r}"
            assert not example.endswith("."), f"not a sentence to read aloud: {example!r}"
            assert "MCP" not in example and "server" not in example.lower()


# --- naming ----------------------------------------------------------------------


def test_the_product_is_never_presented_as_apples():
    """It appears in a client's server list beside names the user does trust,
    and asks for a permission dialog with its name in it. Anywhere the product
    is named to a user, that name says Unofficial."""
    plan = app_setup._build_plan()
    haystack = [plan["title"]] + [
        f"{p.get('title','')} {prose(p)}" for p in plan["pages"]
    ]
    for text in haystack:
        for match in re.finditer(r"Apple Music MCP", text):
            start = max(0, match.start() - 12)
            assert "Unofficial" in text[start:match.start()], f"bare name in: {text[:80]!r}"


def test_the_server_identifies_itself_as_unofficial():
    from applemusic_mcp import server

    assert server.SERVER_NAME == "Unofficial Apple Music MCP"
    assert server.mcp.name == server.SERVER_NAME


def test_the_server_offers_its_icon_to_clients():
    """Clients that show an icon beside a server should show ours."""
    from applemusic_mcp import icon, server

    icons = getattr(server.mcp._mcp_server, "icons", None)
    assert icons, "no icon advertised"
    assert icons[0].mimeType == "image/png"
    assert icons[0].src.startswith("data:image/png;base64,")
    assert icon.DATA_URI == icons[0].src


def test_the_embedded_icon_is_a_real_png():
    import base64

    from applemusic_mcp import icon

    raw = base64.b64decode(icon.PNG_BASE64)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert len(raw) < 200_000, "too large to send on every handshake"


# --- links -----------------------------------------------------------------------


def test_no_link_is_shown_when_none_is_configured(monkeypatch):
    """A placeholder URL is worse than no link."""
    monkeypatch.setattr(app_setup, "YOUTUBE_URL", "")
    assert find("summary")["links"] == []


def test_a_configured_link_appears_on_the_last_page(monkeypatch):
    monkeypatch.setattr(app_setup, "YOUTUBE_URL", "https://www.youtube.com/@someone")
    links = find("summary")["links"]
    assert len(links) == 1
    assert links[0]["url"] == "https://www.youtube.com/@someone"


@pytest.mark.parametrize(
    "hostile",
    ["http://insecure.example", "file:///etc/passwd", "javascript:alert(1)", "not a url"],
)
def test_only_https_links_are_offered(monkeypatch, hostile):
    """The window opens these in a browser, so the scheme is checked on both
    sides -- here, and again in the window itself."""
    monkeypatch.setattr(app_setup, "YOUTUBE_URL", hostile)
    assert find("summary")["links"] == []


def test_plan_is_coherent_without_the_musickit_helper(monkeypatch):
    """A build with no MusicKit helper drops that page -- and must stay whole.

    This is the state of every fresh clone and of CI, so it is the state most
    likely to be shipped untested.
    """
    monkeypatch.setattr(app_setup.musickit, "is_available", lambda: False)
    plan = app_setup._build_plan()
    ids = [p["id"] for p in plan["pages"]]
    assert "musickit" not in ids
    assert ids[0] == "splash" and ids[-1] == "summary"
    # The splash previews the steps, so it must not promise one that is absent.
    steps = [p for p in plan["pages"][1:] if p["id"] != "summary"]
    assert len(plan["pages"][0]["bullets"]) == len(steps), (
        "the splash lists a step the plan does not contain"
    )
