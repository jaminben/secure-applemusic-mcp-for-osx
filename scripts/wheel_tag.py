"""Tag the wheel as macOS-only, because it carries a signed native helper.

Hatchling infers `py3-none-any` from a package that is pure Python, and this
one very nearly is — one 148K signed .app is the exception. That tag would let
pip install it on Linux and Windows, where the helper cannot run and the
MusicKit rail silently does not exist. A platform tag makes pip decline instead,
which is the honest failure.

The helper is a universal binary (arm64 + x86_64), so the tag covers both Mac
architectures rather than pinning to whichever machine built it.
"""

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# macOS 14 is the floor the Swift helper targets (see swift/amcp-musickit/build.sh).
MACOS_TAG = "macosx_14_0_universal2"


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        build_data["pure_python"] = False
        build_data["infer_tag"] = False
        build_data["tag"] = f"py3-none-{MACOS_TAG}"
