"""Coverage tests for generic/shared helpers in server.py.

Covers the line ranges assigned to this slice:
  118-963, 1182-1330, 1462-2410
"""

import csv
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
import responses

from applemusic_mcp import server

# ---------------------------------------------------------------------------
# Token helper
# ---------------------------------------------------------------------------


def _write_tokens(config_dir: Path, dev_token: str, user_token: str) -> None:
    (config_dir / "developer_token.json").write_text(
        json.dumps({"token": dev_token, "expires": time.time() + 86400 * 60})
    )
    (config_dir / "music_user_token.json").write_text(json.dumps({"music_user_token": user_token}))


# ---------------------------------------------------------------------------
# _deduplicate_by_id
# ---------------------------------------------------------------------------


class TestDeduplicateById:
    def test_removes_duplicates(self):
        items = [{"id": "a"}, {"id": "b"}, {"id": "a"}, {"id": "c"}]
        result = server._deduplicate_by_id(items)
        assert [i["id"] for i in result] == ["a", "b", "c"]

    def test_empty_list(self):
        assert server._deduplicate_by_id([]) == []

    def test_no_id_field_dropped_by_default(self):
        items = [{"name": "x"}, {"id": "a"}, {"name": "y"}]
        result = server._deduplicate_by_id(items)
        assert len(result) == 1
        assert result[0]["id"] == "a"

    def test_keep_no_id_true(self):
        items = [{"name": "x"}, {"id": "a"}, {"name": "y"}]
        result = server._deduplicate_by_id(items, keep_no_id=True)
        # 3 unique entries: both no-id items + the id item
        assert len(result) == 3

    def test_custom_id_key(self):
        items = [{"pk": "1"}, {"pk": "2"}, {"pk": "1"}]
        result = server._deduplicate_by_id(items, id_key="pk")
        assert len(result) == 2


# ---------------------------------------------------------------------------
# _format_fuzzy_match
# ---------------------------------------------------------------------------


class TestFormatFuzzyMatch:
    def test_none_returns_empty(self):
        assert server._format_fuzzy_match(None) == ""

    def test_exact_returns_empty(self):
        fm = server.FuzzyMatchResult(
            matched_name="Song",
            query="Song",
            normalized_query="song",
            normalized_match="song",
            transformations=[],
            match_type="exact",
        )
        assert server._format_fuzzy_match(fm) == ""

    def test_partial_match(self):
        fm = server.FuzzyMatchResult(
            matched_name="Rock Playlist Extended",
            query="Rock Playlist",
            normalized_query="rock playlist",
            normalized_match="rock playlist extended",
            transformations=["partial substring match"],
            match_type="partial",
        )
        result = server._format_fuzzy_match(fm)
        assert "Rock Playlist" in result
        assert "Partial" in result

    def test_fuzzy_match_with_transformations(self):
        fm = server.FuzzyMatchResult(
            matched_name="The Beatles",
            query="Beatles",
            normalized_query="beatles",
            normalized_match="beatles",
            transformations=["removed article 'the'"],
            match_type="fuzzy",
        )
        result = server._format_fuzzy_match(fm)
        assert "Fuzzy" in result
        assert "removed article" in result

    def test_fuzzy_match_no_transformations(self):
        fm = server.FuzzyMatchResult(
            matched_name="Cafe",
            query="Café",
            normalized_query="cafe",
            normalized_match="cafe",
            transformations=[],
            match_type="fuzzy",
        )
        result = server._format_fuzzy_match(fm)
        assert "Fuzzy" in result

    def test_fuzzy_partial_match(self):
        fm = server.FuzzyMatchResult(
            matched_name="The Beatles Collection",
            query="Beatles",
            normalized_query="beatles",
            normalized_match="beatles collection",
            transformations=["removed article 'the'", "partial normalized match"],
            match_type="fuzzy_partial",
        )
        result = server._format_fuzzy_match(fm)
        assert "Fuzzy partial" in result
        assert "removed article" in result


# ---------------------------------------------------------------------------
# _normalize_for_match
# ---------------------------------------------------------------------------


class TestNormalizeForMatch:
    def test_none_input(self):
        assert server._normalize_for_match(None) == ""

    def test_empty_string(self):
        assert server._normalize_for_match("") == ""

    def test_lowercase_and_strip(self):
        assert server._normalize_for_match("  HELLO  ") == "hello"

    def test_diacritics_removed(self):
        assert server._normalize_for_match("café") == "cafe"

    def test_ampersand_to_and(self):
        assert server._normalize_for_match("Rock & Roll") == "rock and roll"

    def test_hyphens_become_spaces(self):
        assert server._normalize_for_match("Peek-A-Boo") == "peek a boo"

    def test_punctuation_stripped(self):
        assert server._normalize_for_match("don't") == "dont"

    def test_multiple_spaces_collapsed(self):
        assert server._normalize_for_match("a   b") == "a b"


# ---------------------------------------------------------------------------
# _loose_contains / _loose_equals
# ---------------------------------------------------------------------------


class TestLooseContains:
    def test_empty_needle_always_true(self):
        assert server._loose_contains("", "anything") is True
        assert server._loose_contains(None, "anything") is True

    def test_match(self):
        assert server._loose_contains("rock", "Rock & Roll") is True

    def test_no_match(self):
        assert server._loose_contains("jazz", "Rock & Roll") is False

    def test_diacritics_folded(self):
        assert server._loose_contains("cafe", "Café Music") is True


class TestLooseEquals:
    def test_equal(self):
        assert server._loose_equals("Café", "cafe") is True

    def test_not_equal(self):
        assert server._loose_equals("Rock", "Jazz") is False

    def test_none_vs_none(self):
        assert server._loose_equals(None, None) is True

    def test_none_vs_string(self):
        assert server._loose_equals(None, "rock") is False


# ---------------------------------------------------------------------------
# _normalize_with_tracking
# ---------------------------------------------------------------------------


class TestNormalizeWithTracking:
    def test_basic(self):
        variations, transforms = server._normalize_with_tracking("Hello World")
        assert "hello world" in variations

    def test_diacritics(self):
        variations, transforms = server._normalize_with_tracking("Café")
        assert any("cafe" in v for v in variations)
        assert any("diacritics" in t for t in transforms)

    def test_article_stripped(self):
        variations, transforms = server._normalize_with_tracking("The Beatles")
        assert any("beatles" in v for v in variations)
        assert any("removed article" in t for t in transforms)

    def test_a_article(self):
        variations, transforms = server._normalize_with_tracking("A Night to Remember")
        assert any("night to remember" in v for v in variations)

    def test_an_article(self):
        variations, transforms = server._normalize_with_tracking("An Evening With")
        assert any("evening with" in v for v in variations)

    def test_ampersand_variations(self):
        variations, transforms = server._normalize_with_tracking("Rock & Roll")
        assert len(variations) == 2  # & and "and" variants
        assert any("and" in t for t in transforms)

    def test_and_variations(self):
        variations, transforms = server._normalize_with_tracking("Rock and Roll")
        assert len(variations) == 2

    def test_feat_normalized(self):
        variations, transforms = server._normalize_with_tracking("Song (feat. Artist)")
        assert any("ft" in t for t in transforms)

    def test_featuring_normalized(self):
        variations, transforms = server._normalize_with_tracking("Song featuring Artist")
        assert any("ft" in t for t in transforms)

    def test_apostrophes_removed(self):
        variations, transforms = server._normalize_with_tracking("Don't Stop")
        assert any("dont" in v for v in variations)

    def test_hyphens_to_spaces(self):
        variations, transforms = server._normalize_with_tracking("Some-Track")
        assert any("hyphens" in t for t in transforms)
        assert any("some track" in v for v in variations)

    def test_special_chars_removed(self):
        variations, transforms = server._normalize_with_tracking("Song 🎵 Name")
        assert any("special" in t for t in transforms)

    def test_whitespace_normalized(self):
        # "hello   world" doesn't start with an article so whitespace normalize fires
        variations, transforms = server._normalize_with_tracking("hello   world")
        assert any("whitespace" in t for t in transforms)


# ---------------------------------------------------------------------------
# _fuzzy_match_entity
# ---------------------------------------------------------------------------


class TestFuzzyMatchEntity:
    def _make_candidates(self, names):
        return [{"id": str(i), "name": n} for i, n in enumerate(names)]

    def _extractor(self, c):
        return c["name"]

    def test_empty_candidates(self):
        result, fm = server._fuzzy_match_entity("query", [], self._extractor)
        assert result is None
        assert fm is None

    def test_exact_match(self):
        candidates = self._make_candidates(["Rock", "Jazz", "Pop"])
        result, fm = server._fuzzy_match_entity("Jazz", candidates, self._extractor)
        assert result["name"] == "Jazz"
        assert fm is None  # exact = no fuzzy info

    def test_partial_match(self):
        candidates = self._make_candidates(["Rock Classics", "Pop Hits"])
        result, fm = server._fuzzy_match_entity("Rock", candidates, self._extractor)
        assert result["name"] == "Rock Classics"
        assert fm is not None
        assert fm.match_type == "partial"

    def test_fuzzy_match(self):
        candidates = self._make_candidates(["The Beatles"])
        result, fm = server._fuzzy_match_entity("Beatles", candidates, self._extractor)
        assert result is not None  # fuzzy matches "the beatles" → "beatles"

    def test_no_match(self):
        candidates = self._make_candidates(["Jazz", "Pop"])
        result, fm = server._fuzzy_match_entity("Country", candidates, self._extractor)
        assert result is None
        assert fm is None


# ---------------------------------------------------------------------------
# get_timestamp
# ---------------------------------------------------------------------------


class TestGetTimestamp:
    def test_format(self):
        ts = server.get_timestamp()
        # Should be YYYYMMDD_HHMMSS
        assert len(ts) == 15
        assert ts[8] == "_"
        assert ts[:8].isdigit()
        assert ts[9:].isdigit()


# ---------------------------------------------------------------------------
# write_tracks_csv
# ---------------------------------------------------------------------------


class TestWriteTracksCsv:
    def test_basic_write(self, tmp_path):
        tracks = [
            {
                "name": "Song A",
                "duration": "3:45",
                "artist": "Artist A",
                "album": "Album A",
                "year": "2020",
                "genre": "Rock",
                "explicit": "No",
                "id": "abc123",
            }
        ]
        csv_path = tmp_path / "test.csv"
        server.write_tracks_csv(tracks, csv_path)
        assert csv_path.exists()
        content = csv_path.read_text()
        assert "Song A" in content
        assert "Artist A" in content

    def test_with_extras(self, tmp_path):
        tracks = [
            {
                "name": "Song",
                "duration": "4:00",
                "artist": "Artist",
                "album": "Album",
                "year": "2021",
                "genre": "Pop",
                "explicit": "No",
                "id": "xyz",
                "track_number": 1,
                "disc_number": 1,
                "has_lyrics": True,
                "catalog_id": "cat123",
                "composer": "Me",
                "isrc": "US12345",
                "is_explicit": False,
                "preview_url": "",
                "artwork_url": "",
            }
        ]
        csv_path = tmp_path / "extras.csv"
        server.write_tracks_csv(tracks, csv_path, include_extras=True)
        content = csv_path.read_text()
        assert "track_number" in content
        assert "artwork_url" in content


# ---------------------------------------------------------------------------
# _format_full / _format_clipped / _format_compact / _format_minimal
# ---------------------------------------------------------------------------


class TestFormatFunctions:
    def _track(self, **kwargs):
        base = {
            "name": "Song Name",
            "artist": "Artist",
            "duration": "3:45",
            "album": "Album",
            "year": "2024",
            "genre": "Rock",
            "explicit": "No",
            "id": "123",
        }
        base.update(kwargs)
        return base

    def test_format_full_with_year_genre(self):
        t = self._track()
        out = server._format_full(t)
        assert "Song Name - Artist (3:45) Album [2024] Rock 123" == out

    def test_format_full_explicit(self):
        t = self._track(explicit="Yes")
        out = server._format_full(t)
        assert "[Explicit]" in out

    def test_format_full_no_year_no_genre(self):
        t = self._track(year="", genre="")
        out = server._format_full(t)
        assert "[" not in out
        assert "Song Name - Artist (3:45) Album 123" == out

    def test_format_clipped_truncates(self):
        t = self._track(name="A" * 50, artist="B" * 30, album="C" * 40)
        out = server._format_clipped(t)
        assert "..." in out
        assert "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in out

    def test_format_clipped_explicit(self):
        t = self._track(explicit="Yes", year="2020", genre="Pop")
        out = server._format_clipped(t)
        assert "[Explicit]" in out
        assert "[2020]" in out

    def test_format_compact(self):
        t = self._track(name="A" * 50, artist="B" * 30)
        out = server._format_compact(t)
        assert "(3:45)" in out
        assert "Album" not in out

    def test_format_minimal(self):
        t = self._track()
        out = server._format_minimal(t)
        assert "(3:45)" not in out
        assert "Song Name" in out
        assert "Artist" in out
        assert "123" in out


# ---------------------------------------------------------------------------
# format_output
# ---------------------------------------------------------------------------


class TestFormatOutput:
    def _sample_tracks(self, n=2):
        return [
            {
                "name": f"Track {i}",
                "duration": "3:00",
                "artist": "Artist",
                "album": "Album",
                "year": "2024",
                "genre": "Pop",
                "explicit": "No",
                "id": f"id{i}",
            }
            for i in range(n)
        ]

    def test_empty_items_text(self):
        assert server.format_output([], format="text") == "No results"

    def test_empty_items_json(self):
        assert server.format_output([], format="json") == "[]"

    def test_json_format(self):
        tracks = self._sample_tracks(1)
        out = server.format_output(tracks, format="json")
        data = json.loads(out)
        assert len(data) == 1
        assert "name" in data[0]

    def test_json_full(self):
        tracks = self._sample_tracks(1)
        tracks[0]["extra_field"] = "extra_value"
        out = server.format_output(tracks, format="json", full=True)
        data = json.loads(out)
        assert "extra_field" in data[0]

    def test_json_filters_extra_keys(self):
        tracks = self._sample_tracks(1)
        tracks[0]["extra_field"] = "should_be_filtered"
        out = server.format_output(tracks, format="json", full=False)
        data = json.loads(out)
        assert "extra_field" not in data[0]

    def test_csv_format(self):
        tracks = self._sample_tracks(1)
        out = server.format_output(tracks, format="csv")
        assert "name" in out
        assert "Track 0" in out

    def test_csv_format_full(self):
        tracks = self._sample_tracks(1)
        out = server.format_output(tracks, format="csv", full=True)
        assert "artwork_url" in out

    def test_csv_non_track_data(self):
        items = [{"foo": "bar", "baz": "qux"}]
        out = server.format_output(items, format="csv")
        assert "foo" in out or "bar" in out

    def test_text_format_with_tracks(self):
        tracks = self._sample_tracks(2)
        out = server.format_output(tracks, format="text")
        assert "=== 2 tracks ===" in out
        assert "Track 0" in out

    def test_text_format_with_pagination(self):
        tracks = self._sample_tracks(2)
        out = server.format_output(tracks, format="text", total_count=100, offset=10)
        assert "11-12 of 100 tracks" in out

    def test_text_format_non_track_data_with_artist(self):
        items = [{"name": "Album A", "artist": "Artist A", "id": "alb1"}]
        out = server.format_output(items, format="text")
        assert "Album A" in out
        assert "Artist A" in out

    def test_text_format_non_track_data_name_only(self):
        items = [{"name": "Genre Rock", "id": "g1"}]
        out = server.format_output(items, format="text")
        assert "Genre Rock" in out

    def test_format_none_returns_count(self):
        tracks = self._sample_tracks(3)
        out = server.format_output(tracks, format="none")
        assert "3 items" in out

    def test_export_csv(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "get_cache_dir", lambda: tmp_path)
        tracks = self._sample_tracks(1)
        out = server.format_output(tracks, format="text", export="csv")
        assert "Exported" in out
        assert "exports://" in out

    def test_export_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "get_cache_dir", lambda: tmp_path)
        tracks = self._sample_tracks(1)
        out = server.format_output(tracks, format="text", export="json")
        assert "Exported" in out

    def test_export_json_full(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "get_cache_dir", lambda: tmp_path)
        tracks = self._sample_tracks(1)
        tracks[0]["extra"] = "val"
        out = server.format_output(tracks, format="none", export="json", full=True)
        assert "Exported" in out

    def test_export_csv_non_track_data(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "get_cache_dir", lambda: tmp_path)
        items = [{"name": "Playlist", "id": "p.1"}]
        out = server.format_output(items, format="none", export="csv")
        assert "Exported" in out

    def test_export_csv_full(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "get_cache_dir", lambda: tmp_path)
        tracks = self._sample_tracks(1)
        out = server.format_output(tracks, format="none", export="csv", full=True)
        assert "Exported" in out


# ---------------------------------------------------------------------------
# get_storefront
# ---------------------------------------------------------------------------


class TestGetStorefront:
    def test_defaults_to_us(self, monkeypatch):
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"storefront": "us"})
        assert server.get_storefront() == "us"

    def test_custom_storefront(self, monkeypatch):
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"storefront": "gb"})
        assert server.get_storefront() == "gb"

    def test_missing_storefront_key(self, monkeypatch):
        monkeypatch.setattr(server, "get_user_preferences", lambda: {})
        assert server.get_storefront() == "us"


# ---------------------------------------------------------------------------
# list_exports / read_export
# ---------------------------------------------------------------------------


class TestListExports:
    def _cache(self, tmp_path):
        d = tmp_path / "cache"
        d.mkdir()
        return d

    def test_no_exports(self, tmp_path, monkeypatch):
        # server.py imports get_cache_dir by name — must patch at server module level
        cache = self._cache(tmp_path)
        monkeypatch.setattr(server, "get_cache_dir", lambda: cache)
        out = server.list_exports()
        assert "No exports" in out

    def test_with_files(self, tmp_path, monkeypatch):
        cache = self._cache(tmp_path)
        monkeypatch.setattr(server, "get_cache_dir", lambda: cache)
        (cache / "export_20240101_120000.csv").write_text("a,b")
        out = server.list_exports()
        assert "export_20240101_120000.csv" in out


class TestReadExport:
    def _cache(self, tmp_path):
        d = tmp_path / "cache"
        d.mkdir()
        return d

    def test_not_found(self, tmp_path, monkeypatch):
        cache = self._cache(tmp_path)
        monkeypatch.setattr(server, "get_cache_dir", lambda: cache)
        out = server.read_export("nonexistent.csv")
        assert "not found" in out.lower()

    def test_reads_file(self, tmp_path, monkeypatch):
        cache = self._cache(tmp_path)
        monkeypatch.setattr(server, "get_cache_dir", lambda: cache)
        (cache / "myfile.csv").write_text("hello,world")
        out = server.read_export("myfile.csv")
        assert "hello" in out


# ---------------------------------------------------------------------------
# get_token_expiration_warning — remaining branches
# ---------------------------------------------------------------------------


class TestGetTokenExpirationWarningExtra:
    def test_expired_token(self, mock_config_dir):
        token_file = mock_config_dir / "developer_token.json"
        token_data = {"token": "tok", "expires": time.time() - 1}  # already expired
        token_file.write_text(json.dumps(token_data))
        result = server.get_token_expiration_warning()
        assert result is not None
        assert "EXPIRED" in result

    def test_urgent_warning_7_days(self, mock_config_dir):
        token_file = mock_config_dir / "developer_token.json"
        token_data = {"token": "tok", "expires": time.time() + 86400 * 5}  # 5 days
        token_file.write_text(json.dumps(token_data))
        result = server.get_token_expiration_warning()
        assert result is not None
        assert "day(s)" in result


# ---------------------------------------------------------------------------
# _has_developer_token / _has_user_token / _can_use_library_api
# ---------------------------------------------------------------------------


class TestTokenChecks:
    def test_has_developer_token_false_when_missing(self, mock_config_dir):
        assert server._has_developer_token() is False

    def test_has_developer_token_true_when_present(self, mock_config_dir, mock_developer_token):
        (mock_config_dir / "developer_token.json").write_text(
            json.dumps({"token": mock_developer_token, "expires": time.time() + 86400 * 60})
        )
        assert server._has_developer_token() is True

    def test_has_developer_token_force_tokenless(self, monkeypatch):
        monkeypatch.setenv("APPLEMUSIC_FORCE_TOKENLESS", "1")
        assert server._has_developer_token() is False

    def test_has_user_token_false_when_missing(self, mock_config_dir):
        assert server._has_user_token() is False

    def test_has_user_token_true_when_present(self, mock_config_dir, mock_user_token):
        (mock_config_dir / "music_user_token.json").write_text(
            json.dumps({"music_user_token": mock_user_token})
        )
        assert server._has_user_token() is True

    def test_can_use_library_api_false_tokenless(self, monkeypatch):
        monkeypatch.setenv("APPLEMUSIC_FORCE_TOKENLESS", "1")
        assert server._can_use_library_api() is False

    def test_can_use_library_api_false_missing_user_token(
        self, mock_config_dir, mock_developer_token
    ):
        (mock_config_dir / "developer_token.json").write_text(
            json.dumps({"token": mock_developer_token, "expires": time.time() + 86400 * 60})
        )
        assert server._can_use_library_api() is False


# ---------------------------------------------------------------------------
# _engine
# ---------------------------------------------------------------------------


class TestEngine:
    def test_force_api_mode_env(self, monkeypatch):
        monkeypatch.setenv("APPLEMUSIC_FORCE_API_MODE", "1")
        assert server._engine() == "api"

    def test_mode_native_with_applescript(self, monkeypatch):
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"mode": "native"})
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        assert server._engine() == "native"

    def test_mode_auto_with_applescript(self, monkeypatch):
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"mode": "auto"})
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        assert server._engine() == "native"


# ---------------------------------------------------------------------------
# _use_browser_playback
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _write_rail — sanctioned-first write routing, independent of playback mode
# ---------------------------------------------------------------------------


class TestWriteRail:
    def test_macos_uses_native_regardless_of_mode(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.delenv("APPLEMUSIC_FORCE_TOKENLESS", raising=False)
        # Even pinned to web *playback*, writes stay native on macOS.
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"mode": "web"})
        for op in ("create", "delete", "rename", "move", "remove"):
            assert server._write_rail(op) == "native"

    def test_label_write_appends_only_on_success(self):
        assert (
            server._label_write("Created 'X'", "sanctioned") == "Created 'X' (via Apple Music API)"
        )
        assert server._label_write("Error: nope", "web") == "Error: nope"
        assert server._label_write("Deleted", "native") == "Deleted (via Music.app)"


# ---------------------------------------------------------------------------
# _format_applescript_error
# ---------------------------------------------------------------------------


class TestFormatApplescriptError:
    def test_music_not_running(self):
        raw = "application Music isn't running"
        out = server._format_applescript_error(raw, "list playlists")
        assert "Music.app" in out
        assert "isn't running" in out.lower() or "running" in out.lower()
        assert "list playlists" in out

    def test_automation_denied(self):
        raw = "-1743 Not authorized to send Apple events"
        out = server._format_applescript_error(raw)
        assert "Automation permission" in out or "permission" in out.lower()

    def test_timeout(self):
        raw = "AppleScript timed out after 30 seconds"
        out = server._format_applescript_error(raw, "add track")
        assert "timed out" in out.lower()
        assert "add track" in out

    def test_syntax_error(self):
        raw = "syntax error - Expected end of line but found identifier"
        out = server._format_applescript_error(raw)
        assert "syntax error" in out.lower()

    def test_unknown_error(self):
        raw = "some unknown applescript error"
        out = server._format_applescript_error(raw)
        assert "some unknown applescript error" in out

    def test_no_operation(self):
        raw = "application Music isn't running"
        out = server._format_applescript_error(raw)
        # No "()" parens without an operation
        assert "( )" not in out


# ---------------------------------------------------------------------------
# _apply_pagination
# ---------------------------------------------------------------------------


class TestApplyPagination:
    def test_no_limit_no_offset(self):
        items = list(range(10))
        result, total, err = server._apply_pagination(items)
        assert result == items
        assert total == 10
        assert err is None

    def test_with_limit(self):
        items = list(range(10))
        result, total, err = server._apply_pagination(items, limit=3)
        assert result == [0, 1, 2]
        assert total == 10
        assert err is None

    def test_with_offset(self):
        items = list(range(10))
        result, total, err = server._apply_pagination(items, offset=5)
        assert result == [5, 6, 7, 8, 9]
        assert total == 10

    def test_offset_equals_total(self):
        items = list(range(5))
        result, total, err = server._apply_pagination(items, offset=5)
        assert result == []
        assert err is None  # offset==total is OK (next empty page)

    def test_offset_exceeds_total(self):
        items = list(range(5))
        result, total, err = server._apply_pagination(items, offset=10)
        assert result == []
        assert err is not None
        assert "Offset" in err

    def test_limit_and_offset(self):
        items = list(range(20))
        result, total, err = server._apply_pagination(items, limit=5, offset=3)
        assert result == [3, 4, 5, 6, 7]
        assert total == 20

    def test_empty_list(self):
        result, total, err = server._apply_pagination([])
        assert result == []
        assert total == 0
        assert err is None


# ---------------------------------------------------------------------------
# _detect_id_type
# ---------------------------------------------------------------------------


class TestDetectIdType:
    def test_library_id(self):
        assert server._detect_id_type("i.ABC123XYZ") == "library"

    def test_playlist_id(self):
        assert server._detect_id_type("p.ABC123") == "playlist"

    def test_catalog_id(self):
        assert server._detect_id_type("1234567890") == "catalog"

    def test_persistent_id(self):
        assert server._detect_id_type("ABC123DEF456") == "persistent"

    def test_unknown(self):
        assert server._detect_id_type("short") == "unknown"

    def test_digits_too_short(self):
        assert server._detect_id_type("1234567") == "unknown"  # 7 digits, below MIN

    def test_eight_digits_is_a_catalog_id(self):
        # Store-era catalog ids really are 8 digits (89285956 is "Hedwig's
        # Theme"), so the threshold is 8, not the 10 modern ids happen to use.
        assert server._detect_id_type("89285956") == "catalog"
        assert server.MIN_CATALOG_ID_LENGTH == 8

    def test_pure_digits_12_is_catalog_not_persistent(self):
        # 12 pure digits: catalog wins (isdigit() and len >= 9) before persistent check
        assert server._detect_id_type("123456789012") == "catalog"

    def test_non_hex_chars_is_unknown(self):
        # 12+ chars but contains non-hex like G, H — unknown
        assert server._detect_id_type("AbCdGhIjKlMn") == "unknown"


# ---------------------------------------------------------------------------
# _detect_input_type
# ---------------------------------------------------------------------------


class TestDetectInputType:
    def test_library_id(self):
        assert server._detect_input_type("i.LibID") == server.InputType.LIBRARY_ID

    def test_playlist_id(self):
        assert server._detect_input_type("p.PlaylistID123") == server.InputType.PLAYLIST_ID

    def test_album_id(self):
        assert server._detect_input_type("l.AlbumID123") == server.InputType.ALBUM_ID

    def test_catalog_id(self):
        assert server._detect_input_type("1234567890") == server.InputType.CATALOG_ID

    def test_catalog_id_short(self):
        assert server._detect_input_type("1234567") == server.InputType.NAME  # below MIN

    def test_catalog_id_eight_digits(self):
        assert server._detect_input_type("89285956") == server.InputType.CATALOG_ID

    def test_persistent_id(self):
        assert server._detect_input_type("ABC123DEF456") == server.InputType.PERSISTENT_ID

    def test_name(self):
        assert server._detect_input_type("Bohemian Rhapsody") == server.InputType.NAME

    def test_empty(self):
        assert server._detect_input_type("") == server.InputType.NAME

    def test_p_with_spaces_is_name(self):
        # "p.s. I love you" has non-alnum after p. — treated as name
        assert server._detect_input_type("p.s. I love you") == server.InputType.NAME


# ---------------------------------------------------------------------------
# _resolve_input / _resolve_track / _resolve_album
# ---------------------------------------------------------------------------


class TestResolveInput:
    def test_empty_input(self):
        results = server._resolve_input("", server.EntityType.TRACK)
        assert len(results) == 1
        assert results[0].error == "Empty input"

    def test_single_name(self):
        results = server._resolve_input("Hey Jude", server.EntityType.TRACK)
        assert len(results) == 1
        assert results[0].input_type == server.InputType.NAME
        assert results[0].value == "Hey Jude"

    def test_single_catalog_id(self):
        results = server._resolve_input("1234567890", server.EntityType.TRACK)
        assert len(results) == 1
        assert results[0].input_type == server.InputType.CATALOG_ID

    def test_csv_split(self):
        results = server._resolve_input("Track A, Track B, Track C", server.EntityType.TRACK)
        assert len(results) == 3
        assert results[0].value == "Track A"

    def test_csv_with_artist_not_split(self):
        # When artist provided, comma in name is part of title
        results = server._resolve_input(
            "Take Me Home, Country Roads", server.EntityType.TRACK, "John Denver"
        )
        assert len(results) == 1

    def test_newline_split(self):
        results = server._resolve_input("Track A\nTrack B", server.EntityType.TRACK)
        assert len(results) == 2

    def test_json_array_of_objects(self):
        val = json.dumps([{"name": "Hey Jude", "artist": "Beatles"}, {"name": "Let It Be"}])
        results = server._resolve_input(val, server.EntityType.TRACK)
        assert len(results) == 2
        assert results[0].value == "Hey Jude"
        assert results[0].artist == "Beatles"
        assert results[1].value == "Let It Be"

    def test_json_array_of_strings(self):
        val = json.dumps(["1234567890", "Hey Jude"])
        results = server._resolve_input(val, server.EntityType.TRACK)
        assert len(results) == 2
        assert results[0].input_type == server.InputType.CATALOG_ID

    def test_json_array_invalid_json(self):
        results = server._resolve_input("[not valid json", server.EntityType.TRACK)
        assert len(results) == 1
        assert results[0].error is not None
        assert "JSON" in results[0].error

    def test_json_not_array(self):
        results = server._resolve_input('{"key": "val"}', server.EntityType.TRACK)
        # Starts with "{" not "[", so not detected as JSON — treated as name
        assert len(results) == 1

    def test_json_array_empty(self):
        # UNREACHABLE: server.py:1542 — `isinstance(items, list)` guard after
        # json.loads on a "[...]" string: json.loads("[...]") always returns a list,
        # so the `not isinstance(items, list)` branch can never be True.
        results = server._resolve_input("[]", server.EntityType.TRACK)
        assert len(results) == 1
        assert results[0].error == "Empty JSON array"

    def test_json_object_missing_name(self):
        val = json.dumps([{"artist": "Beatles"}])
        results = server._resolve_input(val, server.EntityType.TRACK)
        assert results[0].error is not None
        assert "missing" in results[0].error.lower()

    def test_json_invalid_item_type(self):
        val = json.dumps([42])
        results = server._resolve_input(val, server.EntityType.TRACK)
        assert results[0].error is not None

    def test_newline_list_with_only_blanks_returns_empty_input(self):
        # "\n\n".strip() == "" → hits the empty-input guard before the newline branch
        # The "Empty newline list" branch is dead: any stripped non-empty value with
        # a \n will have at least one non-blank line (UNREACHABLE: server.py:1627)
        results = server._resolve_input("\n\n", server.EntityType.TRACK)
        assert len(results) == 1
        assert results[0].error == "Empty input"

    def test_empty_csv_list(self):
        results = server._resolve_input(",,,", server.EntityType.TRACK)
        # All parts are empty after strip — returns error
        assert len(results) == 1
        assert results[0].error == "Empty CSV"


class TestResolveTrackAlbumWrappers:
    def test_resolve_track(self):
        results = server._resolve_track("1234567890")
        assert results[0].input_type == server.InputType.CATALOG_ID

    def test_resolve_album(self):
        results = server._resolve_album("l.AlbumID123")
        assert results[0].input_type == server.InputType.ALBUM_ID


# ---------------------------------------------------------------------------
# _build_track_results
# ---------------------------------------------------------------------------


class TestBuildTrackResults:
    def test_empty_both(self):
        out = server._build_track_results([], [])
        assert "No tracks were processed" in out

    def test_only_results(self):
        out = server._build_track_results(["Song A added", "Song B added"], [])
        assert "2 track(s)" in out
        assert "Song A added" in out

    def test_only_errors(self):
        out = server._build_track_results([], ["Song C not found"])
        assert "1 track(s)" in out
        assert "Song C not found" in out

    def test_both(self):
        out = server._build_track_results(["Song A"], ["Song B failed"])
        assert "Processed" in out or "processed" in out
        assert "Song A" in out
        assert "Song B failed" in out

    def test_custom_verbs(self):
        out = server._build_track_results(["Song A"], [], success_verb="added")
        assert "Added" in out


# ---------------------------------------------------------------------------
# _search_catalog_albums / _find_matching_catalog_album
# ---------------------------------------------------------------------------


class TestSearchCatalogAlbums:
    @responses.activate
    def test_returns_albums(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "albums": {
                        "data": [
                            {
                                "id": "album123",
                                "attributes": {
                                    "name": "Abbey Road",
                                    "artistName": "The Beatles",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        result = server._search_catalog_albums("Abbey Road")
        assert len(result) == 1
        assert result[0]["id"] == "album123"

    @responses.activate
    def test_returns_empty_on_error(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            status=401,
        )
        result = server._search_catalog_albums("anything")
        assert result == []


class TestFindMatchingCatalogAlbum:
    @responses.activate
    def test_finds_exact_match(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "albums": {
                        "data": [
                            {
                                "id": "alb1",
                                "attributes": {
                                    "name": "Abbey Road",
                                    "artistName": "The Beatles",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        album, err, fm = server._find_matching_catalog_album("Abbey Road", "The Beatles")
        assert album is not None
        assert album["id"] == "alb1"
        assert err is None

    @responses.activate
    def test_not_found(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={"results": {"albums": {"data": []}}},
            status=200,
        )
        album, err, fm = server._find_matching_catalog_album("Nonexistent Album")
        assert album is None
        assert err == "Not found in catalog"

    @responses.activate
    def test_relaxes_artist_filter_on_miss(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "albums": {
                        "data": [
                            {
                                "id": "alb1",
                                "attributes": {
                                    "name": "Abbey Road",
                                    "artistName": "Different Artist",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        # Artist filter misses, should try all
        album, err, fm = server._find_matching_catalog_album("Abbey Road", "Wrong Artist")
        # Could still match via relaxed search
        # Just ensure it doesn't crash — result depends on matching


# ---------------------------------------------------------------------------
# _search_library_songs
# ---------------------------------------------------------------------------


class TestSearchLibrarySongs:
    @responses.activate
    def test_returns_songs(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/search",
            json={
                "results": {
                    "library-songs": {
                        "data": [
                            {
                                "id": "i.lib1",
                                "attributes": {
                                    "name": "Let It Be",
                                    "artistName": "The Beatles",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        result = server._search_library_songs("Let It Be")
        assert len(result) == 1
        assert result[0]["id"] == "i.lib1"

    @responses.activate
    def test_returns_empty_on_error(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/search",
            status=500,
        )
        result = server._search_library_songs("anything")
        assert result == []


# ---------------------------------------------------------------------------
# _find_matching_catalog_song
# ---------------------------------------------------------------------------


class TestFindMatchingCatalogSong:
    @responses.activate
    def test_finds_song(self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        mock_cache = MagicMock()
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "songs": {
                        "data": [
                            {
                                "id": "song1",
                                "attributes": {
                                    "name": "Hey Jude",
                                    "artistName": "The Beatles",
                                    "albumName": "Hey Jude",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        song, err, fm = server._find_matching_catalog_song("Hey Jude", "The Beatles")
        assert song is not None
        assert song["id"] == "song1"
        assert err is None

    @responses.activate
    def test_not_found(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={"results": {"songs": {"data": []}}},
            status=200,
        )
        song, err, fm = server._find_matching_catalog_song("Nonexistent Song")
        assert song is None
        assert err == "Not found in catalog"

    @responses.activate
    def test_relaxes_artist_filter(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        mock_cache = MagicMock()
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "songs": {
                        "data": [
                            {
                                "id": "s1",
                                "attributes": {
                                    "name": "Hey Jude",
                                    "artistName": "Different Artist",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        # Artist filter misses — should try all songs
        song, err, fm = server._find_matching_catalog_song("Hey Jude", "Wrong Artist")
        # Just confirm no crash


# ---------------------------------------------------------------------------
# _cache_song_metadata
# ---------------------------------------------------------------------------


class TestCacheSongMetadata:
    def test_caches_explicit_song(self, monkeypatch):
        mock_cache = MagicMock()
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        song = {
            "id": "cat123",
            "attributes": {
                "name": "Song",
                "artistName": "Artist",
                "albumName": "Album",
                "contentRating": "explicit",
                "isrc": "US12345",
            },
        }
        server._cache_song_metadata(song)
        mock_cache.set_track_metadata.assert_called_once()
        call_kwargs = mock_cache.set_track_metadata.call_args[1]
        assert call_kwargs["explicit"] == "Yes"
        assert call_kwargs["catalog_id"] == "cat123"

    def test_skips_empty_id(self, monkeypatch):
        mock_cache = MagicMock()
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        song = {"id": "", "attributes": {"name": "Song"}}
        server._cache_song_metadata(song)
        mock_cache.set_track_metadata.assert_not_called()


# ---------------------------------------------------------------------------
# _find_track_id
# ---------------------------------------------------------------------------


class TestFindTrackId:
    @responses.activate
    def test_finds_library_track(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        mock_cache = MagicMock()
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/search",
            json={
                "results": {
                    "library-songs": {
                        "data": [
                            {
                                "id": "i.lib1",
                                "attributes": {
                                    "name": "Hey Jude",
                                    "artistName": "The Beatles",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        lib_id, cat_id, display = server._find_track_id("Hey Jude", "The Beatles")
        assert lib_id == "i.lib1"
        assert cat_id is None
        assert "Hey Jude" in display

    @responses.activate
    def test_falls_back_to_catalog(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        mock_cache = MagicMock()
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        # Library search empty
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/search",
            json={"results": {"library-songs": {"data": []}}},
            status=200,
        )
        # Catalog search finds it
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "songs": {
                        "data": [
                            {
                                "id": "1234567890",
                                "attributes": {
                                    "name": "Hey Jude",
                                    "artistName": "The Beatles",
                                    "albumName": "Hey Jude",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        lib_id, cat_id, display = server._find_track_id("Hey Jude", "The Beatles")
        assert lib_id is None
        assert cat_id == "1234567890"

    @responses.activate
    def test_not_found(self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        mock_cache = MagicMock()
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/search",
            json={"results": {"library-songs": {"data": []}}},
            status=200,
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={"results": {"songs": {"data": []}}},
            status=200,
        )
        lib_id, cat_id, display = server._find_track_id("Nonexistent Song")
        assert lib_id is None
        assert cat_id is None
        assert display == ""


# ---------------------------------------------------------------------------
# _add_to_library_api / _add_album_to_library
# ---------------------------------------------------------------------------


class TestAddToLibraryApi:
    def test_empty_ids(self):
        ok, msg = server._add_to_library_api([])
        assert ok is False
        assert "No catalog IDs" in msg

    @responses.activate
    def test_success_200(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library",
            status=200,
        )
        ok, msg = server._add_to_library_api(["song123"])
        assert ok is True
        assert "song" in msg

    @responses.activate
    def test_success_204(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library",
            status=204,
        )
        ok, msg = server._add_to_library_api(["song123"])
        assert ok is True

    @responses.activate
    def test_unauthorized_401(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library",
            status=401,
        )
        ok, msg = server._add_to_library_api(["song123"])
        assert ok is False
        assert "401" in msg

    @responses.activate
    def test_forbidden_403(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library",
            status=403,
        )
        ok, msg = server._add_to_library_api(["song123"])
        assert ok is False
        assert "403" in msg

    @responses.activate
    def test_other_error(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library",
            status=500,
        )
        ok, msg = server._add_to_library_api(["song123"])
        assert ok is False
        assert "500" in msg

    @responses.activate
    def test_albums_type(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library",
            status=202,
        )
        ok, msg = server._add_to_library_api(["album123"], "albums")
        assert ok is True
        assert "album" in msg

    @responses.activate
    def test_add_album_to_library(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library",
            status=202,
        )
        ok, msg = server._add_album_to_library("album123")
        assert ok is True


# ---------------------------------------------------------------------------
# _verify_track_not_in_playlist / _verify_track_in_playlist
# ---------------------------------------------------------------------------


class TestVerifyTrackNotInPlaylist:
    def test_no_applescript_returns_false(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        assert server._verify_track_not_in_playlist("Playlist", "Song", "Artist") is False

    def test_empty_track_name_returns_false(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        assert server._verify_track_not_in_playlist("Playlist", "", "Artist") is False

    def test_track_absent_returns_true(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc, "track_exists_in_playlist", lambda pl, tr, ar: (True, False)
        )
        with patch("time.sleep"):
            result = server._verify_track_not_in_playlist("Playlist", "Song", "Artist")
        assert result is True

    def test_track_present_returns_false(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, tr, ar: (True, True))
        with patch("time.sleep"):
            result = server._verify_track_not_in_playlist("Playlist", "Song", "Artist")
        assert result is False


class TestVerifyTrackInPlaylist:
    def test_no_applescript_returns_false(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        with patch("time.sleep"):
            assert server._verify_track_in_playlist("Playlist", "Song", "Artist") is False

    def test_empty_track_name_returns_false(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        with patch("time.sleep"):
            assert server._verify_track_in_playlist("Playlist", "", "Artist") is False

    def test_track_present_returns_true(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, tr, ar: (True, True))
        with patch("time.sleep"):
            result = server._verify_track_in_playlist("Playlist", "Song", "Artist")
        assert result is True

    def test_track_absent_returns_false(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc, "track_exists_in_playlist", lambda pl, tr, ar: (True, False)
        )
        with patch("time.sleep"):
            result = server._verify_track_in_playlist("Playlist", "Song", "Artist")
        assert result is False


# ---------------------------------------------------------------------------
# _smart_as_add_track_to_playlist
# ---------------------------------------------------------------------------


class TestSmartAsAddTrackToPlaylist:
    def test_success_first_try(self, monkeypatch):
        monkeypatch.setattr(
            server.asc, "add_track_to_playlist", lambda pl, tr, ar, al: (True, "ok")
        )
        ok, result, pair = server._smart_as_add_track_to_playlist(
            "Playlist", "Song", "Artist", None
        )
        assert ok is True
        assert pair is None

    def test_failure_with_artist(self, monkeypatch):
        monkeypatch.setattr(
            server.asc, "add_track_to_playlist", lambda pl, tr, ar, al: (False, "Track not found")
        )
        ok, result, pair = server._smart_as_add_track_to_playlist(
            "Playlist", "Song", "Artist", None
        )
        assert ok is False
        assert pair is None

    def test_failure_with_album(self, monkeypatch):
        monkeypatch.setattr(
            server.asc, "add_track_to_playlist", lambda pl, tr, ar, al: (False, "Track not found")
        )
        ok, result, pair = server._smart_as_add_track_to_playlist(
            "Playlist", "Song - Artist", None, "Album"
        )
        assert ok is False

    def test_failure_no_dash_in_name(self, monkeypatch):
        monkeypatch.setattr(
            server.asc, "add_track_to_playlist", lambda pl, tr, ar, al: (False, "Track not found")
        )
        ok, result, pair = server._smart_as_add_track_to_playlist(
            "Playlist", "NoSeparator", None, None
        )
        assert ok is False

    def test_failure_other_error(self, monkeypatch):
        monkeypatch.setattr(
            server.asc, "add_track_to_playlist", lambda pl, tr, ar, al: (False, "Automation denied")
        )
        ok, result, pair = server._smart_as_add_track_to_playlist(
            "Playlist", "Song - Artist", None, None
        )
        assert ok is False

    def test_split_success_with_verify(self, monkeypatch):
        call_count = [0]

        def mock_add(pl, tr, ar, al):
            call_count[0] += 1
            if call_count[0] == 1:
                return False, "Track not found"  # first attempt fails
            return True, "Added"  # split attempt succeeds

        monkeypatch.setattr(server.asc, "add_track_to_playlist", mock_add)
        monkeypatch.setattr(server, "_verify_track_in_playlist", lambda pl, tr, ar: True)
        with patch("time.sleep"):
            ok, result, pair = server._smart_as_add_track_to_playlist(
                "Playlist", "Song - Artist", None, None
            )
        assert ok is True
        assert pair is not None
        assert pair == ("Song", "Artist")

    def test_split_verify_failure(self, monkeypatch):
        call_count = [0]

        def mock_add(pl, tr, ar, al):
            call_count[0] += 1
            if call_count[0] == 1:
                return False, "Track not found"
            return True, "Added"

        monkeypatch.setattr(server.asc, "add_track_to_playlist", mock_add)
        monkeypatch.setattr(server, "_verify_track_in_playlist", lambda pl, tr, ar: False)
        with patch("time.sleep"):
            ok, result, pair = server._smart_as_add_track_to_playlist(
                "Playlist", "Song - Artist", None, None
            )
        assert ok is False
        assert "could not verify" in result


# ---------------------------------------------------------------------------
# _auto_search_and_add_to_playlist
# ---------------------------------------------------------------------------


class TestAutoSearchAndAddToPlaylist:

    @responses.activate
    def test_catalog_search_not_found(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={"results": {"songs": {"data": []}}},
            status=200,
        )
        ok, msg, steps = server._auto_search_and_add_to_playlist("Nonexistent", "", "Playlist")
        assert ok is False
        assert "not found" in msg.lower()

    @responses.activate
    def test_catalog_search_api_error(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            status=500,
        )
        ok, msg, steps = server._auto_search_and_add_to_playlist("Song", "", "Playlist")
        assert ok is False

    @responses.activate
    def test_add_to_library_fails(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "songs": {
                        "data": [
                            {"id": "c1", "attributes": {"name": "Song", "artistName": "Artist"}}
                        ]
                    }
                }
            },
            status=200,
        )
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library",
            status=401,
        )
        ok, msg, steps = server._auto_search_and_add_to_playlist("Song", "Artist", "Playlist")
        assert ok is False


# ---------------------------------------------------------------------------
# _unified_auto_search_to_playlist
# ---------------------------------------------------------------------------


class TestUnifiedAutoSearchToPlaylist:
    def test_no_library_api_available(self, monkeypatch):
        """No token AND no MusicKit helper: the only honest answer. Note the
        explicit is_available stub — without it this test read the DEVELOPER's
        machine, so it passed or failed depending on whether a helper happened
        to be built in the checkout. Routing tests live in test_musickit.py."""
        monkeypatch.setattr(server, "_can_use_library_api", lambda: False)
        monkeypatch.setattr(server.musickit, "is_available", lambda: False)
        ok, msg, steps = server._unified_auto_search_to_playlist("Song", "Artist", "Playlist")
        assert ok is False
        assert "notarized download" in msg

    @responses.activate
    def test_splits_combined_name(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        # No artist provided, "Song - Artist" in track_name → gets split
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "songs": {
                        "data": [
                            {"id": "c1", "attributes": {"name": "Song", "artistName": "Artist"}}
                        ]
                    }
                }
            },
            status=200,
        )
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library",
            status=202,
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.rock", "attributes": {"name": "Rock Hits", "canEdit": True}}]},
            status=200,
        )
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library/playlists/p.rock/tracks",
            status=204,
        )
        monkeypatch.setattr(server, "_verify_track_in_playlist", lambda pl, tr, ar: True)
        with patch("time.sleep"):
            ok, msg, steps = server._unified_auto_search_to_playlist(
                "Song - Artist", "", "Rock Hits"
            )
        assert any("Split" in s for s in steps)


# ---------------------------------------------------------------------------
# _rate_song_api
# ---------------------------------------------------------------------------


class TestRateSongApi:
    def test_invalid_rating(self):
        ok, msg = server._rate_song_api("song123", "neutral")
        assert ok is False
        assert "must be" in msg

    @responses.activate
    def test_love_success(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.PUT,
            "https://api.music.apple.com/v1/me/ratings/songs/song123",
            status=200,
        )
        ok, msg = server._rate_song_api("song123", "love")
        assert ok is True
        assert "love" in msg

    @responses.activate
    def test_dislike_success(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.PUT,
            "https://api.music.apple.com/v1/me/ratings/songs/song123",
            status=204,
        )
        ok, msg = server._rate_song_api("song123", "dislike")
        assert ok is True

    @responses.activate
    def test_unauthorized(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.PUT,
            "https://api.music.apple.com/v1/me/ratings/songs/song123",
            status=401,
        )
        ok, msg = server._rate_song_api("song123", "love")
        assert ok is False
        assert "401" in msg

    @responses.activate
    def test_other_error(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.PUT,
            "https://api.music.apple.com/v1/me/ratings/songs/song123",
            status=500,
        )
        ok, msg = server._rate_song_api("song123", "love")
        assert ok is False
        assert "500" in msg


# ---------------------------------------------------------------------------
# _browser_play
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Additional tests to cover remaining missing lines in assigned ranges
# ---------------------------------------------------------------------------


class TestReadExportPathTraversal:
    """server.py:840 — is_relative_to guard.

    UNREACHABLE: server.py:840 — `file_path = cache_dir / filename` always starts
    with cache_dir so `is_relative_to` uses string prefix comparison and always
    returns True even for `..` traversal (Python does not resolve `..` in
    is_relative_to). The guard can only fire if file_path is somehow built
    outside cache_dir, which the current code never does.
    """

    def test_path_in_cache_reads_normally(self, tmp_path, monkeypatch):
        """Control test — valid filename inside cache dir returns content."""
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "data.csv").write_text("name,id")
        monkeypatch.setattr(server, "get_cache_dir", lambda: cache)
        out = server.read_export("data.csv")
        assert "name" in out


class TestGetTokenExpirationWarningAutoRenew:
    """server.py:855 — can_generate_developer_token() returns True → silent (auto-renews)."""

    def test_no_warning_when_signing_key_available(self, configured_config_dir):
        # configured_config_dir has config.json + .p8 key → can_generate_developer_token=True
        result = server.get_token_expiration_warning()
        # Even with a short-lived token, if the signing key is present it auto-renews
        assert result is None


class TestFindMatchingCatalogSongNoArtist:
    """server.py:1733 — artist_matches inner fn returns True when no artist."""

    @responses.activate
    def test_no_artist_uses_all_songs(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        mock_cache = MagicMock()
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "songs": {
                        "data": [
                            {
                                "id": "s1",
                                "attributes": {
                                    "name": "Hey Jude",
                                    "artistName": "The Beatles",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        # No artist arg → artist_matches returns True (line 1733) for all songs
        song, err, fm = server._find_matching_catalog_song("Hey Jude")
        assert song is not None
        assert err is None

    @responses.activate
    def test_artist_given_no_match_relaxed_also_fails(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """server.py:1760 — return None after relaxed search also finds nothing."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        mock_cache = MagicMock()
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "songs": {
                        "data": [
                            {
                                "id": "s1",
                                "attributes": {
                                    "name": "Totally Different Song",
                                    "artistName": "Wrong Artist",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        # "Nowhere" doesn't match "Totally Different Song" even in relaxed mode
        song, err, fm = server._find_matching_catalog_song("Nowhere", "Some Artist")
        assert song is None
        assert err == "Not found in catalog"


class TestFindMatchingCatalogAlbumNoArtist:
    """server.py:1863 — artist_matches returns True when no artist; 1888 — relaxed fail."""

    @responses.activate
    def test_no_artist_uses_all_albums(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "albums": {
                        "data": [
                            {
                                "id": "alb1",
                                "attributes": {
                                    "name": "Abbey Road",
                                    "artistName": "The Beatles",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        # No artist → artist_matches returns True (line 1863)
        album, err, fm = server._find_matching_catalog_album("Abbey Road")
        assert album is not None

    @responses.activate
    def test_artist_given_relaxed_also_fails(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """server.py:1888 — return None after both filtered and relaxed searches fail."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "albums": {
                        "data": [
                            {
                                "id": "a1",
                                "attributes": {
                                    "name": "Totally Different Album",
                                    "artistName": "Nobody",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        album, err, fm = server._find_matching_catalog_album("Nonexistent", "Some Artist")
        assert album is None
        assert err == "Not found in catalog"


class TestSearchExceptionPaths:
    """The `except` branches hit when get_headers raises.

    Note what these no longer assert: "no token" used to mean these returned
    [], which every caller reads as "not in the catalog" — a false negative on
    a machine that can search the catalog for free. The tokenless rail is now
    taken BEFORE get_headers is reached, so the exception paths are only
    reachable with a token configured.
    """

    def test_catalog_song_search_uses_the_public_rail_without_a_token(
        self, mock_config_dir, monkeypatch
    ):
        monkeypatch.setattr(
            server,
            "_catalog_search_itunes",
            lambda q, n: [{"id": "1440783617", "name": "Strobe", "artist": "deadmau5"}],
        )
        rows = server._search_catalog_songs("strobe")
        # Shaped like Apple's so the matching above it is unchanged.
        assert rows == [
            {
                "id": "1440783617",
                "type": "songs",
                "attributes": {
                    "name": "Strobe",
                    "artistName": "deadmau5",
                    "albumName": "",
                    "url": "",
                },
            }
        ]

    def test_catalog_song_search_exception_still_empty_with_a_token(
        self, mock_config_dir, monkeypatch
    ):
        monkeypatch.setattr(server, "_has_developer_token", lambda: True)
        monkeypatch.setattr(server, "get_headers", lambda: (_ for _ in ()).throw(OSError("boom")))
        assert server._search_catalog_songs("test") == []

    def test_catalog_album_search_uses_the_public_rail_without_a_token(
        self, mock_config_dir, monkeypatch
    ):
        monkeypatch.setattr(
            server,
            "_itunes_find_album",
            lambda q, a="": {"id": "222", "name": "Abbey Road", "artist": "The Beatles"},
        )
        rows = server._search_catalog_albums("abbey road")
        assert rows == [
            {
                "id": "222",
                "type": "albums",
                "attributes": {"name": "Abbey Road", "artistName": "The Beatles"},
            }
        ]

    def test_catalog_album_search_unknown_album_is_empty(self, mock_config_dir, monkeypatch):
        monkeypatch.setattr(server, "_itunes_find_album", lambda q, a="": {})
        assert server._search_catalog_albums("nope") == []

    def test_search_library_songs_exception(self, mock_config_dir):
        # No tokens → get_headers() raises → except Exception caught at 1913-1914
        result = server._search_library_songs("test")
        assert result == []


class TestFindTrackIdContinue:
    """server.py:1945, 1948, 1963, 1966 — continue statements when name/artist don't match."""

    @responses.activate
    def test_skips_non_matching_library_song_by_name(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Library song present but name doesn't contain query → continue at 1945."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        mock_cache = MagicMock()
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        # Library has a song, but its name doesn't match the query
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/search",
            json={
                "results": {
                    "library-songs": {
                        "data": [
                            {
                                "id": "i.notmatch",
                                "attributes": {
                                    "name": "Jazz Standards",
                                    "artistName": "Miles Davis",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        # Catalog also empty
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={"results": {"songs": {"data": []}}},
            status=200,
        )
        lib_id, cat_id, display = server._find_track_id("Hey Jude")
        assert lib_id is None and cat_id is None

    @responses.activate
    def test_skips_library_song_artist_mismatch(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Library song has matching name but wrong artist → continue at 1948."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        mock_cache = MagicMock()
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/search",
            json={
                "results": {
                    "library-songs": {
                        "data": [
                            {
                                "id": "i.wrong",
                                "attributes": {
                                    "name": "Hey Jude",
                                    "artistName": "Rolling Stones",  # wrong artist
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={"results": {"songs": {"data": []}}},
            status=200,
        )
        # Query with specific artist that won't match Rolling Stones
        lib_id, cat_id, display = server._find_track_id("Hey Jude", "The Beatles")
        assert lib_id is None and cat_id is None

    @responses.activate
    def test_skips_catalog_song_name_mismatch(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Catalog song has non-matching name → continue at 1963."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        mock_cache = MagicMock()
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        # Library empty
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/search",
            json={"results": {"library-songs": {"data": []}}},
            status=200,
        )
        # Catalog has song that doesn't match
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "songs": {
                        "data": [
                            {
                                "id": "c1",
                                "attributes": {
                                    "name": "Different Song",
                                    "artistName": "Artist",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        lib_id, cat_id, display = server._find_track_id("Hey Jude")
        assert lib_id is None and cat_id is None

    @responses.activate
    def test_skips_catalog_song_artist_mismatch(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Catalog song has matching name but wrong artist → continue at 1966."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        mock_cache = MagicMock()
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/search",
            json={"results": {"library-songs": {"data": []}}},
            status=200,
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "songs": {
                        "data": [
                            {
                                "id": "c1",
                                "attributes": {
                                    "name": "Hey Jude",
                                    "artistName": "Rolling Stones",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        lib_id, cat_id, display = server._find_track_id("Hey Jude", "The Beatles")
        assert lib_id is None and cat_id is None


class TestAddToLibraryApiException:
    """server.py:2024-2025 — except branch when requests.post raises."""

    def test_exception_returns_error(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        with patch("requests.post", side_effect=ConnectionError("connection refused")):
            ok, msg = server._add_to_library_api(["song123"])
        assert ok is False
        assert "connection refused" in msg


class TestSmartAsAddAllCandidatesFail:
    """server.py:2116 — return False when ALL split candidates fail."""

    def test_all_split_candidates_fail(self, monkeypatch):
        # Always return failure (Track not found → triggers split, split also fails)
        monkeypatch.setattr(
            server.asc, "add_track_to_playlist", lambda pl, tr, ar, al: (False, "Track not found")
        )
        ok, result, pair = server._smart_as_add_track_to_playlist(
            "Playlist", "Song - Artist", None, None
        )
        assert ok is False
        assert pair is None
        assert result == "Track not found"


class TestUnifiedAutoSearchPropagationLag:
    """server.py:2241-2247 — API reports ok but verify returns False (propagation lag)."""

    def test_ok_but_verify_fails_returns_success_with_note(self, monkeypatch):
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        monkeypatch.setattr(
            server,
            "_auto_search_and_add_to_playlist",
            lambda tr, ar, pl, playlist_id=None: (True, "Hey Jude - The Beatles", []),
        )
        monkeypatch.setattr(server, "_verify_track_in_playlist", lambda pl, tr, ar: False)
        with patch("time.sleep"):
            ok, msg, steps = server._unified_auto_search_to_playlist(
                "Hey Jude", "Beatles", "Playlist"
            )
        assert ok is True
        assert any("propagation" in s for s in steps)


class TestAutoSearchAndAddException:
    """server.py:2370-2371 — except branch when requests.get raises during catalog search."""

    def test_exception_returns_error(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        with patch("requests.get", side_effect=ConnectionError("network error")):
            ok, msg, steps = server._auto_search_and_add_to_playlist("Song", "Artist", "Playlist")
        assert ok is False
        assert "Error" in msg


class TestRateSongApiException:
    """server.py:2405-2406 — except branch when requests.put raises."""

    def test_exception_returns_error(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        with patch("requests.put", side_effect=ConnectionError("timeout")):
            ok, msg = server._rate_song_api("song123", "love")
        assert ok is False
        assert "timeout" in msg


# ---------------------------------------------------------------------------
# get_token_expiration_warning — line 855 (can_generate_developer_token → True)
# ---------------------------------------------------------------------------


class TestTokenExpirationWarningAutoRenews:
    """server.py:855 — when signing key is present, no nudge is needed."""

    def test_signing_key_present_returns_none(
        self, configured_config_dir, mock_developer_token, mock_user_token
    ):
        # configured_config_dir has config.json + AuthKey_TEST.p8, so
        # can_generate_developer_token() returns True.  Write developer_token.json
        # so developer_token_info() returns non-None (passes the line 852 guard)
        # and execution reaches line 854 → 855 (return None: auto-renews).
        _write_tokens(configured_config_dir, mock_developer_token, mock_user_token)
        result = server.get_token_expiration_warning()
        assert result is None


# ---------------------------------------------------------------------------
# _unified_auto_search_to_playlist — line 2247 (ok=False propagation)
# ---------------------------------------------------------------------------


class TestUnifiedAutoSearchApiFailure:
    """server.py:2247 — when _auto_search_and_add_to_playlist returns ok=False."""

    def test_api_failure_propagated(self, mock_config_dir, mock_developer_token, mock_user_token):
        # Ensure both tokens exist so _can_use_library_api() returns True,
        # then mock _auto_search_and_add_to_playlist to simulate a catalog failure.
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        with (
            patch.object(server, "_can_use_library_api", return_value=True),
            patch.object(
                server,
                "_auto_search_and_add_to_playlist",
                return_value=(False, "Catalog search failed", ["searched catalog"]),
            ),
        ):
            ok, msg, steps = server._unified_auto_search_to_playlist(
                "Song Title", "Artist Name", "My Playlist"
            )
        assert ok is False
        assert "Catalog search failed" in msg


def test_attach_error_translates_10006():
    """The track-specific -10006 (stuck cloud/shared reference) gets a friendly,
    actionable message instead of the raw AppleScript error."""
    raw = (
        "1475:1514: execution error: Music got an error: Can't set user playlist "
        "id 67261 of source id 61 to shared track id 44260 of library playlist id "
        "62 of source id 61. (-10006)"
    )
    out = server._attach_error("Fire and Rain", raw)
    assert "Fire and Rain" in out and "different version" in out and "-10006" not in out
    # unrelated errors pass through unchanged
    assert server._attach_error("X", "boom") == "X: boom"


def test_gc_exports_bounds_dir(tmp_path, monkeypatch):
    """_gc_exports drops old timestamped exports and caps count, but never touches
    cache.json / the audit log / snapshots."""
    import os
    import time as _t

    old = tmp_path / "library_19990101_000000.csv"
    old.write_text("x")
    os.utime(old, (_t.time() - 10 * 86400, _t.time() - 10 * 86400))
    new = tmp_path / "library_20260628_000000.json"
    new.write_text("x")
    keep = tmp_path / "cache.json"  # no underscore-timestamp → must survive
    keep.write_text("{}")
    audit = tmp_path / "audit_log.jsonl"
    audit.write_text("")
    server._gc_exports(tmp_path)
    assert not old.exists()  # aged out
    assert new.exists() and keep.exists() and audit.exists()


def test_play_after_add_distinguishes_real_failure_from_sync_lag():
    """A non-transient play failure (Automation denied / Music not running) is
    surfaced; a transient 'not synced yet' stays the honest 'sync pending'."""
    denied = server._play_after_add("X by Y", "Not authorized to send Apple events")
    assert "permission denied" in denied.lower() and "sync pending" not in denied.lower()
    transient = server._play_after_add("X by Y", "Track not found")
    assert "sync pending" in transient.lower()


def test_id_based_add_classifies_catalog_id():
    """A numeric catalog id is routed as a CATALOG_ID (ID-based add pins the exact
    edition) rather than treated as a track name."""
    from applemusic_mcp.server import InputType, _resolve_track

    assert _resolve_track("1440857781", "")[0].input_type == InputType.CATALOG_ID
    assert _resolve_track("Hey Jude", "")[0].input_type == InputType.NAME


# ---------------------------------------------------------------------------
# Rate-limit surfacing (#42)
# ---------------------------------------------------------------------------


class TestRateLimitSurfacing:
    """Apple returns no Retry-After on the web-player path and the internal
    resolvers swallow non-200 into [], so a 429 has to be surfaced explicitly or
    it reads as "no such song"."""

    def test_api_error_without_a_response_still_renders(self):
        msg = server._api_error(requests.exceptions.ConnectionError("connection refused"))
        assert msg == "API Error: connection refused"

    @responses.activate
    def test_catalog_search_429_tells_the_user_it_is_throttled(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={"errors": [{"status": "429", "code": "42900"}]},
            status=429,
        )
        result = server.catalog(action="search", query="Money")
        assert "429" in result and "rolling" in result


# ---------------------------------------------------------------------------
# ISRC batch resolve (#42, suggestion 3)
# ---------------------------------------------------------------------------

_ISRC_URL = "https://api.music.apple.com/v1/catalog/us/songs"


def _isrc_song(isrc: str, sid: str, name: str, artist: str = "Oasis") -> dict:
    return {
        "id": sid,
        "attributes": {
            "name": name,
            "artistName": artist,
            "albumName": "Morning Glory",
            "isrc": isrc,
            "durationInMillis": 258773,
            "releaseDate": "1995-10-02",
            "genreNames": ["Alternative"],
        },
    }


class TestParseIsrcList:
    def test_comma_separated_and_normalized(self):
        valid, invalid = server._parse_isrc_list("gbaym9500001, US-ABC-12-34567")
        assert valid == ["GBAYM9500001", "USABC1234567"]
        assert invalid == []

    def test_json_array(self):
        valid, invalid = server._parse_isrc_list('["GBAYM9500001", "USABC1234567"]')
        assert valid == ["GBAYM9500001", "USABC1234567"]
        assert invalid == []

    def test_broken_json_is_reported_not_sent(self):
        valid, invalid = server._parse_isrc_list('["GBAYM9500001",')
        assert valid == []
        assert invalid == ['["GBAYM9500001",']

    def test_malformed_entries_are_rejected(self):
        """Garbage in a batch still costs a request, so it never gets sent."""
        valid, invalid = server._parse_isrc_list("GBAYM9500001, notanisrc, 12345")
        assert valid == ["GBAYM9500001"]
        assert invalid == ["notanisrc", "12345"]

    def test_duplicates_collapse(self):
        valid, _ = server._parse_isrc_list("GBAYM9500001, gbaym9500001, GB-AYM-95-00001")
        assert valid == ["GBAYM9500001"]

    def test_whitespace_separated_and_blank_entries(self):
        valid, invalid = server._parse_isrc_list("GBAYM9500001\n\n USABC1234567 ")
        assert valid == ["GBAYM9500001", "USABC1234567"]
        assert invalid == []

    def test_blank_entries_are_skipped_not_flagged(self):
        """An empty slot in an export is noise, not a malformed ISRC."""
        valid, invalid = server._parse_isrc_list('["GBAYM9500001", "", "  ", "USABC1234567"]')
        assert valid == ["GBAYM9500001", "USABC1234567"]
        assert invalid == []


class TestCatalogResolveIsrc:
    def test_requires_input(self):
        assert "isrcs required" in server.catalog(action="resolve_isrc")

    def test_all_invalid_input(self):
        result = server.catalog(action="resolve_isrc", isrcs="nope, also-nope")
        assert "no valid ISRCs" in result

    @responses.activate
    def test_resolves_in_one_request(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        responses.add(
            responses.GET,
            _ISRC_URL,
            json={"data": [_isrc_song("GBAYM9500001", "1234567890", "Wonderwall")]},
            status=200,
        )
        result = server.catalog(action="resolve_isrc", isrcs="GB-AYM-95-00001")
        assert "Resolved 1/1 ISRCs in 1 request(s)" in result
        assert "GBAYM9500001 -> 1234567890  Wonderwall - Oasis" in result
        assert "filter%5Bisrc%5D=GBAYM9500001" in responses.calls[0].request.url

    @responses.activate
    def test_batches_at_25_per_request(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """The whole point: 30 tracks cost 2 requests, not 30."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        isrcs = [f"GBAYM95{i:05d}" for i in range(30)]
        for chunk_start in (0, 25):
            chunk = isrcs[chunk_start : chunk_start + 25]
            responses.add(
                responses.GET,
                _ISRC_URL,
                json={"data": [_isrc_song(i, f"id{i}", f"Track {i}") for i in chunk]},
                status=200,
            )
        result = server.catalog(action="resolve_isrc", isrcs=",".join(isrcs), format="json")
        payload = json.loads(result)
        assert payload["requests"] == 2
        assert len(payload["resolved"]) == 30
        assert payload["unmatched"] == []

    @responses.activate
    def test_unmatched_isrcs_are_reported(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """filter[isrc] silently omits misses — they only exist as a diff."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        responses.add(
            responses.GET,
            _ISRC_URL,
            json={"data": [_isrc_song("GBAYM9500001", "1234567890", "Wonderwall")]},
            status=200,
        )
        result = server.catalog(action="resolve_isrc", isrcs="GBAYM9500001,USZZZ9900001")
        assert "Resolved 1/2" in result
        assert "Not in the us catalog (1)" in result
        assert "USZZZ9900001" in result

    @responses.activate
    def test_multiple_catalog_matches_are_flagged(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """One ISRC can map to several releases — don't hide that behind 'the first'."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        responses.add(
            responses.GET,
            _ISRC_URL,
            json={
                "data": [
                    _isrc_song("GBAYM9500001", "111", "Wonderwall"),
                    _isrc_song("GBAYM9500001", "222", "Wonderwall (Remastered)"),
                ]
            },
            status=200,
        )
        result = server.catalog(action="resolve_isrc", isrcs="GBAYM9500001")
        assert "-> 111" in result
        assert "[2 catalog matches]" in result

    @responses.activate
    def test_malformed_input_surfaces_alongside_results(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        responses.add(
            responses.GET,
            _ISRC_URL,
            json={"data": [_isrc_song("GBAYM9500001", "1234567890", "Wonderwall")]},
            status=200,
        )
        result = server.catalog(action="resolve_isrc", isrcs="GBAYM9500001, junk")
        assert "Malformed, not sent (1)" in result
        assert "junk" in result

    @responses.activate
    def test_429_in_json_format_sets_the_flag(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        responses.add(responses.GET, _ISRC_URL, json={"errors": []}, status=429)
        payload = json.loads(
            server.catalog(action="resolve_isrc", isrcs="GBAYM9500001", format="json")
        )
        assert payload["throttled"] is True
        assert payload["resolved"] == {}

    @responses.activate
    def test_http_error_is_reported(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        responses.add(responses.GET, _ISRC_URL, json={"errors": []}, status=500)
        result = server.catalog(action="resolve_isrc", isrcs="GBAYM9500001")
        assert "API Error" in result

    def test_missing_credentials_is_reported(self, mock_config_dir):
        result = server.catalog(action="resolve_isrc", isrcs="GBAYM9500001")
        assert "Error" in result or "token" in result.lower()

    @responses.activate
    def test_resolve_alias_and_query_fallback(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """`action="resolve"` works, and the ISRCs may arrive in `query`."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        responses.add(
            responses.GET,
            _ISRC_URL,
            json={"data": [_isrc_song("GBAYM9500001", "1234567890", "Wonderwall")]},
            status=200,
        )
        result = server.catalog(action="resolve", query="GBAYM9500001")
        assert "Resolved 1/1" in result

    @responses.activate
    def test_unknown_action_lists_the_canonical_name(self, mock_config_dir):
        """0.18.0 collapsed resolve_isrc + match into `resolve`; the advertised
        list follows the canonical name, not the aliases."""
        assert "resolve," in server.catalog(action="bogus")


# ---------------------------------------------------------------------------
# Dry-run track matching
# ---------------------------------------------------------------------------


def _cat_song(sid: str, name: str, artist: str, album: str = "Help!", year: str = "1965") -> dict:
    return {
        "id": sid,
        "attributes": {
            "name": name,
            "artistName": artist,
            "albumName": album,
            "releaseDate": f"{year}-01-01",
        },
    }


class TestCatalogMatchTracks:
    """The dry run exists so a bulk add's fuzzy matches get reviewed BEFORE they're
    written — the wrong-edition/wrong-artist case is silent and hard to undo."""

    def test_requires_input(self):
        assert "tracks required" in server.catalog(action="match")

    def test_exact_match_is_not_flagged(self, monkeypatch):
        monkeypatch.setattr(
            server,
            "_find_matching_catalog_song",
            lambda n, a: (_cat_song("111", "Yesterday", "The Beatles"), None, None),
        )
        result = server.catalog(action="match", tracks="Yesterday")
        assert "Matched 1/1" in result
        assert "DRY RUN, nothing was added" in result
        assert "= exact" in result
        assert "NOT an outright title match" not in result

    def test_fuzzy_match_is_flagged_for_review(self, monkeypatch):
        """The case that motivated this: a missing apostrophe lands on another act."""
        fuzzy = server.FuzzyMatchResult(
            matched_name="Don't Let Me Down (feat. Daya)",
            query="Dont Let Me Down",
            normalized_query="dont let me down",
            normalized_match="dont let me down",
            transformations=["apostrophe"],
            match_type="fuzzy_partial",
        )
        monkeypatch.setattr(
            server,
            "_find_matching_catalog_song",
            lambda n, a: (
                _cat_song("222", "Don't Let Me Down (feat. Daya)", "The Chainsmokers"),
                None,
                fuzzy,
            ),
        )
        result = server.catalog(action="match", tracks="Dont Let Me Down")
        assert "? fuzzy_partial" in result
        assert "NOT an outright title match" in result
        assert "The Chainsmokers" in result

    def test_catalog_id_costs_no_request(self, monkeypatch):
        monkeypatch.setattr(
            server,
            "_find_matching_catalog_song",
            lambda n, a: pytest.fail("a catalog id is already exact; don't spend a request"),
        )
        result = server.catalog(action="match", tracks="1441134128")
        assert "# id" in result
        assert "1441134128" in result

    def test_no_match_is_reported(self, monkeypatch):
        monkeypatch.setattr(
            server, "_find_matching_catalog_song", lambda n, a: (None, "Not found in catalog", None)
        )
        result = server.catalog(action="match", tracks="Zzzz Nonexistent")
        assert "Matched 0/1" in result
        assert "No match (1)" in result
        assert "Add them with" not in result  # nothing to add

    def test_unmatchable_id_type_is_reported(self, monkeypatch):
        result = server.catalog(action="match", tracks="i.ABC123DEF")
        assert "can't be name-matched" in result

    def test_ids_are_emitted_ready_to_paste(self, monkeypatch):
        songs = {
            "Yesterday": _cat_song("111", "Yesterday", "The Beatles"),
            "Help": _cat_song("222", "Help!", "The Beatles"),
        }
        monkeypatch.setattr(
            server, "_find_matching_catalog_song", lambda n, a: (songs[n], None, None)
        )
        result = server.catalog(action="match", tracks="Yesterday, Help")
        assert "playlist(action='add', playlist='<name>', track='111,222')" in result

    def test_caps_at_25_and_points_at_isrc(self, monkeypatch):
        """Uncapped, this action is a fresh way to recreate the #42 rate limit —
        it costs one request per track."""
        calls = []

        def one_match(n, a):
            calls.append(n)
            return _cat_song(str(len(calls)), n, "X"), None, None

        monkeypatch.setattr(server, "_find_matching_catalog_song", one_match)
        tracks = ",".join(f"Track {i}" for i in range(40))
        result = server.catalog(action="match", tracks=tracks)
        assert len(calls) == 25
        assert "15 more not attempted (cap 25)" in result
        assert "resolve_isrc" in result

    def test_limit_raises_the_cap_deliberately(self, monkeypatch):
        calls = []

        def one_match(n, a):
            calls.append(n)
            return _cat_song(str(len(calls)), n, "X"), None, None

        monkeypatch.setattr(server, "_find_matching_catalog_song", one_match)
        tracks = ",".join(f"Track {i}" for i in range(30))
        server.catalog(action="match", tracks=tracks, max_tracks=30)
        assert len(calls) == 30

    def test_max_tracks_is_independent_of_the_search_limit(self, monkeypatch):
        """`limit` means 'how many search results'; reusing it as the cap silently
        capped this at 15."""
        calls = []
        monkeypatch.setattr(
            server,
            "_find_matching_catalog_song",
            lambda n, a: (calls.append(n), (_cat_song("1", n, "X"), None, None))[1],
        )
        tracks = ",".join(f"Track {i}" for i in range(40))
        server.catalog(action="match", tracks=tracks, limit=5)
        assert len(calls) == 25

    def test_explicit_small_cap_is_clamped_not_crashing(self, monkeypatch):
        monkeypatch.setattr(
            server, "_find_matching_catalog_song", lambda n, a: (_cat_song("1", n, "X"), None, None)
        )
        result = server.catalog(action="match", tracks="A,B,C", max_tracks=1)
        assert "Matched 1/1" in result

    def test_json_format(self, monkeypatch):
        monkeypatch.setattr(
            server,
            "_find_matching_catalog_song",
            lambda n, a: (_cat_song("111", "Yesterday", "The Beatles"), None, None),
        )
        payload = json.loads(server.catalog(action="match", tracks="Yesterday", format="json"))
        assert payload["ids"] == ["111"]
        assert payload["matched"][0]["confidence"] == "exact"
        assert payload["requests"] == 1
        assert payload["throttled"] is False
        assert payload["unmatched"] == []

    def test_artist_hint_is_passed_through(self, monkeypatch):
        seen = {}

        def capture(n, a):
            seen["name"], seen["artist"] = n, a
            return _cat_song("111", n, a), None, None

        monkeypatch.setattr(server, "_find_matching_catalog_song", capture)
        server.catalog(action="match", tracks="Dont Let Me Down", artist="The Beatles")
        assert seen == {"name": "Dont Let Me Down", "artist": "The Beatles"}

    def test_per_item_artist_from_json_wins(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            server,
            "_find_matching_catalog_song",
            lambda n, a: (seen.append((n, a)), (_cat_song("1", n, a), None, None))[1],
        )
        server.catalog(action="match", tracks='[{"name":"Yesterday","artist":"The Beatles"}]')
        assert seen == [("Yesterday", "The Beatles")]

    def test_bad_json_entry_is_reported_not_matched(self, monkeypatch):
        monkeypatch.setattr(
            server,
            "_find_matching_catalog_song",
            lambda n, a: pytest.fail("a malformed entry must not cost a request"),
        )
        result = server.catalog(action="match", tracks='[{"artist":"The Beatles"}]')
        assert "No match (1)" in result
        assert "name" in result.lower()

    def test_action_aliases(self, monkeypatch):
        monkeypatch.setattr(
            server, "_find_matching_catalog_song", lambda n, a: (_cat_song("1", n, "X"), None, None)
        )
        for action in ("match", "match_tracks", "resolve_tracks"):
            assert "Matched 1/1" in server.catalog(action=action, tracks="Yesterday")

    def test_bare_resolve_dispatches_on_which_param_is_set(self, monkeypatch):
        """`resolve` is ambiguous by name, so it routes on the argument given."""
        monkeypatch.setattr(
            server, "_find_matching_catalog_song", lambda n, a: (_cat_song("1", n, "X"), None, None)
        )
        assert "Matched 1/1" in server.catalog(action="resolve", tracks="Yesterday")
        monkeypatch.setattr(server, "_catalog_resolve_isrc", lambda i, f, fu: f"ISRC PATH: {i}")
        assert "ISRC PATH" in server.catalog(action="resolve", isrcs="GBAYM9500001")

    def test_unknown_action_no_longer_advertises_match(self, mock_config_dir):
        result = server.catalog(action="bogus")
        assert "resolve," in result and "match" not in result

    @responses.activate
    def test_end_to_end_against_a_mocked_catalog(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Exercise the real matcher, not a stubbed one."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={"results": {"songs": {"data": [_cat_song("111", "Yesterday", "The Beatles")]}}},
            status=200,
        )
        result = server.catalog(action="match", tracks="Yesterday", artist="The Beatles")
        assert "= exact" in result
        assert "Yesterday - The Beatles" in result
        assert "111" in result


class TestThrottleHonesty:
    """Fixing the advice in one place and leaving it stale elsewhere would have the
    tool contradicting its own docs."""

    def test_no_stale_wait_a_moment_advice_anywhere(self):
        """Apple's window is rolling; 'wait a moment and retry' is wrong AND makes
        it worse. Guard against it creeping back in."""
        import pathlib

        src = pathlib.Path(server.__file__).parent
        offenders = [
            f"{f.name}:{n}"
            for f in src.glob("*.py")
            for n, line in enumerate(f.read_text().splitlines(), 1)
            if "wait a moment and retry" in line or "try again shortly" in line
        ]
        assert offenders == []

    def test_titles_sent_to_resolve_isrc_point_at_match(self):
        result = server.catalog(action="resolve_isrc", isrcs="Yesterday, Wonderwall")
        assert "action='match'" in result
        assert "USABC1234567" in result  # shows what an ISRC looks like
