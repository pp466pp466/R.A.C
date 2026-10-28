"""session-authentication 規格：密碼以 Argon2id 雜湊；密碼長度規則。"""
import time

import pytest

from app.services import password_hasher as ph
from app.services.password_hasher import (
    MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, PRODUCTION_PARAMETERS, TESTING_PARAMETERS, PasswordHasherService,
    normalize_password, parameters_for_env, password_problem,
)

FAST = PasswordHasherService(TESTING_PARAMETERS)


# ---- 長度與字元規則 -----------------------------------------------------------

@pytest.mark.parametrize("length", [MIN_PASSWORD_LENGTH, 11, 64, MAX_PASSWORD_LENGTH])
def test_lengths_inside_the_range_are_accepted(length):
    assert password_problem("a" * length) is None


@pytest.mark.parametrize("length", [0, 1, 9, 129, 500])
def test_lengths_outside_the_range_are_rejected_with_the_documented_message(length):
    assert password_problem("a" * length) == "密碼長度須為 10 至 128 字。"


def test_no_character_composition_is_required():
    assert password_problem("abcdefghij") is None       # 十個全小寫英文字母
    assert password_problem("這是一個很長的中文密碼喔") is None
    assert password_problem("          ") is None         # 十個空白也是合法長度（空白是密碼的一部分）


def test_length_is_counted_in_characters_after_normalisation_not_bytes():
    assert password_problem("密" * 10) is None            # 10 個字元（30 位元組）
    assert password_problem("😀" * 10) is None
    assert password_problem("密" * 129) is not None


@pytest.mark.parametrize("bad", ["abcdefghi\n", "abc\tdefghijk", "abcdefghij\x00", "abcdefghij\ud800"])
def test_control_characters_and_unencodable_text_are_rejected(bad):
    assert password_problem(bad) == "密碼不可包含控制字元或無法使用的字元。"


@pytest.mark.parametrize("not_text", [None, 123456789012, ["a"], {"a": 1}, b"abcdefghij"])
def test_non_text_is_rejected(not_text):
    assert password_problem(not_text) == "請輸入密碼。"


# ---- 正規化 -------------------------------------------------------------------

def test_fullwidth_and_halfwidth_forms_are_equivalent():
    fullwidth = "１２３４５６７８９０ａｂｃ"
    assert normalize_password(fullwidth) == "1234567890abc"
    stored = FAST.hash(fullwidth)
    assert FAST.verify(stored, "1234567890abc")
    assert FAST.verify(stored, fullwidth)


def test_surrounding_whitespace_is_part_of_the_password():
    stored = FAST.hash("  spaced-secret  ")
    assert FAST.verify(stored, "  spaced-secret  ")
    assert not FAST.verify(stored, "spaced-secret")


# ---- 雜湊 ---------------------------------------------------------------------

def test_the_stored_value_is_an_argon2id_hash_and_never_contains_the_plaintext():
    password = "correct horse battery staple"
    stored = FAST.hash(password)
    assert stored.startswith("$argon2id$")
    assert password not in stored
    assert len(stored) <= 255          # users.password_hash 欄位長度


def test_hashes_are_salted():
    assert FAST.hash("same-password-twice") != FAST.hash("same-password-twice")


def test_verify_accepts_the_right_password_and_rejects_everything_else():
    stored = FAST.hash("the-right-password")
    assert FAST.verify(stored, "the-right-password")
    assert not FAST.verify(stored, "the-wrong-password")
    assert not FAST.verify(stored, "")


@pytest.mark.parametrize("garbage", ["", "not-a-hash", "$argon2id$broken", "$2b$12$abcdefghijklmnopqrstuv"])
def test_verify_never_raises_on_a_malformed_or_foreign_stored_hash(garbage):
    assert FAST.verify(garbage, "whatever-password") is False


def test_verify_without_a_stored_hash_returns_false():
    assert FAST.verify(None, "any-password-here") is False


# ---- 參數與升級 ---------------------------------------------------------------

def test_production_parameters_follow_rfc_9106_low_memory_profile():
    assert PRODUCTION_PARAMETERS == {"time_cost": 3, "memory_cost": 65536, "parallelism": 4}
    for env in ("development", "staging", "production"):
        assert parameters_for_env(env) == PRODUCTION_PARAMETERS
    assert parameters_for_env("testing") == TESTING_PARAMETERS


def test_a_hash_made_with_weaker_parameters_needs_a_rehash_and_a_current_one_does_not():
    strong = PasswordHasherService(PRODUCTION_PARAMETERS)
    weak_hash = FAST.hash("some-password-123")
    assert strong.needs_rehash(weak_hash) is True
    assert FAST.needs_rehash(weak_hash) is False
    assert strong.verify(weak_hash, "some-password-123")        # 舊參數的雜湊仍可驗證
    upgraded = strong.hash("some-password-123")
    assert strong.needs_rehash(upgraded) is False


def test_needs_rehash_treats_a_malformed_hash_as_needing_one():
    assert FAST.needs_rehash("garbage") is True


# ---- 時間一致 -----------------------------------------------------------------

def test_the_dummy_hash_is_a_real_argon2id_hash_created_once_per_parameter_set():
    first = ph._dummy_hash(1, 8, 1)
    assert first.startswith("$argon2id$")
    assert ph._dummy_hash(1, 8, 1) is first


def test_verifying_without_a_hash_costs_about_as_much_as_a_real_verification():
    """帳號不存在（或沒有密碼）時，耗時不能明顯短於真實驗證，否則回應時間會洩漏帳號是否存在。"""
    params = {"time_cost": 2, "memory_cost": 16384, "parallelism": 1}      # 夠大，量得到差異
    service = PasswordHasherService(params)
    stored = service.hash("a-real-password")
    service.burn("warm-up")                                                   # 排除第一次產生假雜湊的成本

    def best_of(action, n=5):
        samples = []
        for _ in range(n):
            start = time.perf_counter()
            action()
            samples.append(time.perf_counter() - start)
        return min(samples)

    real = best_of(lambda: service.verify(stored, "wrong-password-guess"))
    dummy = best_of(lambda: service.verify(None, "wrong-password-guess"))
    assert dummy > real * 0.5, f"沒有雜湊時的驗證太快：{dummy:.4f}s 對 {real:.4f}s"
    assert dummy < real * 2.5
