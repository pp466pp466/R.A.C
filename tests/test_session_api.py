"""`/api/sessions/*`（rpg-protagonist、protagonist-suggestion 規格）：三支端點的成功與各種錯誤代碼與狀態碼、閘門、CSRF、不可快取、
擁有者隔離（他人的草稿一律 404，且與不存在的逐字相同），以及回應不含內部欄位。

用測試應用程式、直接寫入的登入狀態與假的 RPG 供應商（不打真實 API）；內建劇本視為已審查，所以開存檔只多一次主角審查。
"""
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.llm.types import LLMTransientError
from app.models import RpgSession, RuntimeLimit, User
from app.repositories.script_repository import ScriptRepository
from tests.support.auth import FakeClock, add_user, sign_in_as
from tests.support.client import raw_client
from tests.support.llm import FakeLLMProvider, Step
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
    ({"anchor": {**ANCHOR, "text": "他的未婚妻"}, "protagonist": {**CARD, "age": 16}}, "anchor.text"),
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
