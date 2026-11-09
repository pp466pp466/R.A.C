"""Web API Controller：`/api/wallet/*`（玩家的遊戲幣錢包），一律回傳 JSON（見 wallet-api 規格）。

Controller 只讀取請求、呼叫 Service、回應；餘額、簽到的日界與上限、記錄的篩選與分頁、贈幣的補發都在 `WalletService`。
`require_login()` 預設拒絕受限登入狀態（鎖定、待確認年齡、等待刪除），所以尚未完成年齡確認的玩家得到 403
`age_confirmation_required`，也不會發放任何幣；這不是 AI 功能，不另做 `require_chat_access`。
所有寫入的請求都經過全站的 CSRF 與 Origin 檢查；回應一律禁止快取（`/api/wallet` 在 `cache_headers.py` 的前綴清單內）。

- `GET /api/wallet`：兩個餘額、合計、免費幣上限、每輪幣價與簽到狀態（今日是否可簽到、會入帳與會作廢多少、下次可簽到的時間）。
- `POST /api/wallet/checkin`：每日簽到，本文可帶 `confirm_overflow`（布林）。會有幣作廢而沒有確認 → 409 `checkin_overflow` 並附預覽，
  不消耗當日簽到；今日已簽到 → 409 `already_checked_in`。
- `GET /api/wallet/entries`：遊戲幣記錄（由新到舊，游標 `before` 是這位玩家自己的序號，`limit` 預設 20、至多 100）。

**玩家無法經由 HTTP 花費或增加餘額**（簽到除外）：預扣、結算與退回只由伺服器端的回合流程呼叫。
"""
from flask import Blueprint, jsonify, request

from app.controllers.request_helpers import int_arg, read_json_object
from app.security.session import require_login
from app.services.errors import ValidationError
from app.services.registry import get_wallet_service
from app.services.wallet_service import ENTRIES_DEFAULT_LIMIT

wallet_api_bp = Blueprint("wallet_api", __name__, url_prefix="/api/wallet")


@wallet_api_bp.get("")
def get_wallet():
    user = require_login()
    return jsonify(ok=True, wallet=get_wallet_service().view(user).to_dict())


@wallet_api_bp.post("/checkin")
def checkin():
    user = require_login()
    body = read_json_object() if request.get_data(cache=True) else {}
    confirm = body.get("confirm_overflow", False)
    if not isinstance(confirm, bool):
        raise ValidationError(fields={"confirm_overflow": "必須是 true 或 false。"})
    return jsonify(ok=True, wallet=get_wallet_service().checkin(user, confirm_overflow=confirm).to_dict())


@wallet_api_bp.get("/entries")
def entries():
    user = require_login()
    before = request.args.get("before")
    before_seq = int_arg("before", 0) if before not in (None, "") else None
    limit = int_arg("limit", ENTRIES_DEFAULT_LIMIT)
    return jsonify(ok=True, **get_wallet_service().entries(user, before_seq=before_seq, limit=limit).to_dict())
