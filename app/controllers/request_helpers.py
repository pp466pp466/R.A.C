"""Controller 共用的請求輔助。"""
from __future__ import annotations

from typing import Any

from flask import request
from werkzeug.exceptions import BadRequest, UnsupportedMediaType


def read_json_object() -> dict[str, Any]:
    """讀取請求本文，要求是 JSON 且最外層為物件。

    - 內容類型不是 JSON → 415；
    - 本文不是合法 JSON，或最外層不是物件（例如陣列、數字、空本文）→ 400；
    - 本文超過上限（`MAX_CONTENT_LENGTH`）→ Werkzeug 在讀取時拋出 413。
    三者都由統一的錯誤處理轉成繁體中文的 JSON 回應。
    """
    if not request.is_json:
        raise UnsupportedMediaType()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise BadRequest()
    return data
