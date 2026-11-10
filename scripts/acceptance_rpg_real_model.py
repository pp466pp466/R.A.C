"""rpg-turn-engine 的真實模型驗證（任務 6.2）：對**真實的** RPG 模型各跑一次第 0 幕與三個回合，量測並輸出，不做通過或失敗的斷言。

    python scripts/acceptance_rpg_real_model.py --base-url http://localhost:5011 [--helper-url http://localhost:5001] [--server-log 路徑]

這是人工驗證，不是自動測試，也不會被 pytest 收集。前提（腳本不替你做）：
1. 啟動**第二個**應用程式行程，用驗收環境的資料庫，但**不要**覆蓋 `.env` 的 `OPENROUTER_*`（真實金鑰與模型由 `.env` 注入，腳本與輸出都不含金鑰），
   例如（在專案根目錄，埠 5011，`BASE_URL` 要與埠一致，否則 CSRF 的來源檢查會失敗）：
   `APP_ENV=development DATABASE_URL=sqlite+pysqlite:///<驗收資料庫路徑> BASE_URL=http://localhost:5011 python -m flask --app wsgi run --port 5011 --no-reload`
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
from datetime import date
from pathlib import Path

from acceptance_rpg_turn import AGE_RANGE_30, Client, TargetRefused, Turn

ACTIONS = (
    "我向守門的弟子問路，想知道宗門大測是什麼時候。",
    "我跟著他走進山門，仔細觀察周圍的環境與其他弟子。",
    "我鼓起勇氣去拜見這裡的長老，請教修行的方法。",
)


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
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        event = str(record.get("event", ""))
        if event.startswith("rpg.") or event.startswith("llm."):
            keep = {k: v for k, v in record.items() if k not in ("time", "logger", "request_id", "message", "level")}
            print(json.dumps(keep, ensure_ascii=False))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="rpg-turn-engine 的真實模型驗證（只對本機應用程式）")
    parser.add_argument("--base-url", default="http://localhost:5011")
    parser.add_argument("--helper-url", default="http://localhost:5001")
    parser.add_argument("--server-log", type=Path, default=None)
    args = parser.parse_args()
    try:
        app, helper = Client(args.base_url, "--base-url"), Client(args.helper_url, "--helper-url")
    except TargetRefused as error:
        print(f"拒絕執行：{error}", file=sys.stderr)
        return 2
    offset = args.server_log.stat().st_size if args.server_log is not None and args.server_log.exists() else 0

    reply = helper.call("GET", "/fresh/rpg")
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
    for number, action in enumerate(ACTIONS, start=1):
        started = time.monotonic()
        turn = app.stream(f"/api/game/sessions/{save}/turns/stream", {"client_msg_id": f"real-{number}", "action": action})
        describe(f"回合 {number}（{action}）", turn, time.monotonic() - started)

    snapshot = app.call("GET", f"/api/game/sessions/{save}").json.get("session", {})
    print("\n存檔狀態：已完成輪數", snapshot.get("completed_turns"), "；世界狀態", json.dumps(snapshot.get("world"), ensure_ascii=False))
    print("錢包餘額：", app.call("GET", "/api/wallet").json.get("wallet", {}).get("total"))
    log_events(args.server_log, offset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
