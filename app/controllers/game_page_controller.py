"""RPG 的 MVC Page Controller：只渲染 `/game` 一族的頁面外殼與初始狀態，回應一律是 HTML（見 AGENTS.md 第 3 節與 rpg-entry-pages 規格）。

- `/game`：RPG 的入口（選劇本與我的存檔），也是登入後的預設頁面。清單內容**不放進**初始狀態，由前端呼叫兩個唯讀端點
  （`/api/sessions/catalog`、`/api/sessions`）取得。初始狀態以 JSON 區塊（tojson 已跳脫）傳給前端，不以字串拼進腳本：
  - `user`：會員等級與故事偏好（只有這兩項，沒有 email、生日與年齡）。
  - `gate`：帳號與年齡閘門的結果（`null` 或 `age_confirmation_required`、`age_restricted`、`forbidden`），完全來自 `AccessPolicy`，這裡不自行判斷。
- `/game/start/<版本識別碼>`：開存檔精靈（序章、聯繫、創角）。**不使用也不輸出版本識別碼**：版本是否存在、能不能玩由精靈載入時的
  `setup` 端點判斷，前端自己從網址讀取識別碼。初始狀態只有（沒有劇本內容，劇本內容由 `setup` 取得）：
  - `member_tier`：會員等級；`gate`：同上（`AccessPolicy.chat_gate` 的結果，不自行判斷）。
  - `suggest_timeout_ms`：AI 生成預覽的前端逾時 = 後端單次請求總預算 + 5 秒（與 `/scripts` 的送審同一個算法）。
  - `start_timeout_ms`：開存檔的前端逾時 = `2 × 後端單次請求總預算 + 5 秒`（預設 65 秒，與 `session_api_controller` 說明一致）：
    最壞情況是「劇本審查（必要時）＋主角模型審查」兩次呼叫各用滿一份預算。兩者都由設定算出，前端不寫死。
- `/game/<存檔識別碼>`：遊戲主畫面（RC-3b，`rpg-game-ui`）。標題、主角姓名、內容模式與是否完結由 `GameEntryService.session_summary` 取得，
  非擁有者與不存在的存檔得到相同的 404 頁面；**權限判斷全在 Service**，這裡不寫。頁面外殼只有（`tojson` 已跳脫的 JSON 區塊）：
  - `session`：`id`、`title`、`content_tier`、`ended`、`player_name`（都來自 `session_summary`）。
  - `user`：`public_id`（本機暫存的資料庫名稱用，對外識別碼）與 `member_tier`。沒有 email、生日與年齡。
  - `gate`：同上（`AccessPolicy.chat_gate` 的結果）；`content_mode`：`AccessPolicy.content_access` 的生效模式與「是否已滿成年門檻」
    （與 `/api/sessions/catalog` 的 `content_mode` 同一個來源與形狀，沒有生日與年齡），供畫面說明未成年固定為安全模式的原因。
  - `stream`：串流的前端逾時（閒置 `STREAM_IDLE_TIMEOUT_MS`、總計 `STREAM_TOTAL_TIMEOUT_MS`，見 `docs/RPG.md` 7.15、7.17.3；前端不寫死數值）。
  - `limits`：`action_max`，直接引用後端的 `game_service.ACTION_MAX`（後端調整時前端一起變）。
  **不含**回合內容、第 0 幕、記事簿與世界狀態：這些一律由前端呼叫既有端點（快照、回合、記事簿、錢包）取得，所以頁面外殼永遠沒有劇情，
  返回上一頁或頁面快取也不會看到舊內容；本頁**不改任何後端端點**。

兩條路由的路徑段數不同（`/game/start/<x>` 兩段、`/game/<x>` 一段），不會互相吃掉；存檔識別碼是 UUID，不會等於 `start`。
受限登入狀態由 `login_required_page` 預設導向各自的頁面（被鎖定 -> `/appeal`、待確認年齡 -> `/age`、等待刪除 -> `/account`）。
"""
from flask import Blueprint, render_template

from app.runtime import get_config
from app.security.session import current_user, login_required_page
from app.services.game_service import ACTION_MAX
from app.services.registry import get_access_policy, get_game_entry_service

game_pages_bp = Blueprint("game_pages", __name__)

# 串流的前端逾時（毫秒），給前端的初始狀態用。閒置逾時：任何收到的位元組（含 15 秒一次的 ping）都重設；總逾時：從送出起算、不重設。
# 數值的依據見 docs/RPG.md 7.15、7.17.3：模型首字最慢約 35 秒，被擋兩次加狀態呼叫的最壞時間約 150 秒。
STREAM_IDLE_TIMEOUT_MS = 45000
STREAM_TOTAL_TIMEOUT_MS = 180000


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
    # 版本識別碼只在網址裡，頁面不使用它：前端自己從網址讀取並呼叫 setup（版本是否存在、能不能玩由 setup 判斷）
    user = current_user()
    config = get_config()
    llm = config.rpg_llm.llm if config.rpg_llm else config.llm                          # RPG 的模型設定；沒有時沿用客服的預算
    state = {
        "member_tier": user.member_tier,
        "gate": get_access_policy().chat_gate(user),
        "suggest_timeout_ms": int(llm.frontend_timeout_seconds * 1000),
        # 開存檔最壞是兩次模型呼叫（劇本審查與主角審查）各用滿一份預算：(預算 + 5) + 預算 = 2 × 預算 + 5
        "start_timeout_ms": int((llm.frontend_timeout_seconds + llm.request_budget_seconds) * 1000),
    }
    return render_template("game/start.html", initial_state=state)


@game_pages_bp.get("/game/<session_public_id>")
@login_required_page
def game_session_page(session_public_id: str):
    user = current_user()
    summary = get_game_entry_service().session_summary(user, session_public_id)               # 非擁有者與不存在：NotFound -> 404 頁
    policy = get_access_policy()
    access = policy.content_access(user)
    state = {
        "session": {
            "id": summary["id"], "title": summary["title"], "content_tier": summary["content_tier"], "ended": summary["ended"],
            "player_name": summary["player_name"],
        },
        "user": {"public_id": user.public_id, "member_tier": user.member_tier},
        "gate": policy.chat_gate(user),
        "content_mode": {"effective": access.effective, "is_adult": access.is_adult},
        "stream": {"idle_timeout_ms": STREAM_IDLE_TIMEOUT_MS, "total_timeout_ms": STREAM_TOTAL_TIMEOUT_MS},
        "limits": {"action_max": ACTION_MAX},
    }
    return render_template("game/session.html", initial_state=state, save=summary)
