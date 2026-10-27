"""Service 的組裝處：把目前請求的資料庫工作階段接到 Repository，再交給 Service。

端點只呼叫這裡的 `get_*_service()`，不接觸 Repository 或工作階段（見 tests/architecture/test_layering.py）。
"""
from app.db.session import get_session
from app.repositories.health_repository import HealthRepository
from app.services.health_service import HealthService


def get_health_service() -> HealthService:
    return HealthService(HealthRepository(get_session()))
