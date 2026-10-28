"""請求級資料庫工作階段（見 AGENTS.md 第 3、4 節）。

每個請求使用單一 Session：
- 處理成功（狀態碼小於 400）時提交；
- 處理失敗或回應為錯誤時回復；
- 不論結果如何，請求結束時一定關閉並歸還連線。
Session 是惰性的：沒有實際查詢的請求不會佔用連線。

提交成功後才執行的「延後動作」（例如寄信）：用 `defer_until_commit()` 登記，在提交成功之後、
回應送出之後（`response.call_on_close`）依序執行。因此交易已經完成、寄信不會增加回應時間，
也不會在交易中持有鎖。此時請求與 Session 都已關閉，動作必須在登記時就把所需的資料綁好，
不得再使用 `g`、`current_app` 或資料庫。錯誤狀態或回復時，所有已登記的動作一律丟棄。
"""
from __future__ import annotations

import logging
from typing import Callable

from flask import Flask, Response, g
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.observability.errors import describe_exception

logger = logging.getLogger(__name__)

_G_KEY = "db_session"
_DEFERRED_KEY = "deferred_actions"
_KEEP_KEY = "keep_changes_on_client_error"


def init_session(app: Flask, engine: Engine) -> sessionmaker[Session]:
    """替應用程式掛上請求級 Session 的生命週期。"""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app.extensions["db_engine"] = engine
    app.extensions["db_session_factory"] = factory

    @app.before_request
    def _open_session() -> None:
        setattr(g, _G_KEY, factory())

    @app.after_request
    def _commit_on_success(response: Response) -> Response:
        session: Session | None = g.get(_G_KEY)
        keep = g.pop(_KEEP_KEY, False) and response.status_code < 500
        if session is not None and (response.status_code < 400 or keep):
            # 提交失敗會拋出例外，由 Flask 轉為 500，且不會留下半套資料（延後動作也隨之丟棄）
            session.commit()
            actions = g.pop(_DEFERRED_KEY, None)
            if actions:
                response.call_on_close(lambda: _run_deferred(actions))
        else:
            g.pop(_DEFERRED_KEY, None)  # 錯誤回應：交易會回復，延後動作一律丟棄
        return response

    @app.teardown_request
    def _release_session(_exc: BaseException | None) -> None:
        g.pop(_DEFERRED_KEY, None)  # 例外中止的請求：沒有走到 after_request，這裡確保不會殘留
        g.pop(_KEEP_KEY, None)
        session: Session | None = g.pop(_G_KEY, None)
        if session is not None:
            try:
                session.rollback()  # 已提交則無作用；未提交（錯誤回應或例外）則回復
            finally:
                session.close()  # 一定歸還連線

    return factory


def keep_changes_on_error() -> None:
    """要求即使這個請求以 4xx 結束，目前 Session 的變更仍然提交。

    預設的規則是「狀態碼 400 以上就回復」。但有些變更必須在失敗的回應之後仍然存在，
    典型是登入失敗次數：登入失敗回 401，若隨之回復，失敗次數永遠不會累積，帳號就永遠不會被鎖定。
    只對 4xx 有效；5xx（伺服器錯誤）與未處理的例外一律回復。只在「變更本身就是失敗的記帳」時使用。
    """
    setattr(g, _KEEP_KEY, True)


def defer_until_commit(action: Callable[[], None]) -> None:
    """登記一個「請求提交成功後、回應送出後」才執行的動作。

    動作不得使用請求物件或資料庫（執行時它們都已關閉），所需的資料請在登記時綁進去，例如
    `defer_until_commit(lambda: sender.send(to, subject, body))`。動作失敗只會被記錄（錯誤類型），
    不影響回應、已提交的資料與其他動作。
    """
    actions: list[tuple[Callable[[], None], str | None]] = g.setdefault(_DEFERRED_KEY, [])
    actions.append((action, g.get("request_id")))


def _run_deferred(actions: list[tuple[Callable[[], None], str | None]]) -> None:
    for action, request_id in actions:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - 單一動作失敗不得影響其他動作；訊息可能含機密，只記錄類型與位置
            logger.error("延後動作失敗", extra={"request_id": request_id, **describe_exception(exc)})


def get_session() -> Session:
    """取得目前請求的 Session；僅供組裝層（services/registry）使用，端點不得直接呼叫。"""
    session: Session | None = g.get(_G_KEY)
    if session is None:
        raise RuntimeError("目前不在請求中，沒有可用的資料庫工作階段。")
    return session
