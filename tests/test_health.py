"""service-observability 規格：存活檢查與就緒檢查。"""
import logging

import pytest

from app import create_app
from app.config import load_config


def build_app(database_url: str | None = None):
    environ = {"APP_ENV": "testing"}
    if database_url:
        environ["DATABASE_URL"] = database_url
    return create_app(load_config(environ=environ))


@pytest.fixture()
def healthy_client():
    return build_app().test_client()


@pytest.fixture()
def broken_app(tmp_path):
    # 指向不存在的目錄：任何連線都會失敗，用來模擬「資料庫停止」
    return build_app(f"sqlite+pysqlite:///{tmp_path / 'no' / 'such' / 'dir' / 'db.sqlite3'}")


# ---- 存活檢查 -----------------------------------------------------------------

def test_healthz_is_200_with_minimal_json(healthy_client):
    response = healthy_client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}  # 不含版本、設定等內部資訊


def test_healthz_does_not_touch_the_database(tmp_path):
    app = build_app(f"sqlite+pysqlite:///{tmp_path / 'live.sqlite3'}")
    assert app.test_client().get("/healthz").status_code == 200
    assert app.extensions["db_engine"].pool.checkedout() == 0
    assert not (tmp_path / "live.sqlite3").exists()  # 連資料庫檔案都沒被建立


def test_healthz_stays_200_when_the_database_is_down(broken_app):
    assert broken_app.test_client().get("/healthz").status_code == 200


# ---- 就緒檢查 -----------------------------------------------------------------

def test_readyz_is_200_when_the_database_is_available(healthy_client):
    response = healthy_client.get("/readyz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_readyz_is_503_when_the_database_is_down(broken_app):
    response = broken_app.test_client().get("/readyz")
    assert response.status_code == 503
    assert response.get_json() == {"status": "unavailable"}


def test_readyz_503_body_leaks_no_internal_information(broken_app, tmp_path):
    body = broken_app.test_client().get("/readyz").get_data(as_text=True)
    for forbidden in ("sqlite", "unable to open", str(tmp_path), "OperationalError", "Traceback", "dir"):
        assert forbidden not in body


def test_readyz_failure_is_logged_with_type_and_location_only(broken_app, caplog):
    with caplog.at_level(logging.WARNING):
        broken_app.test_client().get("/readyz")
    record = next(r for r in caplog.records if r.getMessage() == "資料庫就緒檢查失敗")
    assert record.error_type == "OperationalError"
    assert "unable to open" not in caplog.text


def test_failed_readiness_check_still_releases_the_connection(broken_app):
    broken_app.test_client().get("/readyz")
    assert broken_app.extensions["db_engine"].pool.checkedout() == 0


# ---- 兩者共通 -----------------------------------------------------------------

@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_health_responses_are_not_cacheable(healthy_client, path):
    assert healthy_client.get(path).headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_health_endpoints_need_no_authentication_and_carry_a_request_id(healthy_client, path):
    response = healthy_client.get(path)
    assert response.status_code == 200
    assert response.headers.get("X-Request-ID")
