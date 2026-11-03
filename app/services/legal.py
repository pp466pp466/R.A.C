"""服務條款、隱私權政策與年齡政策的版本（註冊時記錄使用者同意、確認的是哪一版）。

版本由伺服器決定，不由前端傳入：`/terms`、`/privacy` 頁面顯示的就是條款這一版。
條款內容修改時更新這個字串；正式上線前法務覆核完成後，改成正式版本號。

年齡政策版本記錄「使用者完成年齡確認時，平台採用的規則」。規則本身（最低年齡、成年門檻、區間選項）與版本都在
`app/services/age_policy.py` 的 `AgePolicy`，這裡只是方便引用的別名；規則改變時提高該處的版本，已完成確認的帳號保留當時的版本。
"""
from app.services.age_policy import DEFAULT_AGE_POLICY

CURRENT_TERMS_VERSION = "2026-11-02-draft-4"
CURRENT_AGE_POLICY_VERSION = DEFAULT_AGE_POLICY.version
