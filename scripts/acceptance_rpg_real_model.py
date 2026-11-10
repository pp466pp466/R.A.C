"""rpg-turn-engine 與 rpg-notebook 的真實模型驗證（任務 6.2、5.3）：對**真實的** RPG 模型跑第 0 幕與數個回合，量測並輸出，不做通過或失敗的斷言。

    python scripts/acceptance_rpg_real_model.py --base-url http://localhost:5011 [--helper-url http://localhost:5001] [--server-log 路徑] [--notebook]

預設跑第 0 幕與 3 個回合（rpg-turn-engine）。`--notebook`（rpg-notebook）改為跑 11 輪（約 330 幣，向小幫手補給付費幣），第 11 輪結束後觀察記事簿：
抓取在回應關閉後才執行，所以輪詢檢視端點直到有內容（最多 100 秒），輸出條數與各條字數（對照提示詞要求的每次 5 條、每條 50 字）、耗時與全文（品質由人判斷），
並在提供 `--server-log` 時統計日誌事件（規則層是否誤擋看 `rpg.notebook_abandoned` 的原因 `blocked`、`rpg.turn_blocked`）。

這是人工驗證，不是自動測試，也不會被 pytest 收集。前提（腳本不替你做）：
1. 啟動**第二個**應用程式行程，用驗收環境的資料庫，但**不要**覆蓋 `.env` 的 `OPENROUTER_*`（真實金鑰與模型由 `.env` 注入，腳本與輸出都不含金鑰），
   例如（在專案根目錄，埠 5011，`BASE_URL` 要與埠一致，否則 CSRF 的來源檢查會失敗；要讓腳本讀日誌就把標準輸出導向檔案，路徑傳給 `--server-log`）：
   `APP_ENV=development DATABASE_URL=sqlite+pysqlite:///<驗收資料庫路徑> BASE_URL=http://localhost:5011 python -m flask --app wsgi run --port 5011 --no-reload > <日誌路徑> 2>&1`
2. 驗收環境（`newprojet-acceptance/acceptance.py serve`）的小幫手在執行：它簽出登入 cookie（兩個應用程式共用同一把 SECRET_KEY 與資料庫）。
- **只對本機執行**（同 `acceptance_rpg_turn.py`）。送給模型的只有劇本的內建內容與本腳本寫死的三句示範行動，沒有任何真實個資。
- 輸出每次呼叫的首字時間、整段時間、字數、狀態事件（是否更新、地點、經過月數、通知），並在提供 `--server-log` 時列出
  應用程式日誌裡本次新增的 `rpg.*` 事件（首字毫秒、丟棄項目數、狀態呼叫是否有效），供填入驗收紀錄。
- 劇情全文會印在輸出裡（內容是示範行動的結果），用來人工判斷語言、長度與品質；不寫入任何檔案。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

from acceptance_rpg_turn import AGE_RANGE_30, TURN_COST, Client, TargetRefused, Turn

# 前 3 句是 rpg-turn-engine 的示範行動（預設只跑這 3 句）；第 4 至 11 句只在 --notebook 使用：延續同一條故事線，
# 並刻意讓玩家確立幾個可被記住的設定（答應送信、買乾糧），讓記事簿有東西可抓。全是虛構內容，沒有任何真實個資。
ACTIONS = (
    "我向守門的弟子問路，想知道宗門大測是什麼時候。",
    "我跟著他走進山門，仔細觀察周圍的環境與其他弟子。",
    "我鼓起勇氣去拜見這裡的長老，請教修行的方法。",
    "我向同期入門的新弟子自我介紹，問他們是從哪裡來的。",
    "我去藏書閣借了一本介紹修行基礎的書。",
    "我回到住處打坐，嘗試感受體內的靈氣。",
    "我答應一位師兄，幫他把一封信送到山下的鎮上。",
    "我下山走到鎮上，找到收信的人，把信交給他。",
    "我在鎮上的茶館休息，聽旁人談論最近宗門的傳聞。",
    "我買了一些乾糧，準備明天一早回山門。",
    "我回到山門，向長老報告這一趟下山的見聞。",
)
BASIC_TURNS = 3
NOTEBOOK_TURNS = len(ACTIONS)                       # 第 11 輪完成後才會觸發第一次抓取（完成 10 輪之後的下一輪）
PROMPT_ENTRIES, PROMPT_ENTRY_CHARS = 5, 50          # 提示詞要求的每次條數與每條字數（rpg_notebook_prompt.MAX_ENTRIES_PER_CAPTURE／MAX_ENTRY_CHARS）
NOTEBOOK_WAIT_SECONDS = 100                         # 抓取的總預算是 60 秒；多留餘裕


def describe(label: str, turn: Turn, wall: float) -> None:
    first = turn.first_at("token")
    done = turn.done or {}
    print(f"\n== {label} ==")
    print(f"事件：{' '.join(turn.names) if len(turn.names) < 14 else str(len(turn.names)) + ' 個事件，結尾 ' + ' '.join(turn.names[-3:])}")
    print(f"首字 {first:.1f} 秒" if first is not None else "沒有任何片段", f"；整段 {wall:.1f} 秒；{len(turn.text)} 字；done={done or '（無）'}")
    for state in turn.of("state"):
        print("狀態事件：", json.dumps(state, ensure_ascii=False))
    for transition in turn.of("transition"):
        print("轉場：", json.dumps(transition, ensure_ascii=False))
    for error in turn.of("error"):
        print("錯誤事件：", json.dumps(error, ensure_ascii=False))
    print(turn.text or "（沒有劇情）")


def log_events(path: Path | None, offset: int) -> None:
    if path is None or not path.exists():
        print("\n（未提供 --server-log，略過日誌事件）")
        return
    with path.open("rb") as handle:
        handle.seek(offset)
        lines = handle.read().decode("utf-8-sig", "replace").splitlines()
    print("\n== 應用程式日誌的 rpg.* 與模型呼叫事件 ==")
    tally: Counter[str] = Counter()
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        event = str(record.get("event", ""))
        if event.startswith("rpg.") or event.startswith("llm."):
            keep = {k: v for k, v in record.items() if k not in ("time", "logger", "request_id", "message", "level")}
            print(json.dumps(keep, ensure_ascii=False))
            tally[event + (f"（{record['reason']}）" if event == "rpg.notebook_abandoned" and record.get("reason") else "")] += 1
    print("\n事件統計：" + "、".join(f"{event}={count}" for event, count in sorted(tally.items())))


def plan(notebook: bool) -> tuple[tuple[str, ...], int]:
    """要走的行動與向小幫手補給的付費幣：預設 3 輪不補（首登的 200 幣夠用）；記事簿要 11 輪，首登的 200 幣只夠 6 輪，所以補給 11 輪的幣。"""
    turns = ACTIONS if notebook else ACTIONS[:BASIC_TURNS]
    return turns, (len(turns) * TURN_COST if notebook else 0)


def observe_notebook(app: Client, save: str, since: float) -> None:
    """第 11 輪的抓取在回應關閉後才執行（用的是第 1 至 10 輪）：輪詢檢視端點直到有內容，輸出條數、各條字數與全文（內容來自示範行動，不含個資）。"""
    notebook: dict = {}
    deadline = time.monotonic() + NOTEBOOK_WAIT_SECONDS
    while time.monotonic() < deadline:
        notebook = app.call("GET", f"/api/game/sessions/{save}/notebook").json.get("notebook") or {}
        if (notebook.get("length") or 0) > 0:
            break
        time.sleep(0.5)
    waited = time.monotonic() - since
    print("\n== 記事簿（第 11 輪完成後的抓取）==")
    text = str(notebook.get("text") or "")
    if not text:
        print(f"{NOTEBOOK_WAIT_SECONDS} 秒內記事簿仍是空的：抓取失敗、被放棄或仍在進行（看下方日誌事件；沒有日誌時無法區分）")
        return
    entries = text.split("\n")
    lengths = [len(entry) for entry in entries]
    over = sum(1 for length in lengths if length > PROMPT_ENTRY_CHARS)
    print(f"第 11 輪結束後約 {waited:.1f} 秒內完成；{len(entries)} 條（提示詞要求最多 {PROMPT_ENTRIES} 條）；總長 {notebook.get('length')} 字（容量 {notebook.get('capacity')}）；版本 {notebook.get('version')}；已滿 {notebook.get('full')}")
    print(f"各條字數 {lengths}（提示詞要求每條 {PROMPT_ENTRY_CHARS} 字內；超過的有 {over} 條）")
    for entry in entries:
        print("  -", entry)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="rpg-turn-engine 的真實模型驗證（只對本機應用程式）")
    parser.add_argument("--base-url", default="http://localhost:5011")
    parser.add_argument("--helper-url", default="http://localhost:5001")
    parser.add_argument("--server-log", type=Path, default=None)
    parser.add_argument("--notebook", action="store_true", help=f"走 {NOTEBOOK_TURNS} 輪並觀察記事簿的抓取（補給 {NOTEBOOK_TURNS * TURN_COST} 付費幣；預設只走 {BASIC_TURNS} 輪）")
    args = parser.parse_args()
    try:
        app, helper = Client(args.base_url, "--base-url"), Client(args.helper_url, "--helper-url")
    except TargetRefused as error:
        print(f"拒絕執行：{error}", file=sys.stderr)
        return 2
    offset = args.server_log.stat().st_size if args.server_log is not None and args.server_log.exists() else 0

    turns, coins = plan(args.notebook)
    reply = helper.call("GET", f"/fresh/rpg?coins={coins}" if coins else "/fresh/rpg")
    version = (reply.json.get("versions") or {}).get("endless")
    if reply.status != 200 or not version:
        print("無法建立驗收玩家（驗收環境的小幫手是否在執行？）", file=sys.stderr)
        return 1
    app.cookies = dict(helper.cookies)
    app.load_csrf()
    today = date.today()
    born = today.replace(year=today.year - 30, day=min(today.day, 28))
    if app.call("POST", "/api/auth/verify-age", {"birth_date": born.isoformat(), "age_range": AGE_RANGE_30, "nickname": "驗證玩家"}).status != 200:
        print("年齡確認失敗", file=sys.stderr)
        return 1

    setup = app.call("GET", f"/api/sessions/setup/{version}").json
    card = {"name": "林舟", "personality": "沉默愛思考", "age": 19, "appearance": "高瘦，常穿灰衣"}
    anchor = {"kind": "character", "ref": setup["characters"][0]["name"], "text": "他是我少年時的舊識。"}
    started = time.monotonic()
    created = app.call("POST", "/api/sessions", {"version_id": version, "protagonist": card, "anchor": anchor}, timeout=120)
    print(f"開存檔（含模型層審查）：{created.status}，{time.monotonic() - started:.1f} 秒")
    if created.status != 201:
        print(created.text[:300], file=sys.stderr)
        return 1
    save = created.json["session"]["id"]

    started = time.monotonic()
    opening = app.stream(f"/api/game/sessions/{save}/opening/stream", {})
    describe("第 0 幕", opening, time.monotonic() - started)
    last_done = time.monotonic()
    for number, action in enumerate(turns, start=1):
        started = time.monotonic()
        turn = app.stream(f"/api/game/sessions/{save}/turns/stream", {"client_msg_id": f"real-{number}", "action": action})
        last_done = time.monotonic()
        describe(f"回合 {number}（{action}）", turn, last_done - started)
    if args.notebook:
        observe_notebook(app, save, last_done)
        time.sleep(1.5)                                             # 事件在提交之後才寫入日誌；多等一下再讀

    snapshot = app.call("GET", f"/api/game/sessions/{save}").json.get("session", {})
    print("\n存檔狀態：已完成輪數", snapshot.get("completed_turns"), "；世界狀態", json.dumps(snapshot.get("world"), ensure_ascii=False))
    print("錢包餘額：", app.call("GET", "/api/wallet").json.get("wallet", {}).get("total"))
    log_events(args.server_log, offset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
