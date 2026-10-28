"""access-control 規格：權限測試框架本身要能抓出漏洞，也要放行正確的端點。

這裡用一組測試專用的端點（記憶體字典代替資料表）：一些寫對、一些故意寫錯。
框架必須對寫對的放行，對每一種寫錯的方式都指出是哪一種身分造成的。
"""
import uuid

import pytest
from flask import jsonify
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base
from app.security.session import require_login
from app.services.errors import Forbidden, NotFound
from app.services.registry import get_access_policy
from tests.support.auth import add_user
from tests.support.idor import EndpointSpec, IdorFailure, check_endpoint


@pytest.fixture()
def world():
    app = create_app(load_config(environ={"APP_ENV": "testing"}))
    Base.metadata.create_all(app.extensions["db_engine"])
    app.notes = {}                                           # public_id -> owner 內部編號

    def find(identifier):
        return app.notes.get(identifier)

    # ---- 寫對的 ----
    @app.get("/api/_t/notes/<identifier>")
    def read_note(identifier):
        user = require_login()
        get_access_policy().require_owner(user, find(identifier))
        return jsonify(note=identifier)

    @app.delete("/api/_t/notes/<identifier>")
    def delete_note(identifier):
        user = require_login()
        get_access_policy().require_owner(user, find(identifier))
        del app.notes[identifier]
        return jsonify(ok=True)

    # ---- 故意寫錯的 ----
    @app.get("/api/_t/leaky/<identifier>")                  # 沒有檢查擁有者：任何登入者都讀得到
    def leaky_read(identifier):
        require_login()
        if find(identifier) is None:
            raise NotFound()
        return jsonify(note=identifier)

    @app.delete("/api/_t/leaky-delete/<identifier>")        # 任何登入者都刪得掉別人的資源
    def leaky_delete(identifier):
        require_login()
        if app.notes.pop(identifier, None) is None:
            raise NotFound()
        return jsonify(ok=True)

    @app.get("/api/_t/forbidden/<identifier>")              # 對他人回 403：洩漏「存在」
    def forbidden_read(identifier):
        user = require_login()
        owner = find(identifier)
        if owner is None:
            raise NotFound()
        if owner != user.id:
            raise Forbidden()
        return jsonify(note=identifier)

    @app.get("/api/_t/distinguishable/<identifier>")        # 對他人也回 404，但訊息不同：仍洩漏「存在」
    def distinguishable_read(identifier):
        user = require_login()
        owner = find(identifier)
        if owner is None:
            raise NotFound()
        if owner != user.id:
            raise NotFound("這份內容不屬於您。")
        return jsonify(note=identifier)

    @app.get("/api/_t/public/<identifier>")                 # 忘了要求登入
    def public_read(identifier):
        if find(identifier) is None:
            raise NotFound()
        return jsonify(note=identifier)

    with Session(app.extensions["db_engine"]) as session:
        alice, bob = add_user(session, 1), add_user(session, 2)
        app.alice_id, app.bob_id = alice.id, bob.id
    return app


def users_of(app):
    from app.models import User

    with Session(app.extensions["db_engine"]) as session:
        return session.get(User, app.alice_id), session.get(User, app.bob_id)


def create_note(app, owner):
    identifier = str(uuid.uuid4())
    app.notes[identifier] = owner.id
    return identifier


def spec(method, path, **kwargs):
    return EndpointSpec(method, path, create_resource=create_note, **kwargs)


# ---- 放行正確的端點 -----------------------------------------------------------

@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_a_correct_endpoint_passes(world, method):
    alice, bob = users_of(world)
    check_endpoint(world, spec(method, "/api/_t/notes/{id}"), owner=alice, intruder=bob)


def test_a_correct_delete_endpoint_really_removed_the_resource_only_for_the_owner(world):
    alice, bob = users_of(world)
    check_endpoint(world, spec("DELETE", "/api/_t/notes/{id}"), owner=alice, intruder=bob)
    assert world.notes == {}                                 # 最後由擁有者刪除；前面他人的嘗試沒有得手


# ---- 抓出每一種寫錯的方式 -----------------------------------------------------

@pytest.mark.parametrize(
    ("method", "path", "identity"),
    [
        ("GET", "/api/_t/leaky/{id}", "intruder"),              # 他人讀得到
        ("DELETE", "/api/_t/leaky-delete/{id}", "intruder"),    # 他人刪得掉
        ("GET", "/api/_t/forbidden/{id}", "intruder"),          # 回 403 而不是 404
        ("GET", "/api/_t/distinguishable/{id}", "consistency"), # 404 但與「不存在」的回應不同
        ("GET", "/api/_t/public/{id}", "intruder"),             # 沒要求登入：已登入的他人也讀得到
    ],
)
def test_each_kind_of_mistake_is_caught_and_attributed_to_the_right_identity(world, method, path, identity):
    alice, bob = users_of(world)
    with pytest.raises(IdorFailure) as exc:
        check_endpoint(world, spec(method, path), owner=alice, intruder=bob)
    assert exc.value.identity == identity
    assert path.split("{")[0] in str(exc.value) or method in str(exc.value)


def test_a_write_endpoint_that_lets_an_intruder_in_is_caught_before_the_owner_call_could_hide_it(world):
    """若先讓擁有者刪除，再測他人，會變成對「已不存在」的資源測試而誤判通過。"""
    alice, bob = users_of(world)
    with pytest.raises(IdorFailure) as exc:
        check_endpoint(world, spec("DELETE", "/api/_t/leaky-delete/{id}"), owner=alice, intruder=bob)
    assert exc.value.identity == "intruder"


def test_a_missing_anonymous_check_is_attributed_to_anonymous_when_intruder_is_handled(world):
    """另一種寫法：登入的他人被擋下（404），但未登入的人卻不是 401。"""
    @world.get("/api/_t/anonymous-ok/<identifier>")
    def anonymous_ok(identifier):
        from app.security.session import current_user

        user = current_user()
        owner = world.notes.get(identifier)
        if owner is None or (user is not None and owner != user.id):
            raise NotFound()
        return jsonify(note=identifier)               # 未登入者（user 為 None）被放行

    alice, bob = users_of(world)
    with pytest.raises(IdorFailure) as exc:
        check_endpoint(world, spec("GET", "/api/_t/anonymous-ok/{id}"), owner=alice, intruder=bob)
    assert exc.value.identity == "anonymous"


def test_the_owner_must_succeed_with_the_expected_status(world):
    alice, bob = users_of(world)
    with pytest.raises(IdorFailure) as exc:
        check_endpoint(world, spec("GET", "/api/_t/notes/{id}", success_status=201), owner=alice, intruder=bob)
    assert exc.value.identity == "owner"


def test_owner_and_intruder_must_be_different_people(world):
    alice, _ = users_of(world)
    with pytest.raises(ValueError):
        check_endpoint(world, spec("GET", "/api/_t/notes/{id}"), owner=alice, intruder=alice)
