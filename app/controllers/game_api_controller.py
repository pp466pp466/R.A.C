"""Web API Controller：`/api/game/*`（RPG 回合與第 0 幕的串流，以及唯讀的存檔與回合），見 rpg-turn-stream 規格與 design D1、D2。

- `POST /api/game/sessions/<id>/turns/stream`：本文 `{"client_msg_id": "…", "action": "…"}`，回應 `text/event-stream`。
- `POST /api/game/sessions/<id>/opening/stream`：第 0 幕（開場），沒有本文欄位；已完成者回傳既有文字，不重新生成。
- `GET  /api/game/sessions/<id>`：存檔狀態、第 0 幕、世界狀態的玩家視圖與已完成輪數（已完結的存檔仍可讀）。
- `GET  /api/game/sessions/<id>/turns?since_seq=&limit=`：序號遞增的回合；被擋與中斷的回合不含任何部分內容。

Controller 只讀取請求並回傳 `Response`：閘門、驗證、擁有者（非擁有者與不存在同為 404）、預扣與搶佔都在 `GameService`。
**開始前的失敗**（驗證、閘門、餘額不足、衝突、404、沒有供應商）都在建立 `Response` 之前由 `GameService` 拋出，
由 controllers/errors.py 轉成一般 JSON 錯誤，不開啟事件串流。串流開始後的失敗一律是 `error` 事件。
`TurnRunner`／`OpeningRunner` 與其依賴（工作階段工廠、供應商、設定、時鐘）**必須在建立 Response 之前**取出（`registry.build_*`）：
產生器在 `after_request` 之後才執行，不能使用請求的工作階段、`g` 或 `current_app`。
回合的記事簿抓取（rpg-notebook 的 design D1）在回應**關閉之後**（`response.call_on_close`）執行：事件序列不變、`done` 之後連線隨即結束，
抓取的依賴同樣在請求內取好；回呼只在 `TurnRunner.capture_target()` 有值（`done` 已被取走的正常完成回合）時才抓取，且絕不拋出。

CSRF 與 Origin 在 `before_request` 統一檢查；受限登入狀態（鎖定、等待刪除、待確認年齡）由 `require_login` 預設拒絕。
`/api/game/` 的 JSON 回應禁止快取（`cache_headers.py`）；事件串流自帶 `Cache-Control: no-cache, no-transform`。
"""
from __future__ import annotations

from typing import Callable, Iterable

from flask import Blueprint, Response, jsonify

from app.controllers.request_helpers import int_arg, read_json_object
from app.observability import rpg_events
from app.security.session import require_login
from app.services.errors import ServiceUnavailable
from app.services.game_service import TURNS_PAGE_MAX
from app.services.registry import build_notebook_capturer, build_opening_runner, build_turn_runner, get_game_service, has_rpg_provider
from app.services.rpg_replay import opening_replay_events, turn_replay_events
from app.services.rpg_runner_base import SseEvent
from app.services.rpg_turn_runner import TurnRunner

game_api_bp = Blueprint("game_api", __name__, url_prefix="/api/game")

# 回應標頭（rpg-turn-stream 規格）：關閉中介快取與轉換，並要求反向代理（Nginx）不要緩衝事件
SSE_CONTENT_TYPE = "text/event-stream; charset=utf-8"
SSE_CACHE_CONTROL = "no-cache, no-transform"
NO_PROVIDER_MESSAGE = "故事引擎目前無法使用，請稍後再試。"


def _event_stream(events: Iterable[SseEvent], *, after_close: Callable[[], None] | None = None) -> Response:
    """把事件逐個編碼送出。客戶端斷線時 WSGI 伺服器會關閉回應，這裡把關閉動作傳給事件產生器（`TurnRunner`／`OpeningRunner`
    的 `GeneratorExit` 路徑負責收尾與退款）。`after_close` 在回應關閉之後呼叫（串流已結束、不影響事件序列與結束時間）。"""

    def body():
        try:
            for event in events:
                yield event.encode()
        finally:
            close = getattr(events, "close", None)
            if close is not None:
                close()

    response = Response(body(), content_type=SSE_CONTENT_TYPE)
    response.headers["Cache-Control"] = SSE_CACHE_CONTROL
    response.headers["X-Accel-Buffering"] = "no"
    if after_close is not None:
        response.call_on_close(after_close)
    return response


def _notebook_follow_up(runner: TurnRunner) -> Callable[[], None]:
    """回應關閉之後的記事簿抓取。依賴在這裡（請求內）取好；回呼先問 `runner.capture_target()`，沒有值（被擋、中斷、結局、`done` 寫入前
    斷線）就什麼都不做。`NotebookCapturer.run` 本身不拋出，這裡再包一層：任何例外都只記一筆列舉事件，不得影響連線的收尾。"""
    capturer = build_notebook_capturer()

    def follow_up() -> None:
        target = runner.capture_target()
        if target is None:
            return
        try:
            capturer.run(target)
        except Exception:                                                    # noqa: BLE001 — 背景工作不得影響已送達的回合
            try:
                rpg_events.log_notebook_failed(
                    user_id=target.user_id, session_id=target.session_id, boundary=None, reason="unavailable", calls=0, latency_ms=0, model=None,
                )
            except Exception:                                                # noqa: BLE001
                pass

    return follow_up


def _require_provider() -> None:
    """沒有 RPG 模型供應商時（例如本機開發沒有金鑰）在預扣之前就回 503：不扣幣、不搶佔、不寫入任何資料。"""
    if not has_rpg_provider():
        raise ServiceUnavailable(NO_PROVIDER_MESSAGE)


@game_api_bp.post("/sessions/<session_id>/turns/stream")
def stream_turn(session_id: str):
    user = require_login()
    body = read_json_object()
    _require_provider()
    game = get_game_service()
    begun = game.begin_turn(user, session_id, client_msg_id=body.get("client_msg_id"), action=body.get("action"))
    if begun.replay is not None:                                  # 已完成的重送：不呼叫模型、不碰錢包
        return _event_stream(turn_replay_events(begun.replay, coins=game.balance(user)))
    assert begun.plan is not None
    runner = build_turn_runner(begun.plan, user)
    return _event_stream(runner.events(), after_close=_notebook_follow_up(runner))


@game_api_bp.post("/sessions/<session_id>/opening/stream")
def stream_opening(session_id: str):
    user = require_login()
    _require_provider()
    game = get_game_service()
    begun = game.begin_opening(user, session_id)
    if begun.done is not None:                                    # 已 ready／fallback（含舊存檔）：回傳既有文字，不重抽
        return _event_stream(opening_replay_events(begun.done, coins=game.balance(user)))
    assert begun.plan is not None
    return _event_stream(build_opening_runner(begun.plan).events())


@game_api_bp.get("/sessions/<session_id>")
def get_session(session_id: str):
    user = require_login()
    return jsonify(session=get_game_service().session_snapshot(user, session_id))


@game_api_bp.get("/sessions/<session_id>/turns")
def list_turns(session_id: str):
    user = require_login()
    turns, has_more = get_game_service().turn_views(
        user, session_id, since_seq=int_arg("since_seq", 0), limit=int_arg("limit", TURNS_PAGE_MAX),
    )
    return jsonify(turns=turns, has_more=has_more)
