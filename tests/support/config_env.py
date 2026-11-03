"""組出 staging 與 production 測試設定所需的環境變數。

staging 與 production 需要寄信設定（營運告警與管理員的重設密碼信）與對外網址；測試用的值一律是假的。
"""
from __future__ import annotations

FAKE_SMTP_PASSWORD = "smtp-pw-for-tests-only"
FAKE_GEMINI_KEY = "gemini-key-for-tests-only"

# staging 與 production 必要的寄信與對外網址設定（假值）
AUTH_ENV = {
    "BASE_URL": "https://cs.example.com",
    "MAIL_SMTP_HOST": "smtp.example.com",
    "MAIL_SMTP_PORT": "587",
    "MAIL_FROM": "service@example.com",
    "MAIL_SMTP_USER": "mailer",
    "MAIL_SMTP_PASSWORD": FAKE_SMTP_PASSWORD,
}

# staging 與 production 必須設定告警收件人（假值）；寄信設定由 AUTH_ENV 提供
ALERT_ENV = {"ALERT_EMAIL_TO": "ops@example.com"}

def production_like_env(env: str = "production", **overrides: str) -> dict[str, str]:
    """能通過驗證的 staging／production 環境變數（SQLite 記憶體資料庫，非 sa、不含 TrustServerCertificate）。"""
    values = {
        "APP_ENV": env,
        "SECRET_KEY": "k" * 32,
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "GEMINI_API_KEY": FAKE_GEMINI_KEY,
        **AUTH_ENV,
        **ALERT_ENV,
    }
    values.update(overrides)
    return values
