"""offline-frontend-assets 規格：資產清單可驗證。"""
import hashlib
import json
import re
import shutil
from pathlib import Path

from app.config import PROJECT_ROOT

STATIC_DIR = PROJECT_ROOT / "app" / "static"
ALLOWED_LICENSES = {"MIT", "OFL-1.1"}
# 專案自己撰寫、不屬於第三方資產的字型相關檔案
OWN_FILES_IN_FONTS = {"fonts/fonts.css"}


def load_manifest(static_dir: Path = STATIC_DIR) -> dict:
    return json.loads((static_dir / "MANIFEST.json").read_text(encoding="utf-8-sig"))


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_problems(static_dir: Path = STATIC_DIR) -> list[str]:
    """回傳清單與實際檔案不一致的描述；空清單代表一致。"""
    problems: list[str] = []
    for asset in load_manifest(static_dir)["assets"]:
        for entry in asset["files"]:
            path = static_dir / entry["path"]
            if not path.is_file():
                problems.append(f"找不到檔案：{entry['path']}")
            elif sha256_of(path) != entry["sha256"]:
                problems.append(f"校驗碼不一致：{entry['path']}")
    return problems


def listed_paths(static_dir: Path = STATIC_DIR) -> set[str]:
    return {entry["path"] for asset in load_manifest(static_dir)["assets"] for entry in asset["files"]}


# ---- 清單內容 -----------------------------------------------------------------

def test_every_checksum_in_the_manifest_matches_the_file_on_disk():
    assert manifest_problems() == []


def test_manifest_entries_are_complete_and_well_formed():
    for asset in load_manifest()["assets"]:
        assert asset["name"] and asset["version"] and asset["files"]
        assert asset["license"] in ALLOWED_LICENSES, f"{asset['name']} 的授權不在允許清單"
        assert asset["source"].startswith("https://"), f"{asset['name']} 缺少來源網址"
        for entry in asset["files"]:
            assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), f"{entry['path']} 的校驗碼格式不正確"


def test_third_party_files_with_a_version_in_their_source_url_are_consistent():
    for asset in load_manifest()["assets"]:
        if "registry.npmjs.org" in asset["source"]:
            assert asset["version"] in asset["source"], f"{asset['name']} 的版本與來源網址不一致"
            assert asset["upstream_integrity"].startswith("sha512-")


def test_no_unlisted_third_party_file_exists_in_vendor_or_fonts():
    listed = listed_paths()
    on_disk = {
        p.relative_to(STATIC_DIR).as_posix()
        for folder in ("vendor", "fonts")
        if (STATIC_DIR / folder).is_dir()
        for p in (STATIC_DIR / folder).rglob("*")
        if p.is_file()
    }
    unlisted = on_disk - listed - OWN_FILES_IN_FONTS
    assert unlisted == set(), f"這些第三方檔案不在資產清單內：{sorted(unlisted)}"


def test_every_vendor_library_ships_its_license_file():
    for asset in load_manifest()["assets"]:
        if any(e["path"].startswith("vendor/") for e in asset["files"]):
            assert any(e["path"].endswith("/LICENSE") for e in asset["files"]), f"{asset['name']} 缺少授權檔"


# ---- 竄改偵測（用暫存複本，不動到真正的資產）-------------------------------------

def _copy_static(tmp_path: Path) -> Path:
    target = tmp_path / "static"
    target.mkdir()
    shutil.copy(STATIC_DIR / "MANIFEST.json", target / "MANIFEST.json")
    for relative in listed_paths():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(STATIC_DIR / relative, destination)
    return target


def test_tampering_with_a_vendor_file_is_detected(tmp_path):
    static_copy = _copy_static(tmp_path)
    assert manifest_problems(static_copy) == []
    target = static_copy / "vendor" / "vue" / "vue.global.prod.js"
    target.write_bytes(target.read_bytes() + b"\n/* injected */")
    problems = manifest_problems(static_copy)
    assert problems == ["校驗碼不一致：vendor/vue/vue.global.prod.js"]


def test_a_missing_file_is_detected(tmp_path):
    static_copy = _copy_static(tmp_path)
    (static_copy / "vendor" / "axios" / "axios.min.js").unlink()
    assert "找不到檔案：vendor/axios/axios.min.js" in manifest_problems(static_copy)


def test_the_update_script_check_mode_agrees_with_the_test(tmp_path):
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "update_asset_manifest.py"), "--check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
