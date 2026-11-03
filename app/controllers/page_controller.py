"""MVC Page Controller：只負責渲染頁面外殼與初始狀態，回應一律為 HTML（見 AGENTS.md 第 3 節）。"""
from flask import Blueprint, redirect, render_template

from app.security.redirects import landing_for
from app.security.session import current_user

page_bp = Blueprint("pages", __name__)


@page_bp.get("/")
def home():
    # 已登入者直接去該去的位置（預設頁面，或受限登入狀態各自的頁面）；未登入者看到首頁
    user = current_user()
    if user is not None:
        return redirect(landing_for(user))
    # 僅供 C-1 驗證基礎樣板的最小首頁；後續 change 會以實際頁面取代
    return render_template("home.html")
