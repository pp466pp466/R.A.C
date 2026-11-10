"""rpg-turn-engine（RC-2a）的 HTTP 驗收腳本（任務 6.1）。

    python scripts/acceptance_rpg_turn.py [--base-url http://localhost:5010] [--helper-url http://localhost:5001]
                                         [--model-url http://127.0.0.1:5002] [--server-log 路徑]

對一個**本機驗收環境**（應用程式、切換身分的小幫手、假的 RPG 模型供應商）走完一位新玩家的整段旅程，逐項輸出「通過」、「失敗」或「未驗證」，
最後輸出摘要（結束代碼：沒有失敗為 0，有失敗為 1，位址不是本機為 2）。與 `tests/test_rpg_flow.py` 檢查同一組行為，差別是這裡走真正的網路連線
（Werkzeug 開發伺服器、真實的串流分塊與標頭、模型端是另一個行程），而不是 Flask 測試客戶端。

- **只對本機執行**：位址的主機必須是 localhost、127.0.0.1 或 ::1，否則在送出任何請求之前拒絕；沒有任何參數可以繞過。腳本會建立玩家、花費遊戲幣。
- **腳本本身不含機密**：登入用的 cookie 由驗收環境的小幫手簽出（`GET <helper>/fresh/rpg`，每次建立一位全新、尚未年齡確認的玩家），
  只存在記憶體中，不輸出、不寫檔；輸出只有項目名稱、數字與結果，不含 cookie、權杖或回應全文。
- **與假模型的約定**：動作文字裡的關鍵字決定假模型的行為，每段動作另帶本次執行的唯一標記，所以假模型的計數不會跨回合或跨次執行干擾。

    測試被擋一次   第一次呼叫的劇情含必擋的句子（未成年的性內容），自動重生後的第二次呼叫正常
    測試被擋兩次   每次呼叫都含必擋的句子
    測試中斷       第一次呼叫送出一句就關閉連線（沒有結束標記）；同一則訊息重送時正常
    測試慢速       每秒一句、共 12 句（讓客戶端有時間中途斷線）
    測試結局       狀態呼叫提議結局（只有自訂結局的劇本才生效）

- 假模型的 `GET /_status`（`--model-url`）提供呼叫計數，用來確認「重送不呼叫模型」與「客戶端斷線時應用程式關閉上游串流」；
  連不上時這兩項標為「未驗證」。`--server-log` 是應用程式日誌的路徑：提供時掃描本次執行期間新增的內容，確認沒有劇情與行動文字。
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

PASS, FAIL, UNVERIFIED = "通過", "失敗", "未驗證"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
TURN_COST = 30
SIGNUP_GRANT = 200
AGE_RANGE_30 = "25_34"                      # 與假模型共同約定的文字（見模組說明）：不匯入應用程式程式碼，這個腳本只講 HTTP
STORY = "清晨的海風吹過山門。守門的弟子向你點了點頭。你踏進了霧氣之中。"
OPENING = "晨霧之中，山門緩緩開啟。遠處傳來悠長的鐘聲。"
BAD_SENTENCE = "十歲的少女與他做愛。"
EVENT_NAMES = frozenset({"meta", "token", "ping", "transition", "state", "done", "error"})


class TargetRefused(Exception):
    """位址不是本機。"""


def check_local(url: str, label: str) -> tuple[str, int]:
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS or parts.port is None or parts.path not in ("", "/") or parts.username:
        raise TargetRefused(f"{label} 必須是 http://localhost:<埠> 這種本機位址，收到的不是")
    return parts.hostname, parts.port


# ---- HTTP -----------------------------------------------------------------------------------------------------------

@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    text: str

    @property
    def json(self) -> dict[str, Any]:
        try:
            value = json.loads(self.text)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    @property
    def code(self) -> str | None:
        error = self.json.get("error")
        return error.get("code") if isinstance(error, dict) else None


@dataclass
class Turn:
    """一次串流請求的結果：開始前的錯誤是一般的 JSON（`status` 非 200、`events` 為空），串流開始後是事件清單。"""

    status: int
    headers: dict[str, str]
    events: list[tuple[str, dict[str, Any], float]] = field(default_factory=list)      # (名稱, 資料, 距送出請求的秒數)
    error_code: str | None = None
    aborted: bool = False
    malformed: list[str] = field(default_factory=list)
    ended_at: float | None = None       # 伺服器結束回應（讀到結尾）距送出請求的秒數；客戶端自己提早離開（stop_after）時為 None

    @property
    def names(self) -> list[str]:
        return [name for name, _, _ in self.events]

    def of(self, name: str) -> list[dict[str, Any]]:
        return [data for event, data, _ in self.events if event == name]

    def first_at(self, name: str) -> float | None:
        return next((seconds for event, _, seconds in self.events if event == name), None)

    @property
    def text(self) -> str:
        return "".join(str(data.get("text", "")) for data in self.of("token"))

    @property
    def done(self) -> dict[str, Any] | None:
        done = self.of("done")
        return done[0] if done else None


class Client:
    def __init__(self, base_url: str, label: str) -> None:
        self.host, self.port = check_local(base_url, label)
        self.netloc = urlsplit(base_url).netloc
        self.origin = f"http://{self.netloc}"
        self.cookies: dict[str, str] = {}
        self.csrf: str | None = None

    def _connect(self, method: str, path: str, body: dict | None, timeout: float) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        # Windows 上 `localhost` 會先試 IPv6（::1），開發伺服器只聽 127.0.0.1 時要等約 2 秒才退回：直接連 127.0.0.1，Host 與 Origin 仍用原本的主機名稱
        address = "127.0.0.1" if self.host == "localhost" else self.host
        connection = http.client.HTTPConnection(address, self.port, timeout=timeout)
        headers = {"Accept": "application/json, text/event-stream", "Host": self.netloc}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in self.cookies.items())
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if method != "GET":
            headers["Origin"] = self.origin
            if self.csrf:
                headers["X-CSRF-Token"] = self.csrf
        connection.request(method, path, payload, headers)
        response = connection.getresponse()
        for line in response.headers.get_all("Set-Cookie") or []:
            name, _, rest = line.split(";")[0].partition("=")
            if "Max-Age=0" in line:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = rest
        return connection, response

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 60) -> Reply:
        connection, response = self._connect(method, path, body, timeout)
        try:
            return Reply(response.status, {k.lower(): v for k, v in response.getheaders()}, response.read().decode("utf-8", "replace"))
        finally:
            connection.close()

    def load_csrf(self, path: str = "/age") -> bool:
        match = re.search(r'name="csrf-token" content="([^"]+)"', self.call("GET", path).text)
        self.csrf = match.group(1) if match else None
        return self.csrf is not None

    def stream(self, path: str, body: dict, *, stop_after: Callable[[str, dict], bool] | None = None, timeout: float = 200) -> Turn:
        """送出串流請求並逐個讀取事件。`stop_after(事件名稱, 資料)` 回傳 True 時立刻關閉連線（模擬玩家離開頁面）。"""
        started = time.monotonic()
        connection, response = self._connect("POST", path, body, timeout)
        turn = Turn(response.status, {k.lower(): v for k, v in response.getheaders()})
        try:
            if "text/event-stream" not in turn.headers.get("content-type", ""):
                turn.error_code = Reply(response.status, turn.headers, response.read().decode("utf-8", "replace")).code
                return turn
            name, lines = None, []
            while True:
                raw = response.readline()
                if not raw:
                    turn.ended_at = time.monotonic() - started
                    break
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    lines.append(line[6:])
                elif line == "":
                    if name is None and not lines:
                        continue
                    try:
                        data = json.loads("".join(lines))
                    except ValueError:
                        turn.malformed.append(name or "?")
                        data = {}
                    turn.events.append((name or "?", data, time.monotonic() - started))
                    if stop_after is not None and stop_after(name or "?", data):
                        turn.aborted = True
                        return turn
                    name, lines = None, []
                else:
                    turn.malformed.append(line[:20])
            return turn
        finally:
            connection.close()


# ---- 驗收 -----------------------------------------------------------------------------------------------------------

class Report:
    def __init__(self) -> None:
        self.results: list[tuple[str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        status = PASS if ok else FAIL
        print(f"{status}  {name}" + (f"  — {detail}" if detail and not ok else ""), flush=True)
        self.results.append((name, status))
        return ok

    def unverified(self, name: str, why: str) -> None:
        print(f"{UNVERIFIED}  {name}  — {why}", flush=True)
        self.results.append((name, UNVERIFIED))

    def note(self, text: str) -> None:
        print(f"      {text}", flush=True)

    def count(self, status: str) -> int:
        return sum(1 for _, result in self.results if result == status)


def fetch_model_status(model: Client | None) -> dict[str, int] | None:
    if model is None:
        return None
    try:
        reply = model.call("GET", "/_status", timeout=5)
    except OSError:
        return None
    return {k: v for k, v in reply.json.items() if isinstance(v, int)} if reply.status == 200 else None


class Acceptance:
    def __init__(self, app: Client, helper: Client, model: Client | None, report: Report, server_log: Path | None) -> None:
        self.app, self.helper, self.model, self.r = app, helper, model, report
        self.run_id = secrets.token_hex(3)
        self.server_log = server_log
        self.log_start = server_log.stat().st_size if server_log is not None and server_log.exists() else 0
        self.sequence = 0
        self.versions: dict[str, str | None] = {}
        self.timings: dict[str, float] = {}

    # ---- 小工具 -----------------------------------------------------------------------------------

    def total(self) -> int:
        return int(self.app.call("GET", "/api/wallet").json["wallet"]["total"])

    def action(self, keyword: str = "") -> str:
        self.sequence += 1
        return f"{keyword} 驗收 {self.run_id}-{self.sequence} 我沿著山路往上走。".strip()

    def play(self, save: str, msg: str, action: str, **kwargs: Any) -> Turn:
        return self.app.stream(f"/api/game/sessions/{save}/turns/stream", {"client_msg_id": msg, "action": action}, **kwargs)

    def turns(self, save: str) -> list[dict[str, Any]]:
        return list(self.app.call("GET", f"/api/game/sessions/{save}/turns").json.get("turns", []))

    def sequence_ok(self, turn: Turn, expected: list[str]) -> bool:
        """事件名稱依序符合 `expected`（`token` 與 `ping` 可重複或缺席，其餘必須照順序出現）。"""
        collapsed = [name for name in turn.names if name != "ping"]
        squeezed = [name for index, name in enumerate(collapsed) if not (name == "token" and index and collapsed[index - 1] == "token")]
        return squeezed == expected and not turn.malformed and all(name in EVENT_NAMES for name in turn.names)

    # ---- 旅程 ---------------------------------------------------------------------------------------

    def run(self) -> None:
        if not self.setup():
            return
        save = self.open_save("endless")
        if save is None:
            return
        self.opening(save)
        self.normal_turn(save)
        self.blocked_once(save)
        self.upstream_interrupt(save)
        self.client_disconnect(save)
        self.blocked_twice(save)
        self.reads(save)
        self.ending()
        self.out_of_coins(save)
        self.ledger()
        self.isolation(save)
        self.log_scan()

    def setup(self) -> bool:
        r = self.r
        reply = self.helper.call("GET", "/fresh/rpg")
        if not r.check("驗收環境的小幫手建立全新玩家（/fresh/rpg）", reply.status == 200 and bool(self.helper.cookies), f"{reply.status}"):
            return False
        self.app.cookies = dict(self.helper.cookies)
        self.versions = reply.json.get("versions", {})
        r.check("驗收環境有兩份可開存檔的劇本（無結局、自訂結局）", all(self.versions.get(k) for k in ("endless", "custom_ending")), str(self.versions))
        if not r.check("取得 CSRF 權杖", self.app.load_csrf()):
            return False
        today = date.today()
        try:
            born = today.replace(year=today.year - 30)
        except ValueError:
            born = today.replace(year=today.year - 30, day=28)
        early = self.app.call("GET", "/api/wallet")
        r.check("年齡確認之前不能用錢包（403 age_confirmation_required）", early.status == 403 and early.code == "age_confirmation_required", f"{early.status} {early.code}")
        confirmed = self.app.call("POST", "/api/auth/verify-age", {"birth_date": born.isoformat(), "age_range": AGE_RANGE_30, "nickname": "驗收玩家"})
        if not r.check("年齡確認通過（200）", confirmed.status == 200, f"{confirmed.status} {confirmed.text[:120]}"):
            return False
        r.check(f"首次登入的免費幣於年齡確認通過時發放（{SIGNUP_GRANT}）", self.total() == SIGNUP_GRANT, str(self.total()))
        return True

    def open_save(self, which: str) -> str | None:
        r = self.r
        version = self.versions.get("endless" if which == "endless" else "custom_ending")
        setup = self.app.call("GET", f"/api/sessions/setup/{version}")
        characters = setup.json.get("characters") or []
        if not r.check(f"創角資料（{which}）可讀、有可聯繫的人物", setup.status == 200 and bool(characters), f"{setup.status}"):
            return None
        card = {"name": "林舟", "personality": "沉默愛思考", "age": 19, "appearance": "高瘦，常穿灰衣"}
        anchor = {"kind": "character", "ref": characters[0]["name"], "text": "他是我少年時的舊識。"}
        before = self.total()
        started = self.app.call("POST", "/api/sessions", {"version_id": version, "protagonist": card, "anchor": anchor}, timeout=90)
        if not r.check(f"開存檔（{which}）→ 201", started.status == 201, f"{started.status} {started.text[:160]}"):
            return None
        r.check(f"開存檔不扣遊戲幣（{which}）", self.total() == before)
        return str(started.json["session"]["id"])

    def opening(self, save: str) -> None:
        r = self.r
        early = self.play(save, "too-early", self.action())
        r.check("第 0 幕完成之前送回合 → 409 opening_pending（JSON，不開串流）", early.status == 409 and early.error_code == "opening_pending", f"{early.status} {early.error_code}")
        before = self.total()
        first = self.app.stream(f"/api/game/sessions/{save}/opening/stream", {})
        r.check("第 0 幕：200、text/event-stream、no-cache、X-Accel-Buffering: no",
                first.status == 200 and "text/event-stream" in first.headers.get("content-type", "") and "no-cache" in first.headers.get("cache-control", "")
                and first.headers.get("x-accel-buffering") == "no", str(first.headers))
        r.check("第 0 幕：事件序列 meta → token… → done", self.sequence_ok(first, ["meta", "token", "done"]), str(first.names))
        r.check("第 0 幕：meta.kind 為 opening、done.opening_state 為 ready", (first.of("meta") or [{}])[0].get("kind") == "opening" and (first.done or {}).get("opening_state") == "ready")
        r.check("第 0 幕：文字與假模型的開場一致、模型的 JSON 或結束標記不外洩", first.text == OPENING and "[DONE]" not in first.text, first.text)
        r.check("第 0 幕不扣遊戲幣", self.total() == before and (first.done or {}).get("coins") == before)
        calls = (fetch_model_status(self.model) or {}).get("stream_calls")
        again = self.app.stream(f"/api/game/sessions/{save}/opening/stream", {})
        r.check("第 0 幕再次請求：回傳既有文字（meta.replayed）、不重抽", (again.of("meta") or [{}])[0].get("replayed") is True and again.text == OPENING, again.text)
        if calls is None:
            r.unverified("第 0 幕再次請求不呼叫模型", "連不上假模型的 /_status")
        else:
            r.check("第 0 幕再次請求不呼叫模型", (fetch_model_status(self.model) or {}).get("stream_calls") == calls)

    def normal_turn(self, save: str) -> None:
        r = self.r
        before = self.total()
        started = time.monotonic()
        turn = self.play(save, "m-1", self.action("正常"))
        elapsed = time.monotonic() - started
        r.check("回合：事件序列 meta → token… → state → done", self.sequence_ok(turn, ["meta", "token", "state", "done"]), str(turn.names))
        r.check("回合：done 為 complete、turn_number 1、coins 為剩餘餘額", turn.done == {"status": "complete", "turn_number": 1, "coins": before - TURN_COST}, str(turn.done))
        r.check("回合：劇情文字完整、沒有 JSON 原文", turn.text == STORY and "{" not in turn.text, turn.text)
        first, done_at = turn.first_at("token"), turn.first_at("done")
        r.check("回合：串流是逐步送達的（首字明顯早於結束）", first is not None and done_at is not None and done_at - first > 0.3, f"首字 {first} 結束 {done_at}")
        if first is not None:
            self.timings["first_token_s"] = first
        self.timings["turn_total_s"] = elapsed
        r.check("回合：扣 30 幣一次", self.total() == before - TURN_COST)
        state = (turn.of("state") or [{}])[0]
        r.check("回合：state 事件帶世界狀態摘要（地點、經過月數）", state.get("updated") is True and bool(state.get("location")), str(state))

    def blocked_once(self, save: str) -> None:
        r = self.r
        before = self.total()
        turn = self.play(save, "m-2", self.action("測試被擋一次"))
        retries = [d.get("will_retry") for d in turn.of("transition")]
        r.check("被擋一次：transition(will_retry=true) 後自動重生並完成", retries == [True] and (turn.done or {}).get("status") == "complete", f"{retries} {turn.done}")
        r.check("被擋一次：被擋的句子從未送給前端", BAD_SENTENCE not in json.dumps(turn.events, ensure_ascii=False))
        r.check("被擋一次：只扣一次（重生不再次扣）", self.total() == before - TURN_COST, f"{before} -> {self.total()}")

    def upstream_interrupt(self, save: str) -> None:
        r = self.r
        before = self.total()
        action = self.action("測試中斷")
        broken = self.play(save, "m-3", action)
        r.check("上游中途斷線：最後一個事件是 error、沒有 done", broken.names[-1:] == ["error"] and "done" not in broken.names, str(broken.names))
        r.check("上游中途斷線：預扣退回（餘額不變）", self.total() == before, f"{before} -> {self.total()}")
        view = next((t for t in self.turns(save) if t["client_msg_id"] == "m-3"), {})
        r.check("上游中途斷線：回合標為 interrupted、不含部分劇情", view.get("status") == "interrupted" and view.get("story") is None, str(view))
        retry = self.play(save, "m-3", action)
        r.check("以同一編號重送：attempt 為 2、完成並扣 30 幣", (retry.of("meta") or [{}])[0].get("attempt") == 2 and (retry.done or {}).get("status") == "complete" and self.total() == before - TURN_COST, f"{retry.names} {retry.done}")
        calls = fetch_model_status(self.model)
        replay = self.play(save, "m-3", action)
        r.check("已完成回合的重送：meta(replayed) → token → done，全文一致", replay.names == ["meta", "token", "done"] and (replay.of("meta") or [{}])[0].get("replayed") is True and replay.text == STORY, str(replay.names))
        r.check("已完成回合的重送不扣幣", self.total() == before - TURN_COST)
        if calls is None:
            r.unverified("已完成回合的重送不呼叫模型", "連不上假模型的 /_status")
        else:
            now = fetch_model_status(self.model) or {}
            r.check("已完成回合的重送不呼叫模型", now.get("stream_calls") == calls.get("stream_calls") and now.get("state_calls") == calls.get("state_calls"))

    def client_disconnect(self, save: str) -> None:
        r = self.r
        before = self.total()
        baseline = fetch_model_status(self.model)
        turn = self.play(save, "m-4", self.action("測試慢速"), stop_after=lambda name, data: name == "token")
        r.check("客戶端在第一個片段後關閉連線", turn.aborted and turn.names.count("token") == 1, str(turn.names))
        interrupted = None
        for _ in range(60):                                                  # 伺服器要在下一次寫入失敗時才察覺斷線
            interrupted = next((t for t in self.turns(save) if t["client_msg_id"] == "m-4"), None)
            if interrupted and interrupted.get("status") != "streaming":
                break
            time.sleep(0.5)
        r.check("客戶端斷線：回合標為 interrupted（伺服器停止生成並收尾）", bool(interrupted) and interrupted.get("status") == "interrupted" and interrupted.get("story") is None, str(interrupted))
        r.check("客戶端斷線：預扣退回（餘額不變）", self.total() == before, f"{before} -> {self.total()}")
        if baseline is None:
            r.unverified("客戶端斷線：應用程式關閉上游的模型串流", "連不上假模型的 /_status")
        else:
            closed = False
            for _ in range(40):
                closed = (fetch_model_status(self.model) or {}).get("upstream_closed", 0) > baseline.get("upstream_closed", 0)
                if closed:
                    break
                time.sleep(0.5)
            r.check("客戶端斷線：應用程式關閉上游的模型串流", closed)
        r.check("客戶端斷線後伺服器仍正常（/healthz 200）", self.app.call("GET", "/healthz").status == 200)

    def blocked_twice(self, save: str) -> None:
        r = self.r
        before = self.total()
        turn = self.play(save, "m-5", self.action("測試被擋兩次"))
        retries = [d.get("will_retry") for d in turn.of("transition")]
        r.check("被擋兩次：transition(true) → transition(false) → done(blocked)", retries == [True, False] and (turn.done or {}).get("status") == "blocked" and (turn.done or {}).get("turn_number") is None, f"{retries} {turn.done}")
        r.check("被擋兩次：被擋的句子從未送給前端", BAD_SENTENCE not in json.dumps(turn.events, ensure_ascii=False))
        r.check("被擋兩次：退款（餘額不變）", self.total() == before, f"{before} -> {self.total()}")
        view = next((t for t in self.turns(save) if t["client_msg_id"] == "m-5"), {})
        r.check("被擋兩次：回合標為 blocked、讀取時不含任何文字", view.get("status") == "blocked" and view.get("story") is None, str(view))

    def reads(self, save: str) -> None:
        r = self.r
        snapshot = self.app.call("GET", f"/api/game/sessions/{save}").json.get("session", {})
        r.check("讀取存檔：已完成 3 輪、第 0 幕文字、世界狀態有地點", snapshot.get("completed_turns") == 3 and (snapshot.get("opening") or {}).get("text") == OPENING and bool((snapshot.get("world") or {}).get("location")), str(snapshot)[:200])
        r.check("讀取存檔：世界狀態沒有 NPC 角色卡或隱藏特質", "entities" not in json.dumps(snapshot.get("world"), ensure_ascii=False) and "隱藏" not in json.dumps(snapshot.get("world"), ensure_ascii=False))
        listing = self.turns(save)
        complete = [t for t in listing if t["status"] == "complete"]
        r.check("回合清單：依序、完成的回合 1 至 3 含劇情", [t["turn_number"] for t in complete] == [1, 2, 3] and all(t["story"] == STORY for t in complete))
        r.check("回合清單：被擋與中斷的回合不含劇情", all(t["story"] is None for t in listing if t["status"] != "complete"))
        last_seq = listing[-1]["seq"] if listing else 0
        r.check("增量同步：since_seq 取最後一筆之後為空", self.app.call("GET", f"/api/game/sessions/{save}/turns?since_seq={last_seq}").json.get("turns") == [])

    def ending(self) -> None:
        r = self.r
        save = self.open_save("custom_ending")
        if save is None:
            return
        self.app.stream(f"/api/game/sessions/{save}/opening/stream", {})
        before = self.total()
        action = self.action("測試結局")
        turn = self.play(save, "end-1", action)
        r.check("結局：模型提議結局 → done.status 為 ended", (turn.done or {}).get("status") == "ended", str(turn.done))
        r.check("結局：結局回合照常扣 30 幣", self.total() == before - TURN_COST)
        over = self.play(save, "after-end", self.action())
        r.check("完結後不得再送新回合（409 session_ended）", over.status == 409 and over.error_code == "session_ended", f"{over.status} {over.error_code}")
        replay = self.play(save, "end-1", action)
        r.check("結局回合的重送仍拿得到結果（done.status 為 ended）", (replay.done or {}).get("status") == "ended", str(replay.names))
        snapshot = self.app.call("GET", f"/api/game/sessions/{save}").json.get("session", {})
        r.check("完結後仍可讀取（ended 為真、歷史完整）", snapshot.get("ended") is True and snapshot.get("completed_turns") == 1)

    def out_of_coins(self, save: str) -> None:
        r = self.r
        played = 0
        while self.total() >= TURN_COST and played < 12:
            played += 1
            if (self.play(save, f"drain-{played}", self.action("正常")).done or {}).get("status") != "complete":
                break
        balance = self.total()
        r.check(f"把餘額用到不足一輪（剩 {balance}）", 0 <= balance < TURN_COST)
        count = len(self.turns(save))
        broke = self.play(save, "broke", self.action())
        r.check("餘額不足：409 insufficient_coins（JSON，不開串流）", broke.status == 409 and broke.error_code == "insufficient_coins", f"{broke.status} {broke.error_code}")
        r.check("餘額不足：沒有留下回合列", len(self.turns(save)) == count)
        r.check("餘額不足：餘額沒有變成負數", self.total() == balance)
        checkin = self.app.call("POST", "/api/wallet/checkin", {})
        r.check("每日簽到補幣（+200）後可以再送回合", checkin.status == 200 and self.total() == balance + 200, f"{checkin.status} {checkin.text[:120]}")
        again = self.play(save, "broke", self.action("正常"))
        r.check("簽到後以同一編號重送成功", (again.done or {}).get("status") == "complete", str(again.names))

    def ledger(self) -> None:
        r = self.r
        entries: list[dict[str, Any]] = []
        before: int | None = None
        for _ in range(20):
            reply = self.app.call("GET", "/api/wallet/entries?limit=100" + (f"&before={before}" if before else ""))
            entries.extend(reply.json.get("entries", []))
            before = reply.json.get("next_before")
            if not before:
                break
        total = sum(e["free_delta"] + e["paid_delta"] for e in entries)
        r.check("帳本：每一筆異動加總等於目前餘額", total == self.total(), f"{total} vs {self.total()}")
        holds = [e for e in entries if e["kind"] == "turn_hold"]
        r.check("帳本：沒有仍在預扣中的項目", not any(e["pending"] for e in entries))
        r.check("帳本：結算的回合預扣每筆 30 幣", bool(holds) and all(e["free_delta"] + e["paid_delta"] == -TURN_COST for e in holds))
        kinds = sorted({e["kind"] for e in entries})
        self.r.note(f"帳本 {len(entries)} 筆，種類：{', '.join(kinds)}；結算的回合 {len(holds)} 個")

    def isolation(self, save: str) -> None:
        r = self.r
        anonymous = Client(f"http://{self.app.host}:{self.app.port}", "base-url")
        r.check("未登入讀取存檔 → 401", anonymous.call("GET", f"/api/game/sessions/{save}").status == 401)
        stranger_helper = Client(f"http://{self.helper.host}:{self.helper.port}", "helper-url")
        stranger_helper.call("GET", "/fresh/rpg")
        stranger = Client(f"http://{self.app.host}:{self.app.port}", "base-url")
        stranger.cookies = dict(stranger_helper.cookies)
        stranger.load_csrf()
        stranger.call("POST", "/api/auth/verify-age", {"birth_date": date.today().replace(year=date.today().year - 30, day=min(date.today().day, 28)).isoformat(), "age_range": AGE_RANGE_30, "nickname": "旁人"})
        peek = stranger.call("GET", f"/api/game/sessions/{save}")
        r.check("他人讀取我的存檔 → 404（與不存在相同）", peek.status == 404, str(peek.status))
        steal = stranger.stream(f"/api/game/sessions/{save}/turns/stream", {"client_msg_id": "x", "action": "偷看"})
        r.check("他人對我的存檔送回合 → 404、不扣他的幣", steal.status == 404 and int(stranger.call("GET", "/api/wallet").json["wallet"]["total"]) == SIGNUP_GRANT)

    def log_scan(self) -> None:
        r = self.r
        if self.server_log is None or not self.server_log.exists():
            r.unverified("應用程式日誌沒有劇情與行動文字", "未提供 --server-log")
            return
        with self.server_log.open("rb") as handle:
            handle.seek(self.log_start)
            text = handle.read().decode("utf-8-sig", "replace")
        leaked = [needle for needle in (STORY, "清晨的海風", OPENING, BAD_SENTENCE, self.run_id, "我沿著山路往上走") if needle in text]
        r.check(f"應用程式日誌（本次新增的 {len(text)} 字元）沒有劇情與行動文字", not leaked, f"找到：{leaked}")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="rpg-turn-engine 的 HTTP 驗收（只對本機驗收環境）")
    parser.add_argument("--base-url", default="http://localhost:5010", help="應用程式")
    parser.add_argument("--helper-url", default="http://localhost:5001", help="驗收環境的切換身分小幫手")
    parser.add_argument("--model-url", default="http://127.0.0.1:5002", help="假的 RPG 模型（計數端點 /_status）")
    parser.add_argument("--server-log", type=Path, default=None, help="應用程式日誌的路徑（選填）")
    args = parser.parse_args(argv)
    try:
        app, helper = Client(args.base_url, "--base-url"), Client(args.helper_url, "--helper-url")
        model = Client(args.model_url, "--model-url")
    except TargetRefused as error:
        print(f"拒絕執行：{error}", file=sys.stderr)
        return 2
    report = Report()
    started = time.monotonic()
    try:
        Acceptance(app, helper, model, report, args.server_log).run()
    except OSError as error:
        report.check("連線到驗收環境", False, f"{type(error).__name__}（驗收環境是否已啟動？）")
    print(f"\n摘要：通過 {report.count(PASS)}、失敗 {report.count(FAIL)}、未驗證 {report.count(UNVERIFIED)}（{time.monotonic() - started:.1f} 秒）")
    return 1 if report.count(FAIL) else 0


if __name__ == "__main__":
    sys.exit(main())
