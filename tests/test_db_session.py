"""database-access 規格：請求級交易生命週期與 BaseRepository。"""
import pytest
from flask import Flask, abort, jsonify
from sqlalchemy import String, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.db.dialect import create_engine_for
from app.db.session import get_session, init_session
from app.repositories.base import BaseRepository


class _Base(DeclarativeBase):
    pass


class Note(_Base):
    __tablename__ = "note"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50), unique=True)


class NoteRepository(BaseRepository[Note]):
    model = Note


@pytest.fixture()
def engine(tmp_path):
    # 檔案型 SQLite 使用一般連線池，才能驗證連線是否被歸還
    eng = create_engine_for(f"sqlite+pysqlite:///{tmp_path / 'session.sqlite3'}")
    _Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def client(engine):
    app = Flask(__name__)
    init_session(app, engine)

    @app.post("/ok")
    def ok():
        NoteRepository(get_session()).add(Note(title="ok"))
        return jsonify(status="ok")

    @app.post("/boom")
    def boom():
        NoteRepository(get_session()).add(Note(title="boom"))
        raise RuntimeError("處理中發生未預期錯誤")

    @app.post("/bad")
    def bad():
        NoteRepository(get_session()).add(Note(title="bad"))
        abort(400)

    @app.post("/dup")
    def dup():
        repo = NoteRepository(get_session())
        repo.add(Note(title="dup"))
        # 同一個 title 再加一次：違反唯一約束。flush 當下就會失敗，因此改為直接加入 session
        # 並延後到提交時才暴露，以驗證「提交失敗 → 500 且無殘留」
        get_session().add(Note(title="dup"))
        return jsonify(status="should-not-commit")

    @app.get("/nodb")
    def nodb():
        return jsonify(status="no db access")

    app.testing = False  # 讓未預期例外轉為 500 回應，而不是在測試中重新拋出
    return app.test_client()


def _titles(engine) -> list[str]:
    with Session(engine) as session:
        return sorted(session.scalars(select(Note.title)).all())


def test_successful_request_commits_and_releases_connection(client, engine):
    assert client.post("/ok").status_code == 200
    assert _titles(engine) == ["ok"]
    assert engine.pool.checkedout() == 0


def test_exception_rolls_back_and_releases_connection(client, engine):
    assert client.post("/boom").status_code == 500
    assert _titles(engine) == []
    assert engine.pool.checkedout() == 0


def test_error_response_does_not_commit(client, engine):
    assert client.post("/bad").status_code == 400
    assert _titles(engine) == []
    assert engine.pool.checkedout() == 0


def test_commit_failure_becomes_500_and_leaves_no_partial_data(client, engine):
    assert client.post("/dup").status_code == 500
    assert _titles(engine) == []
    assert engine.pool.checkedout() == 0


def test_request_without_database_access_does_not_take_a_connection(client, engine):
    assert client.get("/nodb").status_code == 200
    assert engine.pool.checkedout() == 0


def test_get_session_outside_request_is_an_error():
    with pytest.raises(RuntimeError):
        get_session()


def test_repository_add_get_list_delete_without_committing():
    engine = create_engine_for("sqlite+pysqlite:///:memory:")
    _Base.metadata.create_all(engine)
    with Session(engine) as session:
        repo = NoteRepository(session)
        note = repo.add(Note(title="第一則"))
        assert note.id is not None  # flush 後已取得主鍵
        assert repo.get(note.id).title == "第一則"
        assert [n.title for n in repo.list()] == ["第一則"]
        repo.delete(note)
        assert repo.get(note.id) is None
        session.rollback()
        assert session.scalar(select(func.count()).select_from(Note)) == 0
