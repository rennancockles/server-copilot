"""Zip the ChatGPT plugin in ``chatgpt-plugin/``, ready to upload as a plugin update.

The zip holds the *contents* of the folder at its root (dotfiles such as ``.app.json`` and
``.codex-plugin/`` included, ``.DS_Store`` left out), not the folder itself. The release pipeline
runs this and attaches the zip to the GitHub Release; locally, ``make plugin-zip`` does the same.

    python scripts/build_chatgpt_plugin.py [OUT_DIR] [--expect-version X.Y.Z]

``--expect-version`` fails the build when a plugin manifest carries another version, so a tag can
never ship a zip that ChatGPT would see as an old version. The build also refuses to run while
``.app.json`` still holds the placeholder app id, so a zip with an invalid id is never produced.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "chatgpt-plugin"
MANIFESTS = ("plugin.json", ".codex-plugin/plugin.json")
IGNORED_NAMES = frozenset({".DS_Store"})
PLACEHOLDER = "REPLACE_WITH_THE_APP_ID"


def manifest_versions(plugin_dir: Path = PLUGIN_DIR) -> dict[str, str]:
    """The ``version`` of every manifest, by path inside the plugin folder."""
    return {
        name: str(json.loads((plugin_dir / name).read_text(encoding="utf-8"))["version"])
        for name in MANIFESTS
    }


def unfilled_app_ids(plugin_dir: Path = PLUGIN_DIR) -> list[str]:
    """The apps in ``.app.json`` whose id is still the placeholder."""
    apps = json.loads((plugin_dir / ".app.json").read_text(encoding="utf-8"))["apps"]
    return [name for name, app in apps.items() if PLACEHOLDER in str(app["id"])]


def build(out_dir: Path, plugin_dir: Path = PLUGIN_DIR) -> Path:
    """Write ``server-copilot-chatgpt-plugin-<version>.zip`` into ``out_dir``; return its path."""
    version = manifest_versions(plugin_dir)["plugin.json"]
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"server-copilot-chatgpt-plugin-{version}.zip"
    files = sorted(p for p in plugin_dir.rglob("*") if p.is_file() and p.name not in IGNORED_NAMES)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            # A fixed timestamp and mode keep the zip identical for the same content.
            info = zipfile.ZipInfo(path.relative_to(plugin_dir).as_posix(), (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return target


def main(argv: list[str] | None = None, plugin_dir: Path = PLUGIN_DIR) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out_dir", nargs="?", default="dist", type=Path)
    parser.add_argument("--expect-version", help="fail unless every manifest has this version")
    args = parser.parse_args(argv)

    if args.expect_version:
        wrong = {n: v for n, v in manifest_versions(plugin_dir).items() if v != args.expect_version}
        if wrong:
            print(f"Plugin version is not {args.expect_version}: {wrong}", file=sys.stderr)
            return 1
    if unfilled := unfilled_app_ids(plugin_dir):
        print(
            f"chatgpt-plugin/.app.json still has the placeholder app id for {unfilled}: "
            "put the app id ChatGPT gave you (asdk_app_...) there first.",
            file=sys.stderr,
        )
        return 1
    print(build(args.out_dir, plugin_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
