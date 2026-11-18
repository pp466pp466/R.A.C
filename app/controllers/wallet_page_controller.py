"""錢包頁的 MVC Page Controller：只渲染 `/wallet` 的頁面外殼與初始狀態，回應一律是 HTML（見 AGENTS.md 第 3 節與 wallet-hub-page 規格、wallet-hub 的 design D6）。

- 初始狀態（`tojson` 已跳脫的 JSON 區塊）只有 `member_tier`（會員等級，頂列的標籤用）。**不放**餘額、帳本、email、生日與年齡：
  餘額由前端呼叫既有的 `GET /api/wallet` 取得（那個端點會補發贈幣與退回逾時預扣，有寫入的副作用，頁面渲染不該做這些事），
  所以頁面外殼本身永遠沒有玩家的資料，返回上一頁或頁面快取也不會看到舊的餘額。
- 受限登入狀態由 `login_required_page` 預設導向各自的頁面（被鎖定 -> `/appeal`、待確認年齡 -> `/age`、等待刪除 -> `/account`），
  未登入導向登入頁；這裡不寫任何權限判斷。回應的 `Cache-Control: no-store` 由全站的 HTML 快取標頭處理（`app/security/cache_headers.py`）。
- 分頁（免費、儲值）與「紀錄」檢視是頁內的 Vue 狀態，不新增路由；本頁**不新增任何 API、資料表、設定或遷移**。
"""
from flask import Blueprint, render_template

from app.security.session import current_user, login_required_page

wallet_pages_bp = Blueprint("wallet_pages", __name__)


@wallet_pages_bp.get("/wallet")
@login_required_page
def wallet_page():
    user = current_user()
    state = {"member_tier": user.member_tier}
    return render_template("wallet/hub.html", initial_state=state)
