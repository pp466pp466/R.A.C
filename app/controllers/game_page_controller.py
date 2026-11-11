"""RPG 的 MVC Page Controller：只渲染 `/game` 一族的頁面外殼與初始狀態，回應一律是 HTML（見 AGENTS.md 第 3 節與 rpg-entry-pages 規格）。

- `/game`：RPG 的入口（選劇本與我的存檔），也是登入後的預設頁面。清單內容**不放進**初始狀態，由前端呼叫兩個唯讀端點
  （`/api/sessions/catalog`、`/api/sessions`）取得。初始狀態以 JSON 區塊（tojson 已跳脫）傳給前端，不以字串拼進腳本：
  - `user`：會員等級與故事偏好（只有這兩項，沒有 email、生日與年齡）。
  - `gate`：帳號與年齡閘門的結果（`null` 或 `age_confirmation_required`、`age_restricted`、`forbidden`），完全來自 `AccessPolicy`，這裡不自行判斷。
- `/game/start/<版本識別碼>`：開存檔精靈。目前只是外殼（精靈本體在後續批次）；版本是否存在、能不能玩由精靈載入時的 `setup` 端點判斷。
- `/game/<存檔識別碼>`：存檔頁的空殼（遊戲主畫面屬 RC-3b）。標題、主角姓名與內容模式由 `GameEntryService.session_summary` 取得，
  非擁有者與不存在的存檔得到相同的 404 頁面；**權限判斷全在 Service**，這裡不寫。頁面不發任何請求、不呼叫任何會產生內容或扣遊戲幣的端點。

兩條路由的路徑段數不同（`/game/start/<x>` 兩段、`/game/<x>` 一段），不會互相吃掉；存檔識別碼是 UUID，不會等於 `start`。
受限登入狀態由 `login_required_page` 預設導向各自的頁面（被鎖定 -> `/appeal`、待確認年齡 -> `/age`、等待刪除 -> `/account`）。
"""
from flask import Blueprint, render_template

from app.security.session import current_user, login_required_page
from app.services.registry import get_access_policy, get_game_entry_service

game_pages_bp = Blueprint("game_pages", __name__)


@game_pages_bp.get("/game")
@login_required_page
def game_index_page():
    user = current_user()
    state = {
        "user": {"member_tier": user.member_tier, "story_preference": user.story_preference},
        "gate": get_access_policy().chat_gate(user),
    }
    return render_template("game/index.html", initial_state=state)


@game_pages_bp.get("/game/start/<version_public_id>")
@login_required_page
def game_start_page(version_public_id: str):
    # 版本識別碼只在網址裡，頁面（目前的外殼）不使用它；精靈上線後由前端自己從網址讀取並呼叫 setup
    return render_template("game/start.html")


@game_pages_bp.get("/game/<session_public_id>")
@login_required_page
def game_session_page(session_public_id: str):
    summary = get_game_entry_service().session_summary(current_user(), session_public_id)    # 非擁有者與不存在：NotFound -> 404 頁
    return render_template("game/session.html", save=summary)
