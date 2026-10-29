"""客服的 MVC Page Controller：只渲染 `/cs` 的頁面外殼與初始狀態，回應一律是 HTML（見 AGENTS.md 第 3 節）。

所有資料操作都在 `/api/cs/*` 完成。初始狀態以 JSON 區塊（tojson 已跳脫）傳給前端，不以字串拼進腳本：
- `chat_gate`：閘門結果（`null` 或 `age_confirmation_required`、`age_restricted`、`forbidden`）。列表端點不過閘門，
  所以頁面開啟時就要知道能不能送訊息；結果完全來自 `AccessPolicy`，這裡不自行判斷。
- `request_timeout_ms`：前端請求逾時 = 伺服器的總預算 + 5 秒（由設定算出，前端不寫死）。
- `service_available`：是否有模型供應商（本機沒有金鑰時為 false，送訊息會得到 503）。
- `user`：只有對外識別碼、顯示名稱與會員等級；識別碼用來區分瀏覽器內各帳號的離線快取。
"""
from flask import Blueprint, render_template

from app.runtime import get_config, get_llm_provider
from app.security.session import current_user, login_required_page
from app.services.chat_service import MAX_MESSAGE_CHARS
from app.services.registry import get_access_policy

cs_pages_bp = Blueprint("cs_pages", __name__)


@cs_pages_bp.get("/cs")
@login_required_page
def cs_page():
    user = current_user()
    state = {
        "user": {"public_id": user.public_id, "display_name": user.display_name, "member_tier": user.member_tier},
        "chat_gate": get_access_policy().chat_gate(user),
        "service_available": get_llm_provider() is not None,
        "request_timeout_ms": int(get_config().llm.frontend_timeout_seconds * 1000),
        "max_message_chars": MAX_MESSAGE_CHARS,
    }
    return render_template("cs/chat.html", initial_state=state)
