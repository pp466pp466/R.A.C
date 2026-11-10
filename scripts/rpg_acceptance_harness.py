"""rpg-turn-engine 驗收用的測試替身（任務 6.1 與容器驗收共用）：假的 RPG 模型供應商，以及「建立全新玩家」的小幫手。

只用標準函式庫加應用程式本身（`create_app`），不含機密；只綁定 127.0.0.1。兩種用法：

1. 被匯入：本機驗收環境（專案之外的 `newprojet-acceptance/acceptance.py`）的假模型繼承 `FakeRpgModel`，小幫手呼叫 `fresh_player`；
2. 單獨執行（容器驗收在映像內使用）：
   `python rpg_acceptance_harness.py serve --model-port 9100 --helper-port 9101`（工作目錄須是應用程式根目錄，並已設定 `DATABASE_URL` 等環境變數）。

**假模型**（OpenAI 相容）依請求種類回應：串流（`stream: true`）是劇情或第 0 幕；系統指令含「記事簿記錄員」是記事簿抓取（rpg-notebook）；
含「狀態記錄員」是狀態呼叫；其餘（主角的模型層審查等）一律通過（子類可覆寫 `review`）。動作文字裡的關鍵字決定行為，約定見
`acceptance_rpg_turn.py`（劇情與狀態）與 `acceptance_rpg_notebook.py`（記事簿）的說明：
測試被擋一次、測試被擋兩次、測試中斷、測試慢速、測試結局；測試記事簿超量、測試記事簿放棄、測試記事簿違規、測試記事簿慢速。
`GET /_status` 回傳呼叫計數（串流、狀態、記事簿抓取、上游被關閉的次數）。

**記事簿抓取**看的是視窗內（抓取呼叫的資料區塊「最近的回合」）所有玩家行動，任何一輪帶關鍵字就生效：
預設回 3 條各約 26 字的條目（開頭是「第 N 至 M 輪」，所以不同視窗的條目不會被去重）；`超量` 回 5 條各 440 字（合計 2204 字：超過免費會員的
容量 2000、仍在容量加預備空間 2500 之內，記事簿因此「已滿」）；`放棄` 回 5 條各 520 字（合計 2604 字：超過 2500，精簡重試後仍超過而放棄）；
`違規` 在預設條目之外加一條必擋的句子（規則層整次放棄）；`慢速` 在回覆前先等 5 秒（讓串流先結束、抓取後完成）。

**小幫手**：`GET /fresh/rpg[?coins=N]` 建立一位全新、尚未年齡確認的玩家並回傳登入 cookie（`Set-Cookie`）與可開存檔的劇本版本
（`{"ok": true, "versions": {"endless": …, "custom_ending": …}}`）；`custom_ending` 是驗收專用的自訂結局內建劇本（`ensure_ending_script` 冪等建立）。
`coins` 是額外補給的**付費幣**（管理員調整，上限 5000，預設 0）：記事簿的驗收要走 11 輪以上（每輪 30 幣），首登的 200 幣不夠。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

EMAIL_DOMAIN = "loadtest.invalid"              # RFC 2606 保留網域：永遠不會有真實信箱
ENDLESS_SEED_KEY = "xianxia-qingyun"           # `flask seed-scripts` 建立的首發劇本（沒有結局）
ENDING_SEED_KEY = "accept-ending"              # 驗收專用：自訂結局的劇本（模型提議結局才會生效）

# 假劇情：句子都是通過審查的一般敘事；BAD_SENTENCE 一定被規則層擋下（未成年的性內容）
STORY_SENTENCES = ("清晨的海風吹過山門。", "守門的弟子向你點了點頭。", "你踏進了霧氣之中。")
OPENING_SENTENCES = ("晨霧之中，山門緩緩開啟。", "遠處傳來悠長的鐘聲。")
SLOW_SENTENCES = tuple(f"第 {n} 段緩慢的描述，山路繼續向前延伸。" for n in range(1, 13))
BAD_SENTENCE = "十歲的少女與他做愛。"

# 記事簿抓取（與 `acceptance_rpg_notebook.py` 約定的數字；字數對照免費會員：容量 2000、預備空間 500）
NOTEBOOK_SYSTEM_MARKER = "記事簿記錄員"              # 抓取呼叫的系統指令（rpg_notebook_prompt）自稱的角色
NOTEBOOK_CONDENSE_LABEL = "上次的條目"                # 只有「精簡重試」的使用者內容才有這個資料區塊
KW_NOTEBOOK_OVER, KW_NOTEBOOK_ABANDON = "測試記事簿超量", "測試記事簿放棄"
KW_NOTEBOOK_VIOLATION, KW_NOTEBOOK_SLOW = "測試記事簿違規", "測試記事簿慢速"
NOTEBOOK_DEFAULT_ENTRIES = ("主角在青雲宗山門認識了守門弟子。", "主角答應在山門前等候蘇長風長老。", "主角決定先沿著山路往上走。")
NOTEBOOK_OVER_ENTRY_CHARS = 440                     # 5 條 × 440 ＋ 4 個換行 ＝ 2204 字：已滿但不超過 2500
NOTEBOOK_TOO_LONG_ENTRY_CHARS = 520                 # 5 條 × 520 ＋ 4 個換行 ＝ 2604 字：超過 2500
NOTEBOOK_SLOW_SECONDS = 5.0
NOTEBOOK_FILLER = "山門前的石階共有九百九十九級，每一級都刻著一位先人的名字。"
FRESH_COINS_MAX = 5000

RPG_LOCK = threading.Lock()
RPG_STATS = {"stream_calls": 0, "state_calls": 0, "capture_calls": 0, "upstream_closed": 0}
RPG_ATTEMPTS: dict[str, int] = {}


class FakeRpgModel(BaseHTTPRequestHandler):
    """OpenAI 相容的假模型。只看 user 角色的訊息；不回顯任何內容。"""

    def log_message(self, fmt, *args):  # noqa: D401 - 靜音預設的存取日誌
        pass

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _sse(self, payload: dict | str) -> None:
        data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.wfile.write(b"data: " + data.encode("utf-8") + b"\n\n")
        self.wfile.flush()

    def do_GET(self) -> None:
        """`GET /_status`：呼叫計數（驗收腳本用來確認「重送不呼叫模型」與「客戶端斷線時應用程式關閉上游串流」）。"""
        if self.path.split("?")[0] != "/_status":
            return self._send(404, {"error": {"message": "not found"}})
        with RPG_LOCK:
            self._send(200, dict(RPG_STATS))

    def do_POST(self) -> None:
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._send(400, {"error": {"message": "bad json"}})
        messages = body.get("messages", [])
        text = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str))
        system = messages[0].get("content", "") if messages and isinstance(messages[0].get("content"), str) else ""
        if body.get("stream") is True:
            return self.stream_story(messages)
        if NOTEBOOK_SYSTEM_MARKER in system:
            return self.capture_call(text)
        if "狀態記錄員" in system:
            return self.state_call(text)
        return self.review(text, body)

    def review(self, text: str, body: dict) -> None:
        """其餘的非串流呼叫（主角的模型層審查等）：預設一律通過。"""
        verdict = {"verdict": "pass", "categories": []}
        self._send(200, {
            "id": "acceptance", "object": "chat.completion", "model": body.get("model", ""),
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(verdict)}}],
            "usage": {"prompt_tokens": 300, "completion_tokens": 20},
        })

    def stream_story(self, messages: list) -> None:
        """劇情或第 0 幕的串流。同一段動作文字第幾次被呼叫由計數決定（每段動作都帶唯一的標記，所以計數不會跨回合干擾）。"""
        last = ""
        for message in reversed(messages):
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                last = message["content"]
                break
        match = re.search(r"【本次行動】\n(.*?)\n【/本次行動】", last, re.S)
        action = match.group(1) if match else None            # 第 0 幕沒有行動
        with RPG_LOCK:
            RPG_STATS["stream_calls"] += 1
            if action is not None:
                RPG_ATTEMPTS[action] = RPG_ATTEMPTS.get(action, 0) + 1
            attempt = RPG_ATTEMPTS.get(action, 1) if action is not None else 1
        sentences: list[str] = list(OPENING_SENTENCES if action is None else STORY_SENTENCES)
        pause, cut = 0.3, False
        if action is not None:
            if "測試被擋兩次" in action or ("測試被擋一次" in action and attempt == 1):
                sentences.insert(1, BAD_SENTENCE)
            if "測試慢速" in action:
                sentences, pause = list(SLOW_SENTENCES), 1.0
            if "測試中斷" in action and attempt == 1:
                sentences, cut = sentences[:1], True
        label = "第 0 幕" if action is None else f"回合（第 {attempt} 次呼叫）"
        print(f"[假模型] 串流 {label} -> {len(sentences)} 句" + ("，中途斷線" if cut else ""), flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for sentence in sentences:
                self._sse({"choices": [{"index": 0, "delta": {"content": sentence}, "finish_reason": None}]})
                time.sleep(pause)
            if cut:
                self.close_connection = True
                return                                          # 沒有結束標記就關閉連線：應用程式視為上游中途斷線
            self._sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 900, "completion_tokens": 60}})
            self._sse("[DONE]")
        except OSError:                                         # 應用程式關閉了上游連線（客戶端斷線時應該如此）；BrokenPipe、ConnectionReset 都是 OSError
            with RPG_LOCK:
                RPG_STATS["upstream_closed"] += 1
            print("[假模型] 串流 -> 應用程式已關閉連線", flush=True)

    def state_call(self, user_text: str) -> None:
        with RPG_LOCK:
            RPG_STATS["state_calls"] += 1
        if "測試結局" in user_text:
            proposal: dict[str, Any] = {"elapsed_months": 2, "ending_proposed": True}
        else:
            proposal = {"elapsed_months": 1, "current_location": "青雲宗山門", "status_summary": "你剛踏入山門。"}
        print(f"[假模型] 狀態呼叫 -> {'結局' if proposal.get('ending_proposed') else '一般'}", flush=True)
        self._send(200, {
            "id": "acceptance", "object": "chat.completion", "model": "acceptance/fake-rpg",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(proposal, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 400, "completion_tokens": 30},
        })

    def capture_call(self, user_text: str) -> None:
        """記事簿抓取（非串流、結構化輸出 `{"entries": [...]}`）。計數在等待之前加，所以驗收腳本能看到「已收到、還在處理」。"""
        condense = NOTEBOOK_CONDENSE_LABEL in user_text
        entries, slow = capture_entries(user_text)
        with RPG_LOCK:
            RPG_STATS["capture_calls"] += 1
        print(f"[假模型] 記事簿抓取 -> {len(entries)} 條，{sum(len(e) for e in entries)} 字" + ("（精簡重試）" if condense else "") + ("，慢速" if slow else ""), flush=True)
        if slow:
            time.sleep(NOTEBOOK_SLOW_SECONDS)
        self._send(200, {
            "id": "acceptance", "object": "chat.completion", "model": "acceptance/fake-rpg",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({"entries": entries}, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 120},
        })


def capture_entries(user_text: str) -> tuple[list[str], bool]:
    """依抓取呼叫的使用者內容（「最近的回合」區塊的「第 N 輪／玩家行動：…／劇情：…」）決定回傳的條目；回傳（條目, 是否慢速）。
    純函式，不連網：單元測試直接呼叫。精簡重試時仍照同樣的關鍵字回應，所以 `放棄` 的精簡重試也會超過上限。"""
    turns = re.findall(r"第 (\d+) 輪\n玩家行動：(.*?)\n劇情：", user_text, re.S)
    numbers = [int(number) for number, _ in turns]
    actions = "\n".join(action for _, action in turns)
    label = f"第 {min(numbers)} 至 {max(numbers)} 輪" if numbers else "第 0 至 0 輪"
    entries = [f"{label}：{text}" for text in NOTEBOOK_DEFAULT_ENTRIES]
    if KW_NOTEBOOK_ABANDON in actions:
        entries = [_long_entry(label, index, NOTEBOOK_TOO_LONG_ENTRY_CHARS) for index in range(1, 6)]
    elif KW_NOTEBOOK_OVER in actions:
        entries = [_long_entry(label, index, NOTEBOOK_OVER_ENTRY_CHARS) for index in range(1, 6)]
    if KW_NOTEBOOK_VIOLATION in actions:
        entries.append(BAD_SENTENCE)
    return entries, KW_NOTEBOOK_SLOW in actions


def _long_entry(label: str, index: int, chars: int) -> str:
    """剛好 `chars` 字、各條不同（開頭帶視窗與序號）的長條目，內容是無害的敘述。"""
    head = f"{label}的設定{index}："
    return (head + NOTEBOOK_FILLER * (chars // len(NOTEBOOK_FILLER) + 1))[:chars]


# ---- 劇本與玩家 ---------------------------------------------------------------------------------------------------

def ending_script_content() -> dict[str, Any]:
    """驗收專用的自訂結局劇本（欄位齊全、全部在上限之內；與 `tests/support/script_data.valid()` 同一份形狀，映像裡沒有 tests，所以內嵌）。"""
    return {
        "title": "驗收：有結局的劇本", "summary": "少年入宗門，一步步走向自己的道。", "world_name": "乾歷朝仙俠世界",
        "content_tier": "safe", "background": "靈氣復甦的年代，宗門林立。\n凡人亦可修行。", "opening": "山門前，晨霧未散。",
        "rules": "境界分為煉氣、築基、金丹。", "tags": ["仙俠", "成長"], "calendar": {"name": "乾歷朝", "start_year": 1200},
        "ending_mode": "custom", "ending_text": "主角在山中安然離世。",
        "characters": [
            {"name": "林曉", "personality": "活潑好動", "age": 16, "appearance": "瘦小", "occupation": "學徒"},
            {"name": "蘇長風", "personality": "沉穩寡言", "age": 168, "appearance": "鬢髮微白", "occupation": "長老", "hidden_trait": "暗中守護青雲宗"},
        ],
        "locations": ["古劍山莊", "青雲宗山門"],
        "milestones": [{"title": "宗門大測", "trigger": {"kind": "age", "value": 20}}],
    }


def ensure_ending_script(app) -> bool:
    """冪等：確認有一份自訂結局的內建劇本。回傳這次是否新建立。"""
    from app.repositories.script_repository import ScriptRepository
    from app.services.script_spec import validate_script

    with app.extensions["db_session_factory"]() as session:
        repository = ScriptRepository(session)
        if repository.get_builtin(ENDING_SEED_KEY) is not None:
            return False
        result = validate_script(ending_script_content(), max_tier="adult")
        repository.publish_builtin(
            ENDING_SEED_KEY, content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=datetime.now(timezone.utc),
        )
        session.commit()
    return True


def coins_from_path(path: str) -> int:
    """小幫手網址的 `?coins=N`：額外補給的付費幣。不是整數、負數或缺值為 0，超過上限取上限。"""
    values = parse_qs(urlsplit(path).query).get("coins") or []
    try:
        return max(0, min(FRESH_COINS_MAX, int(values[0]))) if values else 0
    except ValueError:
        return 0


def fresh_player(app, coins: int = 0) -> tuple[str, dict[str, str | None]]:
    """建立一位全新、尚未年齡確認的玩家：回傳 `Set-Cookie` 的值與兩份可開存檔的劇本版本（對外識別碼）。

    `coins` 大於 0 時另補給這麼多**付費幣**（走管理員調整的同一條路，不受免費幣上限限制）；首登贈幣仍在年齡確認通過時才發放。"""
    from app.models.user import STATUS_ACTIVE, User
    from app.repositories.script_repository import ScriptRepository
    from app.repositories.user_repository import UserRepository
    from app.services.legal import CURRENT_TERMS_VERSION
    from app.services.normalization import canonical_email, normalize_email
    from app.services.registry import build_wallet_service

    now = datetime.now(timezone.utc)
    email = f"accept-rpg-{secrets.token_hex(5)}@{EMAIL_DOMAIN}"
    serializer = app.session_interface.get_signing_serializer(app)
    with app.extensions["db_session_factory"]() as session:
        user = User(
            email=normalize_email(email), email_canonical=canonical_email(email), display_name="驗收-rpg", password_hash=None,
            email_verified_at=now, status=STATUS_ACTIVE, terms_version=CURRENT_TERMS_VERSION, terms_accepted_at=now,
            age_deadline_at=now + timedelta(days=30),
        )
        UserRepository(session).add_user(user)
        session.flush()
        if coins > 0:
            build_wallet_service(session, clock=lambda: datetime.now(timezone.utc)).adjust(user.id, bucket="paid", delta=min(coins, FRESH_COINS_MAX))
        value = serializer.dumps({"uid": user.id, "tv": user.token_version})
        versions: dict[str, str | None] = {}
        for label, seed_key in (("endless", ENDLESS_SEED_KEY), ("custom_ending", ENDING_SEED_KEY)):
            found = ScriptRepository(session).get_builtin(seed_key)
            versions[label] = found[1].public_id if found and found[1] else None
        session.commit()
    return f"{app.config['SESSION_COOKIE_NAME']}={value}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax", versions


def make_fresh_handler(app):
    """只有 `GET /fresh/rpg` 的小幫手（本機驗收環境另有自己的切換身分頁面，會直接呼叫 `fresh_player`）。"""

    class Helper(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self) -> None:
            if self.path.split("?")[0] != "/fresh/rpg":
                self.send_error(404)
                return
            cookie, versions = fresh_player(app, coins=coins_from_path(self.path))
            raw = json.dumps({"ok": True, "versions": versions}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Helper


# ---- 單獨執行（容器驗收）------------------------------------------------------------------------------------------

def serve(model_port: int, helper_port: int) -> None:
    sys.path.insert(0, os.getcwd())                 # 工作目錄是應用程式根目錄（容器的 /app）
    from app import create_app

    app = create_app()
    ensure_ending_script(app)
    for port, handler in ((model_port, FakeRpgModel), (helper_port, make_fresh_handler(app))):
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"READY model={model_port} helper={helper_port}", flush=True)
    while True:
        time.sleep(3600)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="rpg-turn-engine 驗收用的假模型與全新玩家小幫手")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("serve", help="啟動假模型與小幫手（容器驗收用）")
    run.add_argument("--model-port", type=int, default=9100)
    run.add_argument("--helper-port", type=int, default=9101)
    args = parser.parse_args()
    serve(args.model_port, args.helper_port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
