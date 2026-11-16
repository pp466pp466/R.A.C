"""rpg-turn-revision（RC-2c）的 HTTP 驗收腳本（任務 5.2）。

    python scripts/acceptance_rpg_revision.py [--base-url http://localhost:5010] [--helper-url http://localhost:5001]
                                              [--model-url http://127.0.0.1:5002] [--server-log 路徑]
                                              [--only journey,privacy,failures,conflicts,rejections,coins,notebook]

對一個**本機驗收環境**（應用程式、切換身分的小幫手、假的 RPG 模型供應商；啟動方式同 `acceptance_rpg_turn.py`）走完重新生成與編輯最後一輪的
七個案例，逐項輸出「通過」、「失敗」或「未驗證」，最後輸出摘要（結束代碼：沒有失敗為 0，有失敗為 1，位址不是本機為 2）。
與 `tests/test_rpg_revision_flow.py`、`tests/test_game_revision_endpoints.py` 檢查同一組行為，差別是這裡走真正的網路連線
（Werkzeug 開發伺服器、真實的串流與連線關閉、模型端是另一個行程）。

- **只對本機執行**、腳本本身不含機密、輸出只有項目名稱、數字與結果（同 `acceptance_rpg_turn.py`）。
- 每個案例各用一位**全新玩家**（小幫手 `GET /fresh/rpg?coins=N` 補給付費幣），所以案例彼此獨立，可用 `--only` 單獨重跑；
  全部約 4 至 5 分鐘（假模型的失敗與慢速情境會等待）。
- **與假模型的約定**（`rpg_acceptance_harness.py` 的說明有完整版）：行動文字裡的關鍵字決定行為。原本那一輪是對同一段行動的第 1 次呼叫，
  所以重新生成一定是第 2 次起；編輯的新行動是全新的文字，既有的回合關鍵字可以直接放進去。

    測試重生         第 2 次起的劇情是另一段文字（含「重新生成後」），狀態提議後山與 2 個月（原本那一輪：山門與 1 個月）
    測試重生被擋一次／被擋兩次／中斷／慢速／狀態失敗   第 2 次呼叫起的各種失敗與慢速
    測試編輯         編輯後的新行動：劇情含「編輯後」，狀態提議藏經閣與 3 個月
    測試狀態失敗     這段行動的所有狀態呼叫都回 500

- 七個案例：
    journey     一輪 → 兩次免費重新生成（帳本不動）→ 第三次付費（淨扣 30）→ 編輯（淨扣 30、免費次數歸零）→ 再重新生成又免費；
                每次替換後：輪數與序號不變、`revision` 加 1、世界狀態是「還原再套用」（經過月數不累加）、`last_turn` 與 `done` 一致
    privacy     理由帶標記文字：重新生成的所有模型呼叫都不含它（先用一個把標記放進行動的對照回合確認偵測有效）
    failures    被擋一次（自動重生）、被擋兩次（保留舊內容）、上游中斷、狀態呼叫失敗、付費編輯被擋與狀態失敗（退回、再付費只淨扣一次）、
                串流中途斷線（認領被釋放）：每一種都保留舊內容、不消耗免費次數、遊戲幣與預期一致
    conflicts   修訂進行中：同一個 `expected_revision`、編輯與新行動都得到 409 `turn_in_progress`；完成後以舊的 `expected_revision` 得到 `revision_conflict`
    rejections  不是最後一輪（`not_last`）、已完結（`ended`）、404（不存在與他人）、驗證（理由太短、分類、行動相同、規則層命中）、
                未登入與缺 CSRF；被拒絕的請求沒有扣幣也沒有留下認領
    coins       餘額不足：免費的重新生成不需要餘額、第三次付費得到 409 `insufficient_coins`，沒有認領、免費次數不變
    notebook    第 11 輪（記事簿在此抓取）完成後修訂：記事簿的版本與內容不變、沒有新的抓取呼叫
- `--server-log` 是應用程式日誌的路徑：提供時，腳本讀取本次新增的 JSON 行，確認 `rpg.revision_*` 事件的筆數、種類與原因，並掃描整段日誌找不到任何
  理由、行動、劇情與被擋的句子；未提供時這些項目標為「未驗證」。假模型的 `GET /_status`（`--model-url`）提供呼叫計數，連不上時相關項目標為「未驗證」。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from acceptance_rpg_notebook import NotebookAcceptance, Player, ServerLog
from acceptance_rpg_turn import (
    BAD_SENTENCE, EVENT_NAMES, FAIL, PASS, SIGNUP_GRANT, TURN_COST, UNVERIFIED, Client, Report, TargetRefused, Turn, fetch_model_status,
)

KW_REGEN, KW_EDIT = "測試重生", "測試編輯"
KW_REGEN_BLOCK_ONCE, KW_REGEN_BLOCK_TWICE = "測試重生被擋一次", "測試重生被擋兩次"
KW_REGEN_CUT, KW_REGEN_SLOW, KW_REGEN_STATE_FAIL = "測試重生中斷", "測試重生慢速", "測試重生狀態失敗"
KW_STATE_FAIL, KW_BLOCK_TWICE, KW_ENDING = "測試狀態失敗", "測試被擋兩次", "測試結局"
REASON_MARKER = "驗收理由標記"
REGEN_MARK, EDIT_MARK = "重新生成後", "編輯後"                       # 假模型的修訂劇情含這些字
REGEN_WORLD = (2, "青雲宗後山")                                      # （經過月數，地點）：重新生成的狀態提議
EDIT_WORLD = (3, "青雲宗藏經閣")
NORMAL_WORLD = (1, "青雲宗山門")                                      # 普通回合的狀態提議
FREE_REGENERATIONS = 2                                             # 設定表 rpg.free_regenerations 的初始值
CATEGORIES = ("setting_mismatch", "repetitive_or_dull", "typo_or_quality", "blocked_transition", "other")
CASES = ("journey", "privacy", "failures", "conflicts", "rejections", "coins", "notebook")
SLOW_WAIT_SECONDS = 40.0


class RevisionAcceptance(NotebookAcceptance):
    def __init__(self, base_url: str, helper_url: str, model: Client | None, report: Report, log: ServerLog) -> None:
        super().__init__(base_url, helper_url, model, report, log)
        self.reasons = 0
        self.watched.update({REGEN_MARK, EDIT_MARK, REASON_MARKER})
        self.watched.update({"重新生成後，晨霧散去，山門前換了一番景象。", "編輯後的行動讓故事轉了個彎。"})

    # ---- 小工具 -----------------------------------------------------------------------------------

    def reason(self, text: str = "劇情太重複了想換個寫法") -> str:
        self.reasons += 1
        value = f"{text}{self.reasons}"
        self.watched.add(value)
        return value

    def edit_action(self, extra: str = "") -> str:
        return self.action(f"{KW_EDIT} {extra}".strip())

    def clone(self, player: Player) -> Client:
        other = Client(self.base_url, "--base-url")
        other.cookies, other.csrf = dict(player.client.cookies), player.client.csrf
        return other

    def sign_in(self, coins: int) -> tuple[Player, str] | None:
        player = self.fresh(coins=coins)
        save = self.start_save(player) if player else None
        return (player, save) if player is not None and save is not None else None

    def post_turn(self, player: Player, save: str, msg: str, action: str, **kwargs: Any) -> Turn:
        return player.client.stream(f"/api/game/sessions/{save}/turns/stream", {"client_msg_id": msg, "action": action}, **kwargs)

    def body(self, kind: str, revision: Any, *, action: str | None = None, category: str = "repetitive_or_dull", reason: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"expected_revision": revision}
        if kind == "regenerate":
            body.update({"category": category, "reason": self.reason() if reason is None else reason})
        else:
            body["action"] = action if action is not None else self.edit_action()
        return body

    def path(self, save: str, msg: str, kind: str) -> str:
        return f"/api/game/sessions/{save}/turns/{msg}/{kind}/stream"

    def revise(self, player: Player, save: str, msg: str, kind: str, revision: int, **options: Any) -> Turn:
        stream_options = {key: options.pop(key) for key in ("stop_after", "timeout") if key in options}
        return player.client.stream(self.path(save, msg, kind), self.body(kind, revision, **options), **stream_options)

    def reject(self, player: Player | Client, save: str, msg: str, kind: str, body: dict[str, Any]):
        client = player.client if isinstance(player, Player) else player
        return client.call("POST", self.path(save, msg, kind), body)

    def snapshot(self, player: Player, save: str) -> dict[str, Any]:
        return player.client.call("GET", f"/api/game/sessions/{save}").json.get("session", {})

    def turn_of(self, player: Player, save: str, msg: str) -> dict[str, Any]:
        turns = player.client.call("GET", f"/api/game/sessions/{save}/turns?since_seq=0").json.get("turns", [])
        return next((turn for turn in turns if turn.get("client_msg_id") == msg), {})

    def entry_count(self, player: Player) -> int:
        count, before = 0, None
        for _ in range(20):
            reply = player.client.call("GET", "/api/wallet/entries?limit=100" + (f"&before={before}" if before else ""))
            count += len(reply.json.get("entries", []))
            before = reply.json.get("next_before")
            if not before:
                break
        return count

    def world_of(self, player: Player, save: str) -> tuple[int, str]:
        world = self.snapshot(player, save).get("world", {})
        return world.get("elapsed_months"), world.get("location")

    def stream_ok(self, turn: Turn, kind: str, msg: str, revision: int) -> bool:
        meta = (turn.of("meta") or [{}])[0]
        return (turn.status == 200 and "text/event-stream" in turn.headers.get("content-type", "") and turn.names[:1] == ["meta"]
                and set(turn.names) <= EVENT_NAMES and not turn.malformed
                and meta.get("kind") == kind and meta.get("client_msg_id") == msg and meta.get("revision") == revision)

    def applied(self, label: str, turn: Turn, *, kind: str, msg: str, revision: int, turn_number: int, free_left: int, mark: str) -> bool:
        """成功的修訂：事件序列（meta … state → done）、`done` 的欄位與劇情內容。"""
        done = turn.done or {}
        names = turn.names
        ordered = "state" in names and names[-1:] == ["done"] and names.index("state") == len(names) - 2
        ok = (self.stream_ok(turn, kind, msg, revision) and ordered and "error" not in names
              and (done.get("status"), done.get("turn_number"), done.get("revision"), done.get("free_left")) == ("complete", turn_number, revision + 1, free_left)
              and mark in turn.text and BAD_SENTENCE not in json.dumps(turn.events, ensure_ascii=False))
        return self.r.check(f"{label}：meta → token → state → done(complete)，輪數 {turn_number}、revision {revision + 1}、剩餘免費 {free_left}、劇情含「{mark}」", ok,
                            f"{turn.status} {names[:3]}…{names[-3:]} {done} {turn.text[:30]}")

    def tally(self, since: int, label: str, *, started: int, completed: int, failed: int, reasons: list[str] | None = None, kinds: dict[str, int] | None = None) -> None:
        """日誌的 `rpg.revision_*` 事件：開始、完成、失敗的筆數，失敗原因（列舉）與種類的分布。"""
        if not self.log.available:
            self.r.unverified(f"{label}：日誌的 rpg.revision_* 事件", "未提供 --server-log")
            return
        found = {name: self.log.named(since, f"rpg.revision_{name}") for name in ("started", "completed", "failed")}
        counts = {name: len(events) for name, events in found.items()}
        ok = counts == {"started": started, "completed": completed, "failed": failed}
        self.r.check(f"{label}：日誌 rpg.revision_started {started}、completed {completed}、failed {failed} 筆", ok, str(counts))
        if reasons is not None:
            actual = sorted(str(event.get("reason")) for event in found["failed"])
            self.r.check(f"{label}：失敗原因為 {sorted(reasons)}", actual == sorted(reasons), str(actual))
        if kinds is not None:
            actual_kinds = dict(Counter(str(event.get("kind")) for event in found["started"]))
            self.r.check(f"{label}：開始的種類 {kinds}", actual_kinds == kinds, str(actual_kinds))

    # ---- 案例 ---------------------------------------------------------------------------------------

    def run(self, only: tuple[str, ...]) -> None:
        for name in CASES:
            if name in only:
                print(f"\n== 案例 {name} ==", flush=True)
                getattr(self, f"case_{name}")()
        self.log_scan()

    def case_journey(self) -> None:
        r = self.r
        signed = self.sign_in(coins=300)
        if signed is None:
            return
        player, save = signed
        start = SIGNUP_GRANT + 300
        since, base = self.log.mark(), self.capture_calls()
        original = self.action(KW_REGEN)
        first = self.post_turn(player, save, "n-1", original)
        done = first.done or {}
        r.check("第 1 輪完成：revision 1、剩餘免費重新生成 2", (done.get("status"), done.get("turn_number"), done.get("revision"), done.get("free_left")) == ("complete", 1, 1, FREE_REGENERATIONS), str(done))
        last = self.snapshot(player, save).get("last_turn") or {}
        r.check(f"快照的 last_turn：可修訂、revision 1、免費 2（重新生成 0 幣、編輯 {TURN_COST} 幣）",
                (last.get("client_msg_id"), last.get("revision"), last.get("revisable"), last.get("free_left"), last.get("regenerate_cost"), last.get("edit_cost"))
                == ("n-1", 1, True, FREE_REGENERATIONS, 0, TURN_COST), str(last))
        r.check("原本那一輪的世界狀態：1 個月、山門", self.world_of(player, save) == NORMAL_WORLD, str(self.world_of(player, save)))
        balance = self.total(player)
        r.check(f"遊戲幣：扣了一輪（{start} → {start - TURN_COST}）", balance == start - TURN_COST, str(balance))
        entries = self.entry_count(player)
        story = first.text
        seq = self.turn_of(player, save, "n-1").get("seq")

        def check_world_and_balance(label: str, expected_world: tuple[int, str], expected_balance: int, revision: int, action: str, text: str) -> None:
            view = self.turn_of(player, save, "n-1")
            r.check(f"{label}：回合內容已替換、輪數與序號不變、revision {revision}、行動與預期一致、已完成輪數仍是 1",
                    view.get("story") == text and view.get("turn_number") == 1 and view.get("seq") == seq and view.get("revision") == revision
                    and view.get("action") == action and self.snapshot(player, save).get("completed_turns") == 1, str({k: v for k, v in view.items() if k != "story"}))
            world = self.snapshot(player, save).get("world", {})
            r.check(f"{label}：世界狀態是「還原再套用」（{expected_world[0]} 個月、{expected_world[1]}；不是 1 加提議的月數）",
                    (world.get("elapsed_months"), world.get("location")) == expected_world and expected_world[1] in world.get("places", []), str(world))
            r.check(f"{label}：遊戲幣餘額 {expected_balance}", self.total(player) == expected_balance, str(self.total(player)))

        one = self.revise(player, save, "n-1", "regenerate", 1)
        self.applied("第 1 次重新生成（免費）", one, kind="regenerate", msg="n-1", revision=1, turn_number=1, free_left=1, mark=REGEN_MARK)
        r.check("新舊劇情不同、新劇情沒有留下舊的句子", one.text != story and story not in one.text)
        check_world_and_balance("第 1 次重新生成", REGEN_WORLD, balance, 2, original, one.text)
        r.check("免費的重新生成：帳本沒有新的列", self.entry_count(player) == entries)
        two = self.revise(player, save, "n-1", "regenerate", 2)
        self.applied("第 2 次重新生成（免費）", two, kind="regenerate", msg="n-1", revision=2, turn_number=1, free_left=0, mark=REGEN_MARK)
        check_world_and_balance("第 2 次重新生成", REGEN_WORLD, balance, 3, original, two.text)
        last = self.snapshot(player, save).get("last_turn") or {}
        r.check(f"免費次數用完：last_turn 的免費 0、重新生成改為 {TURN_COST} 幣", (last.get("free_left"), last.get("regenerate_cost"), last.get("revision")) == (0, TURN_COST, 3), str(last))
        three = self.revise(player, save, "n-1", "regenerate", 3)
        self.applied("第 3 次重新生成（付費）", three, kind="regenerate", msg="n-1", revision=3, turn_number=1, free_left=0, mark=REGEN_MARK)
        check_world_and_balance("第 3 次重新生成", REGEN_WORLD, balance - TURN_COST, 4, original, three.text)
        r.check("付費的 done.coins 是結算後的餘額", (three.done or {}).get("coins") == balance - TURN_COST, str((three.done or {}).get("coins")))
        new_action = self.edit_action()
        edited = self.revise(player, save, "n-1", "edit", 4, action=new_action)
        self.applied("編輯行動（付費）", edited, kind="edit", msg="n-1", revision=4, turn_number=1, free_left=FREE_REGENERATIONS, mark=EDIT_MARK)
        check_world_and_balance("編輯行動", EDIT_WORLD, balance - 2 * TURN_COST, 5, new_action, edited.text)
        world = self.snapshot(player, save).get("world", {})
        r.check("編輯：回到回合開始時的世界再套用（地點不含上一次重新生成的後山）", REGEN_WORLD[1] not in world.get("places", []), str(world.get("places")))
        again = self.revise(player, save, "n-1", "regenerate", 5)
        self.applied("編輯後再重新生成（免費次數已歸零，又是免費）", again, kind="regenerate", msg="n-1", revision=5, turn_number=1, free_left=FREE_REGENERATIONS - 1, mark=EDIT_MARK)
        r.check(f"遊戲幣：總共淨扣 {3 * TURN_COST}（原本那輪、第 3 次重新生成、編輯各 {TURN_COST}）", self.total(player) == start - 3 * TURN_COST, str(self.total(player)))
        last = self.snapshot(player, save).get("last_turn") or {}
        r.check("最後的 last_turn：revision 6、可修訂、剩餘免費 1", (last.get("revision"), last.get("revisable"), last.get("free_left")) == (6, True, FREE_REGENERATIONS - 1), str(last))
        if base is None:
            r.unverified("修訂不觸發記事簿抓取", "連不上假模型的 /_status")
        else:
            r.check("修訂不觸發記事簿抓取（抓取呼叫次數不變）", self.capture_calls() == base)
        self.tally(since, "旅程", started=5, completed=5, failed=0, kinds={"regenerate": 4, "edit": 1})
        if self.log.available:
            started = self.log.named(since, "rpg.revision_started")
            paid = sorted(str(event.get("outcome")) for event in started)
            r.check("日誌：開始事件的費用——3 次免費、2 次付費", paid == ["free", "free", "free", "paid", "paid"], str(paid))

    def case_privacy(self) -> None:
        r = self.r
        player = self.fresh(coins=0)
        if player is None:
            return
        control, save = self.start_save(player), self.start_save(player)
        if control is None or save is None:
            return
        status = fetch_model_status(self.model)
        if status is None:
            r.unverified("理由不進模型", "連不上假模型的 /_status")
            return
        before = status.get("marker_calls", 0)
        r.check("對照回合：行動帶標記文字會被偵測到（偵測有效）", self.post_turn(player, control, "n-1", self.action(REASON_MARKER)).done is not None
                and (fetch_model_status(self.model) or {}).get("marker_calls", 0) > before)
        self.post_turn(player, save, "n-1", self.action(KW_REGEN))
        before = (fetch_model_status(self.model) or {}).get("marker_calls", 0)
        reason = f"{REASON_MARKER}：劇情太重複了"
        turn = self.revise(player, save, "n-1", "regenerate", 1, reason=reason)
        r.check("重新生成帶標記的理由：完成", (turn.done or {}).get("status") == "complete", str(turn.names))
        after = (fetch_model_status(self.model) or {}).get("marker_calls", 0)
        r.check("重新生成送給模型的所有請求（串流與狀態）都不含理由", after == before, f"{before} → {after}")
        for category in CATEGORIES[:2]:
            sent = self.revise(player, save, "n-1", "regenerate", 2 if category == CATEGORIES[0] else 3, category=category, reason=f"{REASON_MARKER}（{category}）")
            r.check(f"分類 {category} 也不進模型", (sent.done or {}).get("status") == "complete" and (fetch_model_status(self.model) or {}).get("marker_calls", 0) == before)

    def case_failures(self) -> None:
        r = self.r
        signed = self.sign_in(coins=100)
        if signed is None:
            return
        player, save = signed
        since = self.log.mark()
        upstream = (fetch_model_status(self.model) or {}).get("upstream_closed")
        failed_reasons: list[str] = []

        # 1. 被擋一次：自動重生，成功，不另外扣幣
        action = self.action(KW_REGEN_BLOCK_ONCE)
        self.post_turn(player, save, "n-1", action)
        balance = self.total(player)
        turn = self.revise(player, save, "n-1", "regenerate", 1)
        retries = [data.get("will_retry") for data in turn.of("transition")]
        r.check("被擋一次：transition(will_retry=true) 後自動重生並完成（revision 2、剩餘免費 1）",
                retries == [True] and (turn.done or {}).get("status") == "complete" and (turn.done or {}).get("revision") == 2 and (turn.done or {}).get("free_left") == 1, f"{retries} {turn.done}")
        r.check("被擋一次：被擋的句子從未送給前端，劇情是重新生成的版本", BAD_SENTENCE not in json.dumps(turn.events, ensure_ascii=False) and REGEN_MARK in turn.text)
        r.check("被擋一次：沒有扣幣（免費）", self.total(player) == balance, str(self.total(player)))

        # 2. 被擋兩次：保留舊內容；認領已釋放，下一次重新生成成功
        action = self.action(KW_REGEN_BLOCK_TWICE)
        self.post_turn(player, save, "n-2", action)
        before_story, before_world, balance = self.turn_of(player, save, "n-2").get("story"), self.world_of(player, save), self.total(player)
        turn = self.revise(player, save, "n-2", "regenerate", 1)
        retries = [data.get("will_retry") for data in turn.of("transition")]
        done = turn.done or {}
        r.check("被擋兩次：transition(true) → transition(false) → done(blocked)，revision 與免費次數不變",
                retries == [True, False] and (done.get("status"), done.get("revision"), done.get("free_left")) == ("blocked", 1, FREE_REGENERATIONS), f"{retries} {done}")
        r.check("被擋兩次：被擋的句子從未送給前端", BAD_SENTENCE not in json.dumps(turn.events, ensure_ascii=False))
        view = self.turn_of(player, save, "n-2")
        r.check("被擋兩次：舊劇情、revision 與世界狀態原樣保留，遊戲幣不變", view.get("story") == before_story and view.get("revision") == 1 and self.world_of(player, save) == before_world and self.total(player) == balance, str(view.get("revision")))
        failed_reasons.append("blocked")
        retry = self.revise(player, save, "n-2", "regenerate", 1)
        r.check("被擋兩次之後：認領已釋放，下一次重新生成成功（revision 2、剩餘免費 1）", (retry.done or {}).get("status") == "complete" and (retry.done or {}).get("revision") == 2 and (retry.done or {}).get("free_left") == 1, str(retry.done))

        # 3. 上游中斷：error、保留舊內容、不消耗免費次數
        action = self.action(KW_REGEN_CUT)
        self.post_turn(player, save, "n-3", action)
        before_story, balance = self.turn_of(player, save, "n-3").get("story"), self.total(player)
        broken = self.revise(player, save, "n-3", "regenerate", 1)
        error = (broken.of("error") or [{}])[0]
        r.check("上游中途斷線：最後一個事件是 error（generation_failed）、沒有 done", broken.names[-1:] == ["error"] and error.get("code") == "generation_failed" and "done" not in broken.names, str(broken.names))
        view, last = self.turn_of(player, save, "n-3"), self.snapshot(player, save).get("last_turn") or {}
        r.check("上游中途斷線：舊劇情與 revision 不變、遊戲幣不變、免費次數沒有用掉、可再次修訂", view.get("story") == before_story and view.get("revision") == 1 and self.total(player) == balance
                and (last.get("free_left"), last.get("revisable")) == (FREE_REGENERATIONS, True), f"{view.get('revision')} {last}")
        failed_reasons.append("model_failed")
        retry = self.revise(player, save, "n-3", "regenerate", 1)
        r.check("上游中斷之後再重新生成：成功（revision 2、剩餘免費 1）", (retry.done or {}).get("status") == "complete" and (retry.done or {}).get("revision") == 2 and (retry.done or {}).get("free_left") == 1, str(retry.done))

        # 4. 狀態呼叫失敗：文字照常串流，但修訂失敗、保留舊內容與舊世界狀態
        action = self.action(KW_REGEN_STATE_FAIL)
        self.post_turn(player, save, "n-4", action)
        before_story, before_world, balance = self.turn_of(player, save, "n-4").get("story"), self.world_of(player, save), self.total(player)
        broken = self.revise(player, save, "n-4", "regenerate", 1)
        r.check("狀態呼叫失敗：最後一個事件是 error（generation_failed）、沒有 done", broken.names[-1:] == ["error"] and (broken.of("error") or [{}])[0].get("code") == "generation_failed" and "done" not in broken.names, str(broken.names))
        view = self.turn_of(player, save, "n-4")
        r.check("狀態呼叫失敗：舊劇情、revision 與世界狀態原樣保留，遊戲幣不變", view.get("story") == before_story and view.get("revision") == 1 and self.world_of(player, save) == before_world and self.total(player) == balance, str(view.get("revision")))
        failed_reasons.append("state_failed")

        # 5. 付費的編輯：被擋兩次與狀態失敗都退回，之後成功的編輯只淨扣一次
        original = self.action()
        self.post_turn(player, save, "n-5", original)
        balance = self.total(player)
        blocked = self.revise(player, save, "n-5", "edit", 1, action=self.edit_action(KW_BLOCK_TWICE))
        r.check("編輯被擋兩次：done(blocked)、舊行動與舊劇情保留、預扣退回（餘額不變）", (blocked.done or {}).get("status") == "blocked" and (blocked.done or {}).get("revision") == 1
                and self.turn_of(player, save, "n-5").get("action") == original and self.total(player) == balance, f"{blocked.done} {self.total(player)} vs {balance}")
        failed_reasons.append("blocked")
        broken = self.revise(player, save, "n-5", "edit", 1, action=self.edit_action(KW_STATE_FAIL))
        r.check("編輯的狀態呼叫失敗：error、舊行動保留、預扣退回（餘額不變）", broken.names[-1:] == ["error"] and self.turn_of(player, save, "n-5").get("action") == original and self.total(player) == balance, f"{broken.names} {self.total(player)} vs {balance}")
        failed_reasons.append("state_failed")
        final_action = self.edit_action("繼續")
        success = self.revise(player, save, "n-5", "edit", 1, action=final_action)
        r.check(f"兩次失敗之後的編輯成功：revision 2、行動已替換，總共只淨扣 {TURN_COST}（鍵不重複）", (success.done or {}).get("revision") == 2 and self.turn_of(player, save, "n-5").get("action") == final_action
                and self.total(player) == balance - TURN_COST, f"{success.done} {self.total(player)} vs {balance - TURN_COST}")

        # 6. 串流中途斷線：停止、保留舊內容、釋放認領
        action = self.action(KW_REGEN_SLOW)
        self.post_turn(player, save, "n-6", action)
        before_story, balance = self.turn_of(player, save, "n-6").get("story"), self.total(player)
        left = self.revise(player, save, "n-6", "regenerate", 1, stop_after=lambda name, data: name == "token")
        r.check("客戶端在第一個片段後關閉連線", left.aborted and left.names.count("token") == 1, str(left.names))
        released = self.wait_until(lambda: (self.snapshot(player, save).get("last_turn") or {}).get("revisable") is True, seconds=SLOW_WAIT_SECONDS, step=0.5)
        r.check("客戶端斷線：伺服器停止生成並釋放認領（last_turn 又可修訂）", released)
        view, last = self.turn_of(player, save, "n-6"), self.snapshot(player, save).get("last_turn") or {}
        r.check("客戶端斷線：舊劇情與 revision 不變、遊戲幣不變、免費次數沒有用掉", view.get("story") == before_story and view.get("revision") == 1 and self.total(player) == balance and last.get("free_left") == FREE_REGENERATIONS, f"{view.get('revision')} {last}")
        failed_reasons.append("disconnected")
        if upstream is None:
            r.unverified("客戶端斷線：應用程式關閉上游的模型串流", "連不上假模型的 /_status")
        else:
            closed = self.wait_until(lambda: (fetch_model_status(self.model) or {}).get("upstream_closed", 0) > upstream, seconds=20)
            r.check("客戶端斷線：應用程式關閉上游的模型串流", closed)
        last_balance = self.total(player)
        r.check("伺服器仍正常（/healthz 200）", self.clone(player).call("GET", "/healthz").status == 200)
        r.check(f"整個案例的遊戲幣：6 輪加 1 次成功的編輯，共 {7 * TURN_COST}", last_balance == SIGNUP_GRANT + 100 - 7 * TURN_COST, str(last_balance))
        self.tally(since, "失敗案例", started=10, completed=4, failed=6, reasons=failed_reasons)

    def case_conflicts(self) -> None:
        r = self.r
        signed = self.sign_in(coins=0)
        if signed is None:
            return
        player, save = signed
        self.post_turn(player, save, "n-1", self.action(KW_REGEN_SLOW))
        balance = self.total(player)
        holder: dict[str, Turn] = {}
        started = threading.Event()
        worker = self.clone(player)

        def first_token(name: str, data: dict[str, Any]) -> bool:
            if name == "token":
                started.set()
            return False                                                     # 不離開：讓它一路串流到結束

        def slow_regenerate() -> None:
            holder["turn"] = worker.stream(self.path(save, "n-1", "regenerate"), self.body("regenerate", 1), stop_after=first_token)

        thread = threading.Thread(target=slow_regenerate, daemon=True)
        thread.start()
        if not r.check("慢速的重新生成已開始串流", started.wait(30)):
            return
        last = self.snapshot(player, save).get("last_turn") or {}
        r.check("修訂進行中：last_turn 不可修訂", last.get("revisable") is False, str(last))
        same = self.reject(player, save, "n-1", "regenerate", self.body("regenerate", 1))
        r.check("同一個 expected_revision 的第二個重新生成 → 409 turn_in_progress", same.status == 409 and same.code == "turn_in_progress", f"{same.status} {same.code}")
        edit = self.reject(player, save, "n-1", "edit", self.body("edit", 1))
        r.check("進行中的編輯 → 409 turn_in_progress", edit.status == 409 and edit.code == "turn_in_progress", f"{edit.status} {edit.code}")
        new_turn = player.client.call("POST", f"/api/game/sessions/{save}/turns/stream", {"client_msg_id": "n-2", "action": self.action()})
        r.check("修訂進行中送新行動 → 409 turn_in_progress，沒有新的回合列", new_turn.status == 409 and new_turn.code == "turn_in_progress" and not self.turn_of(player, save, "n-2"), f"{new_turn.status} {new_turn.code}")
        thread.join(SLOW_WAIT_SECONDS)
        turn = holder.get("turn")
        r.check("第一個重新生成最後完成（revision 2、剩餘免費 1）", turn is not None and (turn.done or {}).get("status") == "complete" and (turn.done or {}).get("revision") == 2 and (turn.done or {}).get("free_left") == 1, str(turn.done if turn else None))
        r.check("兩個請求只有一個生效：遊戲幣不變（免費）、回合 revision 2", self.total(player) == balance and self.turn_of(player, save, "n-1").get("revision") == 2, str(self.total(player)))
        stale = self.reject(player, save, "n-1", "regenerate", self.body("regenerate", 1))
        r.check("完成之後以舊的 expected_revision 重新生成 → 409 revision_conflict", stale.status == 409 and stale.code == "revision_conflict", f"{stale.status} {stale.code}")
        stale_edit = self.reject(player, save, "n-1", "edit", self.body("edit", 1))
        r.check("完成之後以舊的 expected_revision 編輯 → 409 revision_conflict，沒有扣幣", stale_edit.status == 409 and stale_edit.code == "revision_conflict" and self.total(player) == balance, f"{stale_edit.status} {stale_edit.code}")
        r.check("衝突都沒有留下認領：last_turn 可修訂、revision 2", (self.snapshot(player, save).get("last_turn") or {}).get("revisable") is True)

    def case_rejections(self) -> None:
        r = self.r
        signed = self.sign_in(coins=0)
        if signed is None:
            return
        player, save = signed
        first, second = self.action(), self.action()
        self.post_turn(player, save, "n-1", first)
        self.post_turn(player, save, "n-2", second)
        balance = self.total(player)

        def untouched(label: str) -> None:
            last = self.snapshot(player, save).get("last_turn") or {}
            r.check(f"{label}：沒有扣幣、沒有留下認領（revision 1、可修訂、免費 {FREE_REGENERATIONS}）",
                    self.total(player) == balance and (last.get("revision"), last.get("revisable"), last.get("free_left")) == (1, True, FREE_REGENERATIONS), f"{self.total(player)} {last}")

        for kind in ("regenerate", "edit"):
            reply = self.reject(player, save, "n-1", kind, self.body(kind, 1))
            r.check(f"對不是最後一輪的 {kind} → 409 not_revisable（not_last）", reply.status == 409 and reply.code == "not_revisable" and reply.json.get("reason") == "not_last", f"{reply.status} {reply.code} {reply.json.get('reason')}")
            reply = self.reject(player, save, "n-zzz", kind, self.body(kind, 1))
            r.check(f"不存在的回合 {kind} → 404", reply.status == 404, f"{reply.status}")
        untouched("不是最後一輪與不存在")
        other = self.fresh(coins=0)
        if other is not None:
            for kind in ("regenerate", "edit"):
                reply = self.reject(other, save, "n-2", kind, self.body(kind, 1))
                r.check(f"他人的存檔 {kind} → 404（與不存在相同）", reply.status == 404, f"{reply.status}")
            r.check("他人的嘗試沒有任何影響", self.snapshot(player, save).get("last_turn", {}).get("revision") == 1 and self.total(player) == balance)
        anonymous = Client(self.base_url, "--base-url")
        anonymous.load_csrf("/login")                                        # 未登入也拿得到 CSRF 權杖，所以擋下的是登入檢查
        reply = anonymous.call("POST", self.path(save, "n-2", "regenerate"), self.body("regenerate", 1))
        r.check("未登入 → 401", reply.status == 401, f"{reply.status} {reply.code}")
        no_csrf = self.clone(player)
        no_csrf.csrf = None
        for kind in ("regenerate", "edit"):
            reply = no_csrf.call("POST", self.path(save, "n-2", kind), self.body(kind, 1))
            r.check(f"缺 CSRF 權杖的 {kind} → 403 csrf_failed", reply.status == 403 and reply.code == "csrf_failed", f"{reply.status} {reply.code}")
        untouched("未登入與缺 CSRF")

        def invalid(label: str, kind: str, body: dict[str, Any], field: str | None = None, *, blocked: bool = False) -> None:
            reply = self.reject(player, save, "n-2", kind, body)
            if blocked:
                ok = reply.status == 422 and reply.code == "input_blocked" and bool(reply.json.get("categories"))
            else:
                fields = (reply.json.get("error") or {}).get("fields") or {}
                ok = reply.status in (400, 422) and reply.code == "validation_error" and (field is None or field in fields)
            r.check(f"{label} → {'422 input_blocked（只有類別）' if blocked else '驗證錯誤' + (f'（欄位 {field}）' if field else '')}", ok, f"{reply.status} {reply.code} {reply.json.get('error')}")

        invalid("理由太短", "regenerate", self.body("regenerate", 1, reason="太短"), "reason")
        invalid("理由太長（51 字）", "regenerate", self.body("regenerate", 1, reason="長" * 51), "reason")
        invalid("缺理由", "regenerate", {"expected_revision": 1, "category": "other"}, "reason")
        invalid("分類不在列舉內", "regenerate", self.body("regenerate", 1, category="not_a_category"), "category")
        invalid("缺 expected_revision", "regenerate", {"category": "other", "reason": self.reason()}, "expected_revision")
        invalid("編輯的行動與原本相同", "edit", self.body("edit", 1, action=second))
        invalid("編輯的行動是空白", "edit", self.body("edit", 1, action="   "))
        invalid("編輯的行動超過 1000 字", "edit", self.body("edit", 1, action="長" * 1001))
        invalid("編輯的行動命中規則層", "edit", self.body("edit", 1, action=BAD_SENTENCE), blocked=True)
        wrong = self.reject(player, save, "n-2", "regenerate", self.body("regenerate", 5))
        r.check("expected_revision 與後端不一致 → 409 revision_conflict", wrong.status == 409 and wrong.code == "revision_conflict", f"{wrong.status} {wrong.code}")
        untouched("所有被拒絕的請求")
        ok = self.revise(player, save, "n-2", "regenerate", 1)
        r.check("被拒絕之後，正確的重新生成仍然成功", (ok.done or {}).get("status") == "complete" and (ok.done or {}).get("revision") == 2, str(ok.done))

        ended = self.fresh(coins=0)
        saved = self.start_save(ended, "custom_ending") if ended else None
        if ended is None or saved is None:
            return
        turn = self.post_turn(ended, saved, "n-1", self.action(KW_ENDING))
        r.check("自訂結局的劇本：狀態呼叫提議結局，done 為 ended、存檔已完結", (turn.done or {}).get("status") == "ended" and self.snapshot(ended, saved).get("ended") is True, str(turn.done))
        for kind in ("regenerate", "edit"):
            reply = self.reject(ended, saved, "n-1", kind, self.body(kind, 1))
            r.check(f"結局那一輪的 {kind} → 409 not_revisable（ended）", reply.status == 409 and reply.code == "not_revisable" and reply.json.get("reason") == "ended", f"{reply.status} {reply.code} {reply.json.get('reason')}")
        r.check("已完結的存檔沒有扣幣", self.total(ended) == SIGNUP_GRANT - TURN_COST, str(self.total(ended)))

    def case_coins(self) -> None:
        r = self.r
        signed = self.sign_in(coins=0)
        if signed is None:
            return
        player, save = signed
        turns = SIGNUP_GRANT // TURN_COST                                     # 6 輪之後剩 20，不夠一輪
        for number in range(1, turns + 1):
            self.post_turn(player, save, f"n-{number}", self.action(KW_REGEN))
        balance = self.total(player)
        msg = f"n-{turns}"
        r.check(f"{turns} 輪之後餘額 {SIGNUP_GRANT - turns * TURN_COST}，不夠一輪（{TURN_COST}）", balance == SIGNUP_GRANT - turns * TURN_COST < TURN_COST, str(balance))
        for revision in (1, 2):
            turn = self.revise(player, save, msg, "regenerate", revision)
            r.check(f"餘額不足時，第 {revision} 次（免費）重新生成仍可進行", (turn.done or {}).get("status") == "complete" and (turn.done or {}).get("revision") == revision + 1 and self.total(player) == balance, str(turn.done))
        for kind in ("regenerate", "edit"):
            reply = self.reject(player, save, msg, kind, self.body(kind, 3))
            r.check(f"餘額不足的付費 {kind} → 409 insufficient_coins（需要 {TURN_COST}、餘額 {balance}）", reply.status == 409 and reply.code == "insufficient_coins"
                    and (reply.json.get("needed"), reply.json.get("balance")) == (TURN_COST, balance), f"{reply.status} {reply.code} {reply.json.get('needed')} {reply.json.get('balance')}")
        last = self.snapshot(player, save).get("last_turn") or {}
        r.check("餘額不足：沒有認領、revision 與免費次數不變、餘額不變", (last.get("revision"), last.get("revisable"), last.get("free_left")) == (3, True, 0) and self.total(player) == balance, f"{last} {self.total(player)}")

    def case_notebook(self) -> None:
        r = self.r
        player = self.fresh(coins=11 * TURN_COST)
        save = self.start_save(player) if player else None
        if player is None or save is None:
            return
        base = self.capture_calls()
        if not self.play_turns(player, save, 1, 10, {}):
            return
        eleventh = self.play(player, save, 11)
        r.check("第 11 輪完成", (eleventh.done or {}).get("status") == "complete" and (eleventh.done or {}).get("turn_number") == 11, str(eleventh.done))
        self.wait_until(lambda: (self.notebook(player, save).get("length") or 0) > 0)
        notebook = self.notebook(player, save)
        r.check("第 11 輪之後記事簿已有內容（版本 2）", notebook.get("version") == 2 and (notebook.get("length") or 0) > 0, str(notebook.get("version")))
        calls = self.capture_calls()
        turn = self.revise(player, save, "n-11", "regenerate", 1)
        r.check("重新生成第 11 輪：完成（revision 2）", (turn.done or {}).get("status") == "complete" and (turn.done or {}).get("revision") == 2, str(turn.done))
        time.sleep(3.0)                                                      # 若錯誤地觸發抓取，給它時間發生
        after = self.notebook(player, save)
        r.check("記事簿的版本與全文不變", (after.get("version"), after.get("text")) == (notebook.get("version"), notebook.get("text")), f"{notebook.get('version')} → {after.get('version')}")
        if base is None or calls is None:
            r.unverified("修訂沒有新的抓取呼叫", "連不上假模型的 /_status")
        else:
            r.check("修訂沒有新的抓取呼叫（第 11 輪的抓取在新行動送出時才發生）", self.capture_calls() == calls, f"{calls} → {self.capture_calls()}")
        r.check("已完成輪數仍是 11", self.snapshot(player, save).get("completed_turns") == 11)

    def log_scan(self) -> None:
        if not self.log.available:
            self.r.unverified("應用程式日誌沒有任何理由、行動、劇情與被擋的句子", "未提供 --server-log")
            return
        text = self.log.text(self.log_start)
        leaked = [needle[:12] for needle in sorted(self.watched) if needle in text]
        self.r.check(f"應用程式日誌（本次新增的 {len(text)} 字元）沒有任何理由、行動、劇情與被擋的句子（檢查 {len(self.watched)} 段）", not leaked, f"找到：{leaked}")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="rpg-turn-revision 的 HTTP 驗收（只對本機驗收環境）")
    parser.add_argument("--base-url", default="http://localhost:5010", help="應用程式")
    parser.add_argument("--helper-url", default="http://localhost:5001", help="驗收環境的切換身分小幫手")
    parser.add_argument("--model-url", default="http://127.0.0.1:5002", help="假的 RPG 模型（計數端點 /_status）")
    parser.add_argument("--server-log", type=Path, default=None, help="應用程式日誌的路徑（選填）")
    parser.add_argument("--only", default=",".join(CASES), help=f"只跑指定的案例（逗號分隔，預設全部：{','.join(CASES)}）")
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
        RevisionAcceptance(args.base_url, args.helper_url, model, report, ServerLog(args.server_log)).run(only)
    except OSError as error:
        report.check("連線到驗收環境", False, f"{type(error).__name__}（驗收環境是否已啟動？）")
    print(f"\n摘要：通過 {report.count(PASS)}、失敗 {report.count(FAIL)}、未驗證 {report.count(UNVERIFIED)}（{time.monotonic() - started:.1f} 秒）")
    return 1 if report.count(FAIL) else 0


if __name__ == "__main__":
    sys.exit(main())
