# Default to the project environment so targets work from a plain checkout with
# no venv activated. Override for a specific interpreter:  PY="python3.12" make test
PY ?= uv run python

.PHONY: help test test-all preflight preflight-ui invariants app notarize release clean-dist dev dev-stop reset

# Needed to name the release zip the way the README tells people to expect it.
VERSION := $(shell sed -nE 's/^version = "(.*)"/\1/p' pyproject.toml | head -1)
ARCH    := $(shell uname -m)
RELEASE_ZIP := AppleMusicMCP-$(VERSION)-macos-$(ARCH).zip

help:
	@echo "make test         - fast suite (mocked logic); what GitHub CI runs"
	@echo "make coverage     - fast suite + 100% coverage gate (no real account access)"
	@echo "make test-all     - fast + live API suite (needs tokens for a signed-in account)"
	@echo "make preflight    - PRE-RELEASE GATE: fast + live env check + live API suite"
	@echo "make preflight-ui - NATIVE UI GATE: live Music.app playback paths (run on iMac AND mini,"
	@echo "                    unlocked console session) before any UI-touching release"
	@echo "make dev          - run the server from source (hot-reload dev loop)"
	@echo "make dev-stop     - stop the dev helper"
	@echo "make invariants   - capability invariants only (the fork's reason to exist)"
	@echo "make app          - build the standalone AppleMusicMCP.app"
	@echo "make notarize     - submit the built app to Apple, staple the ticket, re-zip"
	@echo "make reset        - wipe this Mac back to a first-run machine (backs up creds)"
	@echo "make release      - invariants + tests + wheel/sdist + signed, NOTARIZED .app + checksums"
	@echo ""
	@echo "  SIGN_ID=\"My Cert\" make app     # sign the bundle (recommended: TCC keys on it)"

# Fast, deterministic, no account needed. Mirrors CI (-m 'not slow and not ui').
test:
	$(PY) -m pytest -q

# Fast suite with coverage. Enforces 100% (fail_under in pyproject); external
# network is blocked in non-live tests, so this never touches a real account.
coverage:
	$(PY) -m pytest -q --cov=applemusic_mcp --cov-report=term-missing

# Everything, including the live API mutation tests. Requires a developer token
# (generate-token or harvested) + a media-user-token (`applemusic-mcp signin`)
# for an account with an active subscription.
test-all:
	TEST_API=1 $(PY) -m pytest -o addopts="" -v

# The gate to run before every release. See RELEASING.md.
preflight:
	./scripts/preflight.sh

# Native Music.app UI-automation gate. Run on the iMac AND the mini (unlocked
# console session, signed in) before any release that touches UI logic. Plays
# muted. See RELEASING.md.
preflight-ui:
	./scripts/preflight-ui.sh


# The fork's defining property: no Accessibility, no browser automation, no
# credential harvesting, no URL handoff. Fast, and worth running on its own.
invariants:
	$(PY) -m pytest -q --no-cov tests/test_capability_invariants.py tests/test_engine_resolution.py \
	    tests/test_ipc.py tests/test_app_setup.py

# Standalone app with a vendored Python. SIGN_ID is optional but recommended:
# macOS keys the Automation grant on the signing identity, so an unsigned build
# re-prompts whenever its contents change.
app:
	./tools/build-app.sh --zip $(if $(SIGN_ID),--sign "$(SIGN_ID)",)

clean-dist:
	rm -rf dist

# Submit the built app to Apple, staple the ticket into the bundle, and re-zip.
# Needs a Developer ID build (SIGN_ID=... make app) and stored notary credentials;
# tools/notarize.sh prints the one-time setup command if they are missing.
#
# Stapling REPLACES the bundle on disk, so anything checksummed before this point
# describes a file nobody will ever download.
notarize:
	./tools/notarize.sh

# Undo an install so the first-run experience can be tested again. Backs up
# ~/.config first and refuses to continue unless the backup verifies.
reset:
	./scripts/reset-install.sh

# Everything a release needs, in the order that fails cheapest first.
release: clean-dist invariants test
	$(PY) -m pytest -q --no-cov -m slow tests/test_ipc.py
	uv build
	./tools/build-app.sh --zip $(if $(SIGN_ID),--sign "$(SIGN_ID)",)
	@if [ -n "$(SIGN_ID)" ]; then \
	  ./tools/notarize.sh; \
	  mv -f dist/AppleMusicMCP.zip dist/$(RELEASE_ZIP); \
	  ( cd dist && shasum -a 256 $(RELEASE_ZIP) > $(RELEASE_ZIP).sha256 ); \
	  echo "==> notarized, stapled, and repackaged as $(RELEASE_ZIP)"; \
	else \
	  echo; \
	  echo "!! UNSIGNED BUILD - NOT NOTARIZED."; \
	  echo "   Gatekeeper will refuse this on any Mac that downloads it."; \
	  echo "   Re-run as:  SIGN_ID=\"Developer ID Application: ...\" make release"; \
	  echo; \
	fi
	cd dist && shasum -a 256 *.whl *.tar.gz *.zip > SHA256SUMS.txt
	@echo
	@echo "Artifacts in dist/ (verify SHA256SUMS.txt before publishing):"
	@ls -lh dist/ | tail -n +2
	@echo
	@echo "NOTE: publishing is gated on the upstream disclosure window — see DISCLOSURE.md"


# Development loop. The installed app vendors its own copy of the code, so
# testing a change there costs a rebuild + reinstall + launchd restart. This
# runs the same helper straight from the working tree under its own bundle
# identity and socket, and the server module is imported inside the forked
# child — so an edit is live on the NEXT call, with no restart:
#
#     make dev
#     tools/mcp-call --dev playback action=now_playing
#
# Restart only after editing helper.py / ipc.py / shim.py, which the parent holds.
dev:
	./tools/dev-helper.sh start $(if $(SIGN_ID),--sign "$(SIGN_ID)",)

dev-stop:
	./tools/dev-helper.sh stop
