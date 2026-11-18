"""MVC Page Controller：只負責渲染頁面外殼與初始狀態，回應一律為 HTML（見 AGENTS.md 第 3 節）。"""
from flask import Blueprint, redirect, render_template

from app.runtime import get_age_policy
from app.security.redirects import landing_for
from app.security.session import current_user

page_bp = Blueprint("pages", __name__)


@page_bp.get("/")
def home():
    # 已登入者直接去該去的位置（預設頁面，或受限登入狀態各自的頁面）；未登入者看到歡迎頁，登入與勾選條款在 /login
    user = current_user()
    if user is not None:
        return redirect(landing_for(user))
    return render_template("home.html", min_age=get_age_policy().min_register_age)      # 年齡數字來自 AgePolicy，範本不寫死
