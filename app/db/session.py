"""請求級資料庫工作階段（見 AGENTS.md 第 3、4 節）。

每個請求使用單一 Session：
- 處理成功（狀態碼小於 400）時提交；
- 處理失敗或回應為錯誤時回復；
- 不論結果如何，請求結束時一定關閉並歸還連線。
Session 是惰性的：沒有實際查詢的請求不會佔用連線。
"""
from __future__ import annotations

from flask import Flask, Response, g
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_G_KEY = "db_session"


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
        if session is not None and response.status_code < 400:
            # 提交失敗會拋出例外，由 Flask 轉為 500，且不會留下半套資料
            session.commit()
        return response

    @app.teardown_request
    def _release_session(_exc: BaseException | None) -> None:
        session: Session | None = g.pop(_G_KEY, None)
        if session is not None:
            try:
                session.rollback()  # 已提交則無作用；未提交（錯誤回應或例外）則回復
            finally:
                session.close()  # 一定歸還連線

    return factory


def get_session() -> Session:
    """取得目前請求的 Session；僅供組裝層（services/registry）使用，端點不得直接呼叫。"""
    session: Session | None = g.get(_G_KEY)
    if session is None:
        raise RuntimeError("目前不在請求中，沒有可用的資料庫工作階段。")
    return session
