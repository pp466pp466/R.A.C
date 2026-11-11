"""`/api/sessions/*`（rpg-protagonist、protagonist-suggestion、rpg-entry-api 規格）：三支寫入或生成端點的成功與各種錯誤代碼與狀態碼、
閘門、CSRF、不可快取、擁有者隔離（他人的草稿一律 404，且與不存在的逐字相同），以及回應不含內部欄位；檔案後段是 RPG 入口的兩個
唯讀端點（`GET /api/sessions/catalog`、`GET /api/sessions`）：閘門與受限登入狀態、分頁參數、回應形狀、不改動錢包也不呼叫模型。

用測試應用程式、直接寫入的登入狀態與假的 RPG 供應商（不打真實 API）；內建劇本視為已審查，所以開存檔只多一次主角審查。
清單的查詢與欄位細節在 `test_game_entry_service.py`（含 SQL Server），這裡只驗 HTTP 層。
"""
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.llm.types import LLMTransientError
from app.models import LlmCallLog, RpgSession, RuntimeLimit, User, Wallet, WalletEntry
from app.models.user import STATUS_DELETION_PENDING
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository
from app.services.game_entry_service import CARD_FIELDS, SAVE_FIELDS
from tests.support.auth import START, FakeClock, add_user, sign_in_as
from tests.support.client import raw_client
from tests.support.llm import FakeLLMProvider, Step
from tests.support.protagonist import protagonist as stored_protagonist
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


def test_setup_for_a_script_above_the_players_mode_is_a_403(app):
    client = signed_in(app, age=15)
    response = client.get(f"/api/sessions/setup/{builtin(app, title='一般', content_tier='standard')}")
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "content_tier_restricted"


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


def test_the_script_above_the_players_mode_is_a_403(app, provider):
    client = signed_in(app, age=15)
    response = client.post("/api/sessions", json=start_body(builtin(app, title="一般", content_tier="standard")))
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "content_tier_restricted" and saves(app) == 0


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
MINE = "/api/sessions"
ENTRY_PATHS = (CATALOG, MINE)
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


def page_ids(client, path, key, limit):
    """依序讀取每一頁直到 `has_more` 為假，回傳各頁串起來的 `id`。"""
    collected, offset = [], 0
    for _ in range(50):                                                    # 有上限：被破壞成永遠 has_more 時要失敗，不要卡住
        body = client.get(f"{path}?limit={limit}&offset={offset}").get_json()
        collected += [item["id"] for item in body[key]]
        if not body["has_more"]:
            return collected
        offset += limit
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


def test_an_admin_who_has_not_confirmed_the_age_reads_a_safe_list_but_still_cannot_start_a_save(app, provider):
    safe = builtin(app, title="安全")
    builtin(app, title="一般", content_tier="standard")
    client = signed_in(app, role="admin", age=None)
    body = client.get(CATALOG).get_json()
    assert [card["id"] for card in body["scripts"]] == [safe]
    assert body["content_mode"] == {"effective": "safe", "is_adult": False}
    assert client.get(MINE).get_json()["sessions"] == []
    start = client.post(MINE, json=start_body(safe))
    assert start.status_code == 403 and start.get_json()["error"]["code"] == "age_confirmation_required"
    assert saves(app) == 0 and provider.calls == []


def test_a_minor_is_told_why_the_catalog_is_safe_and_the_response_has_no_age_data(app):
    builtin(app, title="安全")
    builtin(app, title="一般", content_tier="standard")
    response = signed_in(app, age=15).get(CATALOG)
    body = response.get_json()
    assert [card["title"] for card in body["scripts"]] == ["安全"]
    assert body["content_mode"] == {"effective": "safe", "is_adult": False}
    text = response.get_data(as_text=True)
    for forbidden in ("birth", "age_range", "age_policy", "\"age\""):
        assert forbidden not in text


def test_an_adult_with_the_default_settings_reads_the_standard_catalog(app):
    safe, standard = builtin(app, title="安全"), builtin(app, title="一般", content_tier="standard")
    builtin(app, title="成人", content_tier="adult")
    body = signed_in(app).get(CATALOG).get_json()
    assert sorted(card["id"] for card in body["scripts"]) == sorted([safe, standard])
    assert body["content_mode"] == {"effective": "standard", "is_adult": True}


def test_only_published_versions_are_listed_never_the_owners_own_draft(app):
    owner = signed_in(app, 1)
    published = builtin(app, title="公開")
    draft(app, 1, title="我的草稿")
    assert [card["id"] for card in owner.get(CATALOG).get_json()["scripts"]] == [published]


@pytest.mark.parametrize(("path", "query", "fields"), [
    (CATALOG, "limit=0", ["limit"]), (CATALOG, "limit=25", ["limit"]), (CATALOG, "limit=-3", ["limit"]), (CATALOG, "limit=abc", ["limit"]),
    (CATALOG, "limit=1.5", ["limit"]), (CATALOG, "offset=-1", ["offset"]), (CATALOG, "offset=10001", ["offset"]), (CATALOG, "offset=x", ["offset"]),
    (CATALOG, "limit=0&offset=-1", ["limit", "offset"]),
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
    (CATALOG, ""), (CATALOG, "limit="), (CATALOG, "limit=1"), (CATALOG, "limit=24"), (CATALOG, "offset=0"), (CATALOG, "offset=10000"),
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
    assert set(body) == {"ok", "scripts", "has_more", "content_mode"} and body["ok"] is True
    assert set(body["content_mode"]) == {"effective", "is_adult"}
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
