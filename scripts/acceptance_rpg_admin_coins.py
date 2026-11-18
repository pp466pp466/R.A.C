"""admin-unlimited-coins（管理員帳號不消耗遊戲幣）的 HTTP 驗收腳本（任務 4.2）。

    python scripts/acceptance_rpg_admin_coins.py [--base-url http://localhost:5010] [--helper-url http://localhost:5001]
                                                 [--model-url http://127.0.0.1:5002] [--server-log 路徑]
                                                 [--only journey,interrupt,ordinary]

對一個**本機驗收環境**（應用程式、切換身分的小幫手、假的 RPG 模型供應商；啟動方式同 `acceptance_rpg_turn.py`）驗證：
餘額為 0 的管理員可以玩一輪與多次修訂、帳本只有金額 0 的列、餘額始終是 0，而同一個環境裡餘額不足一輪的一般玩家仍得到 409 `insufficient_coins`。
逐項輸出「通過」、「失敗」或「未驗證」，最後輸出摘要（結束代碼：沒有失敗為 0，有失敗為 1，位址不是本機為 2）。
與 `tests/test_rpg_admin_flow.py` 檢查同一組行為，差別是這裡走真正的網路連線（Werkzeug 開發伺服器、真實的串流與連線關閉、模型端是另一個行程）。

- **只對本機執行**、腳本本身不含機密、輸出只有項目名稱、數字與結果（同 `acceptance_rpg_turn.py`）。
- **管理員身分**由小幫手 `GET /fresh/rpg?admin=1` 建立（角色 admin、啟用中、尚未年齡確認；不經 Google 與密碼兩步驟、不發首登贈幣，餘額是 0）。
  驗收環境的小幫手要支援 `?admin=1`（`rpg_acceptance_harness.fresh_player(admin=…)`；舊的小幫手忽略它並建立一般玩家，第一項檢查會失敗並提示重新啟動）。
- 每個案例各用一位**全新**的管理員或玩家，案例彼此獨立，可用 `--only` 單獨重跑。三個案例：

    journey     餘額 0 的管理員：一輪 → 兩次免費修訂（重新生成與編輯共用）→ 三次付費的修訂（重新生成、編輯、再重新生成）都成功；
                `done.coins` 一律是 0、餘額始終是 0、`last_turn` 的重新生成與編輯費用是 0、`/api/wallet` 回 `coin_exempt: true` 且 `turn_cost` 為 0；
                帳本只有金額 0 的 `turn_hold`、全部已結算（免費的修訂沒有帳本列）；假模型確實收到每一次呼叫
    interrupt   管理員的回合在上游中途斷線：退回（金額 0）、餘額仍是 0；以同一編號重送，`attempt` 加 1、仍是 0 幣；
                記錄頁不顯示已退回的預扣與退回列（設計如此），所以只看得到重送的那一筆 0 幣預扣
    ordinary    一般玩家（首登贈幣 200）用到餘額不足一輪後：`coin_exempt` 為假、`turn_cost` 為 30；再送一輪得到 409 `insufficient_coins`、
                沒有回合列、餘額不變

- `--server-log` 是應用程式日誌的路徑：提供時，腳本掃描本次新增的內容，確認沒有玩家行動、劇情與被擋的句子（客服的額度與 `llm_call_logs` 不經 HTTP，由
  `tests/test_rpg_admin_flow.py` 檢查）。假模型的 `GET /_status`（`--model-url`）提供呼叫計數，連不上時相關項目標為「未驗證」。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from acceptance_rpg_notebook import Player, ServerLog
from acceptance_rpg_revision import (
    EDIT_MARK, FREE_REGENERATIONS, KW_REGEN, REGEN_MARK, RevisionAcceptance,
)
from acceptance_rpg_turn import FAIL, PASS, SIGNUP_GRANT, TURN_COST, UNVERIFIED, Client, Report, TargetRefused, fetch_model_status

KW_CUT = "測試中斷"
CASES = ("journey", "interrupt", "ordinary")
LEDGER_KINDS = frozenset({"turn_hold", "turn_refund"})          # 管理員的帳本只有這兩種；沒有簽到、贈幣或調整


class AdminCoinsAcceptance(RevisionAcceptance):
    # ---- 小工具 -----------------------------------------------------------------------------------

    def fresh_admin(self) -> Player | None:
        """建立全新的管理員並完成年齡確認（管理員在帳號設定頁補填，暱稱選填）：回傳的 `Player` 沒有補給任何幣。"""
        r = self.r
        helper = Client(self.helper_url, "--helper-url")
        reply = helper.call("GET", "/fresh/rpg?admin=1")
        app = Client(self.base_url, "--base-url")
        app.cookies = dict(helper.cookies)
        versions = reply.json.get("versions") or {}
        if not r.check("小幫手建立全新管理員（?admin=1）", reply.status == 200 and bool(app.cookies) and bool(versions.get("endless")),
                       f"{reply.status}（驗收環境的小幫手是否支援 ?admin=1？改過 rpg_acceptance_harness.py 與 acceptance.py 之後要重新啟動）"):
            return None
        if not r.check("取得 CSRF 權杖（管理員在 /account，/age 只給待確認的玩家）", app.load_csrf("/account")):
            return None
        wallet = app.call("GET", "/api/wallet").json.get("wallet") or {}
        if not r.check("這個帳號是管理員（/api/wallet 的 coin_exempt 為真）", wallet.get("coin_exempt") is True, str(wallet.get("coin_exempt"))):
            return None
        confirmed = app.call("POST", "/api/auth/verify-age", {"birth_date": self._birth_date(), "age_range": "25_34"})
        if not r.check("管理員年齡確認通過（200，沒有暱稱）", confirmed.status == 200, f"{confirmed.status} {confirmed.text[:120]}"):
            return None
        player = Player(app, versions, 0)
        r.check("管理員不發首登贈幣：餘額是 0", self.total(player) == 0, str(self.total(player)))
        return player

    @staticmethod
    def _birth_date() -> str:
        from datetime import date

        today = date.today()
        try:
            return today.replace(year=today.year - 30).isoformat()
        except ValueError:
            return today.replace(year=today.year - 30, day=28).isoformat()

    def wallet_of(self, player: Player) -> dict[str, Any]:
        return player.client.call("GET", "/api/wallet").json.get("wallet") or {}

    def ledger(self, player: Player) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        before = None
        for _ in range(20):
            reply = player.client.call("GET", "/api/wallet/entries?limit=100" + (f"&before={before}" if before else ""))
            entries.extend(reply.json.get("entries", []))
            before = reply.json.get("next_before")
            if not before:
                break
        return entries

    def ledger_is_all_zero(self, label: str, player: Player, holds: int) -> None:
        entries = self.ledger(player)
        deltas = [(entry["free_delta"], entry["paid_delta"]) for entry in entries]
        kinds = sorted({str(entry["kind"]) for entry in entries})
        self.r.check(f"{label}：記錄頁有 {holds} 筆 turn_hold、每一列的增減量都是 0、沒有仍在預扣中的項目、種類只有預扣與退回",
                     sum(1 for entry in entries if entry["kind"] == "turn_hold") == holds and all(delta == (0, 0) for delta in deltas)
                     and not any(entry["pending"] for entry in entries) and set(kinds) <= LEDGER_KINDS, f"{len(entries)} 筆 {kinds} {deltas[:6]}")

    # ---- 案例 ---------------------------------------------------------------------------------------

    def run(self, only: tuple[str, ...]) -> None:
        for name in CASES:
            if name in only:
                print(f"\n== 案例 {name} ==", flush=True)
                getattr(self, f"case_{name}")()
        self.log_scan()

    def case_journey(self) -> None:
        r = self.r
        admin = self.fresh_admin()
        save = self.start_save(admin) if admin else None
        if admin is None or save is None:
            return
        wallet = self.wallet_of(admin)
        r.check("錢包：coin_exempt 為真、turn_cost.standard 為 0、餘額 0（沒有被發放任何幣）",
                (wallet.get("coin_exempt"), (wallet.get("turn_cost") or {}).get("standard"), wallet.get("total")) == (True, 0, 0), str(wallet))
        calls = (fetch_model_status(self.model) or {}).get("stream_calls")
        original = self.action(KW_REGEN)
        first = self.post_turn(admin, save, "n-1", original)
        done = first.done or {}
        r.check("餘額 0 的管理員送出第 1 輪：完成（沒有 insufficient_coins）、done.coins 為 0、剩餘免費 2",
                (first.status, done.get("status"), done.get("turn_number"), done.get("coins"), done.get("free_left")) == (200, "complete", 1, 0, FREE_REGENERATIONS), f"{first.status} {first.error_code} {done}")
        r.check("第 1 輪之後餘額仍是 0", self.total(admin) == 0, str(self.total(admin)))
        last = self.snapshot(admin, save).get("last_turn") or {}
        r.check("快照的 last_turn：可修訂、免費 2、重新生成與編輯的費用都是 0", (last.get("revisable"), last.get("free_left"), last.get("regenerate_cost"), last.get("edit_cost")) == (True, FREE_REGENERATIONS, 0, 0), str(last))

        one = self.revise(admin, save, "n-1", "regenerate", 1)
        self.applied("管理員第 1 次操作：重新生成（免費）", one, kind="regenerate", msg="n-1", revision=1, turn_number=1, free_left=1, mark=REGEN_MARK)
        two = self.revise(admin, save, "n-1", "edit", 2)
        self.applied("管理員第 2 次操作：編輯行動（免費，與重新生成共用免費次數）", two, kind="edit", msg="n-1", revision=2, turn_number=1, free_left=0, mark=EDIT_MARK)
        r.check("兩次免費的修訂：餘額仍是 0、帳本只有第 1 輪的預扣一列（免費的修訂沒有預扣）", self.total(admin) == 0 and len(self.ledger(admin)) == 1, f"{self.total(admin)} {len(self.ledger(admin))}")
        last = self.snapshot(admin, save).get("last_turn") or {}
        r.check("免費次數用完（一次重新生成加一次編輯）：free_left 0，但重新生成與編輯的費用仍是 0（一般玩家這時是 30）",
                (last.get("free_left"), last.get("regenerate_cost"), last.get("edit_cost")) == (0, 0, 0), str(last))

        three = self.revise(admin, save, "n-1", "regenerate", 3)
        self.applied("管理員第 3 次操作：重新生成（超過免費次數，預扣 0）", three, kind="regenerate", msg="n-1", revision=3, turn_number=1, free_left=0, mark=EDIT_MARK)
        four = self.revise(admin, save, "n-1", "edit", 4)
        self.applied("管理員第 4 次操作：編輯行動（超過免費次數，預扣 0）", four, kind="edit", msg="n-1", revision=4, turn_number=1, free_left=0, mark=EDIT_MARK)
        five = self.revise(admin, save, "n-1", "regenerate", 5)
        self.applied("管理員第 5 次操作：再重新生成", five, kind="regenerate", msg="n-1", revision=5, turn_number=1, free_left=0, mark=EDIT_MARK)
        coins = [(turn.done or {}).get("coins") for turn in (three, four, five)]
        r.check("三次超過免費次數的修訂：done.coins 都是 0、餘額始終是 0", coins == [0, 0, 0] and self.total(admin) == 0, f"{coins} {self.total(admin)}")
        last = self.snapshot(admin, save).get("last_turn") or {}
        r.check("最後的 last_turn：revision 6、可修訂、費用 0", (last.get("revision"), last.get("revisable"), last.get("regenerate_cost"), last.get("edit_cost")) == (6, True, 0, 0), str(last))
        self.ledger_is_all_zero("旅程", admin, holds=4)                       # 第 1 輪，加上三次超過免費次數的修訂各一筆
        if calls is None:
            r.unverified("模型照常被呼叫（管理員不扣幣不等於不呼叫模型）", "連不上假模型的 /_status")
        else:
            now = (fetch_model_status(self.model) or {}).get("stream_calls") or 0
            r.check("模型照常被呼叫：一輪加五次修訂的串流呼叫都到了假模型（至少 6 次）", now - calls >= 6, f"{calls} → {now}")

    def case_interrupt(self) -> None:
        r = self.r
        admin = self.fresh_admin()
        save = self.start_save(admin) if admin else None
        if admin is None or save is None:
            return
        action = self.action(KW_CUT)
        broken = self.post_turn(admin, save, "c-1", action)
        r.check("上游中途斷線：最後一個事件是 error、沒有 done", broken.names[-1:] == ["error"] and "done" not in broken.names, str(broken.names))
        r.check("斷線之後餘額仍是 0（沒有變成負數）", self.total(admin) == 0, str(self.total(admin)))
        view = self.turn_of(admin, save, "c-1")
        r.check("回合標為 interrupted、不含部分劇情", view.get("status") == "interrupted" and view.get("story") is None, str({k: v for k, v in view.items() if k != "story"}))
        retry = self.post_turn(admin, save, "c-1", action)
        meta = (retry.of("meta") or [{}])[0]
        r.check("以同一編號重送：attempt 為 2、完成、done.coins 為 0", (meta.get("attempt"), (retry.done or {}).get("status"), (retry.done or {}).get("coins")) == (2, "complete", 0), f"{retry.names} {retry.done}")
        r.check("重送之後餘額仍是 0", self.total(admin) == 0, str(self.total(admin)))
        # 兩次嘗試各有一筆預扣（新的鍵），第一筆已退回；記錄頁不顯示已退回的預扣與退回列，所以只看得到重送的那一筆（attempt 2 已由 meta 證明）
        self.ledger_is_all_zero("中斷與重送", admin, holds=1)

    def case_ordinary(self) -> None:
        r = self.r
        player = self.fresh(coins=0)
        save = self.start_save(player) if player else None
        if player is None or save is None:
            return
        turns = SIGNUP_GRANT // TURN_COST                                     # 6 輪之後剩 20，不夠一輪
        for number in range(1, turns + 1):
            self.post_turn(player, save, f"o-{number}", self.action())
        balance = self.total(player)
        r.check(f"{turns} 輪之後餘額 {SIGNUP_GRANT - turns * TURN_COST}，不夠一輪（{TURN_COST}）", balance == SIGNUP_GRANT - turns * TURN_COST < TURN_COST, str(balance))
        wallet = self.wallet_of(player)
        r.check(f"一般玩家的錢包：coin_exempt 為假、turn_cost.standard 為 {TURN_COST}", (wallet.get("coin_exempt"), (wallet.get("turn_cost") or {}).get("standard")) == (False, TURN_COST), str(wallet))
        count = len(player.client.call("GET", f"/api/game/sessions/{save}/turns").json.get("turns", []))
        broke = self.post_turn(player, save, "o-broke", self.action())
        r.check("一般玩家餘額不足：409 insufficient_coins（JSON，不開串流）", broke.status == 409 and broke.error_code == "insufficient_coins", f"{broke.status} {broke.error_code}")
        after = len(player.client.call("GET", f"/api/game/sessions/{save}/turns").json.get("turns", []))
        r.check("一般玩家餘額不足：沒有留下回合列、餘額不變", after == count and self.total(player) == balance, f"{count} → {after}，{balance} → {self.total(player)}")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="admin-unlimited-coins 的 HTTP 驗收（只對本機驗收環境）")
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
        AdminCoinsAcceptance(args.base_url, args.helper_url, model, report, ServerLog(args.server_log)).run(only)
    except OSError as error:
        report.check("連線到驗收環境", False, f"{type(error).__name__}（驗收環境是否已啟動？）")
    print(f"\n摘要：通過 {report.count(PASS)}、失敗 {report.count(FAIL)}、未驗證 {report.count(UNVERIFIED)}（{time.monotonic() - started:.1f} 秒）")
    return 1 if report.count(FAIL) else 0


if __name__ == "__main__":
    sys.exit(main())
