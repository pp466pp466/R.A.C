"""健康檢查的業務邏輯。

回傳值只有「可用／不可用」，失敗細節（連線字串、驅動訊息）不得外流；
日誌只記錄例外類型與位置（見 app/observability/errors.py）。
"""
import logging

from sqlalchemy.exc import SQLAlchemyError

from app.observability.errors import describe_exception
from app.repositories.health_repository import HealthRepository

logger = logging.getLogger(__name__)


class HealthService:
    def __init__(self, repository: HealthRepository) -> None:
        self._repository = repository

    def is_database_ready(self) -> bool:
        try:
            return self._repository.ping()
        except SQLAlchemyError as exc:
            logger.warning("資料庫就緒檢查失敗", extra=describe_exception(exc))
            return False
