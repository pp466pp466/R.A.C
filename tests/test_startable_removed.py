"""script-takedown 規格「下架的版本不能開新存檔」與 rpg-sessions 規格「誰可以用哪個版本開存檔」的下架情境：
`removed` 的版本（含作者自己、含作者或管理員下架）在三個入口——開存檔、創角資料（setup）、AI 生成預覽（suggest）——
一律與不存在相同的 404、**沒有呼叫任何模型**、沒有開出存檔；恢復之後可開；已開出的存檔不受影響、可繼續遊玩。

服務層的測試在 SQLite 與真實 SQL Server 各跑一次；HTTP 層（三個端點的 404 逐字相同）用測試應用程式（SQLite）。
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, LlmCallLog, RpgSession
from app.models.script import REMOVER_ADMIN, REMOVER_AUTHOR, VERSION_REMOVED
from app.repositories.script_repository import ScriptRepository
from app.services.current_user import CurrentUser
from app.services.errors import NotFound
from tests.support.auth import FakeClock, add_user
from tests.support.idor import logged_in_client
from tests.support.llm import FakeLLMProvider
from tests.support.protagonist import start_kwargs
from tests.support.review import PassingProtagonistReviews, PassingReviews
from tests.support.script_review import at, put_in_status
from tests.test_protagonist_suggest import NPC, make_service as protagonist_service, output
from tests.test_session_service import count, service_for

GHOST = "00000000-0000-4000-8000-000000000000"
REMOVERS = [REMOVER_AUTHOR, REMOVER_ADMIN]


class NoModelCalls(PassingReviews):
    """任何審查呼叫都代表模型被呼叫了：開存檔的 404 必須在審查之前發生。"""

    def ensure_reviewed(self, *args, **kwargs):
        raise AssertionError("下架的版本不該走到劇本審查")


class NoProtagonistReviews(PassingProtagonistReviews):
    def review(self, *args, **kwargs):
        raise AssertionError("下架的版本不該走到主角審查")


def removed_version(session: Session, owner, removed_by: str):
    version = put_in_status(
        ScriptRepository(session), owner, VERSION_REMOVED, removed_by=removed_by,
        characters=[{"name": "林曉", "personality": "活潑好動", "age": 16, "appearance": "瘦小"}, NPC],
    )
    session.commit()
    return version


def users(session: Session):
    owner, other = add_user(session, 1), add_user(session, 2)
    return owner, CurrentUser.from_model(owner), CurrentUser.from_model(other)


# ---- 開存檔 --------------------------------------------------------------------------------------


@pytest.mark.parametrize("removed_by", REMOVERS)
def test_nobody_can_start_a_save_from_a_removed_version_not_even_the_author_and_no_model_is_reached(member_session, removed_by):
    owner, author, other = users(member_session)
    version = removed_version(member_session, owner, removed_by)
    service, _ = service_for(member_session, reviews=NoModelCalls(), protagonist_reviews=NoProtagonistReviews())
    for who in (author, other):
        with pytest.raises(NotFound):
            service.start_session(who, version.public_id, **start_kwargs())
    assert count(member_session, RpgSession) == 0


def test_the_404_for_a_removed_version_is_the_same_as_for_a_missing_one(member_session):
    owner, author, other = users(member_session)
    version = removed_version(member_session, owner, REMOVER_ADMIN)
    service, _ = service_for(member_session)
    errors = []
    for who, target in ((author, version.public_id), (other, version.public_id), (author, GHOST)):
        with pytest.raises(NotFound) as caught:
            service.start_session(who, target, **start_kwargs())
        errors.append((caught.value.code, caught.value.message))
    assert len(set(errors)) == 1


@pytest.mark.parametrize("removed_by", REMOVERS)
def test_after_the_takedown_is_lifted_the_same_version_starts_a_save(member_session, removed_by):
    owner, author, other = users(member_session)
    version = removed_version(member_session, owner, removed_by)
    repo = ScriptRepository(member_session)
    service, _ = service_for(member_session)
    with pytest.raises(NotFound):
        service.start_session(other, version.public_id, **start_kwargs())
    if removed_by == REMOVER_AUTHOR:
        repo.restore_own(owner.id, version.public_id)
    else:
        repo.restore_by_admin(version.public_id)
    member_session.commit()
    save = service.start_session(other, version.public_id, **start_kwargs())
    assert save.user_id == other.id and save.source_version_id == version.id


# ---- 創角資料與 AI 生成預覽 ----------------------------------------------------------------------


@pytest.mark.parametrize("removed_by", REMOVERS)
def test_setup_and_suggest_on_a_removed_version_are_404_for_everyone_and_the_model_is_never_called(member_session, removed_by):
    owner, author, other = users(member_session)
    version = removed_version(member_session, owner, removed_by)
    provider = FakeLLMProvider().script(output())
    service = protagonist_service(member_session, provider)
    for who in (author, other):
        with pytest.raises(NotFound):
            service.setup(who, version.public_id)
        with pytest.raises(NotFound):
            service.suggest(who, version.public_id, "想當郎中")
    member_session.expire_all()
    assert provider.calls == [] and member_session.scalars(select(LlmCallLog)).all() == []


def test_setup_and_suggest_work_again_after_the_author_restores(member_session):
    owner, author, other = users(member_session)
    version = removed_version(member_session, owner, REMOVER_AUTHOR)
    provider = FakeLLMProvider().script(output())
    service = protagonist_service(member_session, provider)
    with pytest.raises(NotFound):
        service.setup(other, version.public_id)
    ScriptRepository(member_session).restore_own(owner.id, version.public_id)
    member_session.commit()
    assert service.setup(other, version.public_id)["title"]
    assert service.suggest(other, version.public_id, "想當郎中")["protagonist"]["name"] == "林舟"
    assert len(provider.calls) == 1


# ---- 已開出的存檔 --------------------------------------------------------------------------------


@pytest.mark.parametrize("removed_by", REMOVERS)
def test_a_save_opened_before_the_takedown_keeps_working(member_session, removed_by):
    owner, author, other = users(member_session)
    repo = ScriptRepository(member_session)
    version = removed_version(member_session, owner, REMOVER_AUTHOR)
    repo.restore_own(owner.id, version.public_id)                       # 先讓它上架，其他玩家才開得了存檔
    member_session.commit()
    service, clock = service_for(member_session)
    save = service.start_session(other, version.public_id, **start_kwargs())
    if removed_by == REMOVER_AUTHOR:
        repo.remove_own(owner.id, version.public_id, at(5))
    else:
        repo.remove_by_admin(version.public_id, "other_policy", "備註", at(5))
    member_session.commit()
    started = service.begin_turn(other, save.public_id, client_msg_id="m-1", user_action="我走進山門")
    assert (started.outcome, started.turn.turn_status, started.turn.seq) == ("created", "streaming", 1)
    member_session.expire_all()
    kept = member_session.get(RpgSession, save.id)
    assert kept.status == "active" and kept.source_version_id == version.id


# ---- HTTP 層 -------------------------------------------------------------------------------------


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


@pytest.mark.parametrize("removed_by", REMOVERS)
def test_the_three_http_entries_answer_a_removed_version_exactly_like_a_missing_one(app, removed_by):
    with Session(app.extensions["db_engine"]) as s:
        owner_row, other_row = add_user(s, 1, nickname="擁有者", nickname_code="OWN1"), add_user(s, 2, nickname="旁人", nickname_code="OTH2")
        owner = SimpleNamespace(id=owner_row.id, token_version=owner_row.token_version)
        other = SimpleNamespace(id=other_row.id, token_version=other_row.token_version)
        version = removed_version(s, owner_row, removed_by).public_id
    for who in (owner, other):
        client = logged_in_client(app, who)
        calls = {
            "setup": lambda target: client.get(f"/api/sessions/setup/{target}"),
            "suggest": lambda target: client.post("/api/sessions/protagonist/suggest", json={"version_id": target, "intro": "想當郎中"}),
            "start": lambda target: client.post("/api/sessions", json={"version_id": target, **start_kwargs()}),
        }
        for name, call in calls.items():
            gone, missing = call(version), call(GHOST)
            assert gone.status_code == missing.status_code == 404, name
            assert gone.get_data() == missing.get_data(), name
    with Session(app.extensions["db_engine"]) as s:
        assert s.scalars(select(RpgSession)).all() == [] and s.scalars(select(LlmCallLog)).all() == []
