"""劇本審核相關測試的共用輔助（rpg-script-publishing 第 2 組之後）：作者、版本與「把版本放進某個狀態」。

只用 `ScriptRepository` 的正式轉移方法建立資料（與正式流程相同的路徑），不直接改資料列，所以測試出來的狀態都是真的走得到的狀態。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import ScriptVersion, User
from app.models.base import utcnow
from app.models.script import (
    REMOVER_ADMIN, REMOVER_AUTHOR, VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_REMOVED,
    VERSION_SUPERSEDED,
)
from app.repositories.runtime_limit_repository import RuntimeLimitRepository
from app.repositories.script_repository import ScriptRepository
from app.repositories.script_submission_repository import ScriptSubmissionRepository
from app.repositories.user_repository import UserRepository
from app.services.access_policy import AccessPolicy
from app.services.runtime_limits import RuntimeLimits
from app.services.script_authoring_service import ScriptAuthoringService
from tests.support.auth import FakeClock
from tests.support.script_data import validated

BASE = utcnow().replace(microsecond=0)


def authoring_service(session: Session, clock: FakeClock | None = None) -> ScriptAuthoringService:
    """作者端的創作 Service（真的 Repository 與真的 AccessPolicy，只有時鐘是假的），與 `registry.get_script_authoring_service` 同一組裝。"""
    clock = clock or FakeClock()
    return ScriptAuthoringService(
        scripts=ScriptRepository(session), users=UserRepository(session), submissions=ScriptSubmissionRepository(session),
        policy=AccessPolicy(clock=clock), limits=RuntimeLimits(limits=RuntimeLimitRepository(session), clock=clock), clock=clock,
    )


def at(minutes: int = 0) -> datetime:
    """一個固定基準時間之後的時間點（讓排序測試的先後明確）。"""
    return BASE + timedelta(minutes=minutes)


def make_author(session: Session, n: int = 1, **overrides) -> User:
    user = User(
        email=f"author{n}@example.com", email_canonical=f"author{n}@example.com", display_name=f"作者{n}", status="active", **overrides,
    )
    session.add(user)
    session.flush()
    return user


def new_script(repo: ScriptRepository, author: User, **changes) -> ScriptVersion:
    """作者建立一個新劇本（第 1 版草稿）。"""
    result = validated(**changes)
    return repo.create_script(
        author.id, author_name=f"筆名{author.id}", author_code="a1b2c3", content=result.content, content_hash=result.content_hash,
        total_chars=result.total_chars,
    )


def new_draft(repo: ScriptRepository, author: User, script_public_id: str, **changes) -> ScriptVersion:
    result = validated(**changes)
    version = repo.create_draft_version(
        author.id, script_public_id, content=result.content, content_hash=result.content_hash, total_chars=result.total_chars,
    )
    assert version is not None
    return version


def script_public_id(repo: ScriptRepository, version: ScriptVersion) -> str:
    return repo.get_script_of(version).public_id


def put_in_status(
    repo: ScriptRepository, author: User, status: str, *, removed_by: str = REMOVER_AUTHOR, now: datetime | None = None, **changes,
) -> ScriptVersion:
    """把一個新劇本的第 1 版放進指定的狀態（`removed` 可指定下架者；`superseded` 會再上架第 2 版）。"""
    now = now or at()
    version = new_script(repo, author, **changes)
    if status == VERSION_DRAFT:
        return version
    repo.submit_for_review(author.id, version.public_id, now=now)
    if status == VERSION_PENDING:
        return version
    if status == VERSION_REJECTED:
        repo.reject_version(version.public_id, "other_policy", "管理員的備註", now)
        return version
    repo.approve_version(version.public_id, now=now)
    if status == VERSION_PUBLISHED:
        return version
    if status == VERSION_REMOVED:
        if removed_by == REMOVER_ADMIN:
            repo.remove_by_admin(version.public_id, "other_policy", "下架的備註", now)
        else:
            repo.remove_own(author.id, version.public_id, now)
        return version
    assert status == VERSION_SUPERSEDED
    successor = new_draft(repo, author, script_public_id(repo, version))
    repo.submit_for_review(author.id, successor.public_id, now=now)
    repo.approve_version(successor.public_id, now=now + timedelta(minutes=1))
    return version
