# Default to the project environment so targets work from a plain checkout with
# no venv activated. Override for a specific interpreter:  PY="python3.12" make test
PY ?= uv run python

.PHONY: help test test-all preflight preflight-ui invariants swift app release-assets wheel notarize release publish-release clean-dist dev dev-stop reset

# Needed to name the release zip the way the README tells people to expect it.
VERSION := $(shell sed -nE 's/^version = "(.*)"/\1/p' pyproject.toml | head -1)
ARCH    := $(shell uname -m)
RELEASE_ZIP := UnofficialAppleMusicMCP-$(VERSION)-macos-$(ARCH).zip

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
	@echo "make swift        - build the two Swift helpers (MusicKit + setup wizard)"
	@echo "make app          - build the standalone UnofficialAppleMusicMCP.app"
	@echo "make notarize     - submit the built app to Apple, staple the ticket, re-zip"
	@echo "make reset        - wipe this Mac back to a first-run machine (backs up creds)"
	@echo "                      (publish-release is retired — release-assets does it)"
	@echo "make release-assets - BOTH arch apps + the wheel: build, notarize, staple, zip,"
	@echo "                      checksum. EXTRA=--upload attaches them to the tag."
	@echo "make wheel        - just the PyPI wheel (notarizes its bundled helper first)"
	@echo "make release      - invariants + tests + every release artifact + checksums"
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
# The two Swift helpers. They are NOT in git -- they are signed binaries, and a
# committed copy would carry the original author's identity and be useless to a
# fork -- so the .app build can only copy them if someone remembered to build
# them first. Forgetting produced a shippable app with catalog playback silently
# missing, which is why build-app.sh now refuses that for a Developer ID build
# and why this runs as a prerequisite rather than living in a README step.
swift:
	./swift/amcp-musickit/build.sh $(if $(SIGN_ID),--sign "$(SIGN_ID)",)
	./swift/amcp-setup/build.sh $(if $(SIGN_ID),--sign "$(SIGN_ID)",)

app: swift
	./tools/build-app.sh --zip $(if $(SIGN_ID),--sign "$(SIGN_ID)",)

# Both Mac architectures. There is no universal .app: it vendors a per-arch
# CPython and uv publishes no universal2 build, so a "universal" bundle would
# mean carrying two Pythons. Two arch-specific apps are smaller and honest.
# The nested Swift helpers ARE universal, so they work in either.

# EVERY release artifact, in the one order that produces working ones: build
# each architecture, notarize it, staple it, and only THEN zip — plus the wheel,
# whose bundled helper needs its own ticket. Ends by proving each zip survives
# Gatekeeper with the quarantine bit set, which is the check that catches a zip
# made before stapling. EXTRA=--upload attaches them to the tag.
#
# There is no universal .app: the bundle vendors a per-architecture CPython and
# uv publishes no universal2 build. The nested Swift helpers ARE universal.
release-assets:
	./tools/release-assets.sh $(if $(SIGN_ID),--sign "$(SIGN_ID)",) $(EXTRA)

# Just the wheel. Its bundled helper needs its OWN notarization ticket — the
# app bundle's does not travel with it — so staple before building.
wheel: swift
	./tools/notarize-helper.sh
	uv build --wheel

clean-dist:
	rm -rf dist

# Submit the built app to Apple, staple the ticket into the bundle, and re-zip.
# Needs a Developer ID build (SIGN_ID=... make app) and stored notary credentials;
# tools/notarize.sh prints the one-time setup command if they are missing.
#
# Stapling REPLACES the bundle on disk, so anything checksummed before this point
# describes a file nobody will ever download.
# Ad-hoc, for a one-off app you built with `make app`. The release path does
# NOT use this: release-assets.sh notarizes each architecture where it staged
# it, so after a release there is no dist/UnofficialAppleMusicMCP.app for this
# to find.
notarize:
	./tools/notarize.sh

# Undo an install so the first-run experience can be tested again. Backs up
# ~/.config first and refuses to continue unless the backup verifies.
reset:
	./scripts/reset-install.sh

# Attach the notarized app to its GitHub Release. Separate from `release` on
# purpose: building is local and repeatable, publishing is outward-facing and
# not undoable, so it should be a decision rather than a side effect.
#
# This cannot live in release.yml -- that runs on ubuntu-latest, which cannot
# build a .app, sign it with a Developer ID held in a Mac keychain, or
# notarize. CI makes the Release; only a Mac can fill it.
# Retired. It uploaded ONE architecture and no wheel, and expected an app at
# dist/UnofficialAppleMusicMCP.app that release-assets.sh no longer leaves
# there — so running it after a real release produced an incomplete one.
publish-release:
	@echo "publish-release is retired: it shipped one architecture and no wheel."
	@echo "Use:  make release-assets SIGN_ID=\"...\" EXTRA=--upload"
	@exit 1

# Everything a release needs, in the order that fails cheapest first.
# The full ritual: gates, then every artifact. Delegates the artifact half to
# tools/release-assets.sh, where the ordering traps are encoded — this target
# used to build ONE architecture, zip it before notarizing (so the zip tripped
# Gatekeeper on download), and never produced the stable-name asset that the
# README, the landing page and every directory listing resolve through.
release: clean-dist invariants test swift
	$(PY) -m pytest -q --no-cov -m slow tests/test_ipc.py
	./tools/release-assets.sh $(if $(SIGN_ID),--sign "$(SIGN_ID)",) $(EXTRA)
	@echo
	@echo "NOTE: publishing is gated on the upstream disclosure window — see DISCLOSURE.md"


dev:
	./tools/dev-helper.sh start $(if $(SIGN_ID),--sign "$(SIGN_ID)",)

dev-stop:
	./tools/dev-helper.sh stop
