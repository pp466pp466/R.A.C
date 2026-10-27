"""基線：只建立 Alembic 版本機制，不建立任何業務資料表。

業務資料表由後續 change 以新的 revision 加入（使用者資料表屬 C-2，客服資料表屬 C-3）。

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-27
"""

# revision identifiers, used by Alembic.
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
