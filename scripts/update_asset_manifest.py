"""重新計算（或檢查）本機前端資產的 SHA-256，維護 app/static/MANIFEST.json。

用法：
    python scripts/update_asset_manifest.py           # 重算並寫回每個檔案的 sha256
    python scripts/update_asset_manifest.py --check   # 只檢查；有不一致就以非零碼結束

更新第三方資產的完整流程見 README「離線資產與更新方式」。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "static"
MANIFEST_PATH = STATIC_DIR / "MANIFEST.json"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="只檢查，不寫回")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8-sig"))
    problems: list[str] = []
    changed = 0

    for asset in manifest["assets"]:
        for entry in asset["files"]:
            path = STATIC_DIR / entry["path"]
            if not path.is_file():
                problems.append(f"找不到檔案：{entry['path']}")
                continue
            actual = sha256_of(path)
            if entry.get("sha256") != actual:
                if args.check:
                    problems.append(f"校驗碼不一致：{entry['path']}")
                else:
                    entry["sha256"] = actual
                    changed += 1

    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    if not args.check and changed:
        MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("資產清單檢查通過" if args.check else f"資產清單已更新（{changed} 個檔案的校驗碼有變動）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
