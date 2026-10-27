"""容器驗收（container-runtime 規格）：經由 WSL 內的 Docker 建置並檢查映像與執行行為。

用法：
    python scripts/verify_container.py              # 建置並驗收
    python scripts/verify_container.py --no-build   # 只驗收（映像已存在）

本機沒有 Windows 原生 docker，所有指令都經由 `wsl -d Ubuntu-2204 -u root -- docker ...`。
注意：SQL Server 只監聽 127.0.0.1，WSL 內的容器連不到，因此執行行為的驗收使用 SQLite；
真實 SQL Server 由主機上的整合測試（pytest -m sqlserver）驗證。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DISTRO = "Ubuntu-2204"
FAKE_SECRET = "S3cretValue-for-container-test"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  —  {detail}" if detail else ""))


def to_wsl_path(path: Path) -> str:
    drive, rest = path.resolve().drive, path.resolve().as_posix()[2:]
    return f"/mnt/{drive[0].lower()}{rest}"


def wsl(*args: str, stdin: str | None = None, timeout: int = 300) -> tuple[int, str]:
    completed = subprocess.run(
        ["wsl", "-d", DISTRO, "-u", "root", "--", *args],
        input=stdin.encode("utf-8") if stdin is not None else None,
        capture_output=True,
        timeout=timeout,
    )
    output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
    return completed.returncode, output


def docker(*args: str, stdin: str | None = None, timeout: int = 300) -> tuple[int, str]:
    return wsl("docker", *args, stdin=stdin, timeout=timeout)


def run_python_in_image(image: str, code: str) -> tuple[int, str]:
    """在映像內以 python 執行一段程式（從標準輸入傳入，避免 shell 引號問題；程式碼必須是 ASCII）。"""
    return docker("run", "-i", "--rm", "--entrypoint", "python", image, "-", stdin=code)


def run_sh_in_image(image: str, script: str) -> tuple[int, str]:
    return docker("run", "-i", "--rm", "--entrypoint", "sh", image, "-s", stdin=script)


LAYER_SCAN = r"""
set -u
image="$1"
work=$(mktemp -d)
docker save "$image" -o "$work/image.tar"
mkdir "$work/x" && tar -xf "$work/image.tar" -C "$work/x"
# 每一層（含已被後續層刪除的檔案）都列出檔名，找 .env 與 .git
for blob in "$work"/x/blobs/sha256/*; do tar -tf "$blob" 2>/dev/null; done \
  | grep -E '(^|/)\.env($|\.)|(^|/)\.git(/|$)|(^|/)(tests|docs|openspec)(/|$)' || true
rm -rf "$work"
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", default="platform-foundation:c1")
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()
    image = args.image

    wsl("service", "docker", "start")  # 舊版 WSL 不支援 systemd，需手動啟動（可重複執行）
    code, out = docker("version", "--format", "{{.Server.Version}}")
    check("Docker 服務可用", code == 0, out.strip())
    if code != 0:
        return 2

    if not args.no_build:
        code, out = docker("build", "-t", image, to_wsl_path(PROJECT_ROOT), timeout=900)
        check("映像建置成功", code == 0, "" if code == 0 else out[-600:])
        if code != 0:
            return 1
    code, out = docker("image", "inspect", image, "--format", "{{.Size}}")
    check("映像存在", code == 0, f"{int(out.strip()) / 1024 / 1024:.0f} MB" if code == 0 else out.strip())

    # ---- 映像內容 -----------------------------------------------------------
    code, out = docker("run", "--rm", "--entrypoint", "id", image, "-u")
    check("映像預設使用者不是 root", code == 0 and out.strip() not in ("", "0"), f"uid={out.strip()}")

    code, out = run_sh_in_image(image, "find / -xdev \\( -name '.env' -o -name '.env.*' -o -name '.git' \\) -not -path '/proc/*' 2>/dev/null; "
                                       "ls -d /app/tests /app/docs /app/openspec /app/scripts 2>/dev/null; echo END")
    leftovers = [line for line in out.splitlines() if line and line != "END"]
    check("映像內沒有 .env、.git、測試與文件", leftovers == [], ", ".join(leftovers))

    code, out = wsl("bash", "-s", "--", image, stdin=LAYER_SCAN)
    check("映像的每一層（含已刪除的檔案）都沒有 .env、.git、測試與文件", code == 0 and out.strip() == "", out.strip()[:300])

    code, out = run_sh_in_image(image, "test -f /app/app/static/vendor/vue/vue.global.prod.js && test -f /app/app/static/vendor/axios/axios.min.js "
                                       "&& test -f /app/app/static/vendor/bootstrap/bootstrap.min.css && test -f /app/app/static/fonts/fonts.css "
                                       "&& ls /app/app/static/fonts/*.woff2 | wc -l")
    check("映像內有前端資產與 108 個字型切片", code == 0 and out.strip() == "108", out.strip())

    expected_python = (PROJECT_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    code, out = run_python_in_image(image, "import sys; print('%d.%d' % sys.version_info[:2])")
    check("映像的 Python 主要與次要版本等於 .python-version", out.strip() == expected_python, f"映像 {out.strip()} / 要求 {expected_python}")

    code, out = run_python_in_image(image, "import pyodbc; print(pyodbc.drivers())")
    check("pyodbc 可載入，且列出 ODBC Driver 18 for SQL Server", code == 0 and "ODBC Driver 18 for SQL Server" in out, out.strip())
    code, out = docker("run", "--rm", "--entrypoint", "odbcinst", image, "-q", "-d")
    check("odbcinst 列出 ODBC Driver 18", "ODBC Driver 18 for SQL Server" in out, out.strip().replace("\n", " "))

    # ---- 缺少必要機密 → 啟動失敗，且輸出只列出變數名稱 ---------------------------
    code, out = docker("run", "--rm", "-e", "APP_ENV=production", "-e", f"SECRET_KEY={FAKE_SECRET}", image, timeout=120)
    check("缺少必要機密：容器以非零結束碼結束", code != 0, f"結束碼 {code}")
    check("輸出指出缺少的變數名稱", "DATABASE_URL" in out)
    check("輸出不含任何機密值", FAKE_SECRET not in out)

    code, out = docker("run", "--rm", image.split(":")[0] if False else image, timeout=120)  # 完全沒有 APP_ENV
    check("未設定 APP_ENV：容器拒絕啟動", code != 0 and "APP_ENV" in out, f"結束碼 {code}")

    # ---- 以接近正式環境的設定實際執行 ---------------------------------------------
    for label, env in (
        ("testing", ["APP_ENV=testing"]),
        ("production（SQLite 記憶體）", ["APP_ENV=production", f"SECRET_KEY={FAKE_SECRET}", "DATABASE_URL=sqlite+pysqlite:///:memory:"]),
    ):
        name = f"pf-verify-{uuid.uuid4().hex[:8]}"
        env_args = [item for pair in (("-e", e) for e in env) for item in pair]
        code, out = docker("run", "-d", "--name", name, *env_args, image)
        try:
            check(f"[{label}] 容器啟動", code == 0, out.strip()[:80])
            time.sleep(4)
            fetch = ("import urllib.request, urllib.error\n"
                     "def get(path):\n"
                     "    try:\n"
                     "        r = urllib.request.urlopen('http://127.0.0.1:8080' + path, timeout=5); return r.status, r.read().decode()\n"
                     "    except urllib.error.HTTPError as e:\n"
                     "        return e.code, ''\n"
                     "for p in ('/healthz', '/readyz', '/', '/dev/components', '/static/vendor/vue/vue.global.prod.js'):\n"
                     "    print(p, *get(p)[:2])\n")
            code, out = docker("exec", "-i", name, "python", "-", stdin=fetch)
            lines = {ln.split(" ", 1)[0]: ln.split(" ", 1)[1] if " " in ln else "" for ln in out.splitlines() if ln.startswith("/")}
            check(f"[{label}] /healthz 回 200", lines.get("/healthz", "").startswith("200"), lines.get("/healthz", out[:120]))
            check(f"[{label}] /readyz 回 200", lines.get("/readyz", "").startswith("200"), lines.get("/readyz", ""))
            check(f"[{label}] 首頁與前端靜態檔由容器提供", lines.get("/", "").startswith("200") and lines.get("/static/vendor/vue/vue.global.prod.js", "").startswith("200"))
            dev = lines.get("/dev/components", "")
            if label == "testing":
                check(f"[{label}] 開發示範頁存在", dev.startswith("200"))
            else:
                check(f"[{label}] 開發示範頁是 404（正式環境不提供）", dev.startswith("404"), dev)

            code, out = docker("exec", "-i", name, "python", "-",
                               stdin="d = dict(l.split(':', 1) for l in open('/proc/1/status').read().splitlines() if ':' in l)\n"
                                     "print('uid', d['Uid'].split()[0]); print('cmd', open('/proc/1/cmdline').read().replace(chr(0), ' '))\n")
            check(f"[{label}] 主行程是 gunicorn，且不是 root", "gunicorn" in out and re.search(r"uid (?!0\b)\d+", out) is not None, out.strip().replace("\n", " | ")[:160])

            code, logs = docker("logs", name)
            check(f"[{label}] 日誌顯示使用 gthread，且沒有開發伺服器警告", "gthread" in logs and "development server" not in logs.lower(), "")
            check(f"[{label}] 請求日誌是 JSON，且不含機密", FAKE_SECRET not in logs and '"message":"請求完成"' in logs)

            started = time.time()
            code, out = docker("stop", "-t", "30", name)
            elapsed = time.time() - started
            code2, state = docker("inspect", name, "--format", "{{.State.ExitCode}}")
            check(f"[{label}] 收到終止訊號後正常結束（結束碼 0）", state.strip() == "0", f"結束碼 {state.strip()}，耗時 {elapsed:.1f} 秒")
            code, logs = docker("logs", name)
            check(f"[{label}] 日誌顯示 gunicorn 依序處理終止訊號", "Handling signal: term" in logs or "Shutting down: Master" in logs)
        finally:
            docker("rm", "-f", name)

    failed = [r for r in results if not r[1]]
    print(f"\n結果：{len(results) - len(failed)} / {len(results)} 項通過")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
