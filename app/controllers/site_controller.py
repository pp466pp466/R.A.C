"""網站層級的小端點。

`/favicon.ico`：瀏覽器直接開啟 JSON 端點（例如 /healthz）或沒有 <link rel="icon"> 的頁面時，
一定會去要這個檔案。目前沒有網站圖示，回 204 讓日誌與瀏覽器主控台不會充滿 404 雜訊；
日後有正式圖示時，改成提供該檔案即可。
"""
from flask import Blueprint, Response

site_bp = Blueprint("site", __name__)


@site_bp.get("/favicon.ico")
def favicon() -> Response:
    response = Response(status=204)
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response
