"""管理員後台的 MVC Page Controller：只渲染 `/admin` 的頁面外殼與初始狀態，回應一律是 HTML（見 AGENTS.md 第 3 節）。

所有資料與操作都在 `/api/admin/*` 完成。順序是「登入 → 管理員檢查 → 渲染」：
- 未登入：導向登入頁並保留返回位置（`login_required_page`）；
- 已登入但不是管理員：404，與不存在的頁面完全相同（`AdminService.authorize` 拋出 `NotFound`，由統一的錯誤處理渲染），
  所以一般使用者無從得知後台存在；
- 初始狀態只有管理員自己的顯示名稱；沒有任何統計與他人的資料。
"""
from flask import Blueprint, render_template

from app.security.session import current_user, login_required_page
from app.services.registry import get_admin_service

admin_pages_bp = Blueprint("admin_pages", __name__)


@admin_pages_bp.get("/admin")
@login_required_page
def admin_page():
    user = current_user()
    get_admin_service().authorize(user)
    return render_template("admin/console.html", initial_state={"admin": {"display_name": user.display_name}})