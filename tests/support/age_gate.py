"""年齡閘門測試的共用輔助：只在測試應用程式中註冊、代表「會呼叫 Gemini 的端點」的假端點。

寫法與 C-3 之後的 AI 端點應有的寫法相同：先 `require_login()`，再 `require_chat_access(user)`，之後才做事。
"""
from __future__ import annotations

from flask import Flask, jsonify

from app.security.session import require_login
from app.services.registry import get_access_policy

ENDPOINT = "/api/_t/ai"


def add_ai_endpoint(app: Flask) -> None:
    app.work_done = []                                       # 記錄「真的做了事」的使用者內部編號

    @app.post(ENDPOINT)
    def ai_endpoint():
        user = require_login()
        policy = get_access_policy()
        policy.require_chat_access(user)
        app.work_done.append(user.id)
        ceiling = policy.content_ceiling(user)
        return jsonify(ok=True, ceiling={"sexual_content_level": ceiling.sexual_content_level, "safe_mode": ceiling.safe_mode})
