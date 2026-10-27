"""application-layering 規格：應用程式工廠、基礎樣板、反向代理標頭。"""
import pytest
from sqlalchemy import Integer, inspect, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app import create_app
from app.config import load_config


class _Base(DeclarativeBase):
    pass


class Marker(_Base):
    __tablename__ = "marker"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


def make_testing_app():
    return create_app(load_config(environ={"APP_ENV": "testing"}))


def make_production_like_app(env: str = "production"):
    cfg = load_config(
        environ={
            "APP_ENV": env,
            "SECRET_KEY": "k" * 32,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",  # 驗證用；非 sa，不含 TrustServerCertificate
        }
    )
    app = create_app(cfg)
    app.add_url_rule("/_scheme", "scheme", lambda: __import__("flask").request.scheme + "|" + __import__("flask").request.host)
    return app


def make_development_app(tmp_path):
    cfg = load_config(
        environ={"APP_ENV": "development", "SECRET_KEY": "k", "DATABASE_URL": "sqlite+pysqlite:///:memory:"},
        dotenv_path=tmp_path / "none.env",
    )
    app = create_app(cfg)
    app.add_url_rule("/_scheme", "scheme", lambda: __import__("flask").request.scheme + "|" + __import__("flask").request.host)
    return app


# ---- 應用程式可依設定獨立建立 --------------------------------------------------

def test_two_instances_do_not_share_configuration_or_database():
    a, b = make_testing_app(), make_testing_app()
    assert a is not b
    assert a.extensions["db_engine"] is not b.extensions["db_engine"]

    _Base.metadata.create_all(a.extensions["db_engine"])
    with Session(a.extensions["db_engine"]) as session:
        session.add(Marker(id=1))
        session.commit()

    assert "marker" in inspect(a.extensions["db_engine"]).get_table_names()
    assert "marker" not in inspect(b.extensions["db_engine"]).get_table_names()


def test_create_app_without_arguments_reads_the_environment():
    # conftest 已設定 APP_ENV=testing
    app = create_app()
    assert app.extensions["app_config"].env == "testing"


def test_configuration_is_applied_to_the_instance():
    app = make_production_like_app()
    assert app.config["SECRET_KEY"] == "k" * 32
    assert app.config["DEBUG"] is False


def test_startup_and_requests_never_create_tables(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'startup.sqlite3'}"
    cfg = load_config(environ={"APP_ENV": "testing", "DATABASE_URL": url})
    app = create_app(cfg)
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/api/nothing").status_code == 404
    assert inspect(app.extensions["db_engine"]).get_table_names() == []


# ---- 頁面端點只回傳 HTML；基礎樣板 --------------------------------------------

def test_page_is_html_with_charset_language_and_dark_theme():
    response = make_testing_app().test_client().get("/")
    assert response.status_code == 200
    assert response.content_type.startswith("text/html")
    assert "charset=utf-8" in response.content_type.lower()
    html = response.get_data(as_text=True)
    assert '<meta charset="UTF-8">' in html
    assert 'lang="zh-Hant-TW"' in html
    assert 'data-bs-theme="dark"' in html


def test_responses_keep_traditional_chinese_unescaped():
    app = make_testing_app()
    app.add_url_rule("/api/hello", "hello", lambda: {"訊息": "繁體中文"})
    body = app.test_client().get("/api/hello").get_data(as_text=True)
    assert "繁體中文" in body and "\\u" not in body


def test_favicon_request_is_answered_without_a_404():
    client = make_testing_app().test_client()
    response = client.get("/favicon.ico")
    assert response.status_code == 204
    assert response.data == b""
    assert "max-age" in response.headers["Cache-Control"]


# ---- 反向代理標頭只在預備與正式環境受信任 --------------------------------------

@pytest.mark.parametrize("env", ["production", "staging"])
def test_forwarded_headers_are_trusted_in_production_like_environments(env):
    client = make_production_like_app(env).test_client()
    body = client.get(
        "/_scheme", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "cs.example.com"}
    ).get_data(as_text=True)
    assert body == "https|cs.example.com"


def test_forwarded_headers_are_ignored_in_development(tmp_path):
    client = make_development_app(tmp_path).test_client()
    body = client.get(
        "/_scheme", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.example.com"}
    ).get_data(as_text=True)
    assert body.startswith("http|")
    assert "evil.example.com" not in body


def test_session_is_available_through_the_engine():
    app = make_testing_app()
    with Session(app.extensions["db_engine"]) as session:
        assert session.scalar(select(1)) == 1
