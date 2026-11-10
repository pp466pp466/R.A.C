"""rpg-notebook（RC-2b）的 HTTP 驗收腳本（任務 5.1）。

    python scripts/acceptance_rpg_notebook.py [--base-url http://localhost:5010] [--helper-url http://localhost:5001]
                                             [--model-url http://127.0.0.1:5002] [--server-log 路徑] [--only basic,full,abandon,rules,eof] [--assert-eof]

對一個**本機驗收環境**（應用程式、切換身分的小幫手、假的 RPG 模型供應商；啟動方式同 `acceptance_rpg_turn.py`）走完記事簿的四個案例，
逐項輸出「通過」、「失敗」或「未驗證」，最後輸出摘要（結束代碼：沒有失敗為 0，有失敗為 1，位址不是本機為 2）。
與 `tests/test_rpg_notebook_flow.py`、`tests/test_notebook_api.py` 檢查同一組行為，差別是這裡走真正的網路連線
（Werkzeug 開發伺服器、真實的串流與連線關閉、模型端是另一個行程）。

- **只對本機執行**、腳本本身不含機密、輸出只有項目名稱、數字與結果（同 `acceptance_rpg_turn.py`）。
- 每個案例各用一位**全新玩家**（小幫手 `GET /fresh/rpg?coins=N` 補給付費幣：記事簿要走 11 輪以上，每輪 30 幣，首登的 200 幣不夠），
  所以案例彼此獨立，可用 `--only` 單獨重跑；總共約 75 輪，在驗收環境約 2 分鐘。
- **與假模型的約定**：動作文字裡的關鍵字決定抓取呼叫的行為，只要視窗（第 1 至 10 輪）內任何一輪帶關鍵字就生效。

    測試記事簿慢速   抓取呼叫先等 5 秒才回覆（讓串流先結束、抓取後完成）
    測試記事簿超量   回 5 條各 440 字（合計 2204 字）：超過免費會員的容量 2000、仍在容量加預備空間 2500 之內 → 記事簿「已滿」
    測試記事簿放棄   回 5 條各 520 字（合計 2604 字）：超過 2500，精簡重試後仍超過 → 放棄這個視窗
    測試記事簿違規   在預設條目之外多一條必擋的句子（未成年的性內容）→ 規則層整次放棄
    （沒有關鍵字）   回 3 條各約 26 字、開頭為「第 1 至 10 輪」

- 五個案例：
    basic    11 輪：第 10 輪後記事簿仍空 → 第 11 輪的 `done` 先到、記事簿稍後才有 3 條（慢速）→ 帳本與遊戲幣沒有異動；
             接著玩家編輯與版本衝突、太長、規則層命中、清空，以及未登入、缺 CSRF、他人（404）
    full     31 輪：超量 → 已滿 → 原文再儲存被拒 → 已滿時第 21 輪不呼叫模型 → 刪到容量以內才能儲存 → 第 31 輪 AI 恢復更新
    abandon  11 輪：精簡重試後仍超過上限 → 放棄，記事簿不變
    rules    11 輪：條目含必擋的句子 → 放棄，記事簿不變
    eof      11 輪（慢速）：第 11 輪不提早離開，量 done 到伺服器結束回應的間隔與結束當下的記事簿（design D1 的前提：gthread 先送出結束 chunk、
             再執行關閉回呼）。預設只記錄數字；`--assert-eof`（容器內的 gunicorn 驗收）才要求間隔小於 1 秒、結束當下記事簿仍是空的
- `--server-log` 是應用程式日誌的路徑：提供時，腳本讀取本次新增的 JSON 行，確認 `rpg.notebook_*` 事件、模型呼叫的用途為 `rpg_notebook`
  （帳本的用途）、沒有 `rpg.notebook_failed`，並掃描整段日誌找不到任何記事簿文字；未提供時這些項目標為「未驗證」。
  假模型的 `GET /_status`（`--model-url`）提供抓取呼叫計數，連不上時「呼叫次數」類的項目標為「未驗證」。
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from acceptance_rpg_turn import (
    AGE_RANGE_30, BAD_SENTENCE, FAIL, PASS, SIGNUP_GRANT, TURN_COST, UNVERIFIED, Client, Report, TargetRefused, Turn, fetch_model_status,
)

KW_SLOW, KW_OVER, KW_ABANDON, KW_VIOLATION = "測試記事簿慢速", "測試記事簿超量", "測試記事簿放棄", "測試記事簿違規"
SLOW_SECONDS = 5.0                          # 與假模型的約定（rpg_acceptance_harness.NOTEBOOK_SLOW_SECONDS）
CAPACITY, RESERVE = 2000, 500               # 免費會員
DEFAULT_ENTRIES = 3
WINDOW = 10
EDIT_LINE = "玩家補充：主角怕黑，睡覺一定要點燈。"
FILLER = "山門前的石階共有九百九十九級"       # 假模型長條目的內容
MISSING_ID = "00000000-0000-4000-8000-000000000000"
CASES = ("basic", "full", "abandon", "rules", "eof")
EOF_LIMIT_SECONDS = 1.0                     # gthread 先送出結束 chunk、再執行關閉回呼：done 到結束回應的間隔必須小於這個值（design D1）
LEDGER_KINDS = frozenset({"signup_grant", "admin_adjust", "turn_hold"})


@dataclass
class Player:
    client: Client
    versions: dict[str, str | None]
    coins: int                              # 小幫手額外補給的付費幣


class ServerLog:
    """應用程式日誌（JSON 行）。沒有提供路徑或檔案不存在時 `available` 為假，所有讀取回空。"""

    def __init__(self, path: Path | None) -> None:
        self.path = path if path is not None and path.exists() else None

    @property
    def available(self) -> bool:
        return self.path is not None

    def mark(self) -> int:
        return self.path.stat().st_size if self.path is not None else 0

    def text(self, since: int) -> str:
        if self.path is None:
            return ""
        with self.path.open("rb") as handle:
            handle.seek(since)
            return handle.read().decode("utf-8-sig", "replace")

    def events(self, since: int) -> list[dict[str, Any]]:
        found = []
        for line in self.text(since).splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                found.append(value)
        return found

    def named(self, since: int, name: str) -> list[dict[str, Any]]:
        return [event for event in self.events(since) if event.get("event") == name]


class NotebookAcceptance:
    def __init__(self, base_url: str, helper_url: str, model: Client | None, report: Report, log: ServerLog, *, assert_eof: bool = False) -> None:
        self.base_url, self.helper_url, self.model, self.r, self.log = base_url, helper_url, model, report, log
        self.assert_eof = assert_eof
        self.run_id = secrets.token_hex(3)
        self.sequence = 0
        self.log_start = log.mark()
        self.watched: set[str] = {BAD_SENTENCE, "我沿著山路往上走", self.run_id}      # 整段日誌都不得出現的文字

    # ---- 小工具 -----------------------------------------------------------------------------------

    def action(self, keyword: str = "") -> str:
        self.sequence += 1
        return f"{keyword} 驗收 {self.run_id}-{self.sequence} 我沿著山路往上走。".strip()

    def total(self, player: Player) -> int:
        return int(player.client.call("GET", "/api/wallet").json["wallet"]["total"])

    def capture_calls(self) -> int | None:
        return (fetch_model_status(self.model) or {}).get("capture_calls")

    def notebook(self, player: Player, save: str) -> dict[str, Any]:
        notebook = player.client.call("GET", f"/api/game/sessions/{save}/notebook").json.get("notebook") or {}
        for line in str(notebook.get("text", "")).splitlines():
            if len(line) >= 8:
                self.watched.add(line)
        return notebook

    def put(self, player: Player, save: str, text: str, version: int):
        if len(text) >= 8:
            self.watched.add(text.splitlines()[0] if "\n" in text else text)
        return player.client.call("PUT", f"/api/game/sessions/{save}/notebook", {"text": text, "expected_version": version})

    def wait_until(self, predicate: Callable[[], bool], seconds: float = 40.0, step: float = 0.3) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(step)
        return predicate()

    def wait_for_capture(self, base_calls: int | None, calls: int, log_since: int, event: str) -> None:
        """等一次抓取結束：先等假模型收到 `calls` 次呼叫，再等日誌出現 `event`（沒有日誌時多等 2.5 秒讓資料庫寫入完成）。"""
        if base_calls is not None:
            self.wait_until(lambda: (self.capture_calls() or 0) >= base_calls + calls)
        if self.log.available:
            self.wait_until(lambda: bool(self.log.named(log_since, event)), seconds=20)
        else:
            time.sleep(2.5)

    # ---- 玩家與存檔 ---------------------------------------------------------------------------------

    def fresh(self, coins: int) -> Player | None:
        r = self.r
        helper = Client(self.helper_url, "--helper-url")
        reply = helper.call("GET", f"/fresh/rpg?coins={coins}")
        app = Client(self.base_url, "--base-url")
        app.cookies = dict(helper.cookies)
        versions = reply.json.get("versions") or {}
        if not r.check(f"小幫手建立全新玩家（補給 {coins} 付費幣）", reply.status == 200 and bool(app.cookies) and bool(versions.get("endless")), f"{reply.status}"):
            return None
        if not r.check("取得 CSRF 權杖", app.load_csrf()):
            return None
        today = date.today()
        try:
            born = today.replace(year=today.year - 30)
        except ValueError:
            born = today.replace(year=today.year - 30, day=28)
        confirmed = app.call("POST", "/api/auth/verify-age", {"birth_date": born.isoformat(), "age_range": AGE_RANGE_30, "nickname": "驗收玩家"})
        if not r.check("年齡確認通過（200）", confirmed.status == 200, f"{confirmed.status} {confirmed.text[:120]}"):
            return None
        player = Player(app, versions, coins)
        balance = self.total(player)
        r.check(f"餘額為首登贈幣加補給（{SIGNUP_GRANT} + {coins}）", balance == SIGNUP_GRANT + coins, f"{balance}（驗收環境的小幫手是否支援 ?coins=）")
        return player

    def start_save(self, player: Player) -> str | None:
        r = self.r
        version = player.versions["endless"]
        setup = player.client.call("GET", f"/api/sessions/setup/{version}")
        characters = setup.json.get("characters") or []
        if not r.check("創角資料可讀、有可聯繫的人物", setup.status == 200 and bool(characters), f"{setup.status}"):
            return None
        card = {"name": "林舟", "personality": "沉默愛思考", "age": 19, "appearance": "高瘦，常穿灰衣"}
        anchor = {"kind": "character", "ref": characters[0]["name"], "text": "他是我少年時的舊識。"}
        started = player.client.call("POST", "/api/sessions", {"version_id": version, "protagonist": card, "anchor": anchor}, timeout=90)
        if not r.check("開存檔 → 201", started.status == 201, f"{started.status} {started.text[:160]}"):
            return None
        save = str(started.json["session"]["id"])
        opening = player.client.stream(f"/api/game/sessions/{save}/opening/stream", {})
        r.check("第 0 幕完成（done.opening_state 為 ready）", (opening.done or {}).get("opening_state") == "ready", str(opening.done))
        return save

    def play(self, player: Player, save: str, number: int, keyword: str = "", **kwargs: Any) -> Turn:
        body = {"client_msg_id": f"n-{number}", "action": self.action(keyword)}
        return player.client.stream(f"/api/game/sessions/{save}/turns/stream", body, **kwargs)

    def play_turns(self, player: Player, save: str, first: int, last: int, keywords: dict[int, str] | None = None) -> bool:
        problems = []
        for number in range(first, last + 1):
            turn = self.play(player, save, number, (keywords or {}).get(number, ""))
            done = turn.done or {}
            if done.get("status") != "complete" or done.get("turn_number") != number:
                problems.append(f"{number}:{done.get('status') or turn.error_code or turn.status}")
        return self.r.check(f"第 {first} 至 {last} 輪全部完成", not problems, "、".join(problems))

    def ledger_is_clean(self, player: Player, turns: int, label: str) -> None:
        """遊戲幣只有首登贈幣、補給與每輪 30 幣：抓取不產生任何異動（帳本沒有第四種項目、回合預扣恰好 `turns` 筆）。"""
        entries: list[dict[str, Any]] = []
        before: int | None = None
        for _ in range(20):
            reply = player.client.call("GET", "/api/wallet/entries?limit=100" + (f"&before={before}" if before else ""))
            entries.extend(reply.json.get("entries", []))
            before = reply.json.get("next_before")
            if not before:
                break
        holds = [e for e in entries if e["kind"] == "turn_hold"]
        kinds = {e["kind"] for e in entries}
        balance = self.total(player)
        self.r.check(f"{label}：遊戲幣只有贈幣、補給與 {turns} 筆回合預扣（抓取不扣也不退）",
                     len(holds) == turns and kinds <= LEDGER_KINDS and all(e["free_delta"] + e["paid_delta"] == -TURN_COST for e in holds)
                     and not any(e["pending"] for e in entries) and balance == SIGNUP_GRANT + player.coins - turns * TURN_COST,
                     f"預扣 {len(holds)}、種類 {sorted(kinds)}、餘額 {balance}")

    def log_checks(self, since: int, label: str, *, expect: dict[str, int], notebook_calls: int) -> None:
        """日誌：`expect` 是各 `rpg.notebook_*` 事件的筆數；模型呼叫（用途 `rpg_notebook`，即帳本的用途）共 `notebook_calls` 次且都成功；沒有失敗事件。"""
        if not self.log.available:
            self.r.unverified(f"{label}：日誌的記事簿事件與模型呼叫用途", "未提供 --server-log")
            return
        names = ("captured", "skipped", "abandoned", "failed", "saved", "save_rejected")
        counts = {name: len(self.log.named(since, f"rpg.notebook_{name}")) for name in names}
        wanted = {name: expect.get(name, 0) for name in names}
        self.r.check(f"{label}：日誌的記事簿事件筆數符合（{', '.join(f'{k}={v}' for k, v in wanted.items() if v)}，其餘 0）", counts == wanted, str(counts))
        calls = [e for e in self.log.named(since, "llm.call") if e.get("kind") == "rpg_notebook"]
        self.r.check(f"{label}：模型呼叫的用途 rpg_notebook 共 {notebook_calls} 次、結果皆為 ok", len(calls) == notebook_calls and all(e.get("outcome") == "ok" for e in calls),
                     str([(e.get("outcome"), e.get("model")) for e in calls]))

    # ---- 案例 ---------------------------------------------------------------------------------------

    def run(self, only: tuple[str, ...]) -> None:
        for name in CASES:
            if name in only:
                print(f"\n== 案例 {name} ==", flush=True)
                getattr(self, f"case_{name}")()
        self.log_scan()

    def case_basic(self) -> None:
        r = self.r
        player = self.fresh(coins=11 * TURN_COST)
        save = self.start_save(player) if player else None
        if player is None or save is None:
            return
        since, base = self.log.mark(), self.capture_calls()
        if not self.play_turns(player, save, 1, 10, {2: KW_SLOW}):
            return
        empty = self.notebook(player, save)
        r.check("第 10 輪完成後記事簿仍是空的（版本 1、0 字、容量 2000）", (empty.get("text"), empty.get("version"), empty.get("length"), empty.get("capacity")) == ("", 1, 0, CAPACITY), str(empty))
        snapshot = player.client.call("GET", f"/api/game/sessions/{save}").json.get("session", {})
        r.check("存檔快照的記事簿摘要不含全文", snapshot.get("notebook") == {"version": 1, "length": 0, "capacity": CAPACITY, "full": False}, str(snapshot.get("notebook")))
        if base is None:
            r.unverified("第 10 輪之前沒有任何抓取呼叫", "連不上假模型的 /_status")
        else:
            r.check("第 10 輪之前沒有任何抓取呼叫", self.capture_calls() == base)

        # 第 11 輪：done 一到就離開頁面（同時證明「done 之後立即斷線，抓取仍然完成」）；慢速抓取要 5 秒，所以 done 當下記事簿一定還是空的
        started = time.monotonic()
        eleventh = self.play(player, save, 11, stop_after=lambda name, data: name == "done")
        r.check("第 11 輪：done 為 complete、turn_number 11", (eleventh.done or {}).get("status") == "complete" and (eleventh.done or {}).get("turn_number") == 11, str(eleventh.done))
        done_at = started + (eleventh.first_at("done") or 0.0)
        right_after = self.notebook(player, save)
        r.check("串流在抓取完成之前就結束：done 當下記事簿還是空的", right_after.get("length") == 0 and right_after.get("version") == 1, str(right_after.get("version")))
        appeared = self.wait_until(lambda: (self.notebook(player, save).get("length") or 0) > 0)
        delay = time.monotonic() - done_at
        r.check(f"客戶端在 done 之後離開，抓取仍然完成（{delay:.1f} 秒後記事簿有內容）", appeared)
        r.check(f"抓取在 done 之後才完成（慢速案例：至少 {SLOW_SECONDS - 1:.0f} 秒後）", appeared and delay >= SLOW_SECONDS - 1.0, f"{delay:.1f} 秒")
        notebook = self.notebook(player, save)
        lines = str(notebook.get("text", "")).split("\n")
        r.check(f"記事簿追加 {DEFAULT_ENTRIES} 條、版本 2、字數與全文一致、未滿",
                len(lines) == DEFAULT_ENTRIES and notebook.get("version") == 2 and notebook.get("length") == len(notebook.get("text", "")) and notebook.get("full") is False, str(notebook.get("version")))
        r.check(f"每條都在 {50} 字之內、開頭是「第 1 至 {WINDOW} 輪」（抓的是第 1 至 10 輪）", all(0 < len(line) <= 50 and line.startswith(f"第 1 至 {WINDOW} 輪：") for line in lines), str([len(line) for line in lines]))
        again = player.client.call("GET", f"/api/game/sessions/{save}").json.get("session", {}).get("notebook")
        r.check("存檔快照的摘要與檢視一致", again == {"version": 2, "length": notebook.get("length"), "capacity": CAPACITY, "full": False}, str(again))
        if base is None:
            r.unverified("第 11 輪之後恰好一次抓取呼叫", "連不上假模型的 /_status")
        else:
            r.check("第 11 輪之後恰好一次抓取呼叫", self.capture_calls() == base + 1, f"{self.capture_calls()} vs {base}")
        self.wait_for_capture(base, 1, since, "rpg.notebook_captured")
        self.ledger_is_clean(player, 11, "基本案例")
        if self.log.available:
            captured = self.log.named(since, "rpg.notebook_captured")
            expected = (("boundary", 10), ("entries", DEFAULT_ENTRIES), ("version", 2), ("over_entries", 0), ("over_chars", 0), ("total", notebook.get("length")))
            r.check("日誌：boundary 10、3 條、版本 2、沒有超出提示詞要求（over 0）、總字數等於記事簿字數",
                    len(captured) == 1 and all(captured[0].get(key) == value for key, value in expected), str(captured))
        else:
            r.unverified("日誌：記事簿追加事件的欄位", "未提供 --server-log")
        self.log_checks(since, "基本案例", expect={"captured": 1}, notebook_calls=1)
        self.edits(player, save, notebook, base)

    def edits(self, player: Player, save: str, notebook: dict[str, Any], base: int | None) -> None:
        r = self.r
        since = self.log.mark()
        version, text = int(notebook["version"]), str(notebook["text"])
        reply = player.client.call("GET", f"/api/game/sessions/{save}/notebook")
        r.check("檢視：Cache-Control 為 no-store", "no-store" in reply.headers.get("cache-control", ""), reply.headers.get("cache-control", ""))
        edited = text + "\n" + EDIT_LINE
        saved = self.put(player, save, edited, version)
        body = saved.json.get("notebook") or {}
        r.check("玩家編輯：200、版本加 1、全文與字數一致", saved.status == 200 and body.get("version") == version + 1 and body.get("text") == edited and body.get("length") == len(edited), f"{saved.status} {saved.code}")
        r.check("編輯後再次檢視拿到同一份全文", self.notebook(player, save).get("text") == edited)
        stale = self.put(player, save, "另一個裝置寫的另一份記事簿。", version)
        latest = stale.json.get("notebook") or {}
        r.check("版本衝突：409 notebook_conflict、附後端最新的全文與版本", stale.status == 409 and stale.code == "notebook_conflict" and latest.get("text") == edited and latest.get("version") == version + 1, f"{stale.status} {stale.code}")
        r.check("版本衝突沒有覆蓋後端的內容", self.notebook(player, save).get("text") == edited)
        long = self.put(player, save, "字" * (CAPACITY + 1), version + 1)
        r.check(f"太長：422 notebook_too_long、回報字數 {CAPACITY + 1} 與容量 {CAPACITY}（玩家沒有預備空間）", long.status == 422 and long.code == "notebook_too_long" and long.json.get("length") == CAPACITY + 1 and long.json.get("capacity") == CAPACITY, f"{long.status} {long.code}")
        blocked = self.put(player, save, text + "\n" + BAD_SENTENCE, version + 1)
        r.check("規則層命中：422 notebook_blocked、只回類別、不回顯內容", blocked.status == 422 and blocked.code == "notebook_blocked" and bool(blocked.json.get("categories")) and BAD_SENTENCE not in blocked.text, f"{blocked.status} {blocked.code}")
        r.check("被拒的儲存不改變記事簿", self.notebook(player, save).get("text") == edited)
        # 授權邊界
        anonymous = Client(self.base_url, "--base-url")
        r.check("未登入檢視 → 401", anonymous.call("GET", f"/api/game/sessions/{save}/notebook").status == 401)
        no_csrf = Client(self.base_url, "--base-url")
        no_csrf.cookies = dict(player.client.cookies)
        missing_token = no_csrf.call("PUT", f"/api/game/sessions/{save}/notebook", {"text": "x", "expected_version": version + 1})
        r.check("缺 CSRF 權杖的儲存 → 403 csrf_failed", missing_token.status == 403 and missing_token.code == "csrf_failed", f"{missing_token.status} {missing_token.code}")
        stranger = self.fresh(coins=0)
        if stranger is not None:
            def lookup(method: str, ident: str, payload: dict | None = None):
                return stranger.client.call(method, f"/api/game/sessions/{ident}/notebook", payload)

            payload = {"text": "偷改的內容", "expected_version": 1}
            mine, missing = lookup("GET", save), lookup("GET", MISSING_ID)
            r.check("他人檢視我的記事簿 → 404（與不存在逐字相同）", mine.status == 404 and mine.text == missing.text, f"{mine.status}")
            mine, missing = lookup("PUT", save, payload), lookup("PUT", MISSING_ID, payload)
            r.check("他人儲存我的記事簿 → 404（與不存在逐字相同），且沒有改動", mine.status == 404 and mine.text == missing.text and self.notebook(player, save).get("text") == edited, f"{mine.status}")
        if base is None:
            r.unverified("編輯與被拒的儲存都不呼叫模型", "連不上假模型的 /_status")
        else:
            r.check("編輯與被拒的儲存都不呼叫模型", self.capture_calls() == base + 1)
        cleared = self.put(player, save, "", version + 1)
        r.check("清空：200、0 字、版本再加 1", cleared.status == 200 and (cleared.json.get("notebook") or {}).get("length") == 0 and (cleared.json.get("notebook") or {}).get("version") == version + 2, f"{cleared.status} {cleared.code}")
        self.log_checks(since, "編輯", expect={"saved": 2, "save_rejected": 3}, notebook_calls=0)
        if self.log.available:
            reasons = sorted(e.get("reason") for e in self.log.named(since, "rpg.notebook_save_rejected"))
            r.check("日誌：被拒的原因為 blocked、conflict、too_long", reasons == ["blocked", "conflict", "too_long"], str(reasons))

    def case_full(self) -> None:
        r = self.r
        player = self.fresh(coins=31 * TURN_COST)
        save = self.start_save(player) if player else None
        if player is None or save is None:
            return
        since, base = self.log.mark(), self.capture_calls()
        if not (self.play_turns(player, save, 1, 10, {3: KW_OVER}) and self.play_turns(player, save, 11, 11)):
            return
        self.wait_until(lambda: (self.notebook(player, save).get("length") or 0) > 0)
        full = self.notebook(player, save)
        length, version = int(full.get("length") or 0), full.get("version")
        r.check(f"超量：5 條完整寫入，總長 {length} 字超過容量 {CAPACITY}、在容量加預備空間 {CAPACITY + RESERVE} 之內，記事簿「已滿」",
                CAPACITY < length <= CAPACITY + RESERVE and full.get("full") is True and full.get("reserve") == RESERVE and version == 2 and len(str(full.get("text")).split("\n")) == 5, f"{length} {full}"[:200])
        r.check("存檔快照的摘要標示已滿", (player.client.call("GET", f"/api/game/sessions/{save}").json.get("session", {}).get("notebook") or {}).get("full") is True)
        again = self.put(player, save, str(full["text"]), int(version))
        r.check("已滿時原文再儲存被拒：422 notebook_too_long（玩家沒有預備空間）", again.status == 422 and again.code == "notebook_too_long" and again.json.get("length") == length and again.json.get("capacity") == CAPACITY, f"{again.status} {again.code}")
        calls_after_first = self.capture_calls()
        mark = self.log.mark()
        if not self.play_turns(player, save, 12, 21):
            return
        self.wait_for_capture(calls_after_first, 0, mark, "rpg.notebook_skipped")
        r.check("已滿時第 21 輪的視窗被略過：記事簿完全沒變", self.notebook(player, save) == full)
        if calls_after_first is None:
            r.unverified("已滿時不呼叫模型", "連不上假模型的 /_status")
        else:
            r.check("已滿時不呼叫模型", self.capture_calls() == calls_after_first, f"{self.capture_calls()} vs {calls_after_first}")
        skipped = self.log.named(mark, "rpg.notebook_skipped")
        if self.log.available:
            r.check("日誌：rpg.notebook_skipped（原因 full、boundary 20、沒有呼叫模型）", len(skipped) == 1 and skipped[0].get("reason") == "full" and skipped[0].get("boundary") == 20 and skipped[0].get("attempt") == 0, str(skipped))
        else:
            r.unverified("日誌：rpg.notebook_skipped", "未提供 --server-log")
        trimmed_text = str(full["text"])[:CAPACITY]
        trimmed = self.put(player, save, trimmed_text, int(version))
        body = trimmed.json.get("notebook") or {}
        r.check(f"一次刪到容量以內（{CAPACITY} 字）才能儲存：200、未滿", trimmed.status == 200 and body.get("length") == CAPACITY and body.get("full") is False and body.get("version") == int(version) + 1, f"{trimmed.status} {trimmed.code}")
        mark = self.log.mark()
        if not self.play_turns(player, save, 22, 31):
            return
        self.wait_for_capture(calls_after_first, 1, mark, "rpg.notebook_captured")
        resumed = self.notebook(player, save)
        r.check("刪到容量以內後 AI 恢復更新：第 31 輪追加第 21 至 30 輪的條目", str(resumed.get("text", "")).startswith(trimmed_text) and f"第 21 至 {20 + WINDOW} 輪：" in str(resumed.get("text")), str(resumed.get("version")))
        if calls_after_first is None:
            r.unverified("恢復更新時恰好一次抓取呼叫", "連不上假模型的 /_status")
        else:
            r.check("恢復更新時恰好一次抓取呼叫", self.capture_calls() == calls_after_first + 1)
        self.ledger_is_clean(player, 31, "已滿案例")
        self.log_checks(since, "已滿案例", expect={"captured": 2, "skipped": 1, "saved": 1, "save_rejected": 1}, notebook_calls=2)

    def case_abandon(self) -> None:
        r = self.r
        player = self.fresh(coins=11 * TURN_COST)
        save = self.start_save(player) if player else None
        if player is None or save is None:
            return
        since, base = self.log.mark(), self.capture_calls()
        if not (self.play_turns(player, save, 1, 10, {2: KW_ABANDON}) and self.play_turns(player, save, 11, 11)):
            return
        self.wait_for_capture(base, 2, since, "rpg.notebook_abandoned")
        notebook = self.notebook(player, save)
        r.check("精簡重試後仍超過容量加預備空間：記事簿不變（0 字、版本 1）", (notebook.get("length"), notebook.get("version")) == (0, 1), str(notebook))
        if base is None:
            r.unverified("放棄前先精簡重試一次（共 2 次抓取呼叫）", "連不上假模型的 /_status")
        else:
            r.check("放棄前先精簡重試一次（共 2 次抓取呼叫）", self.capture_calls() == base + 2, f"{self.capture_calls()} vs {base}")
        abandoned = self.log.named(since, "rpg.notebook_abandoned")
        if self.log.available:
            r.check("日誌：rpg.notebook_abandoned（原因 too_long、5 條、2 次呼叫、追加後會有 2500 字以上）",
                    len(abandoned) == 1 and abandoned[0].get("reason") == "too_long" and abandoned[0].get("entries") == 5 and abandoned[0].get("attempt") == 2 and int(abandoned[0].get("total") or 0) > CAPACITY + RESERVE, str(abandoned))
        self.ledger_is_clean(player, 11, "放棄案例")
        self.log_checks(since, "放棄案例", expect={"abandoned": 1}, notebook_calls=2)

    def case_rules(self) -> None:
        r = self.r
        player = self.fresh(coins=11 * TURN_COST)
        save = self.start_save(player) if player else None
        if player is None or save is None:
            return
        since, base = self.log.mark(), self.capture_calls()
        if not (self.play_turns(player, save, 1, 10, {2: KW_VIOLATION}) and self.play_turns(player, save, 11, 11)):
            return
        self.wait_for_capture(base, 1, since, "rpg.notebook_abandoned")
        notebook = self.notebook(player, save)
        r.check("條目含必擋的句子：整次放棄，記事簿不變（0 字、版本 1）、沒有任何條目被寫入", (notebook.get("length"), notebook.get("version")) == (0, 1) and BAD_SENTENCE not in str(notebook), str(notebook.get("version")))
        if base is None:
            r.unverified("規則層放棄不重試（1 次抓取呼叫）", "連不上假模型的 /_status")
        else:
            r.check("規則層放棄不重試（1 次抓取呼叫）", self.capture_calls() == base + 1, f"{self.capture_calls()} vs {base}")
        abandoned = self.log.named(since, "rpg.notebook_abandoned")
        if self.log.available:
            r.check("日誌：rpg.notebook_abandoned（原因 blocked、附類別、被擋的文字不在日誌）",
                    len(abandoned) == 1 and abandoned[0].get("reason") == "blocked" and bool(abandoned[0].get("categories")) and BAD_SENTENCE not in self.log.text(since), str(abandoned))
        self.ledger_is_clean(player, 11, "規則層案例")
        self.log_checks(since, "規則層案例", expect={"abandoned": 1}, notebook_calls=1)

    def case_eof(self) -> None:
        """D1 的前提：串流先結束、關閉回呼之後才執行。第 11 輪不提早離開（讀到伺服器結束回應為止），量 `done` 到結束的間隔與結束當下的記事簿。

        gunicorn gthread 的預期：結束 chunk 在 `done` 之後立刻送出（間隔遠小於慢速抓取的 5 秒），結束當下記事簿還是空的，約 5 秒後才有內容。
        Werkzeug 開發伺服器（HTTP/1.0、沒有 Content-Length）可能要等到回呼跑完才關連線，所以預設只記錄數字、不計入通過或失敗；
        `--assert-eof`（容器內的 gunicorn 驗收使用）才把這兩項當成通過條件。"""
        r = self.r
        player = self.fresh(coins=11 * TURN_COST)
        save = self.start_save(player) if player else None
        if player is None or save is None:
            return
        since, base = self.log.mark(), self.capture_calls()
        if not self.play_turns(player, save, 1, 10, {2: KW_SLOW}):
            return
        started = time.monotonic()
        eleventh = self.play(player, save, 11)
        at_eof = self.notebook(player, save)                    # 讀到結束的當下（多一次往返的誤差在毫秒內）
        done_at, ended_at = eleventh.first_at("done"), eleventh.ended_at
        r.check("第 11 輪：done 為 complete、turn_number 11，且伺服器結束了回應（沒有提早離開）",
                (eleventh.done or {}).get("status") == "complete" and (eleventh.done or {}).get("turn_number") == 11 and ended_at is not None, str(eleventh.done))
        if done_at is not None and ended_at is not None:
            gap = ended_at - done_at
            empty_at_eof = at_eof.get("length") == 0 and at_eof.get("version") == 1
            summary = f"done 在 {done_at:.2f} 秒、結束回應在 {ended_at:.2f} 秒（間隔 {gap:.2f} 秒）；結束當下記事簿 {at_eof.get('length')} 字、版本 {at_eof.get('version')}"
            if self.assert_eof:
                r.note(f"實測：{summary}")
                r.check(f"結束 chunk 先送出：done 到結束回應的間隔小於 {EOF_LIMIT_SECONDS:.0f} 秒（抓取要約 {SLOW_SECONDS:.0f} 秒）", gap < EOF_LIMIT_SECONDS, summary)
                r.check("結束回應當下記事簿還是空的：關閉回呼在結束 chunk 之後才執行", empty_at_eof, summary)
            else:
                r.note(f"資訊（沒有 --assert-eof，不計入通過或失敗）：{summary}")
        appeared = self.wait_until(lambda: (self.notebook(player, save).get("length") or 0) > 0)
        r.check(f"結束回應之後記事簿才更新：關閉回呼確實執行（自第 11 輪送出起 {time.monotonic() - started:.1f} 秒內有內容）", appeared)
        notebook = self.notebook(player, save)
        lines = str(notebook.get("text", "")).split("\n")
        r.check(f"記事簿追加 {DEFAULT_ENTRIES} 條、版本 2、字數與全文一致", len(lines) == DEFAULT_ENTRIES and notebook.get("version") == 2 and notebook.get("length") == len(str(notebook.get("text", ""))), str(notebook.get("version")))
        self.wait_for_capture(base, 1, since, "rpg.notebook_captured")
        self.ledger_is_clean(player, 11, "結束回應案例")
        self.log_checks(since, "結束回應案例", expect={"captured": 1}, notebook_calls=1)

    def log_scan(self) -> None:
        if not self.log.available:
            self.r.unverified("應用程式日誌沒有任何記事簿文字、玩家行動與被擋的句子", "未提供 --server-log")
            return
        text = self.log.text(self.log_start)
        leaked = [needle[:12] for needle in sorted(self.watched) if needle in text]
        self.r.check(f"應用程式日誌（本次新增的 {len(text)} 字元）沒有任何記事簿文字、玩家行動與被擋的句子（檢查 {len(self.watched)} 段）", not leaked, f"找到：{leaked}")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="rpg-notebook 的 HTTP 驗收（只對本機驗收環境）")
    parser.add_argument("--base-url", default="http://localhost:5010", help="應用程式")
    parser.add_argument("--helper-url", default="http://localhost:5001", help="驗收環境的切換身分小幫手")
    parser.add_argument("--model-url", default="http://127.0.0.1:5002", help="假的 RPG 模型（計數端點 /_status）")
    parser.add_argument("--server-log", type=Path, default=None, help="應用程式日誌的路徑（選填）")
    parser.add_argument("--only", default=",".join(CASES), help=f"只跑指定的案例（逗號分隔，預設全部：{','.join(CASES)}）")
    parser.add_argument("--assert-eof", action="store_true",
                        help="把 eof 案例的時序（done 到結束回應小於 1 秒、結束當下記事簿仍是空的）當成通過條件；只在 gunicorn gthread 下使用（容器驗收），開發伺服器預設只記錄數字")
    args = parser.parse_args(argv)
    only = tuple(name.strip() for name in args.only.split(",") if name.strip())
    unknown = [name for name in only if name not in CASES]
    if unknown:
        print(f"未知的案例：{unknown}（可用：{', '.join(CASES)}）", file=sys.stderr)
        return 2
    try:
        Client(args.base_url, "--base-url"), Client(args.helper_url, "--helper-url")
        model = Client(args.model_url, "--model-url")
    except TargetRefused as error:
        print(f"拒絕執行：{error}", file=sys.stderr)
        return 2
    report = Report()
    started = time.monotonic()
    try:
        NotebookAcceptance(args.base_url, args.helper_url, model, report, ServerLog(args.server_log), assert_eof=args.assert_eof).run(only)
    except OSError as error:
        report.check("連線到驗收環境", False, f"{type(error).__name__}（驗收環境是否已啟動？）")
    print(f"\n摘要：通過 {report.count(PASS)}、失敗 {report.count(FAIL)}、未驗證 {report.count(UNVERIFIED)}（{time.monotonic() - started:.1f} 秒）")
    return 1 if report.count(FAIL) else 0


if __name__ == "__main__":
    sys.exit(main())
