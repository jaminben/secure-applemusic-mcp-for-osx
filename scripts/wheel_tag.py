"""Ship the signed MusicKit helper in the wheel, and tag the wheel accordingly.

Two jobs, both conditional on the helper actually having been built:

1. **Force-include the helper.** It lives beside the module, which is where
   ``musickit._candidates()`` looks. It is NOT declared statically in
   pyproject.toml, because hatchling fails the whole build when a statically
   declared forced include is missing — and the ``.app`` is a build artifact
   that is gitignored. That broke every CI run and every fresh clone: `uv sync`
   could not build the package at all. Adding it here means a checkout without
   the helper still installs; it just has no MusicKit rail, which every call
   site already handles through ``musickit.is_available()``.

2. **Tag the wheel for macOS** when the helper is in it. Hatchling would
   otherwise infer ``py3-none-any``, and pip would happily install a wheel
   containing a Developer-ID-signed Mach-O onto Linux. With no helper the wheel
   really is pure Python, so it keeps the portable tag.

The helper is a universal binary (arm64 + x86_64), so the tag covers both Mac
architectures rather than whichever machine built it.
"""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

HELPER_SRC = Path("swift/amcp-musickit/AMCPMusicKit.app")
HELPER_DEST = "applemusic_mcp/AMCPMusicKit.app"
# macOS 14 is the floor the Swift helper targets (swift/amcp-musickit/build.sh).
MACOS_TAG = "macosx_14_0_universal2"


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        helper = Path(self.root) / HELPER_SRC
        if not helper.is_dir():
            # No helper: a portable, pure-Python wheel. Correct for CI and for
            # a source checkout that has not run the Swift build. Never publish
            # one of these — tools/release-assets.sh checks before uploading.
            return
        build_data.setdefault("force_include", {})[str(helper)] = HELPER_DEST
        build_data["pure_python"] = False
        build_data["infer_tag"] = False
        build_data["tag"] = f"py3-none-{MACOS_TAG}"
