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
import json
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import mint_test_sessions  # noqa: E402 - 產生「在容器內簽出測試玩家 cookie」的程式

DISTRO = "Ubuntu-2204"
FAKE_SECRET = "S3cretValue-for-container-test-0123456789"   # staging 與 production 的 SECRET_KEY 至少 32 字元
FAKE_GEMINI_KEY = "AIzaSy-container-fake-gemini-key-0123456789"   # staging 與 production 必須有金鑰才能啟動；容器內沒有網路出口，這把假的永遠不會被送出
# production 的寄信設定（營運告警與管理員的重設密碼信）與 https 的對外網址（寄信目的地是不存在的主機，只為了通過啟動驗證）
PRODUCTION_AUTH_ENV = [
    "BASE_URL=https://cs.example.com", "MAIL_SMTP_HOST=127.0.0.1", "MAIL_SMTP_PORT=1", "MAIL_FROM=service@example.com",
    f"GEMINI_API_KEY={FAKE_GEMINI_KEY}",
    # staging 與 production 必須設定告警收件人（C-4d 起），設定後寄信設定也必須完整；地址是假的，容器沒有網路出口
    "ALERT_EMAIL_TO=ops@example.com",
]
MINT_PREFIX = "container-1"
MEMBER_EMAIL = f"{MINT_PREFIX}-1@loadtest.invalid"      # scripts/mint_test_sessions.py 簽出的測試玩家
ADMIN_EMAIL = "container.admin@example.com"
ADMIN_PASSWORD = "container-admin-password-1"

results: list[tuple[str, bool, str]] = []
EXPECTED_FLOW_RESULTS = 53          # 流程腳本至少要回報這麼多項（調整腳本時同步更新）


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


# 在容器內執行的會員流程（程式碼必須是 ASCII）。模擬入口層：帶 X-Forwarded-Proto: https 與對外主機，
# 因此 staging 的 Secure、__Host- 前綴的 cookie 會被發放；Secure cookie 不會被 urllib 在 http 上送回，所以手動管理 cookie。
AUTH_FLOW = r'''
import json, re, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone

BASE = "http://127.0.0.1:8080"
ORIGIN = "https://cs.example.com"
EMAIL = "%(email)s"
cookies, attributes = {}, {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def call(method, path, body=None, csrf=True):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("X-Forwarded-Proto", "https")
    request.add_header("X-Forwarded-Host", "cs.example.com")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if cookies:
        request.add_header("Cookie", "; ".join(k + "=" + v for k, v in cookies.items()))
    if method != "GET":
        request.add_header("Origin", ORIGIN)
        if csrf:
            request.add_header("X-CSRF-Token", cookies.get("__Host-csrf", ""))
    opener = urllib.request.build_opener(NoRedirect)
    try:
        response = opener.open(request)
    except urllib.error.HTTPError as error:
        response = error
    text = response.read().decode("utf-8", "replace")
    for raw in response.headers.get_all("Set-Cookie") or []:
        name, _, rest = raw.partition("=")
        value = rest.split(";", 1)[0]
        attributes[name] = raw
        if value == "" or "Max-Age=0" in raw:
            cookies.pop(name, None)
        else:
            cookies[name] = value
    try:
        return response.status if hasattr(response, "status") else response.code, json.loads(text), response.headers
    except ValueError:
        return response.status if hasattr(response, "status") else response.code, text, response.headers


def report(name, ok, detail=""):
    print("RESULT|%%s|%%s|%%s" %% (name, "1" if ok else "0", detail))


# the age of "today" in Asia/Taipei; born(N) is a birthday that makes the person exactly N years old (day capped at 28)
today = datetime.now(timezone(timedelta(hours=8))).date()


def born(age):
    return "%%04d-%%02d-%%02d" %% (today.year - age, today.month, min(today.day, 28))


AGE_SECRETS = {"birth_date", "age_range", "age_confirmed_at", "age_policy_version", "is_adult_at_confirmation"}
REMOVED_ENDPOINTS = ("/api/auth/register", "/api/auth/login", "/api/auth/verify-email", "/api/auth/resend-verification", "/api/auth/forgot-password")


status, _, headers = call("GET", "/login")
report("login page is served", status == 200)
csrf_cookie = attributes.get("__Host-csrf", "")
report("csrf cookie is __Host- prefixed, Secure, HttpOnly, SameSite=Lax, Path=/, no Domain",
       all(part in csrf_cookie for part in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/")) and "Domain" not in csrf_cookie, csrf_cookie.split(";")[0][:20])
report("html pages are not cacheable", headers.get("Cache-Control") == "no-store")

# players only sign in with Google: the password and mail-verification endpoints no longer exist (a valid CSRF token and Origin are sent)
for path in REMOVED_ENDPOINTS:
    status, _, _ = call("POST", path, {"email": EMAIL, "password": "x-not-a-password", "display_name": "x", "accept_terms": True, "token": "x"})
    report("the removed endpoint " + path + " answers 404", status == 404, str(status))
for page in ("/forgot-password", "/verify-email"):
    status, _, _ = call("GET", page)
    report("the removed page " + page + " answers 404", status == 404, str(status))
report("no session cookie was issued by any of the removed endpoints", "__Host-session" not in cookies)

# The test player was minted inside this container with its own SECRET_KEY (scripts/mint_test_sessions.py): the same cookie a real login sets
cookies["__Host-session"] = "%(session)s"
status, body, _ = call("GET", "/api/auth/me")
report("the minted player's session cookie is accepted", status == 200 and body["user"]["email"] == EMAIL and body["user"]["role"] == "user", str(status))
session_cookie = attributes.get("__Host-session", "")
report("session cookie is __Host- prefixed, Secure, HttpOnly, SameSite=Lax, Path=/, with Expires, no Domain",
       all(part in session_cookie for part in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/", "Expires=")) and "Domain" not in session_cookie)
me = body
status, _, _ = call("POST", "/api/auth/verify-age", {"birth_date": born(30), "age_range": "25_34"}, csrf=False)
report("verify-age without the CSRF header is refused", status == 403, str(status))
report("/api/auth/me never exposes internals", not ({"id", "token_version", "password_hash"} & set(me["user"])))
report("a minted player reports age_confirmed and age_group, never the birthday or range, and has no password",
       me["user"].get("age_confirmed") is True and me["user"].get("age_group") == "adult" and me["user"].get("has_password") is False
       and not (AGE_SECRETS & set(me["user"])))
status, body, _ = call("GET", "/account")
report("/account renders for a logged-in member", status == 200 and "account-list" in body)
status, body, _ = call("POST", "/api/auth/change-password", {"current_password": "x-not-a-password", "new_password": "y-not-a-password"}, csrf=False)
report("a POST without the CSRF header is refused", status == 403 and body["error"]["code"] == "csrf_failed", str(status))
status, body, _ = call("POST", "/api/auth/change-password", {"current_password": "x-not-a-password", "new_password": "y-not-a-password"})
report("a player has no password to change (400 no_password)", status == 400 and body["error"]["code"] == "no_password", str(status))
status, _, _ = call("POST", "/api/auth/logout")
report("logout succeeds", status == 200)
status, _, _ = call("GET", "/api/auth/me")
report("after logout /api/auth/me is 401", status == 401, str(status))
status, _, _ = call("POST", "/api/auth/verify-age", {"birth_date": born(30), "age_range": "25_34"})
report("verify-age needs a login", status == 401, str(status))

# new pages of the Google-first login: the login page is only the Google button and the terms checkbox, /register leads to it
status, body, _ = call("GET", "/login")
report("the login page has the terms checkbox and no password, email or birthday field",
       status == 200 and "accept-terms" in body and 'type="password"' not in body and 'type="email"' not in body and 'type="date"' not in body)
status, _, headers = call("GET", "/register")
report("/register leads to /login", status == 302 and headers.get("Location") == "/login", headers.get("Location", ""))
for page in ("/age", "/appeal"):
    status, _, headers = call("GET", page)
    report("an anonymous " + page + " redirects to /login keeping next", status == 302 and headers.get("Location", "").startswith("/login?next="), headers.get("Location", ""))
status, _, headers = call("GET", "/login/password")
report("the admin password page needs the step cookie, otherwise it leads back to /login", status == 302 and headers.get("Location") == "/login", headers.get("Location", ""))
status, body, _ = call("POST", "/api/auth/admin-password", {"password": "x"})
report("the admin password endpoint without the step cookie is 401", status == 401 and body["error"]["code"] == "unauthorized", str(status))
status, body, _ = call("POST", "/api/appeals", {"content": "x"})
report("the appeal API needs a login", status == 401, str(status))
status, _, _ = call("GET", "/")
report("the home page is public for an anonymous visitor", status == 200)

# create-admin is run in the container beforehand and deliberately does not fill in the age: the admin has no birthday yet.
# An admin can never log in with only the password (the endpoint is gone): Google comes first and then the password.
status, body, _ = call("POST", "/api/auth/login", {"email": "%(admin_email)s", "password": "%(admin_password)s"})
report("the admin created by the command cannot log in with only the password (the endpoint no longer exists: 404)", status == 404, str(status))
report("... and no session cookie is issued", "__Host-session" not in cookies)
# Google is not configured in this run, so the two-step login cannot be walked through here (tests cover it with a fake provider).
# The remaining checks need an admin session, so one is signed with the server's own secret: the same cookie the real login sets.
import os
from flask import Flask
from flask.sessions import SecureCookieSessionInterface
from sqlalchemy import select
from app import create_app
from app.models.user import User

server = create_app()
with server.extensions["db_session_factory"]() as db_session:
    admin_row = db_session.scalar(select(User).where(User.role == "admin"))
    admin_uid, admin_tv = admin_row.id, admin_row.token_version
signer = Flask("signer")
signer.secret_key = os.environ["SECRET_KEY"]
cookies["__Host-session"] = SecureCookieSessionInterface().get_signing_serializer(signer).dumps({"uid": admin_uid, "tv": admin_tv})
status, body, _ = call("GET", "/api/auth/me")
report("the signed admin session is accepted", status == 200 and body["user"]["role"] == "admin", str(status))
report("the admin starts without any age data and is not in a restricted state",
       body["user"].get("age_confirmed") is False and body["user"].get("age_group") is None and body["user"].get("restriction") is None)
status, body, _ = call("POST", "/api/cs/conversations", {"client_id": "00000000-0000-4000-8000-000000000001"})
report("chat: an admin without age data cannot create a conversation (403 age_confirmation_required)", status == 403 and body["error"]["code"] == "age_confirmation_required", str(status))
status, body, _ = call("POST", "/api/cs/conversations/00000000-0000-4000-8000-0000000000aa/messages", {"client_msg_id": "00000000-0000-4000-8000-000000000002", "content": "hi"})
report("chat: sending a message without age data is 403 before anything else, so no model call is possible", status == 403 and body["error"]["code"] == "age_confirmation_required", str(status))
status, body, _ = call("POST", "/api/auth/verify-age", {"birth_date": born(30), "age_range": "13_17"})
report("verify-age refuses a mismatch", status == 400 and body["error"]["code"] == "age_mismatch", str(status))
status, body, _ = call("POST", "/api/auth/verify-age", {"birth_date": born(12), "age_range": "under_13"})
report("verify-age refuses someone under 13", status == 400 and body["error"]["code"] == "age_not_eligible", str(status))
status, body, _ = call("POST", "/api/auth/verify-age", {"birth_date": born(15), "age_range": "13_17"})
report("verify-age records the data and reports a minor", status == 200 and body["user"]["age_confirmed"] is True and body["user"]["age_group"] == "minor" and not (AGE_SECRETS & set(body["user"])), str(status))
status, body, _ = call("GET", "/api/auth/me")
report("the result persists for the logged-in session", status == 200 and body["user"]["age_confirmed"] is True and body["user"]["age_group"] == "minor", str(status))

CID = "00000000-0000-4000-8000-000000000003"
status, body, headers = call("POST", "/api/cs/conversations", {"client_id": CID})
report("chat: once the age data is filled a conversation can be created (201, no-store)", status == 201 and headers.get("Cache-Control") == "no-store", str(status))
conversation = body["conversation"]
status, body, _ = call("POST", "/api/cs/conversations", {"client_id": CID})
report("chat: creating again with the same client id returns the same conversation (200)", status == 200 and body["conversation"]["id"] == conversation["id"], str(status))
status, body, _ = call("GET", "/api/cs/conversations")
report("chat: the list holds exactly that conversation", status == 200 and [c["id"] for c in body["conversations"]] == [conversation["id"]], str(status))
path = "/api/cs/conversations/" + conversation["id"]
status, body, _ = call("PATCH", path, {"title": "renamed", "state_version": conversation["state_version"]})
report("chat: renaming with the current version succeeds and bumps the version", status == 200 and body["conversation"]["title"] == "renamed" and body["conversation"]["state_version"] == conversation["state_version"] + 1, str(status))
status, body, _ = call("PATCH", path, {"title": "stale", "state_version": conversation["state_version"]})
report("chat: a stale version is 409 version_conflict and carries the latest conversation", status == 409 and body["error"]["code"] == "version_conflict" and body["conversation"]["title"] == "renamed", str(status))
status, body, _ = call("GET", path + "/messages")
report("chat: the new conversation has no messages", status == 200 and body == {"messages": [], "has_more": False}, str(status))
status, body, _ = call("GET", "/cs")
found = re.search(r"id=.initial-state.>(.*?)</script>", body, re.S) if status == 200 else None
report("chat: /cs renders for a logged-in member", status == 200 and found is not None and "cs-app" in body, str(status))
state = json.loads(found.group(1)) if found else {}
report("chat: the initial state has no gate, a 35000 ms timeout and an available provider", state.get("chat_gate") is None and state.get("request_timeout_ms") == 35000 and state.get("service_available") is True, str(state)[:120])
status, body, _ = call("DELETE", path)
report("chat: deleting the conversation succeeds", status == 200 and body == {"ok": True}, str(status))
status, body, _ = call("GET", "/api/cs/conversations")
report("chat: the list is empty again", status == 200 and body["conversations"] == [], str(status))
call("POST", "/api/auth/logout")
status, body, _ = call("GET", "/api/cs/conversations")
report("chat: an anonymous API call is 401", status == 401, str(status))
status, _, headers = call("GET", "/account")
report("an anonymous /account redirects to /login keeping next", status == 302 and headers.get("Location", "").startswith("/login?next="), headers.get("Location", ""))
status, _, headers = call("GET", "/cs")
report("chat: an anonymous /cs redirects to /login keeping next=/cs", status == 302 and headers.get("Location", "") == "/login?next=%%2Fcs", headers.get("Location", ""))
status, _, _ = call("GET", "/terms")
report("/terms and /privacy are public", status == 200 and call("GET", "/privacy")[0] == 200)
'''


def render_auth_flow(session_value: str) -> str:
    """把容器內簽出的測試玩家 session cookie 放進流程腳本（程式碼必須是 ASCII；cookie 值是 base64 與點號組成）。"""
    return AUTH_FLOW % {"email": MEMBER_EMAIL, "session": session_value, "admin_email": ADMIN_EMAIL, "admin_password": ADMIN_PASSWORD}


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
    # 冷啟動時 daemon 在 `service docker start` 返回後還要幾秒才接受連線，所以輪詢最多 30 秒
    for _ in range(15):
        code, out = docker("version", "--format", "{{.Server.Version}}")
        if code == 0:
            break
        time.sleep(2)
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

    expected_python = (PROJECT_ROOT / ".python-version").read_text(encoding="utf-8-sig").strip()
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

    # staging 其餘設定齊全、只缺模型金鑰：同樣啟動失敗，輸出只指出變數名稱，不含其他機密
    code, out = docker(
        "run", "--rm", "-e", "APP_ENV=staging", "-e", f"SECRET_KEY={FAKE_SECRET}", "-e", "DATABASE_URL=sqlite+pysqlite:///:memory:",
        "-e", "BASE_URL=https://cs.example.com", image, timeout=120,
    )
    check("staging 缺少 GEMINI_API_KEY：容器以非零結束碼結束", code != 0, f"結束碼 {code}")
    check("輸出只指出缺少 GEMINI_API_KEY 這個變數名稱，不含其他機密值",
          "GEMINI_API_KEY" in out and FAKE_SECRET not in out and FAKE_GEMINI_KEY not in out, out.strip()[-160:].replace("\n", " "))

    code, out = docker("run", "--rm", image.split(":")[0] if False else image, timeout=120)  # 完全沒有 APP_ENV
    check("未設定 APP_ENV：容器拒絕啟動", code != 0 and "APP_ENV" in out, f"結束碼 {code}")

    # ---- 以接近正式環境的設定實際執行 ---------------------------------------------
    for label, env in (
        ("testing", ["APP_ENV=testing"]),
        ("production（SQLite 記憶體）", ["APP_ENV=production", f"SECRET_KEY={FAKE_SECRET}", "DATABASE_URL=sqlite+pysqlite:///:memory:", *PRODUCTION_AUTH_ENV]),
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
                               stdin="d = dict(l.split(':', 1) for l in open('/proc/1/status', encoding='utf-8-sig').read().splitlines() if ':' in l)\n"
                                     "print('uid', d['Uid'].split()[0]); print('cmd', open('/proc/1/cmdline', encoding='utf-8-sig').read().replace(chr(0), ' '))\n")
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

    verify_member_flow(image)

    failed = [r for r in results if not r[1]]
    print(f"\n結果：{len(results) - len(failed)} / {len(results)} 項通過")
    return 1 if failed else 0


def verify_member_flow(image: str) -> None:
    """會員驗證：新增的相依套件可載入；在容器內以 SQLite 執行 migration 後，簽出測試玩家的 cookie，經 HTTP 檢查已移除的端點、登入狀態與登出。"""
    code, out = run_python_in_image(image, "import argon2, jwt, cryptography; print('imports-ok')")
    check("映像內可載入 argon2、PyJWT 與 cryptography", code == 0 and "imports-ok" in out, out.strip()[:120])

    name = f"pf-auth-{uuid.uuid4().hex[:8]}"
    env = [
        "APP_ENV=staging", f"SECRET_KEY={FAKE_SECRET}", "DATABASE_URL=sqlite+pysqlite:////tmp/member.sqlite3",
        "BASE_URL=https://cs.example.com", f"GEMINI_API_KEY={FAKE_GEMINI_KEY}",
        "ALERT_EMAIL_TO=ops@example.com", "MAIL_SMTP_HOST=127.0.0.1", "MAIL_SMTP_PORT=1", "MAIL_FROM=service@example.com",
    ]
    env_args = [item for pair in (("-e", e) for e in env) for item in pair]
    code, out = docker("run", "-d", "--name", name, *env_args, image)
    try:
        check("[會員流程] 預備環境容器啟動（SQLite 檔案資料庫）", code == 0, out.strip()[:80])
        time.sleep(4)
        code, out = docker("exec", name, "python", "-m", "alembic", "upgrade", "head", timeout=120)
        check("[會員流程] 在容器內執行 alembic upgrade head 成功，升級到生日與年齡區間的 0004",
              code == 0 and "0003_users_age_confirmation" in out and "0004_users_birthdate" in out, out.strip()[-200:].replace("\n", " "))

        check("[會員流程] 遷移一路升級到 0012（內容模式與 RPG 資料表）", "0012_rpg_data_and_script" in out, out.strip()[-120:].replace("\n", " "))

        # 容器內沒有會呼叫 Gemini 的端點可以直接驗年齡閘門；管理員指令刻意不代為填寫年齡，所以用它建立一個「尚未完成」的帳號
        code, out = docker("exec", "-i", name, "python", "-m", "flask", "--app", "wsgi", "create-admin",
                           "--email", ADMIN_EMAIL, "--display-name", "container-admin", "--password-stdin",
                           stdin=ADMIN_PASSWORD + "\n", timeout=120)
        check("[會員流程] 在容器內以 flask create-admin 建立管理員", code == 0, out.strip()[-160:].replace("\n", " "))

        # 測試玩家在容器內建立並簽出 cookie：與 load-testing、staging-verification 用的是同一段程式，映像本身沒有任何簽發登入狀態的指令
        code, out = docker("exec", "-i", name, "python", "-", stdin=mint_test_sessions.render_snippet(1, MINT_PREFIX), timeout=120)
        try:
            minted = mint_test_sessions.parse_output(out)
        except mint_test_sessions.MintError:
            minted = None
        check("[會員流程] 在容器內建立測試玩家並簽出 session cookie（信箱屬於 loadtest.invalid）",
              code == 0 and minted is not None and minted["cookie_name"] == "__Host-session" and minted["sessions"][0]["email"] == MEMBER_EMAIL,
              "" if minted else out.strip()[-160:].replace("\n", " "))
        session_value = minted["sessions"][0]["value"] if minted else ""
        code, out = docker("exec", "-i", name, "python", "-", stdin=render_auth_flow(session_value), timeout=120)
        flow = [line.split("|", 3) for line in out.splitlines() if line.startswith("RESULT|")]
        check("[會員流程] 流程腳本有跑完並回報結果", len(flow) >= EXPECTED_FLOW_RESULTS, f"{len(flow)} 項；{out[-200:]!r}" if len(flow) < EXPECTED_FLOW_RESULTS else f"{len(flow)} 項")
        for _, label, ok, detail in flow:
            check(f"[會員流程] {label}", ok == "1", detail)

        code, logs = docker("logs", name)
        check("[會員流程] 日誌沒有信箱、密碼與金鑰",
              not any(secret in logs for secret in (MEMBER_EMAIL, session_value, ADMIN_EMAIL, ADMIN_PASSWORD, FAKE_SECRET, FAKE_GEMINI_KEY)))
        events = re.findall(r'"event":"([a-z_.]+)"', logs)
        check("[會員流程] 日誌有登出的安全事件，且沒有註冊、密碼登入與信箱驗證的事件",
              "auth.logout" in set(events) and not {e for e in events if e.startswith(("auth.register", "auth.verification", "auth.email_verified"))},
              ", ".join(sorted(set(events))))
        check("[會員流程] 日誌有補填年齡，以及不一致與未滿 13 歲被拒絕的安全事件",
              {"auth.age_confirmed.later", "auth.age_rejected.mismatch", "auth.age_rejected.underage"} <= set(events),
              ", ".join(sorted(set(events))))
        # gunicorn 自己的啟動行（"[2026-10-28 09:00:00 +0000] ..."）本來就有日期，所以只掃結構化 JSON 日誌，並先去掉 time 欄位
        app_lines = [re.sub(r'"time":"[^"]*",?', "", line) for line in logs.splitlines() if line.startswith("{")]
        stray_dates = sorted({m for line in app_lines for m in re.findall(r"\d{4}-\d{2}-\d{2}", line)})
        today = datetime.now(timezone(timedelta(hours=8))).date()
        birthdays = {f"{today.year - age:04d}-{today.month:02d}-{min(today.day, 28):02d}" for age in (30, 15, 12)}
        check("[會員流程] 結構化日誌裡沒有任何日期，也找不到測試用的三個生日",
              not stray_dates and not any(day in logs for day in birthdays), ", ".join(stray_dates)[:160])

        verify_retention_commands(name)
        verify_seed_command(name)
        code, out = docker("exec", name, "sh", "-c", "test ! -e /app/.env && test ! -e /app/tests && echo clean")
        check("[會員流程] 映像內仍然沒有 .env 與測試", code == 0 and out.strip() == "clean", out.strip()[:120])
    finally:
        docker("rm", "-f", name)


def verify_seed_command(name: str) -> None:
    """內建劇本的種子（builtin-script-seed 規格）：資料檔隨映像封裝（`app/data/scripts` 沒有被 .dockerignore 排除），
    `flask seed-scripts` 在容器內可執行、冪等，輸出只有種子鍵與筆數。資料庫已由前面的 alembic 升級到 head。"""
    flask = ("exec", name, "python", "-m", "flask", "--app", "wsgi")
    code, preview = docker(*flask, "seed-scripts", "--dry-run", timeout=120)
    match = re.search(r"共 (\d+) 份", preview)
    total = int(match.group(1)) if match else 0
    check("[內建劇本] seed-scripts --dry-run 在映像內成功執行，找到至少一份資料檔（資料檔已隨映像封裝）",
          code == 0 and total > 0 and "預覽" in preview, preview.strip()[-160:].replace("\n", " | "))
    code, listing = docker("exec", name, "sh", "-c", "ls /app/app/data/scripts")
    check("[內建劇本] 映像內有首發劇本的資料檔", code == 0 and "xianxia-qingyun.json" in listing, listing.strip()[:120])
    code, applied = docker(*flask, "seed-scripts", timeout=120)
    check("[內建劇本] 實際執行建立內建劇本，輸出只有種子鍵與筆數、不含劇本文字",
          code == 0 and f"建立 {total}" in applied and "xianxia-qingyun：建立" in applied
          and not any(text in applied for text in ("青雲問道", "靈氣", "花小蝶")), applied.strip()[-160:].replace("\n", " | "))
    code, again = docker(*flask, "seed-scripts", timeout=120)
    check("[內建劇本] 重複執行沒有任何變動", code == 0 and f"建立 0、更新 0、沒有變動 {total}" in again, again.strip()[-160:].replace("\n", " | "))


# 在容器內放入一個 400 天沒登入的帳號（程式碼必須是 ASCII）
SEED_STALE_ACCOUNT = r'''
from datetime import datetime, timedelta, timezone
from app import create_app
from app.models.user import User

app = create_app()
with app.extensions["db_session_factory"]() as session:
    old = datetime.now(timezone.utc) - timedelta(days=400)
    session.add(User(email="stale.container@example.com", email_canonical="stale.container@example.com", display_name="stale",
                     status="active", last_login_at=old, created_at=old, updated_at=old))
    session.commit()
print("seeded")
'''


def verify_retention_commands(name: str) -> None:
    """年齡統計與閒置帳號清除指令：在容器內以 SQLite 實際執行（此時資料庫有 3 個帳號：註冊的會員、管理員、閒置者）。"""
    code, out = docker("exec", "-i", name, "python", "-", stdin=SEED_STALE_ACCOUNT, timeout=120)
    check("[保存期限] 在容器內放入一個 400 天沒登入的帳號", code == 0 and "seeded" in out, out.strip()[-160:])

    flask = ("exec", name, "python", "-m", "flask", "--app", "wsgi")
    code, stats = docker(*flask, "age-stats", timeout=120)
    lines = {line.strip() for line in stats.splitlines()}
    check("[保存期限] age-stats 只列出各區間人數與尚未完成的人數",
          code == 0 and {"25 至 34 歲：1", "13 至 17 歲：1", "尚未完成年齡確認：1", "合計：3"} <= lines
          and not any(secret in stats for secret in (MEMBER_EMAIL, ADMIN_EMAIL, "stale.container")), stats.strip()[-200:].replace("\n", " | "))

    code, preview = docker(*flask, "purge-inactive-accounts", "--dry-run", timeout=120)
    check("[保存期限] 預覽模式只報告筆數、不刪除", code == 0 and "預覽" in preview and "1 個" in preview and "未刪除" in preview, preview.strip())
    code, still = docker(*flask, "age-stats", timeout=120)
    check("[保存期限] 預覽之後帳號仍是 3 個", "合計：3" in still)

    code, purged = docker(*flask, "purge-inactive-accounts", timeout=120)
    check("[保存期限] 實際執行刪除 1 個閒置帳號，輸出沒有個資",
          code == 0 and "已刪除 1 個" in purged and "stale.container" not in purged, purged.strip())
    code, after = docker(*flask, "age-stats", timeout=120)
    check("[保存期限] 刪除後剩 2 個（管理員與會員都保留）", "合計：2" in after and "尚未完成年齡確認：0" in after, after.strip()[-120:].replace("\n", " | "))
    code, again = docker(*flask, "purge-inactive-accounts", timeout=120)
    check("[保存期限] 重複執行不會再刪任何帳號", code == 0 and "已刪除 0 個" in again, again.strip())
    # 逾時未完成年齡確認的清除（每小時的排程指令）：沒有期限記錄的帳號（管理員與舊帳號）不會被刪
    code, unconfirmed_preview = docker(*flask, "purge-unconfirmed-accounts", "--dry-run", timeout=120)
    check("[保存期限] purge-unconfirmed-accounts 預覽只報告筆數", code == 0 and "預覽" in unconfirmed_preview and "0 個" in unconfirmed_preview and "未刪除" in unconfirmed_preview, unconfirmed_preview.strip())
    code, unconfirmed = docker(*flask, "purge-unconfirmed-accounts", timeout=120)
    check("[保存期限] purge-unconfirmed-accounts 不會刪沒有期限記錄的帳號", code == 0 and "已刪除 0 個" in unconfirmed, unconfirmed.strip())
    code, kept = docker(*flask, "age-stats", timeout=120)
    check("[保存期限] 執行之後帳號仍是 2 個", "合計：2" in kept, kept.strip()[-120:].replace("\n", " | "))
    # 指令是另一個 docker exec 行程，結構化日誌寫在它自己的標準輸出，不在容器主行程的日誌裡
    check("[保存期限] 清除指令輸出了清除事件的日誌，且沒有被刪帳號的信箱",
          '"event":"account.inactive_purged"' in purged and "stale.container" not in purged, purged.strip()[-200:].replace("\n", " | "))


if __name__ == "__main__":
    sys.exit(main())
