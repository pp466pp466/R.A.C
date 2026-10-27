"""shared-ui-components 規格：元件示範頁只在 development 與 testing 環境提供。"""
import pytest

from app import create_app
from app.config import load_config


def client_for(env: str):
    environ = {"APP_ENV": env}
    if env != "testing":
        environ.update({"SECRET_KEY": "k" * 32, "DATABASE_URL": "sqlite+pysqlite:///:memory:"})
    return create_app(load_config(environ=environ, dotenv_path="__none__.env")).test_client()


@pytest.mark.parametrize("env", ["testing", "development"])
def test_demo_page_exists_in_development_like_environments(env):
    response = client_for(env).get("/dev/components")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="app"' in html
    assert "UI.install(app)" in html
    assert "<ui-modal" in html and "<ui-toast-region" in html


@pytest.mark.parametrize("env", ["staging", "production"])
def test_demo_page_is_a_404_in_staging_and_production(env):
    client = client_for(env)
    assert client.get("/dev/components").status_code == 404
    assert client.get("/dev/").status_code == 404
    assert client.get("/dev").status_code == 404


@pytest.mark.parametrize("env", ["staging", "production"])
def test_demo_code_is_not_even_imported_in_staging_and_production(env):
    app = client_for(env).application
    assert "dev" not in app.blueprints
    assert not any(rule.rule.startswith("/dev") for rule in app.url_map.iter_rules())


def test_demo_page_renders_vue_templates_untouched_by_jinja():
    """Vue 的 {{ }} 必須原樣送到瀏覽器（用 raw 區塊保護），不能被 Jinja 吃掉。"""
    html = client_for("testing").get("/dev/components").get_data(as_text=True)
    assert "{{ c.hex }}" in html and "{{ typed }}" in html and "{{ entry }}" in html


def test_demo_page_contains_the_manual_acceptance_checklist():
    html = client_for("testing").get("/dev/components").get_data(as_text=True)
    assert 'id="checklist"' in html
    for expected in ("Esc", "2px 琥珀橙線", "由下往上淡入", "網路面板沒有任何外部請求"):
        assert expected in html
