"""可重複使用的 IDOR（不安全的直接物件參照）權限測試框架（見 access-control 規格「權限測試框架」）。

後續每個「屬於使用者的資源」的讀寫端點（對話、訊息……）都用它檢查四種身分：

| 身分 | 期待 |
|---|---|
| 他人（已登入，但不是擁有者） | 404 |
| 不存在的識別碼（已登入） | 404，**狀態碼與本文必須和「他人」逐字相同**（否則等於告訴對方資源存在） |
| 未登入 | 401 |
| 擁有者 | 成功 |

順序刻意是「他人 → 不存在 → 未登入 → 擁有者」：擁有者的呼叫可能會刪除或修改資源（例如 DELETE），
若先執行，後面對他人的檢查就是對一個已經不存在的資源做的、什麼也證明不了。
擁有者最後執行成功，也同時證明前面他人的寫入請求沒有真的得手。

用法：
    spec = EndpointSpec("DELETE", "/api/cs/conversations/{id}", create_resource=make_conversation, success_status=200)
    check_endpoint(app, spec, owner=alice, intruder=bob)
框架失敗時拋出 `IdorFailure`，`identity` 指出是哪一種身分造成的。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from flask import Flask
from flask.testing import FlaskClient

from app.models.user import User


class IdorFailure(AssertionError):
    """權限檢查不符合預期。`identity` 為 intruder、missing、anonymous、owner、consistency 之一。"""

    def __init__(self, identity: str, message: str) -> None:
        super().__init__(f"[{identity}] {message}")
        self.identity = identity


@dataclass(frozen=True)
class EndpointSpec:
    method: str
    path: str                                             # 路徑樣板；{id} 會被換成資源的對外識別碼
    create_resource: Callable[[Flask, User], str]          # (app, 擁有者) -> 屬於擁有者的新資源的對外識別碼
    json: dict[str, Any] | None = None
    success_status: int = 200
    description: str = field(default="", compare=False)

    def label(self) -> str:
        return self.description or f"{self.method} {self.path}"


def logged_in_client(app: Flask, user: User) -> FlaskClient:
    """直接寫入登入 cookie 的 client（不經過密碼登入）；token_version 取自該使用者目前的值。"""
    client = app.test_client()
    with client.session_transaction() as session:
        session["uid"] = user.id
        session["tv"] = user.token_version
    return client


def _call(client: FlaskClient, spec: EndpointSpec, identifier: str):
    kwargs: dict[str, Any] = {"method": spec.method}
    if spec.json is not None:
        kwargs["json"] = spec.json
    return client.open(spec.path.format(id=identifier), **kwargs)


def check_endpoint(app: Flask, spec: EndpointSpec, *, owner: User, intruder: User) -> None:
    """依序以他人、不存在的識別碼、未登入、擁有者呼叫端點，任何一項不符就拋出 `IdorFailure`。"""
    if owner.id == intruder.id:
        raise ValueError("owner 與 intruder 必須是不同的使用者")
    resource = spec.create_resource(app, owner)

    # 1) 他人：必須是 404（不是 403，也不是成功）
    intruder_response = _call(logged_in_client(app, intruder), spec, resource)
    if intruder_response.status_code != 404:
        raise IdorFailure(
            "intruder", f"{spec.label()}：他人存取別人的資源回應 {intruder_response.status_code}（應為 404）"
        )

    # 2) 不存在的識別碼：同樣 404，且與「他人」的回應逐字相同
    missing_response = _call(logged_in_client(app, intruder), spec, str(uuid.uuid4()))
    if missing_response.status_code != 404:
        raise IdorFailure("missing", f"{spec.label()}：不存在的識別碼回應 {missing_response.status_code}（應為 404）")
    if (intruder_response.status_code, intruder_response.mimetype, intruder_response.get_data()) != (
        missing_response.status_code, missing_response.mimetype, missing_response.get_data()
    ):
        raise IdorFailure(
            "consistency",
            f"{spec.label()}：「他人的資源」與「不存在的資源」的回應不同，會洩漏資源是否存在",
        )

    # 3) 未登入：401
    anonymous_response = _call(app.test_client(), spec, resource)
    if anonymous_response.status_code != 401:
        raise IdorFailure("anonymous", f"{spec.label()}：未登入回應 {anonymous_response.status_code}（應為 401）")

    # 4) 擁有者：成功（最後才執行，因為它可能改變或刪除資源）
    owner_response = _call(logged_in_client(app, owner), spec, resource)
    if owner_response.status_code != spec.success_status:
        raise IdorFailure(
            "owner",
            f"{spec.label()}：擁有者回應 {owner_response.status_code}（應為 {spec.success_status}）"
            "；若端點是寫入類，也可能是前面他人的請求已經改動了資源",
        )
