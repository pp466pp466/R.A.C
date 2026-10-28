"""已登入使用者的快照：Service 回傳給端點與安全輔助的資料（不是 ORM 物件）。

端點與 `app/security` 不得匯入資料模型，所以需要使用者資料時一律拿到這個物件。
`id` 與 `token_version` 只給伺服器內部使用（寫入登入 cookie、權限判斷），**絕不**出現在對外的回應中。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.user import User


@dataclass(frozen=True)
class CurrentUser:
    id: int
    public_id: str
    email: str
    display_name: str
    member_tier: str
    role: str
    status: str
    email_verified: bool
    has_password: bool
    age_confirmed: bool
    token_version: int

    @classmethod
    def from_model(cls, user: User) -> "CurrentUser":
        return cls(
            id=user.id,
            public_id=user.public_id,
            email=user.email,
            display_name=user.display_name,
            member_tier=user.member_tier,
            role=user.role,
            status=user.status,
            email_verified=user.email_verified_at is not None,
            has_password=bool(user.password_hash),
            age_confirmed=user.age_confirmed_at is not None,
            token_version=user.token_version,
        )

    def public_dict(self, *, google_linked: bool | None = None) -> dict[str, Any]:
        """可以回傳給使用者本人的資料。不含內部編號、密碼雜湊與 token_version。

        年齡只回傳「是否已確認」的布林；確認時間與政策版本是內部紀錄，不對外。
        """
        data: dict[str, Any] = {
            "public_id": self.public_id,
            "email": self.email,
            "display_name": self.display_name,
            "member_tier": self.member_tier,
            "role": self.role,
            "status": self.status,
            "email_verified": self.email_verified,
            "has_password": self.has_password,
            "age_confirmed": self.age_confirmed,
        }
        if google_linked is not None:
            data["google_linked"] = google_linked
        return data
