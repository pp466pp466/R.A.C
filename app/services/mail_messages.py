"""系統寄出的信件文案（繁體中文純文字，見 AGENTS.md 第 8 節）。

文案是常數加 `str.format`，**不使用 Jinja**：顯示名稱是使用者輸入的文字，不能把它當成範本處理。
連結一律由設定的對外網址（BASE_URL）組成，絕不使用請求的主機標頭（主機標頭注入會讓重設連結指向攻擊者的網站）。
權杖放在網址的片段（# 之後）：伺服器與入口層的存取日誌不會看到它，也不會經由 Referer 外洩。
"""
from __future__ import annotations

from dataclasses import dataclass

PRODUCT_NAME = "智能客服"
IGNORE_NOTICE = "如果這不是您本人的操作，請忽略這封信，您的帳號不會有任何變動。"


@dataclass(frozen=True)
class MailContent:
    subject: str
    body: str


def verification_link(base_url: str, token: str) -> str:
    return f"{base_url}/verify-email#token={token}"


def reset_link(base_url: str, token: str) -> str:
    return f"{base_url}/reset-password#token={token}"


def verification_mail(display_name: str, link: str) -> MailContent:
    return MailContent(
        subject=f"【{PRODUCT_NAME}】請驗證您的電子郵件信箱",
        body=(
            "{name} 您好：\n\n"
            "感謝您註冊{product}。請點擊下方連結，開啟頁面後按下「確認」完成信箱驗證：\n\n"
            "{link}\n\n"
            "這個連結將於 24 小時後失效，且只能使用一次。\n\n"
            "{ignore}\n\n"
            "{product}\n"
        ).format(name=display_name, product=PRODUCT_NAME, link=link, ignore=IGNORE_NOTICE),
    )


def reset_password_mail(display_name: str, link: str) -> MailContent:
    return MailContent(
        subject=f"【{PRODUCT_NAME}】重設密碼",
        body=(
            "{name} 您好：\n\n"
            "我們收到了重設您帳號密碼的要求。請點擊下方連結，開啟頁面後輸入新的密碼：\n\n"
            "{link}\n\n"
            "這個連結將於 1 小時後失效，且只能使用一次。\n\n"
            "{ignore}\n\n"
            "{product}\n"
        ).format(name=display_name, product=PRODUCT_NAME, link=link, ignore=IGNORE_NOTICE),
    )


def password_changed_mail(display_name: str) -> MailContent:
    return MailContent(
        subject=f"【{PRODUCT_NAME}】您的密碼已變更",
        body=(
            "{name} 您好：\n\n"
            "您的帳號密碼剛剛已經變更，其他裝置上的登入也已一併登出。\n\n"
            "如果這不是您本人的操作，請立即使用「忘記密碼」重設密碼，並留意您的信箱是否安全。\n\n"
            "{product}\n"
        ).format(name=display_name, product=PRODUCT_NAME),
    )
