"""已登入使用者的快照：Service 回傳給端點與安全輔助的資料（不是 ORM 物件）。

端點與 `app/security` 不得匯入資料模型，所以需要使用者資料時一律拿到這個物件。
`id` 與 `token_version` 只給伺服器內部使用（寫入登入 cookie、權限判斷），**絕不**出現在對外的回應中。
`birth_date` 同樣只給伺服器內部使用（權限政策與年齡群組由它於當下計算），**絕不**出現在 `public_dict()`。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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
    birth_date: date | None        # 內部欄位；None = 尚未完成年齡確認
    token_version: int

    @property
    def age_confirmed(self) -> bool:
        """是否已完成年齡確認（只看生日是否存在；是否成年另由政策於當下計算）。"""
        return self.birth_date is not None

    @classmethod
    def from_model(cls, user: User) -> "CurrentUser":
        # 「完成」要求生日與年齡區間都在；兩者本來就由同一個 UPDATE 寫入，這裡再保守一次
        complete = user.birth_date is not None and user.age_range is not None
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
            birth_date=user.birth_date if complete else None,
            token_version=user.token_version,
        )

    def public_dict(self, *, age_group: str | None, google_linked: bool | None = None) -> dict[str, Any]:
        """可以回傳給使用者本人的資料。不含內部編號、密碼雜湊、`token_version`、生日與年齡區間。

        年齡只回「是否已完成」與年齡群組（`minor`、`adult`，尚未完成為 None）；群組必須由呼叫端以時鐘與
        年齡規則算出後傳入（沒有預設值，避免忘了傳而把成年人顯示成未知）。
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
            "age_group": age_group,
        }
        if google_linked is not None:
            data["google_linked"] = google_linked
        return data
