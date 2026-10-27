"""MVC Page Controller：只負責渲染頁面外殼與初始狀態，回應一律為 HTML（見 AGENTS.md 第 3 節）。"""
from flask import Blueprint, render_template

page_bp = Blueprint("pages", __name__)


@page_bp.get("/")
def home() -> str:
    # 僅供 C-1 驗證基礎樣板的最小首頁；後續 change 會以實際頁面取代
    return render_template("home.html")
