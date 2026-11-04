"""劇本創作的 MVC Page Controller：只渲染 `/scripts` 的頁面外殼與初始狀態，回應一律是 HTML（見 AGENTS.md 第 3 節與 script-authoring 規格）。

所有資料操作都在 `/api/scripts/*` 完成。受限登入狀態由 `login_required_page` 預設導向各自的頁面（被鎖定 -> `/appeal`、待確認年齡 -> `/age`、
等待刪除 -> `/account`）。初始狀態以 JSON 區塊（tojson 已跳脫）傳給前端，不以字串拼進腳本：
- `user`：本人的暱稱與短代號、會員等級，以及是否被暫停發佈（`publish_suspended`；前端據此顯示申訴捷徑，見 appeal-shortcut 規格）。
- `gate`：帳號與年齡閘門的結果（`null` 或 `age_confirmation_required`、`age_restricted`、`forbidden`），完全來自 `AccessPolicy`，這裡不自行判斷。
- `submit_timeout_ms`：送審的請求逾時 = 伺服器的總預算 + 5 秒（由設定算出，前端不寫死）；送審會呼叫審查的模型層。
"""
from flask import Blueprint, render_template

from app.runtime import get_config
from app.security.session import current_user, login_required_page
from app.services.registry import get_access_policy

script_pages_bp = Blueprint("script_pages", __name__)


@script_pages_bp.get("/scripts")
@login_required_page
def scripts_page():
    user = current_user()
    config = get_config()
    llm = config.rpg_llm.llm if config.rpg_llm else config.llm
    state = {
        "user": {
            "public_id": user.public_id, "member_tier": user.member_tier, "nickname": user.nickname,
            "nickname_code": user.nickname_code, "publish_suspended": user.publish_suspended_at is not None,
        },
        "gate": get_access_policy().chat_gate(user),
        "submit_timeout_ms": int(llm.frontend_timeout_seconds * 1000),
    }
    return render_template("scripts/workshop.html", initial_state=state)
