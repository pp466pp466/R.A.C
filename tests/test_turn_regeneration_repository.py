"""rpg-turn-revision 規格：`TurnRegenerationRepository`（回饋列的插入、結果轉移、接手時的清理、匯出、串聯刪除、擁有者隔離）。

一般測試以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
import ast
import inspect
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.models import RpgSession, TurnRegeneration, User
from app.models.rpg_session import REGEN_APPLIED, REGEN_BLOCKED, REGEN_CATEGORIES, REGEN_FAILED, REGEN_PENDING
from app.repositories import turn_regeneration_repository
from app.repositories.session_repository import SessionRepository
from app.repositories.turn_regeneration_repository import FINAL_RESULTS, TurnRegenerationRepository
from tests.test_session_repository import NOW, make_user, open_save

MISSING = "00000000-0000-4000-8000-000000000000"
REASON = "劇情和設定不符，想要不同的發展"


@pytest.fixture()
def repo(member_session):
    return TurnRegenerationRepository(member_session)


@pytest.fixture()
def world(member_session):
    alice = make_user(member_session, 1)
    return alice, open_save(SessionRepository(member_session), alice)


def add(repo, user, save, *, turn_seq=3, attempt=2, kind="regenerate", category="setting_mismatch", reason=REASON, is_free=True):
    return repo.add_pending(
        user.id, save.public_id, turn_seq=turn_seq, attempt=attempt, kind=kind, category=category, reason=reason, is_free=is_free,
    )


def rows(member_session, save) -> list[TurnRegeneration]:
    member_session.expire_all()
    return list(member_session.scalars(select(TurnRegeneration).where(TurnRegeneration.session_id == save.id).order_by(TurnRegeneration.id)))


def results(member_session, save) -> list[tuple[int, int, str]]:
    return [(row.turn_seq, row.attempt, row.result) for row in rows(member_session, save)]


# ---- add_pending -------------------------------------------------------------------------------

def test_a_pending_row_keeps_the_players_category_and_reason(repo, member_session, world):
    alice, save = world
    row = add(repo, alice, save)
    member_session.commit()
    stored = rows(member_session, save)
    assert len(stored) == 1 and stored[0].id == row.id
    assert (stored[0].turn_seq, stored[0].attempt, stored[0].kind, stored[0].category, stored[0].reason) == (3, 2, "regenerate", "setting_mismatch", REASON)
    assert (stored[0].is_free, stored[0].result, stored[0].session_id) == (True, REGEN_PENDING, save.id)
    assert stored[0].created_at is not None


def test_an_edit_row_has_no_category_and_no_reason(repo, member_session, world):
    alice, save = world
    add(repo, alice, save, kind="edit", category=None, reason=None, is_free=False)
    stored = rows(member_session, save)[0]
    assert (stored.kind, stored.category, stored.reason, stored.is_free) == ("edit", None, None, False)


def test_unknown_kinds_and_categories_are_refused_before_anything_is_written(repo, member_session, world):
    alice, save = world
    with pytest.raises(ValueError):
        add(repo, alice, save, kind="resend")
    with pytest.raises(ValueError):
        add(repo, alice, save, category="not_a_category")
    assert rows(member_session, save) == []
    for category in REGEN_CATEGORIES:                                          # 五個分類都收
        add(repo, alice, save, attempt=10 + REGEN_CATEGORIES.index(category), category=category)
    assert len(rows(member_session, save)) == 5


def test_a_turn_and_attempt_can_only_have_one_row_and_the_session_stays_usable(repo, member_session, world):
    alice, save = world
    add(repo, alice, save)
    with pytest.raises(IntegrityError):
        add(repo, alice, save, reason="另一個理由")
    add(repo, alice, save, attempt=3)                                           # 失敗的插入沒有弄壞交易
    other = open_save(SessionRepository(member_session), alice)
    add(repo, alice, other)                                                     # 不同存檔可以重複
    assert results(member_session, save) == [(3, 2, "pending"), (3, 3, "pending")]
    assert [row.reason for row in rows(member_session, save)] == [REASON, REASON]      # 第一列沒有被覆蓋


# ---- mark_result -------------------------------------------------------------------------------

@pytest.mark.parametrize("final", FINAL_RESULTS)
def test_only_a_pending_row_can_move_to_a_final_result(repo, member_session, world, final):
    alice, save = world
    add(repo, alice, save)
    assert repo.mark_result(alice.id, save.public_id, turn_seq=3, attempt=2, result=final) is True
    assert results(member_session, save) == [(3, 2, final)]
    for other in FINAL_RESULTS:                                                 # 已經有結果：晚到的收尾不能改寫
        assert repo.mark_result(alice.id, save.public_id, turn_seq=3, attempt=2, result=other) is False
    assert results(member_session, save) == [(3, 2, final)]


def test_the_result_cannot_be_set_back_to_pending_or_to_something_unknown(repo, member_session, world):
    alice, save = world
    add(repo, alice, save)
    for bad in (REGEN_PENDING, "done", ""):
        with pytest.raises(ValueError):
            repo.mark_result(alice.id, save.public_id, turn_seq=3, attempt=2, result=bad)
    assert results(member_session, save) == [(3, 2, "pending")]


def test_marking_targets_one_attempt_of_one_turn(repo, member_session, world):
    alice, save = world
    add(repo, alice, save, turn_seq=3, attempt=2)
    add(repo, alice, save, turn_seq=3, attempt=3)
    add(repo, alice, save, turn_seq=4, attempt=2)
    assert repo.mark_result(alice.id, save.public_id, turn_seq=3, attempt=3, result=REGEN_APPLIED) is True
    assert repo.mark_result(alice.id, save.public_id, turn_seq=9, attempt=1, result=REGEN_APPLIED) is False        # 沒有這一列
    assert results(member_session, save) == [(3, 2, "pending"), (3, 3, "applied"), (4, 2, "pending")]


def test_a_failed_or_blocked_attempt_keeps_the_reason_and_category(repo, member_session, world):
    alice, save = world
    add(repo, alice, save, attempt=2)
    add(repo, alice, save, attempt=3, category="other", reason="想換個方向")
    repo.mark_result(alice.id, save.public_id, turn_seq=3, attempt=2, result=REGEN_FAILED)
    repo.mark_result(alice.id, save.public_id, turn_seq=3, attempt=3, result=REGEN_BLOCKED)
    stored = rows(member_session, save)
    assert [(r.result, r.category, r.reason) for r in stored] == [("failed", "setting_mismatch", REASON), ("blocked", "other", "想換個方向")]


def test_marking_updates_the_update_time(repo, member_session, world):
    alice, save = world
    add(repo, alice, save)
    later = NOW + timedelta(minutes=5)
    repo.mark_result(alice.id, save.public_id, turn_seq=3, attempt=2, result=REGEN_APPLIED, now=later)
    assert rows(member_session, save)[0].updated_at == later


# ---- fail_pending_for_turn ---------------------------------------------------------------------

def test_a_takeover_marks_the_old_pending_rows_of_that_turn_as_failed_and_keeps_their_reasons(repo, member_session, world):
    alice, save = world
    add(repo, alice, save, turn_seq=3, attempt=2)                               # 舊執行者：沒有收尾就消失了
    add(repo, alice, save, turn_seq=2, attempt=2, reason="別的回合的理由")
    assert repo.fail_pending_for_turn(alice.id, save.public_id, turn_seq=3) == 1
    assert results(member_session, save) == [(3, 2, "failed"), (2, 2, "pending")]        # 只動這個回合
    add(repo, alice, save, turn_seq=3, attempt=3, reason="接手後的新理由")                  # 新的認領在清理之後才插入
    assert results(member_session, save) == [(3, 2, "failed"), (2, 2, "pending"), (3, 3, "pending")]
    assert [r.reason for r in rows(member_session, save)][0] == REASON


def test_an_old_executor_finishing_late_cannot_overwrite_the_takeover_cleanup(repo, member_session, world):
    alice, save = world
    add(repo, alice, save, attempt=2)
    repo.fail_pending_for_turn(alice.id, save.public_id, turn_seq=3)
    assert repo.mark_result(alice.id, save.public_id, turn_seq=3, attempt=2, result=REGEN_APPLIED) is False
    assert results(member_session, save) == [(3, 2, "failed")]


def test_cleanup_leaves_final_results_and_other_saves_alone(repo, member_session, world):
    alice, save = world
    other = open_save(SessionRepository(member_session), alice)
    add(repo, alice, save, attempt=2)
    repo.mark_result(alice.id, save.public_id, turn_seq=3, attempt=2, result=REGEN_APPLIED)
    add(repo, alice, other, attempt=2)
    assert repo.fail_pending_for_turn(alice.id, save.public_id, turn_seq=3) == 0
    assert results(member_session, save) == [(3, 2, "applied")] and results(member_session, other) == [(3, 2, "pending")]


# ---- export_for_session ------------------------------------------------------------------------

def test_the_export_lists_every_row_including_failed_and_blocked_without_internal_ids(repo, member_session, world):
    alice, save = world
    add(repo, alice, save, turn_seq=1, attempt=2)
    add(repo, alice, save, turn_seq=1, attempt=3, kind="edit", category=None, reason=None, is_free=False)
    add(repo, alice, save, turn_seq=2, attempt=2, category="other", reason="想換個方向")
    repo.mark_result(alice.id, save.public_id, turn_seq=1, attempt=2, result=REGEN_FAILED)
    repo.mark_result(alice.id, save.public_id, turn_seq=1, attempt=3, result=REGEN_APPLIED)
    repo.mark_result(alice.id, save.public_id, turn_seq=2, attempt=2, result=REGEN_BLOCKED)
    exported = list(repo.export_for_session(alice.id, save.public_id))
    assert [(r["turn_seq"], r["attempt"], r["kind"], r["category"], r["reason"], r["is_free"], r["result"]) for r in exported] == [
        (1, 2, "regenerate", "setting_mismatch", REASON, True, "failed"),
        (1, 3, "edit", None, None, False, "applied"),
        (2, 2, "regenerate", "other", "想換個方向", True, "blocked"),
    ]
    assert all(set(r) == {"turn_seq", "attempt", "kind", "category", "reason", "is_free", "result", "created_at", "updated_at"} for r in exported)
    assert all(r["created_at"] is not None for r in exported)


def test_the_export_pages_through_a_long_list_in_order(repo, member_session, world):
    alice, save = world
    for attempt in range(2, 9):
        add(repo, alice, save, attempt=attempt, reason=f"理由{attempt}")
    assert [r["attempt"] for r in repo.export_for_session(alice.id, save.public_id, page_size=2)] == list(range(2, 9))
    assert [r["reason"] for r in repo.export_for_session(alice.id, save.public_id, page_size=3)][-1] == "理由8"


def test_the_export_only_covers_the_one_save(repo, member_session, world):
    alice, save = world
    other = open_save(SessionRepository(member_session), alice)
    add(repo, alice, save, reason="這一個存檔")
    add(repo, alice, other, reason="另一個存檔")
    assert [r["reason"] for r in repo.export_for_session(alice.id, save.public_id)] == ["這一個存檔"]


# ---- 擁有者隔離 --------------------------------------------------------------------------------

def test_someone_elses_or_unknown_saves_get_none_and_nothing_changes(repo, member_session, world):
    alice, save = world
    add(repo, alice, save)
    mallory = make_user(member_session, 2)
    assert add(repo, mallory, save) is None
    assert repo.mark_result(mallory.id, save.public_id, turn_seq=3, attempt=2, result=REGEN_APPLIED) is None
    assert repo.fail_pending_for_turn(mallory.id, save.public_id, turn_seq=3) is None
    assert list(repo.export_for_session(mallory.id, save.public_id)) == []
    assert add(repo, alice, SimpleNamespace(public_id=MISSING)) is None
    assert list(repo.export_for_session(alice.id, MISSING)) == []
    assert results(member_session, save) == [(3, 2, "pending")]


def test_every_public_method_starts_with_the_user_id():
    tree = ast.parse(inspect.getsource(turn_regeneration_repository))
    klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TurnRegenerationRepository")
    public = {node.name: [a.arg for a in node.args.args] for node in klass.body if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")}
    assert set(public) == {"add_pending", "mark_result", "fail_pending_for_turn", "export_for_session"}
    assert all(args[:2] == ["self", "user_id"] for args in public.values()), public


# ---- 串聯刪除 ----------------------------------------------------------------------------------

def test_the_rows_disappear_with_the_save_and_with_the_player(repo, member_session, world):
    alice, save = world
    bystander = make_user(member_session, 2)
    kept = open_save(SessionRepository(member_session), bystander)
    gone = open_save(SessionRepository(member_session), alice)
    add(repo, alice, save, reason="留到刪帳號")
    add(repo, alice, gone, reason="隨存檔刪除")
    add(repo, bystander, kept, reason="旁人的")
    member_session.commit()

    member_session.execute(delete(RpgSession).where(RpgSession.id == gone.id))          # 資料庫層的串聯
    member_session.commit()
    assert member_session.scalar(select(func.count()).select_from(TurnRegeneration)) == 2
    member_session.execute(delete(User).where(User.id == alice.id))
    member_session.commit()
    assert [r.reason for r in member_session.scalars(select(TurnRegeneration))] == ["旁人的"]
