"""The ChatGPT plugin in chatgpt-plugin/ stays valid, follows the release version and zips right."""

from __future__ import annotations

import importlib.util
import json
import shutil
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from server_copilot import __version__

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "chatgpt-plugin"
MANIFESTS = ("plugin.json", ".codex-plugin/plugin.json")


def _load_build_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_chatgpt_plugin", ROOT / "scripts" / "build_chatgpt_plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_script = _load_build_script()


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_the_manifests_are_valid_json_with_a_name_and_description(manifest: str) -> None:
    data = json.loads((PLUGIN_DIR / manifest).read_text(encoding="utf-8"))

    assert data["name"]
    assert data["description"]


def test_both_manifests_use_the_same_plugin_name() -> None:
    names = {
        json.loads((PLUGIN_DIR / manifest).read_text(encoding="utf-8"))["name"]
        for manifest in MANIFESTS
    }

    assert len(names) == 1


def test_every_file_a_manifest_points_to_is_in_the_folder() -> None:
    root = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    openai = root["extensions"]["com.openai"]
    codex = json.loads((PLUGIN_DIR / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))

    for reference in (
        openai["apps"],
        openai["interface"]["logo"],
        openai["interface"]["composerIcon"],
        codex["apps"],
        codex["interface"]["logo"],
        codex["interface"]["composerIcon"],
        *openai["interface"].get("screenshots", []),
        *codex["interface"].get("screenshots", []),
    ):
        assert (PLUGIN_DIR / reference).is_file(), reference


def test_the_app_manifest_names_an_app_id() -> None:
    data = json.loads((PLUGIN_DIR / ".app.json").read_text(encoding="utf-8"))

    assert data["apps"]
    for app in data["apps"].values():
        assert app["id"].startswith("asdk_app_")


def test_both_manifests_carry_the_release_version() -> None:
    assert build_script.manifest_versions() == dict.fromkeys(MANIFESTS, __version__)


def test_the_release_bumps_the_version_of_both_manifests() -> None:
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    for manifest in MANIFESTS:
        assert f'"chatgpt-plugin/{manifest}:version"' in config


def test_the_release_pipeline_builds_the_zip_and_attaches_it_to_the_release() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "scripts/build_chatgpt_plugin.py" in workflow
    assert '--expect-version "${TAG#v}"' in workflow
    assert "gh release upload" in workflow


def test_the_zip_has_the_folders_contents_at_its_root(tmp_path: Path) -> None:
    target = build_script.build(tmp_path)

    assert target.name == f"server-copilot-chatgpt-plugin-{__version__}.zip"
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert sorted(archive.namelist()) == sorted(
            path.relative_to(PLUGIN_DIR).as_posix()
            for path in PLUGIN_DIR.rglob("*")
            if path.is_file() and path.name not in build_script.IGNORED_NAMES
        )
        assert {".app.json", ".codex-plugin/plugin.json", "plugin.json"} <= set(archive.namelist())


def test_the_zip_leaves_out_ds_store_files_and_is_reproducible(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    shutil.copytree(PLUGIN_DIR, plugin)
    (plugin / ".DS_Store").write_bytes(b"junk")
    (plugin / "assets" / ".DS_Store").write_bytes(b"junk")

    first = build_script.build(tmp_path / "one", plugin)
    second = build_script.build(tmp_path / "two", plugin)

    with zipfile.ZipFile(first) as archive:
        assert not any(name.endswith(".DS_Store") for name in archive.namelist())
    assert first.read_bytes() == second.read_bytes()


def _copy_with_app_id(tmp_path: Path, app_id: str) -> Path:
    plugin = tmp_path / "plugin"
    shutil.copytree(PLUGIN_DIR, plugin)
    manifest = json.loads((plugin / ".app.json").read_text(encoding="utf-8"))
    for app in manifest["apps"].values():
        app["id"] = app_id
    (plugin / ".app.json").write_text(json.dumps(manifest), encoding="utf-8")
    return plugin


def test_the_build_refuses_the_placeholder_app_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plugin = _copy_with_app_id(tmp_path, f"asdk_app_{build_script.PLACEHOLDER}")
    out = tmp_path / "out"

    assert build_script.unfilled_app_ids(plugin)
    assert build_script.main([str(out)], plugin) == 1
    assert "placeholder app id" in capsys.readouterr().err
    assert not list(out.glob("*.zip"))


def test_the_build_accepts_a_real_app_id_and_refuses_a_version_that_is_not_expected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plugin = _copy_with_app_id(tmp_path, "asdk_app_0123456789abcdef")
    out = tmp_path / "out"

    assert not build_script.unfilled_app_ids(plugin)
    assert build_script.main([str(out), "--expect-version", "99.0.0"], plugin) == 1
    assert "99.0.0" in capsys.readouterr().err
    assert not list(out.glob("*.zip"))

    assert build_script.main([str(out), "--expect-version", __version__], plugin) == 0
    assert len(list(out.glob("*.zip"))) == 1
