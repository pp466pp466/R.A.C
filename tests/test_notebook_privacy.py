"""rpg-notebook 規格「記事簿屬個資，日誌只記整數」：日誌、匯出、刪除與管理員。

- 日誌：走過 AI 追加、玩家儲存、以及三種被拒的儲存（太長、規則層、版本衝突）之後掃描全部日誌輸出，找不到任何記事簿的文字或被拒的內容；
- 匯出：個人資料匯出含記事簿全文（AI 追加與玩家編輯的結果）；
- 刪除：記事簿文字只存在於存檔那一列的一個欄位，沒有任何副本；帳號抹除之後，整個資料庫的所有文字與 JSON 欄位都找不到它；
- 管理員：後台的所有 GET 端點（以管理員身分逐一呼叫）都讀不到記事簿全文，後台的程式也完全沒有提到記事簿。

旅程用 `test_rpg_notebook_flow` 的 `world`（假供應商、真實 HTTP 與資料庫；SQLite 與真實 SQL Server 各跑一次）。
"""
import json
import re
from pathlib import Path

import pytest
from sqlalchemy import JSON, String, func, select
from sqlalchemy.orm import Session

from app.models import Base, RpgSession, User
from app.repositories.session_repository import SessionRepository
from tests.support.auth import START, add_user, sign_in_as
from tests.support.game import BAD_SENTENCE
from tests.support.protagonist import protagonist
from tests.support.script_data import validated
from tests.support.llm import Step
from tests.test_account_erasure_rpg_data import erase_inactive, old_user
from tests.test_rpg_notebook_flow import FIRST_ENTRIES, STORY, action, reply, world  # noqa: F401  (world 是 fixture)

PLAYER_SECRET = "只有玩家知道的祕密設定PLAYERSECRET"
TOO_LONG_SECRET = "超長被拒的祕密TOOLONGSECRET"
CONFLICT_SECRET = "版本衝突被拒的祕密CONFLICTSECRET"
ROOT = Path(__file__).resolve().parent.parent


def url(save_id: str) -> str:
    return f"/api/game/sessions/{save_id}/notebook"


def put(client, save_id, text, version):
    return client.put(url(save_id), json={"text": text, "expected_version": version})


def play_the_notebook_journey(play) -> str:
    """11 輪（第 11 輪後 AI 追加兩條）→ 玩家在 AI 的文字後面加一行 → 三種被拒的儲存。回傳最後的記事簿全文。"""
    for n in range(1, 11):
        play.turn(n)
    play.turn(11, Step(reply(*FIRST_ENTRIES)))
    view = play.client.get(url(play.save_id)).get_json()["notebook"]
    assert view["text"] == "\n".join(FIRST_ENTRIES) and view["version"] == 2

    final = view["text"] + "\n" + PLAYER_SECRET
    assert put(play.client, play.save_id, final, view["version"]).status_code == 200                                    # 玩家儲存
    assert put(play.client, play.save_id, TOO_LONG_SECRET + "甲" * 2100, 3).status_code == 422                          # 太長
    assert put(play.client, play.save_id, "設定" + BAD_SENTENCE, 3).status_code == 422                                  # 規則層
    assert put(play.client, play.save_id, CONFLICT_SECRET, 1).status_code == 409                                         # 版本衝突
    assert play.save()[0] == final
    return final


# ---- 日誌 ---------------------------------------------------------------------------------------

def test_the_logs_hold_no_notebook_text_after_ai_appends_player_saves_and_rejections(world, log_lines):
    play = world
    play_the_notebook_journey(play)
    raw = log_lines.raw()
    secrets = (*FIRST_ENTRIES, PLAYER_SECRET, TOO_LONG_SECRET, CONFLICT_SECRET, BAD_SENTENCE, STORY, *(action(n) for n in range(1, 12)))
    for secret in secrets:
        assert secret not in raw, secret
    seen = [line for line in log_lines() if str(line.get("event", "")).startswith("rpg.notebook_")]
    reasons = sorted(line["reason"] for line in seen if line["event"] == "rpg.notebook_save_rejected")
    assert reasons == ["blocked", "conflict", "too_long"]
    assert [line["event"] for line in seen].count("rpg.notebook_saved") == 1 and [line["event"] for line in seen].count("rpg.notebook_captured") == 1
    for line in seen:                                                                   # 這些事件的欄位只有整數與固定的列舉
        for key, value in line.items():
            if key in ("time", "level", "logger", "message", "request_id"):
                continue
            assert isinstance(value, (int, bool)) or (isinstance(value, str) and len(value) < 40 and not re.search(r"[一-鿿]", value)), (key, value)


# ---- 匯出 ---------------------------------------------------------------------------------------

def test_the_personal_export_has_the_whole_notebook_after_ai_and_player_edits(world):
    play = world
    final = play_the_notebook_journey(play)
    response = play.client.post("/api/account/export", json={"format": "json"})
    assert response.status_code == 200
    exported = json.loads(response.get_data().decode("utf-8"))
    (entry,) = [s for s in exported["sessions"] if s["notebook_text"]]
    assert entry["notebook_text"] == final and all(line in entry["notebook_text"] for line in (*FIRST_ENTRIES, PLAYER_SECRET))
    assert CONFLICT_SECRET not in json.dumps(exported, ensure_ascii=False)             # 被拒的內容從未進入資料庫


# ---- 管理員 -------------------------------------------------------------------------------------

def test_no_admin_endpoint_returns_the_notebook(world):
    play = world
    final = play_the_notebook_journey(play)
    engine = play.app.extensions["db_engine"]
    with Session(engine) as s:
        admin = add_user(s, 2, role="admin", age=30)
        admin_id, token_version = admin.id, admin.token_version
        player_public_id = s.scalar(select(User.public_id).where(User.id == play.user_id))
    client = play.app.test_client()
    sign_in_as(client, admin_id, token_version)

    sweep: list[tuple[str, int]] = []
    for rule in play.app.url_map.iter_rules():
        if not (rule.rule.startswith("/api/admin/") and "GET" in rule.methods):
            continue
        candidates = [rule.rule] if "<" not in rule.rule else [
            re.sub(r"<[^>]+>", value, rule.rule) for value in (player_public_id, play.save_id, "1", "00000000-0000-4000-8000-000000000000")
        ]
        for path in candidates:
            response = client.get(path)
            body = response.get_data(as_text=True)
            sweep.append((path, response.status_code))
            for secret in (*FIRST_ENTRIES, PLAYER_SECRET, final):
                assert secret not in body, path
    assert ("/api/admin/overview", 200) in sweep and len(sweep) >= 10                  # 掃描有實際呼叫到端點，不是全部被擋掉
    page = client.get("/admin")
    assert page.status_code == 200 and PLAYER_SECRET not in page.get_data(as_text=True)


@pytest.mark.parametrize("path", [
    "app/controllers/admin_api_controller.py", "app/controllers/admin_page_controller.py", "app/services/admin_service.py",
    "app/repositories/admin_repository.py",
])
def test_the_admin_code_does_not_even_mention_the_notebook(path):
    source = (ROOT / path).read_text(encoding="utf-8-sig").lower()
    assert "notebook" not in source and "rpgsession" not in source and "rpg_session" not in source


def test_there_is_no_admin_route_about_saves_or_notebooks(world):
    rules = [rule.rule for rule in world.app.url_map.iter_rules() if rule.rule.startswith(("/api/admin", "/admin"))]
    assert rules and not [rule for rule in rules if re.search(r"notebook|game|rpg|save", rule)]


# ---- 刪除 ---------------------------------------------------------------------------------------

def where_found(session, needle: str) -> set[str]:
    """整個資料庫裡出現過 `needle` 的欄位（`資料表.欄位`）：文字欄位用 LIKE，JSON 欄位讀出來在 Python 比對（不依 JSON 內容篩選）。"""
    session.expire_all()
    found: set[str] = set()
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, JSON):
                if any(needle in json.dumps(value, ensure_ascii=False) for value in session.scalars(select(column)) if value is not None):
                    found.add(f"{table.name}.{column.name}")
            elif isinstance(column.type, String):
                if session.scalar(select(func.count()).select_from(table).where(column.contains(needle, autoescape=True))):
                    found.add(f"{table.name}.{column.name}")
    return found


def test_the_notebook_lives_in_one_column_of_the_save_row_and_leaves_with_the_account(member_session):
    s = member_session
    columns = sorted(f"{t.name}.{c.name}" for t in Base.metadata.sorted_tables for c in t.columns if "notebook" in c.name)
    table = RpgSession.__table__.name
    assert columns == sorted(f"{table}.{name}" for name in ("notebook_text", "notebook_upto_turn", "notebook_version"))     # 沒有別的資料表放記事簿

    player = old_user(s, 1)
    save = SessionRepository(s).create(
        player.id, source_version_id=None, snapshot=validated().content, snapshot_spec_version=1, protagonist=protagonist("自己", 18),
        content_tier="safe", now=START,
    )
    assert SessionRepository(s).save_notebook(player.id, save.public_id, text=PLAYER_SECRET, expected_version=1) is not None
    s.commit()
    assert where_found(s, PLAYER_SECRET) == {f"{table}.notebook_text"}                  # 整個資料庫只有這一個地方有它（沒有副本）

    player_id = player.id
    assert erase_inactive(s, player_id) is True
    assert where_found(s, PLAYER_SECRET) == set()
    assert s.get(User, player_id) is None and s.scalar(select(func.count()).select_from(RpgSession)) == 0


def test_deleting_the_save_row_takes_the_notebook_with_it(member_session):
    s = member_session
    player = old_user(s, 1)
    repository = SessionRepository(s)
    save = repository.create(
        player.id, source_version_id=None, snapshot=validated().content, snapshot_spec_version=1, protagonist=protagonist("自己", 18),
        content_tier="safe", now=START,
    )
    repository.save_notebook(player.id, save.public_id, text=PLAYER_SECRET, expected_version=1)
    s.commit()
    assert where_found(s, PLAYER_SECRET) == {f"{RpgSession.__table__.name}.notebook_text"}
    s.delete(save)
    s.commit()
    assert where_found(s, PLAYER_SECRET) == set()
