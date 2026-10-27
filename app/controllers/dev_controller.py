"""開發用路由：元件示範頁。只在 development 與 testing 環境註冊，staging 與 production 沒有這些路徑。"""
from flask import Blueprint, render_template

dev_bp = Blueprint("dev", __name__, url_prefix="/dev")


@dev_bp.get("/components")
def components() -> str:
    return render_template("dev_components.html")
