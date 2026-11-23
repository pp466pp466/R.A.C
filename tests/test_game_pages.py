"""rpg-entry-pages 規格：`/game` 一族頁面的外殼、初始狀態、存取規則與前端檔案的規則。

- `/game`：入口頁（選劇本與我的存檔）。初始狀態只有會員等級、故事偏好與閘門結果，**沒有清單內容**。
- `/game/<存檔>`：遊戲主畫面的外殼。初始狀態只有存檔摘要、會員等級、閘門、內容模式、串流逾時與行動上限（沒有劇情），非擁有者與不存在的得到逐字相同的 404。
- `/game/start/<版本>`：開存檔精靈（序章、聯繫、創角）的樣板；初始狀態只有會員等級、閘門結果與兩個請求逾時（沒有劇本內容）；與存檔頁的路徑不衝突。

清單端點的資料與規則在 `test_session_api.py`、`test_game_entry_service.py`；前端純函式與掛載邏輯在 `tests/js/game-labels.test.js`、`tests/js/entry.test.js`。
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.config import PROJECT_ROOT, load_config
from app.models import Base, RpgSession, Wallet
from app.models.rpg_session import SESSION_ENDED
from app.models.script import REVIEW_CATEGORIES
from app.models.script_report import REPORT_NOTE_MAX_CHARS, REPORT_REASONS
from app.models.user import CONTENT_MODES, MEMBER_TIERS, ROLE_ADMIN
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository
from app.services.protagonist import ORIGINS
from app.services.story_preference import AUDIENCES
from app.services.content_tier import SAFE_RULES_VERSION
from tests.support.age import TODAY_UTC
from tests.support.auth import START, FakeClock, add_user
from tests.support.idor import logged_in_client
from tests.support.protagonist import protagonist as stored_protagonist
from tests.support.script_data import validated

STATE_PATTERN = re.compile(r'<script type="application/json" id="initial-state">(.*?)</script>', re.S)
STATIC = PROJECT_ROOT / "app" / "static"
TEMPLATES = PROJECT_ROOT / "app" / "templates" / "game"
JS_DIR = STATIC / "js" / "game"
GHOST = "00000000-0000-4000-8000-000000000000"


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def read(path) -> str:
    return path.read_text(encoding="utf-8-sig")


def strip_js_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<![:\w])//[^\n]*", "", source)


def client_for(app, n: int = 1, **overrides):
    with Session(app.extensions["db_engine"]) as s:
        user = add_user(s, n, **overrides)
        s.refresh(user)
        s.expunge(user)
    return logged_in_client(app, user), user


def state_of(response) -> dict:
    match = STATE_PATTERN.search(response.get_data(as_text=True))
    assert match, "頁面沒有初始狀態區塊"
    return json.loads(match.group(1))


def open_save(app, owner, title="青雲問道", name="林初", ended=False) -> str:
    """直接寫入一個存檔（不經開存檔的審查）；回傳存檔的對外識別碼。"""
    with Session(app.extensions["db_engine"]) as s:
        save = SessionRepository(s).create(
            owner.id, source_version_id=None, snapshot=validated(title=title).content, snapshot_spec_version=1,
            protagonist=stored_protagonist(name), content_tier="safe", now=START,
        )
        if ended:
            save.status = SESSION_ENDED
        s.commit()
        return save.public_id


def publish_builtin(app, title="內建的劇本") -> str:
    result = validated(title=title)
    with Session(app.extensions["db_engine"]) as s:
        version, _ = ScriptRepository(s).publish_builtin(
            f"seed-{title}", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=START,
        )
        s.commit()
        return version.public_id


# ---- /game：存取與初始狀態 ---------------------------------------------------------------------------------------


def test_an_anonymous_visitor_is_sent_to_the_login_page_and_comes_back_to_game(app):
    response = app.test_client().get("/game")
    assert response.status_code == 302
    assert response.headers["Location"] == "/login?next=%2Fgame"


@pytest.mark.parametrize(("overrides", "target"), [
    ({"locked_at": START, "lock_reason": "rate_limited"}, "/appeal"),
    ({"age": None}, "/age"),
])
def test_a_restricted_login_is_sent_to_its_own_page(app, overrides, target):
    client, _ = client_for(app, **overrides)
    for path in ("/game", f"/game/{GHOST}", f"/game/start/{GHOST}", f"/game/script/{GHOST}"):
        response = client.get(path)
        assert response.status_code == 302 and response.headers["Location"].startswith(target), path


def test_a_player_waiting_for_deletion_is_sent_to_the_account_page(app):
    client, _ = client_for(app, status="deletion_pending")
    response = client.get("/game")
    assert response.status_code == 302 and response.headers["Location"].startswith("/account")


def test_the_initial_state_holds_the_tier_the_story_preference_and_the_gate_and_no_list_content(app):
    client, user = client_for(app, story_preference="female")
    publish_builtin(app, title="不該出現在初始狀態的劇本")
    open_save(app, user, title="不該出現在初始狀態的存檔")
    response = client.get("/game")
    assert response.status_code == 200
    state = state_of(response)
    assert state == {"user": {"member_tier": "free", "story_preference": "female"}, "gate": None}
    html = response.get_data(as_text=True)
    assert "不該出現在初始狀態" not in html                                      # 清單內容由前端呼叫唯讀端點取得，不在頁面裡
    assert "user1@example.com" not in html and "birth" not in html.lower()


def test_a_member_tier_other_than_free_is_carried_as_is(app):
    client, _ = client_for(app, member_tier="vip")
    assert state_of(client.get("/game"))["user"]["member_tier"] == "vip"


def test_an_admin_who_has_not_confirmed_the_age_gets_the_gate_code_from_the_server(app):
    client, _ = client_for(app, role=ROLE_ADMIN, age=None)
    response = client.get("/game")
    assert response.status_code == 200                                        # 管理員沒有受限登入狀態：頁面可以開，開始由閘門禁用
    assert state_of(response)["gate"] == "age_confirmation_required"


def test_a_player_who_is_too_young_for_the_models_gets_the_age_restricted_gate(app):
    client, _ = client_for(app, age=12)
    assert state_of(client.get("/game"))["gate"] == "age_restricted"


def test_every_game_page_is_not_cached(app):
    client, user = client_for(app)
    save_id = open_save(app, user)
    for path in ("/game", f"/game/{save_id}", f"/game/start/{GHOST}", f"/game/script/{GHOST}"):
        assert "no-store" in client.get(path).headers.get("Cache-Control", ""), path


def test_the_game_pages_accept_only_get(app):
    client, _ = client_for(app)
    for path in ("/game", f"/game/{GHOST}", f"/game/start/{GHOST}", f"/game/script/{GHOST}"):
        for method in ("post", "put", "delete"):
            assert getattr(client, method)(path).status_code in (400, 403, 405), (method, path)   # 沒有 CSRF 權杖時可能先被擋


# ---- /game/<存檔>：遊戲主畫面的外殼與初始狀態 ---------------------------------------------------------------------
# rpg-game-ui 3.1 **有意識地改變**了這一段的契約（不是刪除，也不是掩蓋缺陷）：存檔頁原本是伺服器渲染的空殼（「劇情畫面建置中」，沒有腳本、不發任何請求），
# 現在是遊戲主畫面的外殼——標題等文字改由 Vue 插值顯示，頁面載入腳本並呼叫既有的端點。不變的是：非擁有者與不存在的存檔逐字相同的 404、
# 權限判斷全在 Service、頁面外殼本身不含任何劇情，也不建立或改動任何資料。

SESSION_STATE_KEYS = {"session", "user", "gate", "content_mode", "stream", "limits", "revision_limits"}


def test_the_owner_gets_the_game_page_with_exactly_the_agreed_initial_state(app):
    client, user = client_for(app)
    save_id = open_save(app, user, title="青雲問道", name="林初")
    response = client.get(f"/game/{save_id}")
    assert response.status_code == 200
    state = state_of(response)
    assert set(state) == SESSION_STATE_KEYS
    assert state["session"] == {"id": save_id, "title": "青雲問道", "content_tier": "safe", "ended": False, "player_name": "林初"}
    assert state["user"] == {"public_id": user.public_id, "member_tier": "free"}
    assert state["gate"] is None
    assert state["stream"] == {"idle_timeout_ms": 45000, "total_timeout_ms": 180000}
    assert state["limits"] == {"action_max": 1000}
    assert state["revision_limits"] == {                                          # rpg-turn-revision：理由字數與分類列舉（前端不寫死）
        "reason_min": 5, "reason_max": 50,
        "categories": ["setting_mismatch", "repetitive_or_dull", "typo_or_quality", "blocked_transition", "other"],
    }
    assert state["content_mode"] == {"effective": "safe", "forced": False}              # 預設安全模式開；沒有 is_adult、生日與年齡
    html = response.get_data(as_text=True)
    assert re.search(r'<div id="game-app" class="gm-shell" v-cloak>', html)
    assert re.search(r'<a class="gm-nav__link" href="/game"[^>]*>回到冒險</a>', html)


def test_the_two_stream_timeouts_and_the_action_limit_are_the_documented_constants(app):
    from app.controllers import game_page_controller
    from app.services import game_service

    assert (game_page_controller.STREAM_IDLE_TIMEOUT_MS, game_page_controller.STREAM_TOTAL_TIMEOUT_MS) == (45000, 180000)
    assert game_service.ACTION_MAX == 1000
    client, user = client_for(app)
    state = state_of(client.get(f"/game/{open_save(app, user)}"))
    assert state["limits"]["action_max"] == game_service.ACTION_MAX              # 引用後端的常數，不在前端或這個控制器另寫一份
    assert state["stream"]["idle_timeout_ms"] == game_page_controller.STREAM_IDLE_TIMEOUT_MS
    source = read(PROJECT_ROOT / "app" / "controllers" / "game_page_controller.py")
    assert '"action_max": ACTION_MAX' in source and not re.search(r'"action_max":\s*\d', source)
    limits = state["revision_limits"]                                              # 修訂的理由字數與分類同樣直接引用後端常數
    assert (limits["reason_min"], limits["reason_max"], limits["categories"]) == (
        game_service.REASON_MIN, game_service.REASON_MAX, list(game_service.CATEGORIES),
    )
    assert '"reason_min": REASON_MIN' in source and not re.search(r'"reason_(min|max)":\s*\d', source)


def test_the_initial_state_and_the_page_never_carry_story_content_the_notebook_the_world_or_personal_data(app):
    client, user = client_for(app)
    save_id = open_save(app, user, title="青雲問道", name="林初")
    with Session(app.extensions["db_engine"]) as s:
        save = SessionRepository(s).get_owned(user.id, save_id)
        save.opening_text = "第零幕的獨特文字"
        save.notebook_text = "記事簿的獨特文字"
        save.current_location = "獨特的所在地"
        save.status_summary = "獨特的狀態摘要"
        s.commit()
    response = client.get(f"/game/{save_id}")
    html = response.get_data(as_text=True)
    for hidden in ("第零幕的獨特文字", "記事簿的獨特文字", "獨特的所在地", "獨特的狀態摘要"):
        assert hidden not in html, hidden                                        # 劇情、第 0 幕、記事簿與世界狀態一律由前端呼叫既有端點取得
    state_text = json.dumps(state_of(response), ensure_ascii=False).lower()
    assert "user1@example.com" not in html and "birth" not in html.lower() and "age_range" not in html.lower()
    for forbidden in ("turns", "opening", "notebook", "world", "protagonist", "email", "birth", "age_range", "display_name"):
        assert forbidden not in state_text, forbidden


def test_the_page_shell_creates_nothing_and_changes_nothing(app):
    client, user = client_for(app)
    save_id = open_save(app, user)

    def snapshot():
        with Session(app.extensions["db_engine"]) as s:
            save = s.scalars(select(RpgSession)).one()
            return (s.scalar(select(func.count()).select_from(Wallet)), s.scalar(select(func.count()).select_from(RpgSession)),
                    save.version, save.opening_state, save.status)

    before = snapshot()
    assert client.get(f"/game/{save_id}").status_code == 200
    assert before == snapshot()                                                  # 沒有開錢包、沒有觸發第 0 幕、沒有改動存檔


def test_an_ended_save_says_so_in_the_initial_state(app):
    client, user = client_for(app)
    state = state_of(client.get(f"/game/{open_save(app, user, ended=True)}"))
    assert state["session"]["ended"] is True


def test_markup_in_the_title_and_the_protagonist_name_is_never_parsed_as_html(app):
    client, user = client_for(app)
    response = client.get(f"/game/{open_save(app, user, title='<b>粗體</b>劇本', name='<i>斜體</i>')}")
    html = response.get_data(as_text=True)
    assert "&lt;b&gt;粗體&lt;/b&gt;劇本" in html                                 # 分頁標題由 Jinja 跳脫
    assert "<b>粗體</b>" not in html and "<i>斜體</i>" not in html
    block = STATE_PATTERN.search(html).group(1)
    assert "<" not in block and ">" not in block                                 # 初始狀態經 tojson 跳脫：嵌在腳本區塊裡不可能提前結束
    state = state_of(response)                                                   # 還原後是原文：Vue 以插值顯示成原文字元
    assert state["session"]["title"] == "<b>粗體</b>劇本" and state["session"]["player_name"] == "<i>斜體</i>"
    template = read(TEMPLATES / "session.html")
    assert "{{ title }}" in template and "{{ playerName }}" in template and "v-html" not in template and "|safe" not in template


def test_the_game_page_carries_the_gate_the_server_decided(app):
    client, user = client_for(app, role=ROLE_ADMIN, age=None)
    response = client.get(f"/game/{open_save(app, user)}")
    assert response.status_code == 200                                        # 管理員沒有受限登入狀態：頁面可以開，輸入由閘門禁用
    assert state_of(response)["gate"] == "age_confirmation_required"
    client, user = client_for(app, 2, age=12)
    assert state_of(client.get(f"/game/{open_save(app, user)}"))["gate"] == "age_restricted"


def test_the_game_page_gets_the_computed_content_mode_from_the_server_with_no_birth_date_or_age(app):
    client, user = client_for(app, age=15)
    response = client.get(f"/game/{open_save(app, user)}")
    mode = state_of(response)["content_mode"]
    assert mode == {"effective": "safe", "forced": False}                         # 與 /api/sessions/catalog 的 content_mode 同一個來源與形狀
    html = response.get_data(as_text=True)
    assert "is_adult" not in html and user.birth_date.isoformat() not in html
    agreed = dict(content_tier_preference="adult", adult_confirmed_at=TODAY_UTC, adult_rules_version=SAFE_RULES_VERSION)
    client, user = client_for(app, 2, age=30, **agreed)
    assert state_of(client.get(f"/game/{open_save(app, user)}"))["content_mode"] == {"effective": "adult", "forced": False}


def test_someone_elses_save_and_a_missing_one_get_the_identical_404(app):
    owner_client, owner = client_for(app, 1)
    other_client, _ = client_for(app, 2)
    save_id = open_save(app, owner, title="只有擁有者看得到的標題", name="只有擁有者看得到的姓名")
    theirs = other_client.get(f"/game/{save_id}")
    missing = other_client.get(f"/game/{GHOST}")
    assert theirs.status_code == missing.status_code == 404
    assert theirs.get_data() == missing.get_data()                              # 逐字相同：不洩漏資源是否存在
    assert "只有擁有者看得到" not in theirs.get_data(as_text=True)
    assert owner_client.get(f"/game/{save_id}").status_code == 200


def test_an_anonymous_visitor_and_a_locked_player_are_sent_away_from_a_save(app):
    anonymous = app.test_client().get(f"/game/{GHOST}")
    assert anonymous.status_code == 302 and anonymous.headers["Location"] == f"/login?next=%2Fgame%2F{GHOST}"   # 帶回到該網址的返回位置
    client, _ = client_for(app, locked_at=START, lock_reason="rate_limited")
    locked = client.get(f"/game/{GHOST}")
    assert locked.status_code == 302 and locked.headers["Location"].startswith("/appeal")


@pytest.mark.parametrize("junk", ["start", "x", "0", "%20", "null"])
def test_a_path_that_is_not_a_save_id_is_a_404_not_an_error(app, junk):
    client, _ = client_for(app)
    assert client.get(f"/game/{junk}").status_code == 404


# ---- /game/start/<版本>：與存檔頁互不衝突 ------------------------------------------------------------------------


def test_the_start_path_is_the_wizard_shell_and_never_mistaken_for_a_save(app):
    client, user = client_for(app)
    version_id = publish_builtin(app)
    response = client.get(f"/game/start/{version_id}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "開始冒險" in html and "劇情畫面建置中" not in html
    assert version_id not in html                                                # 版本識別碼只在網址裡
    assert client.get(f"/game/start/{GHOST}").status_code == 200                 # 版本是否存在由精靈載入時的 setup 端點判斷
    assert client.get("/game/start").status_code == 404                          # 單一路徑段 `start` 是存檔識別碼的位置，不是精靈


def test_the_four_game_routes_are_distinct_rules(app):
    rules = {rule.rule: rule for rule in app.url_map.iter_rules() if rule.rule.startswith("/game")}
    assert set(rules) == {"/game", "/game/start/<version_public_id>", "/game/script/<version_public_id>", "/game/<session_public_id>"}
    for rule in rules.values():
        assert rule.methods - {"HEAD", "OPTIONS"} == {"GET"}, rule.rule


# ---- /game/script/<版本>：劇本詳情頁的外殼（畫面與行為在 tests/js/script-detail.test.js）-----------------------------------------

SCRIPT_STATE_KEYS = {"user", "gate"}


def test_the_script_detail_path_is_the_detail_shell_and_never_mistaken_for_a_save_or_the_wizard(app):
    client, user = client_for(app)
    version_id = publish_builtin(app, title="不該出現在外殼裡的劇本標題")
    save_id = open_save(app, user, title="不該出現在外殼裡的存檔")
    response = client.get(f"/game/script/{version_id}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="script-app"' in html and "劇本詳情" in html
    assert version_id not in html                                                # 版本識別碼只在網址裡
    assert "不該出現在外殼裡" not in html                                          # 外殼不含任何劇本文字：資料由前端呼叫詳情端點取得
    assert "劇情畫面建置中" not in html and 'id="wizard-app"' not in html and 'id="game-app"' not in html
    assert client.get(f"/game/{save_id}").status_code == 200                     # 存檔頁照常
    assert 'id="wizard-app"' in client.get(f"/game/start/{version_id}").get_data(as_text=True)   # 精靈頁照常
    assert client.get("/game/script").status_code == 404                         # 單一路徑段 `script` 是存檔識別碼的位置，不是詳情頁
    assert client.get("/game/script/").status_code == 404


def test_the_script_detail_shell_is_identical_whether_or_not_the_version_exists(app):
    """是否上架、能不能看由詳情端點判斷（404 時前端顯示「找不到這個劇本」）；外殼本身不查詢，所以存在與不存在的版本得到逐字相同的頁面。"""
    client, _ = client_for(app)
    version_id = publish_builtin(app)
    existing = client.get(f"/game/script/{version_id}").get_data(as_text=True)
    missing = client.get(f"/game/script/{GHOST}").get_data(as_text=True)
    csrf = re.compile(r'<meta name="csrf-token" content="[^"]*">')
    assert csrf.sub("", existing) == csrf.sub("", missing)


def test_the_script_detail_initial_state_holds_only_the_member_tier_and_the_gate(app):
    client, _ = client_for(app, member_tier="vip", story_preference="female")
    state = state_of(client.get(f"/game/script/{GHOST}"))
    assert set(state) == SCRIPT_STATE_KEYS
    assert state == {"user": {"member_tier": "vip"}, "gate": None}                # 沒有故事偏好、email、生日與年齡
    html = client.get(f"/game/script/{GHOST}").get_data(as_text=True)
    assert "user1@example.com" not in html and "birth" not in html.lower() and "female" not in html


def test_the_script_detail_shell_carries_the_gate_the_server_decided(app):
    client, _ = client_for(app, role=ROLE_ADMIN, age=None)
    assert state_of(client.get(f"/game/script/{GHOST}"))["gate"] == "age_confirmation_required"   # 管理員沒有受限登入狀態：頁面可以開，開始由閘門禁用
    client, _ = client_for(app, 2, age=12)
    assert state_of(client.get(f"/game/script/{GHOST}"))["gate"] == "age_restricted"


def test_an_anonymous_visitor_is_sent_to_the_login_page_and_comes_back_to_the_script_page(app):
    response = app.test_client().get(f"/game/script/{GHOST}")
    assert response.status_code == 302
    assert response.headers["Location"] == f"/login?next=%2Fgame%2Fscript%2F{GHOST}"


def test_a_player_waiting_for_deletion_is_sent_away_from_the_script_page(app):
    client, _ = client_for(app, status="deletion_pending")
    response = client.get(f"/game/script/{GHOST}")
    assert response.status_code == 302 and response.headers["Location"].startswith("/account")


def test_the_script_shell_creates_nothing_and_changes_nothing(app):
    client, user = client_for(app)
    version_id = publish_builtin(app)
    with Session(app.extensions["db_engine"]) as s:
        before = (s.scalar(select(func.count()).select_from(RpgSession)), s.scalar(select(func.count()).select_from(Wallet)))
    for path in (f"/game/script/{version_id}", f"/game/script/{GHOST}"):
        assert client.get(path).status_code == 200
    with Session(app.extensions["db_engine"]) as s:
        after = (s.scalar(select(func.count()).select_from(RpgSession)), s.scalar(select(func.count()).select_from(Wallet)))
    assert before == after


# ---- /game/start/<版本>：精靈頁的初始狀態與樣板（精靈的行為在 tests/js/wizard-state.test.js 與之後的 wizard.test.js） -----------------

RPG_ENV = {
    "OPENROUTER_API_KEY": "sk-or-v1-fake-key-for-tests-0123456789", "OPENROUTER_MODEL": "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
}
SESSION_API = PROJECT_ROOT / "app" / "controllers" / "session_api_controller.py"


def test_the_wizard_initial_state_holds_only_the_tier_the_gate_and_the_two_timeouts_never_script_content(app):
    client, _ = client_for(app)
    version_id = publish_builtin(app, title="不該出現在精靈外殼的劇本")
    response = client.get(f"/game/start/{version_id}")
    assert response.status_code == 200
    assert state_of(response) == {"member_tier": "free", "gate": None, "suggest_timeout_ms": 35000, "start_timeout_ms": 65000}
    html = response.get_data(as_text=True)
    assert "不該出現在精靈外殼" not in html and version_id not in html            # 劇本內容由 setup 端點取得；識別碼只在網址裡
    assert "user1@example.com" not in html and "birth" not in html.lower()


def test_the_start_timeout_matches_the_65_seconds_the_session_api_documents(app):
    """開存檔最壞是兩次模型呼叫（劇本審查與主角審查）：2 × 後端單次請求總預算 + 5 秒；預設預算 30 秒，所以是 65 秒。"""
    assert "65 秒" in read(SESSION_API)
    client, _ = client_for(app)
    assert state_of(client.get(f"/game/start/{GHOST}"))["start_timeout_ms"] == 65000


@pytest.mark.parametrize("extra", [{}, RPG_ENV], ids=["without-rpg-model", "with-rpg-model"])
def test_both_timeouts_follow_the_budget_setting_not_a_constant(extra):
    config = load_config(environ={"APP_ENV": "testing", "LLM_REQUEST_BUDGET_SECONDS": "40", "LLM_CALL_TIMEOUT_SECONDS": "25", **extra})
    application = create_app(config, clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    client, _ = client_for(application)
    state = state_of(client.get(f"/game/start/{GHOST}"))
    assert state["suggest_timeout_ms"] == 45000                                  # 後端預算 40 秒加 5 秒（與 /scripts 的送審相同的算法）
    assert state["start_timeout_ms"] == 85000                                    # 2 × 40 + 5


@pytest.mark.parametrize("overrides, gate", [
    ({"role": ROLE_ADMIN, "age": None}, "age_confirmation_required"), ({"age": 12}, "age_restricted"), ({"member_tier": "vip"}, None),
])
def test_the_wizard_carries_the_gate_the_server_decided_and_the_member_tier(app, overrides, gate):
    client, _ = client_for(app, **overrides)
    response = client.get(f"/game/start/{GHOST}")
    assert response.status_code == 200                                           # 管理員沒有受限登入狀態：頁面可以開，開始由閘門禁用
    state = state_of(response)
    assert state["gate"] == gate and state["member_tier"] == overrides.get("member_tier", "free")


def test_the_wizard_template_has_the_three_steps_the_three_modals_and_a_state_driven_root():
    html = read(TEMPLATES / "start.html")
    for title_id in ("gw-prologue-title", "gw-contact-title", "gw-create-title"):
        assert f'id="{title_id}"' in html
    assert 'v-for="(item, index) in steps"' in html and "step === 'prologue'" in html and "step === 'contact'" in html
    assert re.search(r'<div id="wizard-app" class="ge-shell" v-cloak>', html)
    assert re.findall(r'<ui-modal [^>]*status-code="(\w+)"', html) == ["SKIP_CONTACT", "OVERWRITE_DRAFT", "LEAVE_WIZARD"]
    assert "<ui-restriction-notice" in html and "<ui-toast-region" in html
    assert re.search(r"\{\{\s*initial_state\|tojson\s*\}\}", html)               # 初始狀態經 tojson 跳脫
    assert re.search(r'<a class="ge-topbar__link" href="/game" @click="leaveClick">', html)   # 離開前要先確認，所以由 Vue 接管點擊
    assert re.search(r':disabled="startDisabled"[^>]*@click="start"', html)      # 「開始冒險」的禁用由 Vue 的狀態決定


def test_the_wizard_shows_script_and_player_text_only_as_plain_interpolations():
    html = read(TEMPLATES / "start.html")
    for expression in ("setup.title", "setup.background", "option.label", "option.detail", "bubble.text", "bubble.speaker", "bubble.note", "draft.intro", "anchor.text"):
        assert re.search(r"\{\{\s*" + re.escape(expression) + r"\s*\}\}|v-model=\"" + re.escape(expression) + '"', html), expression
    assert "v-html" not in html and "innerHTML" not in html and "|safe" not in html


def test_the_contact_step_options_only_bind_a_name_and_an_occupation_never_age_or_hidden_traits():
    html = read(TEMPLATES / "start.html")
    assert "setup.characters" not in html                                        # 選項一律來自 targetGroups（只取姓名與職業，見 wizard-state.test.js）
    assert not re.search(r"option\.(age|hidden\w*|personality|appearance)", html)
    assert "v-for=\"group in targetGroups\"" in html and "v-for=\"option in group.options\"" in html


def test_the_wizard_has_no_avatar_upload_and_no_gender_field():
    html = read(TEMPLATES / "start.html")
    assert not re.search(r'type="file"|gender|avatar|<img|頭像|性別', html, re.I)
    assert 'type="radio"' in html and 'v-model="draft.origin"' in html           # 只有來歷單選
    assert "{{ field.hint }}" in html and "與您本人的年齡無關" in read(JS_DIR / "wizard-state.js")   # 年齡是劇中年齡的說明在年齡欄位下


def test_the_wizard_loads_its_scripts_in_dependency_order_and_hard_codes_no_timeout(app):
    client, _ = client_for(app)
    html = client.get(f"/game/start/{GHOST}").get_data(as_text=True)
    names = [ref.rsplit("/", 1)[-1] for ref in collect(html).refs if ref.endswith(".js")]
    for needed in ("vue.global.prod.js", "axios.min.js", "ui-components.js", "restriction.js", "auth-common.js", "game-labels.js", "wizard-state.js", "game-api.js", "wizard.js"):
        assert needed in names, needed
    assert names[-1] == "wizard.js"                                              # 掛載腳本必須是最後一個（它用到前面所有模組）
    assert names.index("auth-common.js") < names.index("game-labels.js") < names.index("wizard-state.js")     # wizard-state 要用 game-labels 的對照表
    assert names.index("auth-common.js") < names.index("game-api.js") < names.index("wizard.js")
    assert names.index("wizard-state.js") < names.index("wizard.js")
    assert 'id="wizard-pending-note"' not in html and "建置中" not in html           # 過渡用的說明隨 wizard.js 一起移除
    assert not re.search(r"\b(35000|65000|45000|85000)\b", read(TEMPLATES / "start.html"))
    api = strip_js_comments(read(JS_DIR / "game-api.js"))
    assert not re.search(r"timeout\w*\s*[:=]\s*\d", api, re.I)                   # 逾時與中止訊號由呼叫端傳入，這裡不寫死


# ---- /game/<存檔>：遊戲頁的樣板（遊戲邏輯在 tests/js/game-*.test.js；樣板用到的名稱由 tests/js/game-template.test.js 對照 game.js 的 setup()） ----


def test_the_game_template_has_the_timeline_the_composer_the_sidebar_the_modals_and_a_state_driven_root():
    html = read(TEMPLATES / "session.html")
    assert re.search(r'<div id="game-app" class="gm-shell" v-cloak>', html)
    assert 'role="log"' in html and 'ref="scroller"' in html and 'ref="input"' in html and "@scroll=\"onScroll\"" in html
    assert 'v-for="item in items"' in html and ':key="item.key"' in html
    for kind in ("opening", "turn", "note"):
        assert f"item.kind === '{kind}'" in html, kind                           # 第 0 幕、完整回合、轉場與中斷說明；其餘是在途回合
    # rpg-turn-revision 4.4 有意識地改寫這一行（不是刪除）：新增重新生成與編輯兩個 Modal，放在遊戲幣 Modal 之前，所以遊戲幣不足或簽到的 Modal 會疊在它們上面
    assert re.findall(r'<ui-modal [^>]*status-code="(\w+)"', html) == ["REGENERATE_TURN", "EDIT_TURN", "COINS", "LEAVE_GAME", "ABANDON_TURN", "DISCARD_NOTEBOOK"]
    assert "<ui-restriction-notice" in html and "<ui-toast-region" in html
    assert re.search(r"\{\{\s*initial_state\|tojson\s*\}\}", html)               # 初始狀態經 tojson 跳脫
    assert re.search(r'<aside id="gm-sidebar" v-show="sidebarOpen"', html)       # 側欄的顯示由 Vue 接管（預設收合，見 game.js）
    assert re.search(r'<button type="button" class="gm-iconbtn" aria-controls="gm-sidebar" :aria-expanded=', html)
    assert 'class="gm-scrim"' in html and '@click="closeSidebar"' in html          # 手機的覆蓋側欄有遮罩
    assert re.search(r':disabled="sendDisabled"[^>]*@click="send"', html) and ":disabled=\"inputDisabled\"" in html
    assert "@keydown.ctrl.enter.prevent=\"send\"" in html                         # Enter 換行，Ctrl＋Enter 送出
    assert re.search(r'<a class="gm-nav__link" href="/cs">客服</a>', html) and re.search(r'<a class="gm-nav__link" href="/account">帳號</a>', html)


def test_the_game_template_puts_the_notebook_in_a_second_sidebar_tab_with_a_full_entrance_in_the_top_bar_and_a_discard_confirmation():
    """rpg-notebook-panel：側欄有「世界」「記事簿」兩個分頁（預設世界），記事簿面板在側欄內；頂列的「記事簿已滿」入口與分頁徽章共用同一個判斷；
    放棄未儲存變更的確認是自製 Modal（v-model 由 Vue 接管），不使用瀏覽器原生的 confirm。
    （第 3 批曾斷言樣板的 raw 區塊完全沒有「記事簿」與 notebook，因為面板留到這一批；第 4 批有意識地改寫成本測試，不是刪除。）"""
    html = read(TEMPLATES / "session.html")
    body = html[html.index("{% raw %}"):html.index("{% endraw %}")]
    aside = body[body.index('<aside id="gm-sidebar"'):body.index("</aside>")]
    assert "@click=\"selectSideTab('world')\">世界</button>" in aside and "@click=\"selectSideTab('notebook')\">記事簿" in aside
    assert aside.count('role="tab"') == 2 and 'role="tablist"' in aside and aside.count('role="tabpanel"') == 2
    assert "v-show=\"sideTab === 'world'\"" in aside and "v-show=\"sideTab === 'notebook'\"" in aside          # 分頁的顯示由 Vue 接管，不用 data-bs-*
    assert '<span v-if="notebookFull" class="gm-tab__badge">已滿</span>' in aside
    assert re.search(r'<button v-if="notebookFull" type="button" class="gm-chipbtn" @click="openNotebook">記事簿已滿</button>', body)   # 頂列的入口（側欄收合時看得到）
    assert body.index('class="gm-chipbtn"') < body.index('<aside id="gm-sidebar"')
    assert 'v-model="notebookDiscardOpen"' in body and 'status-code="DISCARD_NOTEBOOK"' in body and "confirm(" not in body
    assert "data-bs-" not in body and "v-html" not in body


def test_the_game_template_shows_every_player_and_author_text_only_as_plain_interpolations():
    html = read(TEMPLATES / "session.html")
    for expression in ("title", "playerName", "item.text", "item.action", "item.story", "line", "world.location", "world.summary", "goal", "place", "notice.text",
                       "loadFailure.message", "coinModalText", "checkinMessage",
                       "notebook.text", "notebookConflict.text", "notebookNotice.text", "notebookError.message", "notebookFullText", "notebookEmptyText",
                       "notebookReserveNote", "notebookEditHint", "notebookSaveHint", "notebookOverNote"):
        assert re.search(r"\{\{\s*" + re.escape(expression) + r"(\s*\|\|\s*'[^']*')?\s*\}\}", html), expression   # 只允許以固定文字當作空值的備援
    assert 'v-model="draft"' in html
    for forbidden in ("v-html", "innerHTML", "|safe", "<img", "<iframe", "<script src=\"http"):
        assert forbidden not in html, forbidden
    assert not re.search(r"\{\{[^}]*\|\s*safe", html)
    assert "world.places" in html and "world.goals" in html and not re.search(r"world\.(traits|hidden\w*|arc\w*|entities|npc\w*)", html)   # 不洩漏 NPC 的特質、歸屬與弧線階段


def test_the_notebook_editor_and_its_buttons_stay_above_every_notice_and_the_comparison_so_a_conflict_cannot_push_them_out_of_view():
    # 版本衝突時對照區是整本記事簿（上千字）：它與各種說明必須在編輯框與按鈕「之後」，否則編輯框與儲存鈕會被推出視線（6.1 驗收曾發生）
    html = read(TEMPLATES / "session.html")
    editor = html.index('<textarea id="gm-notebook-draft"')
    save = html.index('@click="saveNotebook"')
    order = [html.index(marker) for marker in ('{{ notebookNotice.text }}', '{{ notebookOverNote }}', '{{ notebookSaveHint }}', '{{ notebookConflict.text }}')]
    assert editor < save < min(order)
    assert order == sorted(order)                                            # 說明（儲存結果、超過容量、尚未修改）在對照區之前
    assert html.count("{{ notebookNotice.text }}") == 1 and html.count("{{ notebookConflict.text }}") == 1


def test_the_game_page_loads_its_scripts_in_dependency_order_and_hard_codes_no_timeout_or_limit(app):
    client, user = client_for(app)
    save_id = open_save(app, user)
    html = client.get(f"/game/{save_id}").get_data(as_text=True)
    names = [ref.rsplit("/", 1)[-1] for ref in collect(html).refs if ref.endswith(".js")]
    expected = ["auth-common.js", "game-labels.js", "sse-parser.js", "turn-stream.js", "game-state.js", "game-store.js", "game-sync.js",
                "game-api.js", "wallet-checkin.js", "game-wallet.js", "game-notebook.js", "game-revision.js", "story-pages.js", "story-reader.js",
                "game-session.js", "game.js"]
    for needed in ("vue.global.prod.js", "axios.min.js", "typewriter.js", "ui-components.js", "restriction.js", *expected):
        assert needed in names, needed
    assert names[-1] == "game.js"                                                # 掛載腳本必須是最後一個（它用到前面所有模組）
    positions = [names.index(name) for name in expected]
    assert positions == sorted(positions), expected                              # 載入順序即相依順序（rpg-game-ui design D6）
    assert names.index("vue.global.prod.js") < names.index("typewriter.js") < names.index("ui-components.js") < names.index("auth-common.js")
    assert names.index("axios.min.js") < names.index("game-api.js")
    assert "建置中" not in html
    template = read(TEMPLATES / "session.html")
    assert not re.search(r"\b(45000|180000|1000)\b", template)                    # 逾時與字數上限只來自初始狀態
    for name in ("game-wallet.js", "game-session.js", "game.js"):
        code = strip_js_comments(read(JS_DIR / name))
        assert not re.search(r"\b(45000|180000)\b", code), name


def test_the_game_page_has_a_page_title_that_is_escaped_and_all_resources_are_local(app):
    client, user = client_for(app)
    html = client.get(f"/game/{open_save(app, user, title='標題 <script>x</script>')}").get_data(as_text=True)
    assert "<title>標題 &lt;script&gt;x&lt;/script&gt;</title>" in html
    assert "<script>x</script>" not in html


# ---- 前端檔案的規則 --------------------------------------------------------------------------------------------------


class _Resources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs, self.data_bs, self.hrefs = [], [], []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in ("src", "href") and value:
                self.refs.append(value)
            if name == "href" and value:
                self.hrefs.append(value)
            if name.startswith("data-bs-"):
                self.data_bs.append((name, value))


def collect(html: str) -> _Resources:
    collector = _Resources()
    collector.feed(html)
    return collector


@pytest.mark.parametrize("path_of", [
    lambda save_id: "/game", lambda save_id: f"/game/{save_id}", lambda save_id: f"/game/start/{save_id}", lambda save_id: f"/game/script/{save_id}",
])
def test_every_game_page_loads_only_local_resources_in_traditional_chinese_utf8(app, path_of):
    client, user = client_for(app)
    html = client.get(path_of(open_save(app, user))).get_data(as_text=True)
    collector = collect(html)
    assert not [ref for ref in collector.refs if re.match(r"(https?:)?//", ref)]
    assert collector.data_bs == [("data-bs-theme", "dark")]
    assert any(ref.endswith("css/game.css") for ref in collector.refs)
    assert '<html lang="zh-Hant-TW"' in html and '<meta charset="UTF-8"' in html


def test_the_index_page_scripts_load_in_dependency_order_and_the_entry_script_comes_last(app):
    client, _ = client_for(app)
    html = client.get("/game").get_data(as_text=True)
    names = [ref.rsplit("/", 1)[-1] for ref in collect(html).refs if ref.endswith(".js")]
    for needed in ("vue.global.prod.js", "axios.min.js", "ui-components.js", "restriction.js", "auth-common.js", "game-labels.js", "mode-prompt.js", "game-api.js", "entry.js"):
        assert needed in names, needed
    assert names[-1] == "entry.js"
    assert names.index("auth-common.js") < names.index("game-api.js") < names.index("entry.js")
    assert names.index("game-labels.js") < names.index("entry.js")
    assert names.index("mode-prompt.js") < names.index("entry.js")                  # 共用的詢問狀態機要先載入
    assert names.index("catalog-state.js") < names.index("entry.js")                # 目錄的搜尋與游標狀態要先載入
    assert 'id="game-app"' in html and re.search(r'<div id="game-app"[^>]*\bv-cloak\b', html)


def test_the_index_page_renders_the_shared_ask_modal_once_with_its_vue_syntax_intact(app):
    """詢問 Modal 是與詳情頁共用的樣板片段（`_mode_prompt.html`）：渲染後恰好一份，Vue 的 `{{ }}` 沒有被 Jinja 吃掉，也沒有殘留的 Jinja 指令。"""
    client, _ = client_for(app)
    html = client.get("/game").get_data(as_text=True)
    assert html.count('<ui-modal v-model="askOpen"') == 1
    for expression in ("{{ modePrompt.cancel }}", "{{ modePrompt.keepSafe }}", "{{ modePrompt.goSettings }}", "{{ line }}"):
        assert expression in html, expression
    assert "{% " not in html and "%}" not in html
    assert html.index('<ui-modal v-model="askOpen"') < html.index("<ui-toast-region")           # Modal 在 Toast 區之前、仍在應用的根元素內
    root_open = html.index('<div id="game-app"')
    assert root_open < html.index('<ui-modal v-model="askOpen"') < html.index('<script type="application/json" id="initial-state">')


def test_the_index_template_has_the_two_tabs_the_navigation_and_the_plain_text_interpolations():
    html = read(TEMPLATES / "index.html")
    assert 'role="tablist"' in html and ">選劇本<" in html and ">我的存檔<" in html
    assert "{{ card.title }}" in html and "{{ save.title" in html                # 作者與玩家的文字以插值顯示
    for href in ("/cs", "/scripts", "/account"):
        assert re.search(rf'<a class="ge-topbar__link" href="{href}">', html), href
    assert "<ui-restriction-notice" in html and "<ui-toast-region" in html
    assert "v-html" not in html and "data-bs-" not in html and "offcanvas" not in html.lower()
    assert re.search(r"\{\{\s*initial_state\|tojson\s*\}\}", html)               # 初始狀態經 tojson 跳脫


# ---- 劇本詳情頁的樣板與腳本（rpg-script-catalog 規格「詳情頁」；行為在 tests/js/script-detail.test.js）-----------------------------------------


def _detail_template() -> str:
    return read(TEMPLATES / "script_detail.html")


def test_the_detail_page_loads_its_scripts_in_dependency_order_and_the_detail_script_comes_last(app):
    client, _ = client_for(app)
    html = client.get(f"/game/script/{GHOST}").get_data(as_text=True)
    names = [ref.rsplit("/", 1)[-1] for ref in collect(html).refs if ref.endswith(".js")]
    for needed in ("vue.global.prod.js", "axios.min.js", "ui-components.js", "restriction.js", "auth-common.js", "game-labels.js", "mode-prompt.js", "script-report.js", "game-api.js", "script-detail.js"):
        assert needed in names, needed
    assert names[-1] == "script-detail.js"
    assert names.index("auth-common.js") < names.index("game-api.js") < names.index("script-detail.js")
    assert names.index("game-labels.js") < names.index("script-detail.js") and names.index("mode-prompt.js") < names.index("script-detail.js")
    assert names.index("script-report.js") < names.index("script-detail.js")                  # 詳情頁的腳本一載入就讀 Game.scriptReport
    assert "entry.js" not in names and "wizard.js" not in names                  # 不載入別頁的腳本
    assert re.search(r'<div id="script-app"[^>]*\bv-cloak\b', html)


def test_the_detail_template_has_the_four_phases_the_actions_and_the_shared_ask_modal():
    html = _detail_template()
    for phase in ("loading", "notfound", "failed", "ready"):
        assert f"phase === '{phase}'" in html, phase
    assert "找不到這個劇本" in html
    assert '<a class="ui-btn ui-btn--primary" href="/game">回到目錄</a>' in html and '<a class="ui-btn" href="/game">返回目錄</a>' in html
    assert '<button type="button" class="ui-btn" @click="loadDetail">重試</button>' in html                 # 暫時失敗可以重試
    assert '<button type="button" class="ui-btn ui-btn--primary" :disabled="!canStart" @click="start">開始冒險</button>' in html
    assert '{% include "game/_mode_prompt.html" %}' in html                                              # 詢問 Modal 與入口頁同一份，不另寫
    assert html.count("<ui-modal") == 1 and 'v-model="reportOpen"' in html                                # 本頁自己的 Modal 只有檢舉這一個
    assert "<ui-restriction-notice" in html and "<ui-toast-region" in html
    assert re.search(r"\{\{\s*initial_state\|tojson\s*\}\}", html)                                         # 初始狀態經 tojson 跳脫


def test_the_detail_template_shows_every_author_text_only_as_a_plain_interpolation():
    html = _detail_template()
    for expression in ("detail.title", "detail.summary", "detail.world_name", "detail.author_name", "detail.author_code", "detail.background",
                       "character.name", "character.occupation", "place", "tag", "failureText"):
        assert re.search(r"\{\{\s*" + re.escape(expression) + r"\s*\}\}", html), expression
    for forbidden in ("v-html", "innerHTML", "|safe", "<img", "<iframe", "data-bs-"):
        assert forbidden not in html, forbidden
    assert not re.search(r"\{\{[^}]*\|\s*safe", html)


def test_the_detail_template_never_asks_for_anything_the_public_detail_does_not_carry():
    """詳情頁只顯示公開的白名單欄位：樣板裡不得出現隱藏特質、個性、里程碑、結局說明、規則、初始對話與審查結果。"""
    html = _detail_template()
    body = html[html.index("{% raw %}"):html.index("{% endraw %}")]
    assert not re.search(r"hidden|personality|milestone|ending_text|ending_note|rules|opening|review|content_hash|content_json", body, re.I)


def test_the_detail_script_only_reads_the_detail_and_writes_only_the_report_and_keeps_nothing_in_the_browser():
    code = strip_js_comments(read(JS_DIR / "script-detail.js"))
    assert set(re.findall(r"Game\.api\.(\w+)", code)) == {"scriptDetail", "reportScript"}
    # 詳情端點的回應是 { ok, script: {…} }（session_api_controller.catalog_detail；test_session_api 固定了 `body["script"]`），
    # 頁面讀 `data.script`：Node 測試用替身回應，這裡守住兩端的包裝一致（第一次瀏覽器冒煙就是因為這個包裝對不上才發現的）
    assert "data.script" in code
    assert "script=get_game_entry_service().script_detail(" in read(PROJECT_ROOT / "app" / "controllers" / "session_api_controller.py")
    for storage in ("localStorage", "sessionStorage", "indexedDB", "document.cookie", "innerHTML", "v-html"):
        assert storage not in code, storage
    assert ".finally(" in code and "inflight = false" in code                                              # 無論成功或失敗都恢復載入狀態
    assert "submitting.value = false" in code and code.count(".finally(") == 2                             # 檢舉送出也一樣：一律在 finally 恢復
    assert "Auth.loginUrlFor(window.location.pathname)" in code and "window.location.assign" in code         # 401 導向登入並保留位置
    assert "Game.modePrompt.create" in code                                                                 # 詢問流程與入口頁共用，不另寫一份
    assert not re.search(r"needsModeAsk|content_tier === 'adult'", code)


def test_every_detail_class_the_template_uses_has_a_style_rule():
    css = read(STATIC / "css" / "game.css")
    used = set()
    for attribute in re.findall(r'class="([^"]*)"', _detail_template()):
        used.update(name for name in attribute.split() if name.startswith(("ge-", "sd-", "gw-")))
    assert {"sd-detail", "sd-title", "sd-summary", "sd-section", "sd-list", "sd-list__item", "sd-list__name", "sd-list__note",
            "sd-report", "sd-report__intro", "sd-report__reasons", "sd-report__reason", "sd-report__hint"} <= used, used
    defined = set(re.findall(r"\.((?:ge|sd|gw)-[\w-]+)", css))
    assert sorted(name for name in used if name not in defined) == []


# ---- 詳情頁的檢舉（rpg-script-reports；script-reports 規格「劇本詳情頁提供檢舉入口」；行為在 tests/js/script-report.test.js 與 script-detail.test.js）------------


def _report_modal() -> str:
    html = _detail_template()
    return html[html.index("<ui-modal"):html.index("</ui-modal>")]


def test_the_detail_page_shows_the_report_entry_by_report_state_and_never_for_the_unavailable_state():
    html = _detail_template()
    assert '<p v-if="reportState !== \'unavailable\'" class="sd-report">' in html
    assert '<button v-if="reportState === \'available\'" type="button" class="ui-btn" @click="openReport">{{ reportCopy.openButton }}</button>' in html
    assert '<button v-else type="button" class="ui-btn" disabled>{{ reportCopy.reportedButton }}</button>' in html


def test_the_report_modal_has_seven_reason_checkboxes_a_counted_text_box_and_a_confirm_that_needs_a_reason():
    modal = _report_modal()
    assert 'v-model="reportOpen"' in modal and ':status-code="reportCopy.code"' in modal and ':title="reportCopy.title"' in modal
    assert 'v-for="item in reportReasons"' in modal and '<input type="checkbox" v-model="reportPicked" :value="item.value" :disabled="submitting">' in modal   # 七個理由來自腳本，不寫死在樣板
    assert "{{ item.label }}" in modal
    assert re.search(r'<textarea id="report-text" class="gw-input"[^>]*v-model="reportText"[^>]*:disabled="submitting"></textarea>', modal)
    assert "{{ reportCheck.length }} / {{ textMax }}" in modal                                           # 字數計數
    assert '<p v-if="reportError" class="ge-error" role="alert">{{ reportError }}</p>' in modal             # 失敗說明在 Modal 內，純文字
    assert ':disabled="!canSubmitReport" @click="submitReport"' in modal                                    # 沒有理由時確認鈕停用
    assert ':disabled="submitting" @click="closeReport"' in modal                                           # 送出中不能取消


def test_the_report_modal_and_entry_use_only_plain_interpolation_and_no_bootstrap_interaction():
    for text in (_report_modal(), _detail_template()):
        for forbidden in ("v-html", "innerHTML", "data-bs-", "|safe"):
            assert forbidden not in text, forbidden
    assert "<form" not in _report_modal()                                                                   # 不靠表單的原生送出，只有按鈕呼叫 submitReport


def test_the_report_script_lists_the_backend_reasons_in_the_backend_order_and_the_same_text_limit():
    code = strip_js_comments(read(JS_DIR / "script-report.js"))
    assert tuple(re.findall(r"\{ value: '(\w+)', label:", code)) == REPORT_REASONS
    assert f"var TEXT_MAX = {REPORT_NOTE_MAX_CHARS};" in code


def test_the_report_script_knows_the_same_three_states_the_detail_service_returns():
    code = strip_js_comments(read(JS_DIR / "script-report.js"))
    assert "var STATES = ['available', 'reported', 'unavailable'];" in code
    service = read(PROJECT_ROOT / "app" / "services" / "game_entry_service.py")
    assert 'return "unavailable"' in service and 'return "reported" if' in service and '"available"' in service


def test_the_report_script_is_pure_and_the_report_api_call_is_a_post_to_the_report_path():
    code = strip_js_comments(read(JS_DIR / "script-report.js"))
    for forbidden in ("localStorage", "sessionStorage", "indexedDB", "document.", "window.", "axios", "fetch(", "innerHTML", "v-html", "console."):
        assert forbidden not in code, forbidden
    api = strip_js_comments(read(JS_DIR / "game-api.js"))
    assert "reportScript: function (versionId, body, options) { return send('post', BASE + '/catalog/' + encodeURIComponent(versionId) + '/report', body, options); }" in api
    assert '@session_api_bp.post("/catalog/<version_id>/report")' in read(PROJECT_ROOT / "app" / "controllers" / "session_api_controller.py")   # 前端的路徑對得上後端


# ---- 入口頁的目錄搜尋與篩選（rpg-script-catalog 規格「目錄畫面的搜尋與篩選」；行為在 tests/js/entry.test.js 與 catalog-state.test.js）----------------

def _scripts_panel() -> str:
    html = read(TEMPLATES / "index.html")
    return html[html.index('id="ge-panel-scripts"'):html.index('id="ge-panel-saves"')]


def test_the_catalog_panel_has_a_search_form_a_channel_select_tag_chips_and_a_clear_button_all_driven_by_vue_state():
    panel = _scripts_panel()
    assert '<form class="ge-search" role="search" @submit.prevent="submitSearch">' in panel             # Enter 與按鈕都走同一個送出
    assert re.search(r'<input id="ge-search-input"[^>]*type="search"[^>]*v-model="searchText"[^>]*:maxlength="qMax"', panel, re.S)
    assert '<label class="ge-search__label" for="ge-search-input">' in panel and '<button type="submit"' in panel and ">搜尋<" in panel
    assert '<select id="ge-audience-select" class="ge-select" :value="catalog.audience" @change="chooseAudience($event.target.value)">' in panel
    assert 'v-for="option in audienceOptions"' in panel and "{{ option.label }}" in panel
    assert 'v-for="chip in tagChips"' in panel and '@click="chooseTag(chip.tag)"' in panel and "{{ chip.tag }}" in panel
    assert panel.count('@click="clearFilters"') == 2 and panel.count(">清除篩選<") == 2                    # 篩選列一個、無結果的說明一個
    assert 'v-if="hasFilters"' in panel
    assert "沒有符合的劇本" in panel and "目前沒有可玩的劇本" in panel
    assert "emptyKind === 'filtered'" in panel and "emptyKind === 'none'" in panel


def test_tag_chips_are_buttons_that_say_whether_they_are_selected_and_never_plain_markup():
    panel = _scripts_panel()
    for expression in ("chip.tag", "tag"):
        assert f":class=\"{{ 'ge-chip--on': isTagOn({expression}) }}\"" in panel, expression
        assert f":aria-pressed=\"isTagOn({expression}) ? 'true' : 'false'\"" in panel, expression
    assert '@click="chooseCardTag(tag)"' in panel and ">{{ tag }}</button>" in panel                    # 卡片上的標籤可點選，文字是插值
    assert panel.count('type="button" class="ge-chip ge-chip--tag ge-chip--button"') == 1 or "ge-chip--button" in panel


def test_the_card_title_links_to_the_detail_page_and_load_more_continues_with_the_same_loader():
    panel = _scripts_panel()
    assert '<h2 class="ge-card__title"><a class="ge-card__link" :href="detailPath(card.id)">{{ card.title }}</a></h2>' in panel
    assert panel.count('@click="loadCatalog"') == 2                                                      # 重試與載入更多都是同一個函式（游標由狀態接續）
    assert "catalog.hasMore" in panel and "catalog.error" in panel
    assert '@click="start(card)"' in panel and ":disabled=\"!canStart\"" in panel


def test_every_card_has_a_visible_view_details_button_next_to_start():
    """使用者驗收時回報：卡片上只看得到「開始」，無法只查看詳情。標題雖然是連結，但原本長得跟普通文字一樣（手機沒有「滑鼠移上去」），
    所以按鈕列要有明確的「查看詳情」，與「開始」並排；兩者都是一般站內連結或按鈕，沒有 Bootstrap 的互動屬性。"""
    panel = _scripts_panel()
    actions = re.search(r'<p class="ge-card__actions">(.*?)</p>', panel, re.S)
    assert actions, "卡片沒有按鈕列"
    body = actions.group(1)
    detail = re.search(r'<a class="ui-btn" :href="detailPath\(card\.id\)">查看詳情</a>', body)
    start = re.search(r'<button type="button" class="ui-btn ui-btn--primary" :disabled="!canStart" @click="start\(card\)">開始</button>', body)
    assert detail and start
    assert detail.start() < start.start()                                                              # 先看詳情、再開始
    assert "data-bs-" not in body and "v-html" not in body


def test_the_card_title_looks_like_a_link_and_the_card_buttons_have_a_touch_sized_target():
    """標題連結要看得出能點（底線），不能只靠 hover 變色；按鈕列的兩個按鈕在手機上約 44px 高。"""
    css = read(STATIC / "css" / "game.css")
    link = css.split(".ge-card__link {", 1)[1].split("}", 1)[0]
    assert "text-decoration: underline" in link and "text-decoration: none" not in link
    actions = css.split(".ge-card__actions .ui-btn {", 1)[1].split("}", 1)[0]
    assert "min-height: 2.75rem" in actions and "align-items: center" in actions
    assert "display: flex" in css.split(".ge-card__actions {", 1)[1].split("}", 1)[0]


def test_the_catalog_panel_shows_author_text_only_as_plain_interpolations_and_uses_no_forbidden_markup():
    panel = _scripts_panel()
    for expression in ("{{ card.title }}", "{{ card.summary }}", "{{ card.world_name }}", "{{ card.author_name }}", "{{ chip.tag }}", "{{ tag }}"):
        assert expression in panel, expression
    assert "v-html" not in panel and "data-bs-" not in panel and "|safe" not in panel
    assert "localStorage" not in panel and "sessionStorage" not in panel


def test_every_catalog_class_the_index_template_uses_has_a_style_rule():
    css = read(STATIC / "css" / "game.css")
    panel = _scripts_panel()
    used = set()
    for attribute in re.findall(r'class="([^"]*)"', panel):
        used.update(name for name in attribute.split() if name.startswith("ge-"))
    used.update(re.findall(r"'(ge-[\w-]+)'\s*:", panel))                                                  # :class 物件裡的類別
    assert {"ge-search", "ge-filters", "ge-tags", "ge-chip--button", "ge-chip--on", "ge-card__link", "ge-select"} <= used, used
    defined = set(re.findall(r"\.(ge-[\w-]+)", css))
    assert sorted(name for name in used if name not in defined) == []


def test_the_catalog_state_constants_match_the_backend_limits_and_the_channel_labels():
    """搜尋框的上限、常用標籤的上限與頻道選項是後端規則的鏡像：後端調整時這裡失敗，提醒同步 `catalog-state.js`。"""
    from app.services.catalog_query import Q_MAX_CHARS
    from app.services.game_entry_service import CATALOG_TAGS_LIMIT

    source = read(JS_DIR / "catalog-state.js")
    assert int(re.search(r"var Q_MAX_CHARS = (\d+);", source).group(1)) == Q_MAX_CHARS
    assert int(re.search(r"var TAGS_MAX = (\d+);", source).group(1)) == CATALOG_TAGS_LIMIT
    values = re.search(r"var AUDIENCE_VALUES = \[([^\]]*)\];", source).group(1)
    assert sorted(re.findall(r"'(\w+)'", values)) == sorted(AUDIENCES)                                       # 選項值與後端的三個頻道完全一致


def test_the_catalog_never_sends_an_offset_and_the_saves_list_still_pages_by_offset():
    api = strip_js_comments(read(JS_DIR / "game-api.js"))
    params = re.search(r"function catalogParams\(options\) \{(.*?)\n  \}", api, re.S).group(1)
    assert "offset" not in params and "'cursor'" in params and "'q'" in params and "'tag'" in params and "'audience'" in params
    entry = strip_js_comments(read(JS_DIR / "entry.js"))
    assert "offset: list.offset" in entry                                                                 # 「我的存檔」仍以位移分頁


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class _Balance(HTMLParser):
    """檢查標籤成對：Vue 的正式版編譯器不會對沒關閉的標籤拋錯，瀏覽器則會悄悄改寫結構，所以要在這裡先擋。"""

    def __init__(self):
        super().__init__()
        self.stack, self.problems = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"</{tag}> 前面是 {self.stack[-1] if self.stack else '（空）'}")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
        else:
            self.stack.pop()


@pytest.mark.parametrize("name", ["index.html", "session.html", "start.html", "script_detail.html", "_mode_prompt.html"])
def test_every_game_template_has_balanced_tags(name):
    balance = _Balance()
    balance.feed(read(TEMPLATES / name))
    assert balance.problems == [] and balance.stack == [], (balance.problems, balance.stack)


def test_the_balance_check_really_catches_a_broken_template():
    for broken in ("<div><p>text</div>", "<div><span></div></span>", "<ul><li>a</ul>", "<div>"):
        balance = _Balance()
        balance.feed(broken)
        assert balance.problems or balance.stack, broken


def test_no_game_template_or_script_uses_forbidden_patterns():
    for path in sorted(TEMPLATES.glob("*.html")):
        text = read(path)
        assert "v-html" not in text and "data-bs-" not in text and "|safe" not in text, path.name
        assert "cdn" not in text.lower() and "googleapis" not in text.lower(), path.name
        assert "/static/" not in text, path.name                                 # 資源一律用 url_for
    js = "\n".join(strip_js_comments(read(path)) for path in sorted(JS_DIR.glob("*.js")))
    assert js, "沒有找到前端腳本"
    for forbidden in ("v-html", "innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "document.write", "console.log"):
        assert forbidden not in js, forbidden
    assert not re.search(r"\b(alert|confirm|prompt)\s*\(", js)
    # 一般 API 一律走離線 axios。原生 fetch 只屬於 RPG 劇情的串流客戶端（AGENTS.md 第 5 節：fetch 搭配 ReadableStream）——
    # rpg-game-ui 之前整個目錄都不得出現 `fetch(`，這是**刻意的放寬**：只有 `turn-stream.js` 可以，而且只能呼叫端注入的 `env.fetch`（不碰全域的 window.fetch）。
    outside_stream = "\n".join(strip_js_comments(read(path)) for path in sorted(JS_DIR.glob("*.js")) if path.name != "turn-stream.js")
    assert "fetch(" not in outside_stream
    stream = strip_js_comments(read(JS_DIR / "turn-stream.js"))
    assert "env.fetch(" in stream
    assert not re.search(r"(?<![\w.])fetch\(", stream) and "window.fetch" not in stream
    assert "bootstrap" not in js.lower()


def test_the_entry_page_keeps_nothing_in_the_browser_and_never_writes(app):
    """rpg-game-ui 2.1 與 2.3 **有意識地改變**了這個測試的契約（不是刪除，也不是掩蓋缺陷）：原本是「整個 `game/` 目錄都不碰瀏覽器儲存、
    API 只有精靈的兩個寫入」；遊戲頁現在要離線閱讀的快取與在途回合，所以 IndexedDB 只允許出現在 `game-store.js`（一個檔案、一個出口），
    其餘的瀏覽器儲存（localStorage、sessionStorage、cookie）整個目錄仍然都不得出現。API 的寫入呼叫由下一個測試固定為四個。
    入口頁本身只讀（rpg-script-publishing 7.2 之後多一個唯讀的 catalogTags）：`entry.js` 只引用 catalog、catalogTags 與 sessions，精靈只用 setup、suggest、start。"""
    code = {path.name: strip_js_comments(read(path)) for path in sorted(JS_DIR.glob("*.js"))}
    assert code, "沒有找到前端腳本"
    for name, js in code.items():
        for storage in ("localStorage", "sessionStorage", "document.cookie"):
            assert storage not in js, (name, storage)
        if name != "game-store.js":
            assert "indexedDB" not in js, name                                   # 瀏覽器端的快取只在 game-store.js
    assert "indexedDB" in code["game-store.js"]
    api = code["game-api.js"]
    assert "axios" in api and "/api/sessions" in api
    assert not re.search(r"""method:\s*['"](post|put|patch|delete)""", api, re.I)   # 兩個清單端點的 get() 只用 GET，入口頁只讀
    assert set(re.findall(r"Game\.api\.(\w+)", code["entry.js"])) == {"catalog", "catalogTags", "sessions"}
    assert set(re.findall(r"Game\.api\.(\w+)", code["wizard.js"])) == {"setup", "suggest", "start"}


def test_the_game_api_writes_through_exactly_six_calls_and_takes_timeouts_and_signals_from_the_caller():
    """寫入的呼叫恰好是 suggest（生成預覽，無狀態）、start（開存檔）、checkin（每日簽到）、saveNotebook（玩家儲存記事簿）、reportScript（檢舉劇本）
    與 suggestActions（遊戲內的行動建議，rpg-story-view）；其餘都是 GET。回合與第 0 幕的串流不在 game-api.js（那是 turn-stream.js 的原生 fetch）。"""
    api = strip_js_comments(read(JS_DIR / "game-api.js"))
    calls = re.findall(r"""(\w+):\s*function\s*\([^)]*\)\s*\{\s*return\s+(?:call|send)\('(get|post|put|patch|delete)'""", api, re.I)
    assert sorted(name for name, method in calls if method != "get") == ["checkin", "reportScript", "saveNotebook", "start", "suggest", "suggestActions"]
    assert dict(calls)["saveNotebook"] == "put" and dict(calls)["checkin"] == "post" and dict(calls)["reportScript"] == "post"
    assert dict(calls)["suggestActions"] == "post"
    # 目錄、常用標籤與劇本詳情（rpg-script-publishing 7.2）也經 send()，但都是 GET：寫入的呼叫仍恰好是上面六個
    assert sorted(name for name, method in calls if method == "get") == [
        "catalog", "catalogTags", "notebook", "scriptDetail", "setup", "snapshot", "turns", "wallet",
    ]
    for needed in ("/api/game/sessions", "/api/wallet", "encodeURIComponent"):
        assert needed in api, needed
    assert "request.timeout" in api and "request.signal" in api                 # 逾時與中止訊號由呼叫端傳入
    assert not re.search(r"timeout\w*\s*[:=]\s*\d", api, re.I)                   # 檔內沒有任何逾時數字
    assert "fetch" not in api and "EventSource" not in api and "/stream" not in api


def test_every_loading_path_restores_its_state_in_a_finally_block():
    js = strip_js_comments(read(JS_DIR / "entry.js"))
    assert ".finally(" in js and "list.loading = false" in js
    assert "window.location.assign" in js and "Auth.loginUrlFor(PAGE_PATH)" in js     # 401 導向登入並保留位置


def test_the_stylesheet_hides_cloaked_elements_never_uses_pure_white_and_never_forces_a_wide_layout():
    css = read(STATIC / "css" / "game.css")
    assert re.search(r"\[v-cloak\]\s*\{\s*display:\s*none", css)
    lowered = css.lower()
    assert "#fff" not in lowered and not re.search(r"(?<![\w-])white(?![\w-])", lowered) and "rgb(255, 255, 255)" not in lowered
    assert not re.search(r"(?<!max-)(?<!min-)width:\s*\d{3,}px", css)                # 沒有固定的寬度（360px 不橫向溢出）
    assert "grid-template-columns: minmax(0, 1fr)" in css                            # 預設（手機）單欄，桌面才分兩欄
    assert re.search(r"@media \(min-width: 768px\)\s*\{\s*\.ge-cards \{ grid-template-columns: repeat\(2", css)


def labels_in(source: str, name: str) -> list[str]:
    """取出 `var NAME = { key: '…', … };` 的鍵（不執行 JavaScript）。"""
    match = re.search(rf"var {name} = \{{(.*?)\}};", source, re.S)
    assert match, name
    return re.findall(r"(\w+):\s*'", match.group(1))


def test_the_label_tables_cover_exactly_the_enums_the_backend_uses():
    """後端新增或改名列舉值時，這裡會失敗，提醒同步 `game-labels.js`（否則畫面會退回預設文字而不是正確的名稱）。"""
    source = read(JS_DIR / "game-labels.js")
    assert sorted(labels_in(source, "MEMBER_LABELS")) == sorted(MEMBER_TIERS)
    # safe-mode-toggle 4.1：卡片的 TIER_LABELS 只有綁定（safe）一個標籤（adult 與舊值 standard 是未綁定、沒有標籤）；
    # 存檔的生效模式 MODE_LABELS 與後端的兩種內容模式完全一致
    assert labels_in(source, "TIER_LABELS") == ["safe"] and "safe" in CONTENT_MODES
    assert sorted(labels_in(source, "MODE_LABELS")) == sorted(CONTENT_MODES)
    assert sorted(labels_in(source, "AUDIENCE_LABELS")) == sorted(AUDIENCES)
    assert sorted(labels_in(source, "ORIGIN_LABELS")) == sorted(ORIGINS)
    assert sorted(labels_in(source, "STATUS_LABELS")) == ["active", "ended"]
    assert sorted(labels_in(source, "CATEGORY_LABELS")) == sorted(REVIEW_CATEGORIES)


def test_the_game_screen_label_tables_cover_exactly_the_backend_turn_statuses_done_statuses_and_stream_error_codes():
    """rpg-game-ui 1.4：回合狀態（`turn_view.status`）、串流 `done` 的結束狀態、串流 `error` 事件的代碼都要有繁體中文對照；
    後端新增或改名時這裡會失敗，提醒同步 `game-labels.js`（否則畫面會退回通用文字）。"""
    from app.models.rpg_session import TURN_STATUSES
    from app.services.rpg_runner_base import DONE_BLOCKED, DONE_COMPLETE, DONE_ENDED, ERROR_MESSAGES

    source = read(JS_DIR / "game-labels.js")
    assert sorted(labels_in(source, "TURN_STATUS_LABELS")) == sorted(TURN_STATUSES)
    assert sorted(labels_in(source, "DONE_STATUS_LABELS")) == sorted([DONE_COMPLETE, DONE_BLOCKED, DONE_ENDED])
    assert sorted(labels_in(source, "STREAM_ERROR_MESSAGES")) == sorted(ERROR_MESSAGES)


def test_the_revision_label_tables_cover_exactly_the_backend_categories_not_revisable_reasons_and_the_error_codes_a_revision_can_send():
    """rpg-turn-revision 4.1：重新生成的五個分類、不能修訂的四個原因、修訂串流可能送出的錯誤代碼，都要有繁體中文對照；
    後端新增或改名時這裡會失敗，提醒同步 `game-labels.js`（否則畫面會退回通用文字）。分類的名稱與順序以頁面初始狀態為準，這裡只比對列舉。"""
    from app.models.rpg_session import REGEN_CATEGORIES
    from app.repositories.session_repository import NOT_REVISABLE_REASONS
    from app.services.errors import NOT_REVISABLE_MESSAGES
    from app.services.rpg_runner_base import ERROR_MESSAGES

    source = read(JS_DIR / "game-labels.js")
    assert sorted(labels_in(source, "REVISION_CATEGORY_LABELS")) == sorted(REGEN_CATEGORIES)
    assert sorted(labels_in(source, "REVISION_NOT_REVISABLE_MESSAGES")) == sorted(NOT_REVISABLE_REASONS) == sorted(NOT_REVISABLE_MESSAGES)
    assert sorted(labels_in(source, "REVISION_ERROR_MESSAGES")) == sorted(set(ERROR_MESSAGES) - {"opening_superseded"})   # 第 0 幕的代碼不會出現在修訂串流
    state = read(JS_DIR / "game-state.js")
    for reason in NOT_REVISABLE_REASONS:                                         # 前端辨識原因的列舉與後端一致
        assert f"{reason}: true" in state, reason


def test_the_revision_error_wording_never_promises_nothing_changed_for_an_outcome_that_is_not_certain():
    """取消、中斷與逾時時伺服器可能已經完成修訂，前端的「不確定」說明不得承諾「沒有扣遊戲幣」；確定的失敗才說（rpg-turn-revision design 風險）。"""
    source = read(JS_DIR / "game-labels.js")
    uncertain = re.search(r"uncertain: '([^']*)'", source).group(1)
    assert "伺服器可能已經完成" in uncertain and "沒有扣" not in uncertain
    for key in ("kept", "kept_blocked", "kept_failed"):
        assert "已保留原本的回應" in re.search(rf"{key}: '([^']*)'", source).group(1), key


def game_screen_css() -> str:
    css = read(STATIC / "css" / "game.css")
    return css[css.index("遊戲主畫面（/game/<存檔>"):]


def test_the_game_screen_styles_use_theme_colors_only_and_the_992px_breakpoint_and_never_force_a_width():
    css = game_screen_css()
    lowered = css.lower()
    assert "#fff" not in lowered and not re.search(r"(?<![\w-])white(?![\w-])", lowered) and "rgb(255, 255, 255)" not in lowered
    assert not re.search(r"#[0-9a-f]{3,8}\b", lowered)                            # 顏色全部取自主題變數（theme.css），不另寫色碼
    assert re.search(r"@media \(min-width: 992px\)", css)                          # 與客服的 Sidebar 同一個斷點
    assert not re.search(r"@media[^{]*\b(min|max)-width:\s*(?!992px|575\.98px)\d", css)   # 除了 992px 與既有的手機斷點，沒有別的斷點
    widths = re.findall(r"(?<![\w-])width:\s*([^;}]+)", css)
    assert set(widths) <= {"1px", "0.5em", "min(320px, 86vw)"}, widths               # 唯一較寬的固定值包在 min() 內：360px 不會被撐出橫向捲軸
    assert not re.search(r"(?<!max-)(?<!min-)width:\s*\d{3,}px", css)
    assert "flex: 0 0 320px" in css                                                   # 桌面側欄 320px，推擠主區域
    assert "overflow-wrap: anywhere" in css and css.count("min-width: 0") >= 2        # flex 子元素可收縮，長字不撐破版面


def test_the_game_screen_keeps_the_composer_in_the_flow_so_it_never_covers_the_last_story_line():
    css = game_screen_css()
    shell = re.search(r"\.gm-shell \{([^}]*)\}", css).group(1)
    assert "height: 100dvh" in shell and "flex-direction: column" in shell and "overflow: hidden" in shell
    scroll = re.search(r"\.gm-scroll \{([^}]*)\}", css).group(1)
    assert "overflow-y: auto" in scroll and "min-height: 0" in scroll and "flex: 1 1 auto" in scroll   # 只有時間軸捲動
    composer = re.search(r"\.gm-composer \{([^}]*)\}", css).group(1)
    assert "flex: 0 0 auto" in composer and "position: fixed" not in composer and "sticky" not in composer   # 在版面流程內，不是浮在內容上
    fixed = re.findall(r"([^{}]+)\{[^}]*position:\s*fixed", css)
    assert [rule.strip() for rule in fixed] == [".gm-side--overlay", ".gm-scrim"]    # 只有覆蓋用的側欄與遮罩是固定定位


def test_every_game_screen_class_the_template_uses_has_a_style_rule():
    css = game_screen_css()
    template = read(TEMPLATES / "session.html")
    body = template[template.index("{% raw %}"):template.index("{% endraw %}")]
    used = set()
    for attribute in re.findall(r'\sclass="([^"]*)"', body):
        used.update(name for name in attribute.split() if name.startswith("gm-"))
    for expression in re.findall(r"'(gm-[\w-]+)\s*'?\s*\+", body):                     # :class="'gm-notice--' + notice.tone" 這類動態類別只檢查前綴
        used.add(expression)
    assert len(used) >= 25, used
    defined = set(re.findall(r"\.(gm-[\w-]+)", css))
    missing = sorted(name for name in used if not any(rule == name or (name.endswith("--") and rule.startswith(name)) for rule in defined))
    assert missing == [], missing


def test_the_streaming_caret_and_every_animation_respect_reduced_motion():
    css = game_screen_css()
    assert "animation: gm-caret" in css and re.search(r"prefers-reduced-motion: reduce\)\s*\{\s*\.gm-story--streaming::after \{ animation: none", css)


# ---- 與其他區域的導覽互通 ----------------------------------------------------------------------------------------


def test_the_game_home_and_the_other_areas_link_to_each_other_as_plain_site_links(app):
    client, _ = client_for(app)
    home = collect(client.get("/game").get_data(as_text=True))
    for target in ("/cs", "/scripts", "/account"):
        assert target in home.hrefs, target                                      # /game 的頂列：客服、創作、帳號
    assert home.data_bs == [("data-bs-theme", "dark")]
    for page in ("/cs", "/scripts", "/account"):
        html = client.get(page).get_data(as_text=True)
        assert 'href="/game"' in html and ">冒險<" in html, page                  # 各頁的頁首都有回到 /game 的「冒險」
        assert collect(html).data_bs == [("data-bs-theme", "dark")], page


def test_the_game_screen_links_back_to_the_adventure_home_support_and_the_account_as_plain_site_links(app):
    client, user = client_for(app)
    page = collect(client.get(f"/game/{open_save(app, user)}").get_data(as_text=True))
    for target in ("/game", "/cs", "/account"):
        assert target in page.hrefs, target                                      # 頂列：回到冒險、客服、帳號（一般站內連結）
    assert page.data_bs == [("data-bs-theme", "dark")]                            # 沒有任何 Bootstrap 互動屬性
    html = read(TEMPLATES / "session.html")
    for label, target in (("回到冒險", "/game"), ("客服", "/cs"), ("帳號", "/account")):
        assert re.search(rf'<a class="gm-nav__link" href="{target}"[^>]*>{label}</a>', html), label
    # 精靈成功開存檔後導向 /game/<存檔>（不改）：路徑由 Game.sessionPath 組出，遊戲頁就在這個網址
    assert "/game/" in read(JS_DIR / "game-labels.js") and "window.location.assign(Game.sessionPath(saved))" in strip_js_comments(read(JS_DIR / "wizard.js"))


# ---- 安全模式（safe-mode-toggle 第 4 組）：入口頁、精靈頁與遊戲頁不再有三種模式、年齡說明與被淘汰的錯誤碼 ---------------------------------------------

def test_no_game_page_script_or_template_keeps_the_three_mode_wording_the_age_note_or_the_retired_refusals():
    retired = ("一般模式", "成人模式", "未滿 18 歲固定", "MINOR_NOTE", "is_adult", "content_tier_restricted", "content_mode_restricted", "依年齡規定")
    files = [*(JS_DIR.glob("*.js")), TEMPLATES / "index.html", TEMPLATES / "session.html", TEMPLATES / "start.html"]
    for file in files:
        if not file.exists():
            continue
        source = read(file)
        for word in retired:
            if file.name == "game-state.js" or file.name == "wizard-state.js":
                continue                                                    # 兩個狀態檔只在註解與退回說明裡提到舊代碼的處理，下方另有逐項檢查
            assert word not in source, (file.name, word)


def test_the_state_files_only_mention_the_retired_refusals_in_comments_not_in_their_gate_tables():
    for name in ("game-state.js", "wizard-state.js"):
        source = read(JS_DIR / name)
        table = re.search(r"var GATE_FAILURES = \{(.*?)\};", source, re.S)
        assert table, name
        assert "content_tier_restricted" not in table.group(1) and "content_mode_restricted" not in table.group(1), name


def test_the_entry_template_shows_the_binding_label_only_when_there_is_one_and_the_saves_show_the_live_mode():
    html = read(TEMPLATES / "index.html")
    assert '<span v-if="tierLabel(card.content_tier)" class="ge-chip">{{ tierLabel(card.content_tier) }}</span>' in html
    assert '<span class="ge-chip">{{ modeLabel(save.content_tier) }}</span>' in html
    assert "未標示" not in html


def test_the_ask_modal_in_the_entry_template_is_vue_driven_with_three_answers():
    html = read(TEMPLATES / "index.html")
    partial = read(TEMPLATES / "_mode_prompt.html")                 # Modal 已抽成與詳情頁共用的片段，入口頁以 include 引入
    assert '{% include "game/_mode_prompt.html" %}' in html
    modal = partial[partial.index('<ui-modal v-model="askOpen"'):partial.index("</ui-modal>")]
    for needle in ('@click="cancelAsk"', '@click="keepSafe"', '@click="goToSettings"', "modePrompt.goSettings", "modePrompt.keepSafe", "modePrompt.cancel", "v-for=\"(line, index) in modePrompt.body\""):
        assert needle in modal, needle
    both = html + partial
    assert "data-bs-" not in both.replace('data-bs-theme="dark"', "") and "v-html" not in both


def test_the_session_template_shows_the_live_mode_chip_and_the_note():
    html = read(TEMPLATES / "session.html")
    assert '<span class="ge-chip">{{ modeLabel }}</span>' in html and "{{ tierLabel }}" not in html
    assert '<p v-if="modeNote" class="ge-banner ge-banner--quiet gm-banner" role="status">{{ modeNote }}</p>' in html


# ---- 遊戲內提示（safe-mode-toggle 第 5 組）：樣板、前端與後端的列舉鍵一致 ---------------------------------------------------

def test_the_session_template_shows_the_hint_as_a_closable_plain_text_notice_with_a_link_to_the_account_settings():
    html = read(TEMPLATES / "session.html")
    start = html.index('<p v-if="safeHint"')
    block = html[start:html.index("</p>", start)]
    assert 'role="status"' in block and "{{ safeHint.text }}" in block
    assert ':href="safeHint.link"' in block and "{{ safeHint.linkText }}" in block
    assert '@click="dismissSafeHint"' in block and ':aria-label="safeHint.closeLabel"' in block and "{{ safeHint.closeText }}" in block
    assert "v-html" not in html and "data-bs-" not in html.replace('data-bs-theme="dark"', "")
    assert html.index('<p v-if="safeHint"') < html.index('<p v-if="notice"')          # 與錯誤與資訊提示是兩個獨立的欄位


def test_the_frontend_and_the_backend_agree_on_the_one_hint_notice_key_and_the_frontend_never_reads_server_text_for_it():
    from app.services.rpg_state_phase import NOTICE_SAFE_MODE_HINT, with_safe_mode_hint

    state_js = read(JS_DIR / "game-state.js")
    assert f"var HINT_KIND = '{NOTICE_SAFE_MODE_HINT}';" in state_js and f"{NOTICE_SAFE_MODE_HINT}: true" in state_js
    assert with_safe_mode_hint({"updated": False, "notices": []}) == {"updated": False, "notices": [{"kind": NOTICE_SAFE_MODE_HINT}]}
    pick = state_js[state_js.index("function pickNotices"):state_js.index("function noticeText")]
    assert "item.text" not in pick                                                      # 提示的文字不採用伺服器送來的任何字


def test_the_hint_wording_in_the_labels_has_no_promise_and_no_age_wording():
    source = read(JS_DIR / "game-labels.js")
    text = re.search(r"var SAFE_HINT = \{\s*text: '([^']+)'", source).group(1)
    assert "可以到帳號設定嘗試關閉安全模式" in text and "仍由模型決定" in text and "可能拒絕或調整" in text
    for forbidden in ("保證", "一定", "必定", "絕對", "未滿 18", "一般模式", "成人模式"):
        assert forbidden not in text, forbidden
