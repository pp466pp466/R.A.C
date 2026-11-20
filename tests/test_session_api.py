"""`/api/sessions/*`（rpg-protagonist、protagonist-suggestion、rpg-entry-api 規格）：三支寫入或生成端點的成功與各種錯誤代碼與狀態碼、
閘門、CSRF、不可快取、擁有者隔離（他人的草稿一律 404，且與不存在的逐字相同），以及回應不含內部欄位；檔案後段是 RPG 入口的兩個
唯讀端點（`GET /api/sessions/catalog`、`GET /api/sessions`）：閘門與受限登入狀態、分頁參數、回應形狀、不改動錢包也不呼叫模型。

用測試應用程式、直接寫入的登入狀態與假的 RPG 供應商（不打真實 API）；內建劇本視為已審查，所以開存檔只多一次主角審查。
清單的查詢與欄位細節在 `test_game_entry_service.py`（含 SQL Server），這裡只驗 HTTP 層。
"""
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.llm.types import LLMTransientError
from app.models import LlmCallLog, RpgSession, RuntimeLimit, User, Wallet, WalletEntry
from app.models.script import VERSION_PUBLISHED, VERSION_REMOVED
from app.models.user import STATUS_DELETION_PENDING
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository
from app.services.content_tier import SAFE_RULES_VERSION
from app.services.catalog_query import encode_cursor
from app.services.catalog_query import encode_cursor
from app.services.game_entry_service import CARD_FIELDS, DETAIL_FIELDS, SAVE_FIELDS
from tests.support.auth import START, FakeClock, add_user, sign_in_as
from tests.support.client import raw_client
from tests.support.llm import FakeLLMProvider, Step
from tests.support.protagonist import protagonist as stored_protagonist
from tests.support.script_review import put_in_status
from tests.support.script_data import validated
from tests.test_llm_providers_by_purpose import BOTH, make_app

NPC = {"name": "蘇長風", "personality": "沉穩寡言", "age": 168, "appearance": "鬢髮微白", "occupation": "長老", "hidden_trait": "暗中守護青雲宗"}
LIN = {"name": "林曉", "personality": "活潑好動", "age": 16, "appearance": "瘦小", "occupation": "學徒"}
ANCHOR = {"kind": "character", "ref": "蘇長風", "text": "他的舊識之後"}
CARD = {"name": "林舟", "personality": "沉默愛思考", "age": 19, "appearance": "高瘦，常穿灰衣"}
GHOST = "00000000-0000-4000-8000-000000000000"
SUGGESTION = {
    "character": {**CARD, "occupation": "遊方郎中", "speech_style": None, "hidden_trait": None},
    "anchor": ANCHOR,
}


def verdict(name="pass", categories=()):
    return json.dumps({"verdict": name, "categories": list(categories)})


@pytest.fixture()
def provider():
    return FakeLLMProvider()


@pytest.fixture()
def app(provider):
    return make_app({**BOTH}, rpg_llm_provider=provider)


def db(app) -> Session:
    return Session(app.extensions["db_engine"])


def signed_in(app, n=1, **overrides):
    with db(app) as s:
        user = add_user(s, n, **overrides)
        uid, tv = user.id, user.token_version
    client = app.test_client()
    sign_in_as(client, uid, tv)
    return client


def builtin(app, **changes):
    """內建劇本的上架版本（沒有作者，視為已審查）；回傳版本的對外識別碼。"""
    changes.setdefault("characters", [LIN, NPC])
    result = validated(**changes)
    with db(app) as s:
        version, _ = ScriptRepository(s).publish_builtin(
            f"seed-{changes.get('title', 'x')}", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=FakeClock()(),
        )
        s.commit()
        return version.public_id


def draft(app, owner_n=1, **changes):
    changes.setdefault("characters", [LIN, NPC])
    result = validated(**changes)
    with db(app) as s:
        owner = s.scalar(select(User).where(User.email == f"user{owner_n}@example.com"))
        version = ScriptRepository(s).create_script(
            owner.id, author_name="甲", author_code="aaaaaa", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars,
        )
        s.commit()
        return version.public_id


def saves(app) -> int:
    with db(app) as s:
        return s.scalar(select(func.count()).select_from(RpgSession))


def start_body(version_id, **changes):
    body = {"version_id": version_id, "protagonist": CARD, "anchor": ANCHOR}
    body.update(changes)
    return body


# ---- 閘門 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), [
    ("GET", f"/api/sessions/setup/{GHOST}"), ("POST", "/api/sessions/protagonist/suggest"), ("POST", "/api/sessions"),
])
def test_anonymous_requests_are_401(app, method, path):
    client = app.test_client()
    response = client.open(path, method=method, json={})
    assert response.status_code == 401 and response.get_json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(("method", "path"), [
    ("GET", f"/api/sessions/setup/{GHOST}"), ("POST", "/api/sessions/protagonist/suggest"), ("POST", "/api/sessions"),
])
def test_a_player_who_has_not_confirmed_their_age_is_a_403(app, method, path):
    client = signed_in(app, age=None)
    response = client.open(path, method=method, json={})
    assert response.status_code == 403


def test_a_locked_account_is_refused_on_every_endpoint(app):
    client = signed_in(app, locked_at=FakeClock()(), lock_reason="rate_limited")
    for method, path in (("GET", f"/api/sessions/setup/{GHOST}"), ("POST", "/api/sessions/protagonist/suggest"), ("POST", "/api/sessions")):
        response = client.open(path, method=method, json={})
        assert response.status_code == 403 and response.get_json()["error"]["code"] == "account_locked", path


def test_posts_without_the_csrf_token_are_refused_and_create_nothing(app):
    version = builtin(app)
    with db(app) as s:
        user = add_user(s, 1)
        uid, tv = user.id, user.token_version
    client = raw_client(app)
    sign_in_as(client, uid, tv)
    for path in ("/api/sessions/protagonist/suggest", "/api/sessions"):
        response = client.post(path, json=start_body(version), headers={"Origin": "http://localhost"})
        assert response.status_code == 403, path
    assert saves(app) == 0


@pytest.mark.parametrize("path", ["/api/sessions/protagonist/suggest", "/api/sessions"])
def test_a_non_json_body_is_415_and_a_non_object_is_400(app, path):
    client = signed_in(app)
    assert client.post(path, data="x", content_type="text/plain").status_code == 415
    assert client.post(path, json=[1, 2]).status_code == 400


# ---- GET /setup ------------------------------------------------------------------------------------


def test_setup_returns_the_creation_data_without_internal_fields(app):
    client, version = signed_in(app), builtin(app)
    response = client.get(f"/api/sessions/setup/{version}")
    body = response.get_json()
    assert response.status_code == 200 and body["ok"] is True
    assert body["title"] == "青雲問道" and body["characters"] == [{"name": "林曉", "occupation": "學徒"}, {"name": "蘇長風", "occupation": "長老"}]
    assert body["suggestions_remaining"] == 5 and body["suggestions_available"] is True and body["limits"]["intro"] == 300
    text = response.get_data(as_text=True)
    for forbidden in ("暗中守護青雲宗", "content_hash", "review", "public_id", "seed"):
        assert forbidden not in text


def test_setup_for_another_players_draft_is_the_same_404_as_for_a_missing_version(app):
    client = signed_in(app, 2)
    with db(app) as s:
        add_user(s, 1)
    owner_draft = draft(app, 1)
    mine, ghost = client.get(f"/api/sessions/setup/{owner_draft}"), client.get(f"/api/sessions/setup/{GHOST}")
    assert (mine.status_code, mine.get_data()) == (ghost.status_code, ghost.get_data()) and mine.status_code == 404


@pytest.mark.parametrize("declared", ["adult", "standard", "safe"])
def test_setup_for_a_script_not_bound_to_the_safe_mode_is_a_200_for_a_safe_mode_player(app, declared):
    client = signed_in(app, age=30, content_tier_preference="safe")                 # 安全模式開的玩家：劇本的內容模式不是拒絕的理由
    response = client.get(f"/api/sessions/setup/{builtin(app, title='未綁定', content_tier=declared)}")
    assert response.status_code == 200 and "content_tier_restricted" not in response.get_data(as_text=True)


# ---- POST /protagonist/suggest -----------------------------------------------------------------------


def test_suggest_returns_a_candidate_and_creates_nothing(app, provider):
    client, version = signed_in(app), builtin(app)
    provider.script(json.dumps(SUGGESTION, ensure_ascii=False))
    response = client.post("/api/sessions/protagonist/suggest", json={"version_id": version, "intro": "我是個流浪的郎中"})
    body = response.get_json()
    assert response.status_code == 200 and body["ok"] is True and body["remaining"] == 4
    assert body["protagonist"]["name"] == "林舟" and body["anchor"] == ANCHOR and saves(app) == 0


@pytest.mark.parametrize(("body", "status", "code"), [
    ({"version_id": GHOST, "intro": "x"}, 404, "not_found"),
    ({"intro": "x"}, 404, "not_found"),
    ({"version_id": 5}, 404, "not_found"),
])
def test_suggest_with_an_unknown_version_is_404(app, body, status, code):
    response = signed_in(app).post("/api/sessions/protagonist/suggest", json=body)
    assert response.status_code == status and response.get_json()["error"]["code"] == code


def test_suggest_with_an_intro_over_300_characters_is_a_400_naming_intro(app, provider):
    client, version = signed_in(app), builtin(app)
    response = client.post("/api/sessions/protagonist/suggest", json={"version_id": version, "intro": "字" * 301})
    error = response.get_json()["error"]
    assert response.status_code == 400 and list(error["fields"]) == ["intro"] and provider.calls == []


def test_suggest_rejected_by_the_rules_layer_is_a_422_with_categories_only(app, provider):
    client, version = signed_in(app), builtin(app)
    response = client.post("/api/sessions/protagonist/suggest", json={"version_id": version, "intro": "十歲的少女與他做愛。"})
    body = response.get_json()
    assert response.status_code == 422 and body["error"]["code"] == "protagonist_rejected" and body["categories"] == ["minor_sexual"]
    assert "做愛" not in response.get_data(as_text=True) and provider.calls == []


def test_suggest_when_the_model_fails_is_a_503_that_points_to_manual_creation(app, provider):
    client, version = signed_in(app), builtin(app)
    provider.script(Step(LLMTransientError("timeout")), Step(LLMTransientError("server_error")))
    response = client.post("/api/sessions/protagonist/suggest", json={"version_id": version})
    body = response.get_json()["error"]
    assert response.status_code == 503 and body["code"] == "suggestion_unavailable" and "自己" in body["message"]


def test_suggest_over_the_daily_limit_is_a_429_with_retry_after_and_no_model_call(app, provider):
    client, version = signed_in(app), builtin(app)
    with db(app) as s:
        s.add(RuntimeLimit(key="protagonist_suggest_daily.free", int_value=1, updated_at=FakeClock()()))
        s.commit()
    provider.script(json.dumps(SUGGESTION, ensure_ascii=False))
    assert client.post("/api/sessions/protagonist/suggest", json={"version_id": version}).status_code == 200
    response = client.post("/api/sessions/protagonist/suggest", json={"version_id": version})
    assert response.status_code == 429 and response.get_json()["error"]["code"] == "suggestion_limit"
    assert int(response.headers["Retry-After"]) >= 1 and len(provider.calls) == 1


# ---- POST /api/sessions ------------------------------------------------------------------------------


def test_starting_a_session_returns_201_and_stores_the_protagonist(app, provider):
    client, version = signed_in(app), builtin(app)
    provider.script(verdict())
    response = client.post("/api/sessions", json=start_body(version))
    body = response.get_json()
    assert response.status_code == 201 and body["ok"] is True
    assert body["session"]["title"] == "青雲問道" and body["session"]["protagonist"]["character"]["name"] == "林舟"
    assert body["session"]["protagonist"]["anchor"] == ANCHOR and len(body["session"]["id"]) == 36
    assert saves(app) == 1
    with db(app) as s:
        save = s.scalar(select(RpgSession))
        assert (save.player_name, save.character_age) == ("林舟", 19) and save.protagonist_json["anchor"] == ANCHOR


def test_skipping_the_anchor_opens_a_save_without_one(app, provider):
    client, version = signed_in(app), builtin(app)
    provider.script(verdict())
    response = client.post("/api/sessions", json={"version_id": version, "protagonist": CARD, "skip_anchor": True})
    assert response.status_code == 201 and response.get_json()["session"]["protagonist"]["anchor"] is None


@pytest.mark.parametrize(("changes", "field"), [
    ({"protagonist": {**CARD, "appearance": ""}}, "protagonist.appearance"),
    ({"protagonist": None}, "protagonist"),
    ({"anchor": None}, "anchor"),
    ({"anchor": {**ANCHOR, "ref": "沒有這個人"}}, "anchor.ref"),
    ({"anchor": {**ANCHOR, "text": "他的戀人，兩人發生過性關係"}, "protagonist": {**CARD, "age": 16}}, "anchor.text"),
])
def test_field_errors_are_a_400_with_the_field_paths_and_no_save(app, provider, changes, field):
    client, version = signed_in(app), builtin(app)
    response = client.post("/api/sessions", json=start_body(version, **changes))
    error = response.get_json()["error"]
    assert response.status_code == 400 and field in error["fields"] and saves(app) == 0 and provider.calls == []


def test_a_rules_layer_rejection_is_a_422_with_categories_and_no_text(app, provider):
    client, version = signed_in(app), builtin(app)
    response = client.post("/api/sessions", json=start_body(version, protagonist={**CARD, "age": 15, "personality": "喜歡性愛的少年"}))
    body = response.get_json()
    assert response.status_code == 422 and body["error"]["code"] == "protagonist_rejected" and body["categories"] == ["minor_sexual"]
    assert "性愛" not in response.get_data(as_text=True) and saves(app) == 0 and provider.calls == []


def test_a_model_layer_rejection_is_a_422_and_an_unavailable_review_is_a_503(app, provider):
    client, version = signed_in(app), builtin(app)
    provider.script(verdict("reject", ["minor_sexual"]))
    rejected = client.post("/api/sessions", json=start_body(version))
    assert rejected.status_code == 422 and rejected.get_json()["categories"] == ["minor_sexual"]
    provider.script(Step(LLMTransientError("timeout")), Step(LLMTransientError("server_error")))
    unavailable = client.post("/api/sessions", json=start_body(version))
    assert unavailable.status_code == 503 and unavailable.get_json()["error"]["code"] == "review_unavailable" and saves(app) == 0


def test_a_flagged_protagonist_still_opens(app, provider):
    client, version = signed_in(app), builtin(app)
    provider.script(verdict("flag", ["other_concern"]))
    assert client.post("/api/sessions", json=start_body(version)).status_code == 201


def test_another_players_draft_is_the_same_404_as_a_missing_version_and_costs_nothing(app, provider):
    owner = signed_in(app, 1)
    owner_draft = draft(app, 1)
    intruder = signed_in(app, 2)
    mine = intruder.post("/api/sessions", json=start_body(owner_draft))
    ghost = intruder.post("/api/sessions", json=start_body(GHOST))
    assert (mine.status_code, mine.get_data()) == (ghost.status_code, ghost.get_data()) and mine.status_code == 404
    assert saves(app) == 0 and provider.calls == []


def test_a_script_not_bound_to_the_safe_mode_can_be_started_by_a_safe_mode_player_and_plays_safe(app, provider):
    client = signed_in(app, age=30, content_tier_preference="safe")                 # 安全模式開的玩家
    provider.script(verdict())
    response = client.post("/api/sessions", json=start_body(builtin(app, title="未綁定", content_tier="adult")))
    assert response.status_code == 201 and saves(app) == 1                          # 不回 content_tier_restricted
    assert response.get_json()["session"]["content_tier"] == "safe"                 # 該存檔以安全模式遊玩


def test_the_owner_may_start_a_save_from_their_own_draft_after_the_script_review(app, provider):
    client = signed_in(app, 1)
    own = draft(app, 1)
    provider.script(verdict(), verdict())                           # 劇本審查 + 主角審查
    assert client.post("/api/sessions", json=start_body(own)).status_code == 201 and len(provider.calls) == 2


# ---- 回應標頭與內容 ------------------------------------------------------------------------------------


def test_every_response_success_or_error_is_not_cacheable(app, provider):
    client, version = signed_in(app), builtin(app)
    provider.script(json.dumps(SUGGESTION, ensure_ascii=False), verdict())
    responses = [
        client.get(f"/api/sessions/setup/{version}"), client.get(f"/api/sessions/setup/{GHOST}"),
        client.post("/api/sessions/protagonist/suggest", json={"version_id": version}),
        client.post("/api/sessions/protagonist/suggest", json={"version_id": GHOST}),
        client.post("/api/sessions", json=start_body(version)), client.post("/api/sessions", json=start_body(GHOST)),
        app.test_client().get(f"/api/sessions/setup/{version}"),
    ]
    assert [r.headers.get("Cache-Control") for r in responses] == ["no-store"] * len(responses)


def test_no_response_carries_internal_ids_hashes_or_review_fields(app, provider):
    client, version = signed_in(app), builtin(app)
    provider.script(json.dumps(SUGGESTION, ensure_ascii=False), verdict())
    texts = [
        client.post("/api/sessions/protagonist/suggest", json={"version_id": version}).get_data(as_text=True),
        client.post("/api/sessions", json=start_body(version)).get_data(as_text=True),
    ]
    for text in texts:
        for forbidden in ("content_hash", "review_", "user_id", "source_version_id", "script_snapshot"):
            assert forbidden not in text



# ---- RPG 入口的兩個唯讀端點：GET /api/sessions/catalog 與 GET /api/sessions（rpg-entry-api 規格）--------------------------------

CATALOG = "/api/sessions/catalog"
TAGS = "/api/sessions/catalog/tags"
MINE = "/api/sessions"
ENTRY_PATHS = (CATALOG, TAGS, MINE)
SECRET_HIDDEN_TRAIT = "暗中守護青雲宗"                                 # 劇本內文裡的標記字串：清單不得帶出


def open_save(app, n=1, title="青雲問道", **changes):
    """直接寫入一個存檔（不經開存檔的審查）；回傳存檔的對外識別碼。"""
    with db(app) as s:
        owner = s.scalar(select(User).where(User.email == f"user{n}@example.com"))
        save = SessionRepository(s).create(
            owner.id, source_version_id=None, snapshot=validated(title=title).content, snapshot_spec_version=1,
            protagonist=stored_protagonist("林初", personality="獨特性格標記"), content_tier="safe", now=FakeClock()(), **changes,
        )
        s.commit()
        return save.public_id


def finish_turns(app, n, save_id, count):
    with db(app) as s:
        owner = s.scalar(select(User).where(User.email == f"user{n}@example.com"))
        repo = SessionRepository(s)
        for index in range(1, count + 1):
            repo.start_turn(owner.id, save_id, client_msg_id=f"t-{index}", user_action=f"行動{index}", now=FakeClock()())
            repo.complete_turn(owner.id, save_id, f"t-{index}", llm_raw_response=f"劇情{index}", structured_result=None, now=FakeClock()())
        s.commit()


def ledger_state(app):
    """錢包、帳本與模型呼叫帳本的完整狀態；兩個清單端點呼叫前後必須相同。"""
    with db(app) as s:
        wallets = [(w.user_id, w.free_balance, w.paid_balance, w.version) for w in s.scalars(select(Wallet).order_by(Wallet.id))]
        entries = s.scalar(select(func.count()).select_from(WalletEntry))
        model_calls = s.scalar(select(func.count()).select_from(LlmCallLog))
    return wallets, entries, model_calls


def page_ids(client, path, key, limit, extra=""):
    """依序讀取每一頁直到 `has_more` 為假，回傳各頁串起來的 `id`。目錄用游標（`next_cursor`），我的存檔用位移；`extra` 是附加的查詢字串。"""
    collected, offset, cursor = [], 0, None
    for _ in range(50):                                                    # 有上限：被破壞成永遠 has_more 時要失敗，不要卡住
        position = f"&cursor={cursor}" if cursor else (f"&offset={offset}" if path == MINE else "")
        body = client.get(f"{path}?limit={limit}{position}{extra}").get_json()
        collected += [item["id"] for item in body[key]]
        if not body["has_more"]:
            if path != MINE:
                assert body["next_cursor"] is None                          # 沒有下一頁時游標為空
            return collected
        cursor, offset = body.get("next_cursor"), offset + limit
        assert path == MINE or cursor                                      # 有下一頁就必須有游標
    raise AssertionError("has_more 一直為真：分頁沒有走到盡頭")


@pytest.mark.parametrize("path", ENTRY_PATHS)
def test_the_entry_lists_are_401_without_a_login(app, path):
    response = app.test_client().get(path)
    assert response.status_code == 401 and response.get_json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(("overrides", "code"), [
    ({"locked_at": START, "lock_reason": "rate_limited"}, "account_locked"),
    ({"age": None}, "age_confirmation_required"),
    ({"status": STATUS_DELETION_PENDING, "deletion_requested_at": START, "status_before_deletion": "active"}, "account_deletion_pending"),
], ids=["locked", "age_pending", "deletion_pending"])
@pytest.mark.parametrize("path", ENTRY_PATHS)
def test_a_restricted_login_gets_its_own_403_code_on_both_lists(app, path, overrides, code):
    response = signed_in(app, **overrides).get(path)
    assert response.status_code == 403 and response.get_json()["error"]["code"] == code
    assert "scripts" not in response.get_json() and "sessions" not in response.get_json()


def test_an_admin_who_has_not_confirmed_the_age_reads_the_whole_list_in_the_safe_mode_but_still_cannot_start_a_save(app, provider):
    safe = builtin(app, title="安全")
    builtin(app, title="未綁定", content_tier="adult")
    client = signed_in(app, role="admin", age=None)
    body = client.get(CATALOG).get_json()
    assert len(body["scripts"]) == 2 and safe in [card["id"] for card in body["scripts"]]
    assert body["content_mode"] == {"effective": "safe", "forced": False}
    assert client.get(MINE).get_json()["sessions"] == []
    start = client.post(MINE, json=start_body(safe))
    assert start.status_code == 403 and start.get_json()["error"]["code"] == "age_confirmation_required"
    assert saves(app) == 0 and provider.calls == []


def test_the_catalog_summary_has_no_age_data_and_no_is_adult(app):
    builtin(app, title="安全")
    response = signed_in(app, age=30).get(CATALOG)
    body = response.get_json()
    assert body["content_mode"] == {"effective": "safe", "forced": False}
    text = response.get_data(as_text=True)
    for forbidden in ("birth", "age_range", "age_policy", "\"age\"", "is_adult"):
        assert forbidden not in text


def test_a_player_with_the_default_settings_reads_every_published_script_in_the_safe_mode(app):
    ids = [builtin(app, title=title, content_tier=tier) for title, tier in (("安全", "safe"), ("舊值", "standard"), ("未綁定", "adult"))]
    body = signed_in(app).get(CATALOG).get_json()
    assert sorted(card["id"] for card in body["scripts"]) == sorted(ids)
    assert body["content_mode"] == {"effective": "safe", "forced": False}
    assert {card["title"]: card["content_tier"] for card in body["scripts"]} == {"安全": "safe", "舊值": "adult", "未綁定": "adult"}


def test_a_player_who_turned_the_safe_mode_off_sees_the_same_list_and_the_adult_mode(app):
    ids = [builtin(app, title=title, content_tier=tier) for title, tier in (("安全", "safe"), ("未綁定", "adult"))]
    agreed = dict(content_tier_preference="adult", adult_confirmed_at=START, adult_rules_version=SAFE_RULES_VERSION)
    body = signed_in(app, **agreed).get(CATALOG).get_json()
    assert sorted(card["id"] for card in body["scripts"]) == sorted(ids)
    assert body["content_mode"] == {"effective": "adult", "forced": False}


def test_only_published_versions_are_listed_never_the_owners_own_draft(app):
    owner = signed_in(app, 1)
    published = builtin(app, title="公開")
    draft(app, 1, title="我的草稿")
    assert [card["id"] for card in owner.get(CATALOG).get_json()["scripts"]] == [published]


@pytest.mark.parametrize(("path", "query", "fields"), [
    (CATALOG, "limit=0", ["limit"]), (CATALOG, "limit=25", ["limit"]), (CATALOG, "limit=-3", ["limit"]), (CATALOG, "limit=abc", ["limit"]),
    (CATALOG, "limit=1.5", ["limit"]), (CATALOG, "cursor=hello", ["cursor"]), (CATALOG, "audience=other", ["audience"]),
    (CATALOG, "audience=ALL", ["audience"]), (CATALOG, "q=一+二+三+四+五+六", ["q"]), (CATALOG, "q=" + "字" * 51, ["q"]),
    (CATALOG, "tag=" + "字" * 13, ["tag"]), (CATALOG, "limit=0&cursor=%40%40&audience=x", ["limit", "cursor", "audience"]),
    (MINE, "limit=0", ["limit"]), (MINE, "limit=51", ["limit"]), (MINE, "limit=abc", ["limit"]), (MINE, "offset=-1", ["offset"]),
    (MINE, "offset=abc", ["offset"]), (MINE, "limit=0&offset=-1", ["limit", "offset"]),
])
def test_an_invalid_page_is_a_400_that_names_the_field_and_returns_no_list(app, path, query, fields):
    client = signed_in(app)
    builtin(app)
    open_save(app)
    response = client.get(f"{path}?{query}")
    body = response.get_json()
    assert response.status_code == 400 and sorted(body["error"]["fields"]) == sorted(fields)
    assert "scripts" not in body and "sessions" not in body


@pytest.mark.parametrize(("path", "query"), [
    (CATALOG, ""), (CATALOG, "limit="), (CATALOG, "limit=1"), (CATALOG, "limit=24"), (CATALOG, "q="), (CATALOG, "tag="), (CATALOG, "audience="),
    (CATALOG, "cursor="), (CATALOG, "offset=0"), (CATALOG, "offset=10000"), (CATALOG, "offset=-1"), (CATALOG, "offset=abc"),   # 目錄不再讀 offset
    (MINE, ""), (MINE, "offset="), (MINE, "limit=1"), (MINE, "limit=50"), (MINE, "offset=10000"),
])
def test_the_boundary_and_omitted_values_are_accepted(app, path, query):
    client = signed_in(app)
    builtin(app)
    open_save(app)
    assert client.get(f"{path}?{query}").status_code == 200


def test_catalog_pages_read_over_http_chain_into_one_full_read_without_repeats(app):
    client = signed_in(app)
    for title in ("甲", "乙", "丙", "丁", "戊"):
        builtin(app, title=title)
    full = client.get(f"{CATALOG}?limit=24").get_json()
    assert len(full["scripts"]) == 5 and full["has_more"] is False
    chained = page_ids(client, CATALOG, "scripts", 2)
    assert chained == [card["id"] for card in full["scripts"]] and len(set(chained)) == 5
    assert client.get(f"{CATALOG}?limit=5").get_json()["has_more"] is False
    assert client.get(f"{CATALOG}?limit=4").get_json()["has_more"] is True


def test_the_story_preference_reorders_the_catalog_over_http_without_changing_the_set(app):
    client = signed_in(app)
    for title, channel in (("男", "male"), ("全", "all"), ("女", "female")):
        builtin(app, title=title, audience=channel)
    order = {}
    for preference in ("male", "female", "all"):
        assert client.put("/api/account/story-preference", json={"story_preference": preference}).status_code == 200
        order[preference] = [card["title"] for card in client.get(CATALOG).get_json()["scripts"]]
    assert order["male"] == ["男", "全", "女"] and order["female"] == ["女", "全", "男"]
    assert sorted(order["male"]) == sorted(order["female"]) == sorted(order["all"])


def test_the_catalog_response_has_exactly_the_documented_keys_and_no_script_body(app):
    builtin(app, title="內建")
    with db(app) as s:
        add_user(s, 2)
    draft(app, 2, title="別人的草稿")
    client = signed_in(app, 1)
    response = client.get(CATALOG)
    body = response.get_json()
    assert set(body) == {"ok", "scripts", "has_more", "next_cursor", "content_mode"} and body["ok"] is True and body["next_cursor"] is None
    assert set(body["content_mode"]) == {"effective", "forced"}
    card = body["scripts"][0]
    assert set(card) == set(CARD_FIELDS) and card["author_name"] == "官方" and card["audience"] == "all"
    assert isinstance(card["id"], str) and len(card["id"]) == 36 and isinstance(card["tags"], list)
    text = response.get_data(as_text=True)
    for forbidden in (SECRET_HIDDEN_TRAIT, "content_hash", "review_", "user_id", "author_id", "source_version", "milestones", "別人的草稿"):
        assert forbidden not in text


def test_the_setup_endpoint_accepts_the_id_a_card_carries(app):
    client = signed_in(app)
    builtin(app)
    card = client.get(CATALOG).get_json()["scripts"][0]
    setup_response = client.get(f"/api/sessions/setup/{card['id']}")
    assert setup_response.status_code == 200 and setup_response.get_json()["title"] == card["title"]


def test_my_sessions_holds_only_the_callers_saves_newest_first_with_complete_turn_counts(app):
    mine, other = signed_in(app, 1), signed_in(app, 2)
    first, second = open_save(app, 1, title="第一個"), open_save(app, 1, title="第二個")
    open_save(app, 2, title="別人的")
    finish_turns(app, 1, first, 2)
    body = mine.get(MINE).get_json()
    assert set(body) == {"ok", "sessions", "has_more"} and body["ok"] is True and body["has_more"] is False
    assert [item["title"] for item in body["sessions"]] == ["第二個", "第一個"]
    assert {item["title"]: item["completed_turns"] for item in body["sessions"]} == {"第一個": 2, "第二個": 0}
    assert [item["title"] for item in other.get(MINE).get_json()["sessions"]] == ["別人的"]


def test_a_save_item_has_exactly_the_documented_keys_and_none_of_the_save_contents(app):
    client = signed_in(app)
    save_id = open_save(app, 1)
    response = client.get(MINE)
    item = response.get_json()["sessions"][0]
    assert set(item) == set(SAVE_FIELDS) and item["id"] == save_id and item["status"] == "active" and item["player_name"] == "林初"
    assert item["content_tier"] == "safe" and item["completed_turns"] == 0 and item["last_active_at"].endswith("Z")
    text = response.get_data(as_text=True)
    for forbidden in ("獨特性格標記", SECRET_HIDDEN_TRAIT, "script_snapshot", "notebook", "world_state", "protagonist", "user_id", "opening"):
        assert forbidden not in text


def test_an_empty_save_list_is_a_200_with_no_more(app):
    body = signed_in(app).get(MINE).get_json()
    assert body["sessions"] == [] and body["has_more"] is False


def test_save_pages_chain_into_the_full_list_over_http(app):
    client = signed_in(app)
    expected = [open_save(app, 1, title=f"存檔{index}") for index in range(5)]
    chained = page_ids(client, MINE, "sessions", 2)
    assert sorted(chained) == sorted(expected) and len(set(chained)) == 5
    assert client.get(f"{MINE}?limit=5").get_json()["has_more"] is False


@pytest.mark.parametrize("path", ENTRY_PATHS)
def test_neither_list_touches_the_wallet_or_calls_a_model_for_a_player_who_never_opened_the_wallet(app, provider, path):
    client = signed_in(app)
    builtin(app)
    open_save(app)
    assert ledger_state(app) == ([], 0, 0)
    assert client.get(path).status_code == 200
    assert ledger_state(app) == ([], 0, 0) and provider.calls == []


def test_neither_list_changes_an_existing_wallet_or_its_ledger(app, provider):
    client = signed_in(app)
    builtin(app)
    open_save(app)
    assert client.get("/api/wallet").status_code == 200                    # 先讓註冊贈幣發放、帳本有內容
    before = ledger_state(app)
    assert before[0] and before[1] >= 1
    for _ in range(3):
        assert client.get(CATALOG).status_code == 200 and client.get(MINE).status_code == 200
    assert ledger_state(app) == before and provider.calls == []


def test_every_response_of_both_lists_is_not_cacheable_including_errors(app):
    client = signed_in(app)
    builtin(app)
    responses = [
        client.get(CATALOG), client.get(MINE), client.get(f"{CATALOG}?limit=0"), client.get(f"{MINE}?limit=abc"),
        app.test_client().get(CATALOG), app.test_client().get(MINE),
        signed_in(app, 2, age=None).get(CATALOG), signed_in(app, 3, locked_at=START, lock_reason="rate_limited").get(MINE),
    ]
    assert [r.status_code for r in responses] == [200, 200, 400, 400, 401, 401, 403, 403]
    assert [r.headers.get("Cache-Control") for r in responses] == ["no-store"] * len(responses)


def test_the_lists_live_next_to_the_other_session_endpoints_and_the_whole_flow_hangs_together(app, provider):
    client = signed_in(app)
    builtin(app)
    card = client.get(CATALOG).get_json()["scripts"][0]
    assert client.get(f"/api/sessions/setup/{card['id']}").status_code == 200
    provider.script(verdict())
    created = client.post(MINE, json=start_body(card["id"]))
    assert created.status_code == 201
    listed = client.get(MINE).get_json()["sessions"]
    assert [(item["id"], item["completed_turns"], item["status"], item["title"]) for item in listed] == [
        (created.get_json()["session"]["id"], 0, "active", card["title"]),
    ]
    for method in ("POST", "PUT", "DELETE"):
        assert client.open(CATALOG, method=method, json={}).status_code == 405, method
    assert client.put(MINE, json={}).status_code == 405 and client.delete(MINE).status_code == 405


def test_one_player_never_sees_another_players_save_in_their_list(app):
    owner, intruder = signed_in(app, 1), signed_in(app, 2)
    save_id = open_save(app, 1)
    assert [item["id"] for item in owner.get(MINE).get_json()["sessions"]] == [save_id]
    assert intruder.get(MINE).get_json()["sessions"] == []
    assert save_id not in intruder.get(MINE).get_data(as_text=True)


# ---- 公開目錄：搜尋、篩選、游標、常用標籤與詳情（rpg-script-catalog 規格；HTTP 層）--------------------------------------------------
# 查詢與游標的細節在 `test_catalog_query`（純函式）與 `test_script_repository_catalog`（含 SQL Server），這裡只驗 HTTP 層。

DETAIL = "/api/sessions/catalog/"


def player_script(app, owner_n=1, status=VERSION_PUBLISHED, **changes):
    """玩家（不是內建）的劇本放進指定狀態；回傳版本的對外識別碼。擁有者須已存在（`signed_in(app, owner_n)`）。"""
    changes.setdefault("characters", [LIN, NPC])
    with db(app) as s:
        owner = s.scalar(select(User).where(User.email == f"user{owner_n}@example.com"))
        version = put_in_status(ScriptRepository(s), owner, status, **changes)
        s.commit()
        return version.public_id


def listed(client, query=""):
    body = client.get(f"{CATALOG}?{query}").get_json()
    return [card["title"] for card in body["scripts"]]


def test_the_catalog_searches_by_keyword_tag_and_channel_over_http(app):
    client = signed_in(app)
    builtin(app, title="青雲劍訣", world_name="某世界", tags=["仙俠"], audience="male")
    builtin(app, title="都市日常", world_name="某世界", tags=["都市", "SciFi"], audience="female")
    builtin(app, title="江湖行", world_name="某世界", tags=["仙俠"])
    assert listed(client, "q=青雲") == ["青雲劍訣"]
    assert listed(client, "q=scifi") == ["都市日常"]                                        # 標籤不分大小寫
    assert listed(client, "tag=仙俠") == ["江湖行", "青雲劍訣"]
    assert listed(client, "tag=SCIFI") == ["都市日常"]
    assert listed(client, "audience=female") == ["都市日常"]
    assert listed(client, "audience=all") == ["江湖行"]
    assert listed(client, "q=青雲&tag=仙俠&audience=male") == ["青雲劍訣"]
    assert listed(client, "q=青雲&audience=female") == []
    assert listed(client, "q=%E9%9D%92%E9%9B%B2+%E5%8A%8D") == ["青雲劍訣"]               # 空白分詞：兩個詞都要符合
    assert listed(client, "q=&tag=&audience=") == ["江湖行", "都市日常", "青雲劍訣"]          # 空字串視為沒有條件


@pytest.mark.parametrize("query", ["q=%25", "q=_", "q=%5Bab%5D", "q=%25%25"])
def test_a_wildcard_keyword_finds_nothing_over_http_instead_of_everything(app, query):
    client = signed_in(app)
    builtin(app, title="普通劇本")
    builtin(app, title="另一個劇本")
    assert listed(client, query) == []


def test_the_keyword_never_matches_the_body_of_a_script_over_http(app):
    client = signed_in(app)
    builtin(app, title="無關標題", world_name="某世界", tags=["甲"], background="只出現在背景裡的字串")
    assert listed(client, "q=只出現在背景") == [] and listed(client, f"q={SECRET_HIDDEN_TRAIT}") == []
    assert listed(client, "q=無關") == ["無關標題"]


def test_the_story_preference_still_orders_a_filtered_catalog_and_the_filters_only_narrow_it(app):
    client = signed_in(app)
    for title, channel in (("青雲男", "male"), ("青雲全", "all"), ("青雲女", "female"), ("江湖男", "male")):
        builtin(app, title=title, audience=channel, world_name="某世界")
    assert client.put("/api/account/story-preference", json={"story_preference": "female"}).status_code == 200
    assert listed(client, "q=青雲") == ["青雲女", "青雲全", "青雲男"]
    assert listed(client, "q=青雲&limit=24") == ["青雲女", "青雲全", "青雲男"]


def test_cursor_pages_chain_with_a_keyword_filter_over_http(app):
    client = signed_in(app)
    for index in range(7):
        builtin(app, title=f"青雲{index}" if index % 2 == 0 else f"江湖{index}", world_name="某世界")
    full = client.get(f"{CATALOG}?limit=24&q=青雲").get_json()
    chained = page_ids(client, CATALOG, "scripts", 1, extra="&q=青雲")
    assert len(full["scripts"]) == 4 and chained == [card["id"] for card in full["scripts"]] and len(set(chained)) == 4


@pytest.mark.parametrize("preference", ["male", "female", "all"])
def test_cursor_pages_keep_the_preference_tiers_over_http(app, preference):
    client = signed_in(app)
    for title, channel in (("男一", "male"), ("全一", "all"), ("女一", "female"), ("男二", "male"), ("全二", "all"), ("女二", "female")):
        builtin(app, title=title, audience=channel)
    assert client.put("/api/account/story-preference", json={"story_preference": preference}).status_code == 200
    full = client.get(f"{CATALOG}?limit=24").get_json()["scripts"]
    assert page_ids(client, CATALOG, "scripts", 2) == [card["id"] for card in full] and len(full) == 6
    assert page_ids(client, CATALOG, "scripts", 1) == [card["id"] for card in full]


def test_a_cursor_is_an_opaque_url_safe_token_and_the_last_page_has_none(app):
    client = signed_in(app)
    for title in ("甲", "乙", "丙"):
        builtin(app, title=title)
    first = client.get(f"{CATALOG}?limit=2").get_json()
    assert first["has_more"] is True and isinstance(first["next_cursor"], str)
    assert first["next_cursor"].replace("-", "").replace("_", "").isalnum()
    last = client.get(f"{CATALOG}?limit=2&cursor={first['next_cursor']}").get_json()
    assert [card["title"] for card in last["scripts"]] == ["甲"] and last["has_more"] is False and last["next_cursor"] is None


def test_the_old_offset_parameter_is_ignored_by_the_catalog(app):
    client = signed_in(app)
    for title in ("甲", "乙", "丙", "丁", "戊"):
        builtin(app, title=title)
    plain = client.get(f"{CATALOG}?limit=2").get_json()
    for offset in ("3", "0", "-1", "abc", "99999"):
        assert client.get(f"{CATALOG}?limit=2&offset={offset}").get_json() == plain, offset


@pytest.mark.parametrize("cursor", [
    "hello", "%00", "a" * 300, "AAAA", "W10", "e30", "WzEsIjIwMjYtMTEtMjBUMDg6MzA6MTUuMTIzNDU2WiIsNV0%3D", "%E9%9D%92%E9%9B%B2", "..%2F..%2Fetc",
    "1%3B+DROP+TABLE+scripts",
])
def test_a_damaged_cursor_is_a_400_naming_cursor_and_never_a_500(app, cursor):
    client = signed_in(app)
    builtin(app)
    response = client.get(f"{CATALOG}?cursor={cursor}")
    assert response.status_code == 400 and list(response.get_json()["error"]["fields"]) == ["cursor"]
    assert "scripts" not in response.get_json()


def test_a_valid_but_foreign_cursor_only_moves_the_starting_point(app):
    client = signed_in(app)
    for title in ("甲", "乙", "丙"):
        builtin(app, title=title)
    far_future = encode_cursor(0, datetime(2999, 1, 1, tzinfo=timezone.utc), 2_000_000)
    far_past = encode_cursor(0, datetime(2001, 1, 1, tzinfo=timezone.utc), 1)
    assert [c["title"] for c in client.get(f"{CATALOG}?cursor={far_future}").get_json()["scripts"]] == ["丙", "乙", "甲"]
    assert client.get(f"{CATALOG}?cursor={far_past}").get_json()["scripts"] == []


def test_the_tags_endpoint_counts_only_published_versions_and_merges_case(app):
    client = signed_in(app)
    signed_in(app, 2)
    builtin(app, title="一", tags=["仙俠", "SciFi"])
    builtin(app, title="二", tags=["仙俠", "scifi"])
    player_script(app, 2, title="三", tags=["仙俠"])
    player_script(app, 2, VERSION_REMOVED, title="下架", tags=["仙俠", "已下架"])
    draft(app, 2, title="草稿", tags=["仙俠", "草稿標籤"])
    response = client.get(TAGS)
    body = response.get_json()
    assert response.status_code == 200 and set(body) == {"ok", "tags"} and body["ok"] is True
    assert body["tags"] == [{"tag": "仙俠", "count": 3}, {"tag": "scifi", "count": 2}]
    assert all(set(item) == {"tag", "count"} for item in body["tags"])


def test_the_tags_endpoint_is_empty_when_nothing_is_published(app):
    assert signed_in(app).get(TAGS).get_json() == {"ok": True, "tags": []}


def test_the_detail_of_a_published_script_has_exactly_the_public_keys(app):
    client = signed_in(app)
    version = builtin(app, title="青雲問道", tags=["仙俠"], ending_mode="custom", ending_text="結局說明的獨特字串", ending_age=80, rules="規則的獨特字串")
    response = client.get(f"{DETAIL}{version}")
    body = response.get_json()
    assert response.status_code == 200 and body["ok"] is True and set(body["script"]) == set(DETAIL_FIELDS)
    detail = body["script"]
    assert detail["id"] == version and detail["title"] == "青雲問道" and detail["tags"] == ["仙俠"] and detail["author_name"] == "官方"
    assert detail["has_custom_ending"] is True and detail["version_no"] == 1 and detail["published_at"].endswith("Z")
    assert detail["background"].startswith("靈氣復甦") and detail["locations"] == ["古劍山莊", "青雲宗山門"]
    assert detail["characters"] == [{"name": "林曉", "occupation": "學徒"}, {"name": "蘇長風", "occupation": "長老"}]
    assert detail["content_mode"] == {"effective": "safe", "forced": False} and detail["audience"] == "all"


def test_the_detail_never_carries_hidden_traits_milestones_endings_rules_the_opening_or_internal_fields(app):
    client = signed_in(app)
    version = builtin(app, ending_mode="custom", ending_text="結局說明的獨特字串", ending_age=80, rules="規則的獨特字串", opening="開場白的獨特字串")
    text = client.get(f"{DETAIL}{version}").get_data(as_text=True)
    for forbidden in (
        SECRET_HIDDEN_TRAIT, "沉穩寡言", "鬢髮微白", "宗門大測", "結局說明的獨特字串", "規則的獨特字串", "開場白的獨特字串",
        "hidden_trait", "personality", "milestones", "opening", "ending_text", "rules", "content_hash", "review_", "user_id", "author_id", "creator",
        "script_id", "live_script_id", "removed_by", "decision_", "submitted_at", "seed_key",
    ):
        assert forbidden not in text, forbidden


def test_a_players_script_shows_the_pen_name_and_the_short_code_but_no_user_id(app):
    client = signed_in(app, 1)
    signed_in(app, 2)
    version = player_script(app, 2, title="玩家劇本", audience="female")
    detail = client.get(f"{DETAIL}{version}").get_json()["script"]
    assert detail["author_code"] == "a1b2c3" and detail["author_name"].startswith("筆名") and detail["audience"] == "female"


def test_every_script_that_is_not_published_is_the_same_404_as_one_that_does_not_exist_even_for_its_author(app):
    author = signed_in(app, 1)
    other = signed_in(app, 2)
    own_draft = draft(app, 1, title="我的草稿")
    removed = player_script(app, 1, VERSION_REMOVED, title="我下架的")
    ids = [own_draft, removed, GHOST, "not-a-uuid", "0" * 36]
    bodies = []
    for client in (author, other):
        for version in ids:
            response = client.get(f"{DETAIL}{version}")
            assert response.status_code == 404, version
            bodies.append(response.get_data())
    assert len(set(bodies)) == 1                                                     # 逐字相同：看不出是下架、草稿還是不存在


def test_a_restored_script_has_a_detail_again_and_a_taken_down_one_has_none(app):
    client = signed_in(app, 1)
    version = player_script(app, 1, title="可恢復")
    with db(app) as s:
        repo = ScriptRepository(s)
        owner = s.scalar(select(User).where(User.email == "user1@example.com"))
        repo.remove_own(owner.id, version, FakeClock()())
        s.commit()
        assert client.get(f"{DETAIL}{version}").status_code == 404
        repo.restore_own(owner.id, version)
        s.commit()
    assert client.get(f"{DETAIL}{version}").status_code == 200


def test_an_admin_who_has_not_confirmed_the_age_reads_the_detail_and_the_tags_but_nothing_else_changes(app, provider):
    version = builtin(app, tags=["仙俠"])
    client = signed_in(app, role="admin", age=None)
    assert client.get(f"{DETAIL}{version}").status_code == 200 and client.get(TAGS).get_json()["tags"] == [{"tag": "仙俠", "count": 1}]
    assert provider.calls == [] and saves(app) == 0


def test_the_new_catalog_endpoints_never_touch_the_wallet_or_call_a_model(app, provider):
    client = signed_in(app)
    version = builtin(app, tags=["仙俠"])
    assert ledger_state(app) == ([], 0, 0)
    for url in (f"{CATALOG}?q=青雲&tag=仙俠&audience=all&limit=1", TAGS, f"{DETAIL}{version}", f"{DETAIL}{GHOST}"):
        client.get(url)
    assert ledger_state(app) == ([], 0, 0) and provider.calls == [] and saves(app) == 0


def test_the_new_catalog_endpoints_are_not_cacheable_including_errors(app):
    client = signed_in(app)
    version = builtin(app)
    urls = [f"{CATALOG}?q=青雲", f"{CATALOG}?cursor=bad", TAGS, f"{DETAIL}{version}", f"{DETAIL}{GHOST}"]
    responses = [client.get(url) for url in urls] + [app.test_client().get(TAGS), app.test_client().get(f"{DETAIL}{version}")]
    assert [r.status_code for r in responses] == [200, 400, 200, 200, 404, 401, 401]
    assert [r.headers.get("Cache-Control") for r in responses] == ["no-store"] * len(responses)


@pytest.mark.parametrize("path", [TAGS, f"{DETAIL}{GHOST}"])
def test_the_new_catalog_endpoints_are_read_only(app, path):
    client = signed_in(app)
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert client.open(path, method=method, json={}).status_code == 405, method


def test_the_tags_route_is_not_swallowed_by_the_detail_route(app):
    """`/catalog/tags` 是固定路徑：不會被 `/catalog/<version_id>` 當成識別碼而得到 404。"""
    assert signed_in(app).get(TAGS).status_code == 200
