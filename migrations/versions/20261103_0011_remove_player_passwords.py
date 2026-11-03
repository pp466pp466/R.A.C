"""移除玩家端密碼（remove-player-password-auth）：只動資料，不動結構。

玩家只用 Google 登入，不設密碼、沒有信箱驗證（AGENTS.md 第 10 節）。這個遷移把既有資料帶到相同的狀態：
- 非管理員帳號：`password_hash` 設為空、`failed_login_count` 設為 0、`locked_until` 設為空
  （玩家不持有密碼；留著舊的雜湊只是不再有用途的個資）。管理員的密碼與鎖定計數完全不變。
- **曾持有密碼雜湊的**非管理員帳號另外 `token_version` 加 1：它們的既有登入狀態（含遷移前被預先佔用信箱的攻擊者以密碼取得的）
  隨憑證一起失效，擁有者以同信箱的 Google 登入即可回來；從來沒有密碼的玩家（純 Google）與管理員的登入狀態不受影響。
- `users.status` 與 `users.status_before_deletion` 為 `pending_verification` 者改為 `active`
  （這個狀態已退役；`email_verified_at` 保持原樣，所以「未驗證的遺留帳號」仍能被 Google 合併規則辨識）。

屬於 expand 階段：沒有任何欄位或資料表被刪除。遷移先於新版程式執行，此時跑的仍是舊版程式，而舊版程式對
「玩家沒有密碼、狀態為 active」的帳號本來就有處理（純 Google 帳號的既有路徑），所以順序是安全的。
可重複執行（冪等）：第二次執行時已經沒有符合條件的列。

用 `sa.table`／`sa.column` 的輕量資料表加 `sa.update` 表達，不是原生 SQL，也不匯入應用程式程式碼
（migration 描述的是「當時」的結構，不隨模型修改而改變）。

回復（downgrade）不做任何事：被清除的密碼雜湊無法還原，回復靠遷移前的備份（部署流程固定「備份 → migration → 部署」）。
舊版程式在遷移後仍可運作，所以回復映像不需要回復資料。

Revision ID: 0011_remove_player_passwords
Revises: 0010_google_first_login
Create Date: 2026-11-03
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0011_remove_player_passwords"
down_revision = "0010_google_first_login"
branch_labels = None
depends_on = None

ROLE_ADMIN = "admin"
LEGACY_PENDING_STATUS = "pending_verification"
ACTIVE_STATUS = "active"

_users = sa.table(
    "users",
    sa.column("role", sa.Unicode(10)),
    sa.column("status", sa.Unicode(20)),
    sa.column("status_before_deletion", sa.Unicode(20)),
    sa.column("password_hash", sa.Unicode(255)),
    sa.column("token_version", sa.Integer()),
    sa.column("failed_login_count", sa.Integer()),
    sa.column("locked_until", sa.DateTime()),
)


def upgrade() -> None:
    bind = op.get_bind()
    # 先撤銷「曾經靠密碼取得」的登入狀態，再清除雜湊（順序不影響結果，但這樣第二次執行時沒有雜湊可比對，所以不會重複加一）
    bind.execute(
        sa.update(_users)
        .where(_users.c.role != ROLE_ADMIN, _users.c.password_hash.is_not(None))
        .values(token_version=_users.c.token_version + 1)
    )
    bind.execute(
        sa.update(_users)
        .where(_users.c.role != ROLE_ADMIN)
        .values(password_hash=None, failed_login_count=0, locked_until=None)
    )
    bind.execute(sa.update(_users).where(_users.c.status == LEGACY_PENDING_STATUS).values(status=ACTIVE_STATUS))
    bind.execute(
        sa.update(_users)
        .where(_users.c.status_before_deletion == LEGACY_PENDING_STATUS)
        .values(status_before_deletion=ACTIVE_STATUS)
    )


def downgrade() -> None:
    """資料遷移無法還原（雜湊已清除）：什麼也不做，需要時由備份還原。"""
