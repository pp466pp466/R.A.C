"""age-confirmation 規格：年齡規則的單一來源、年齡計算的邊界，以及生日與年齡區間的驗證。"""
from datetime import date, datetime, timezone

import pytest

from app import create_app
from app.config import load_config
from app.runtime import get_age_policy
from app.services.age_policy import (
    DEFAULT_AGE_POLICY, MISMATCH_MESSAGE, AgePolicy, VerifiedAge, age_group, age_on, range_for_age, taipei_today,
    validate_age_input,
)
from app.services.errors import ValidationError
from app.services.legal import CURRENT_AGE_POLICY_VERSION

TODAY = date(2026, 10, 28)
POLICY = DEFAULT_AGE_POLICY


# ---- 年齡計算 -----------------------------------------------------------------

@pytest.mark.parametrize(
    ("birth", "expected"),
    [
        (date(2013, 10, 28), 13),      # 13 歲生日當天就滿 13 歲
        (date(2013, 10, 29), 12),      # 前一天仍是 12 歲
        (date(2008, 10, 28), 18),
        (date(2008, 10, 29), 17),
        (date(2026, 10, 28), 0),
        (date(1900, 1, 1), 126),
    ],
)
def test_the_birthday_itself_is_the_day_the_age_increases(birth, expected):
    assert age_on(birth, TODAY) == expected


def test_a_leap_day_birthday_turns_the_year_on_march_first_in_a_common_year():
    """民法第 121 條：沒有相當日者以該月末日為期間末日，所以 2 月 28 日尚未滿歲，3 月 1 日起滿歲。"""
    born = date(2008, 2, 29)
    assert age_on(born, date(2026, 2, 28)) == 17
    assert age_on(born, date(2026, 3, 1)) == 18
    assert age_on(born, date(2028, 2, 29)) == 20                 # 閏年則在 2 月 29 日當天


@pytest.mark.parametrize(
    ("utc", "taipei"),
    [
        (datetime(2026, 10, 27, 15, 59, 59, tzinfo=timezone.utc), date(2026, 10, 27)),     # 台北 23:59:59
        (datetime(2026, 10, 27, 16, 0, 0, tzinfo=timezone.utc), date(2026, 10, 28)),       # 台北 00:00:00，已是隔天
        (datetime(2026, 10, 27, 16, 0, 0), date(2026, 10, 28)),                            # 沒有時區資訊者視為 UTC
    ],
)
def test_the_calendar_day_is_taken_in_asia_taipei_not_utc(utc, taipei):
    assert taipei_today(utc) == taipei


def test_a_birthday_that_is_already_today_in_taipei_counts_even_if_it_is_still_yesterday_in_utc():
    now = datetime(2026, 10, 27, 17, 0, tzinfo=timezone.utc)              # UTC 還是 10/27，台北已是 10/28 01:00
    assert age_on(date(2013, 10, 28), taipei_today(now)) == 13
    assert age_on(date(2013, 10, 28), now.date()) == 12                   # 若錯用 UTC 日期就會差一天


@pytest.mark.parametrize(
    ("age", "code"),
    [(0, "under_13"), (12, "under_13"), (13, "13_17"), (17, "13_17"), (18, "18_24"), (24, "18_24"), (25, "25_34"),
     (34, "25_34"), (35, "35_44"), (44, "35_44"), (45, "45_54"), (54, "45_54"), (55, "55_plus"), (120, "55_plus")],
)
def test_every_age_belongs_to_exactly_one_range(age, code):
    assert range_for_age(age, POLICY).code == code
    assert sum(1 for r in POLICY.ranges if r.contains(age)) == 1


def test_the_age_group_is_minor_below_the_adult_age_and_none_without_a_birthday():
    assert age_group(date(2008, 10, 29), TODAY, POLICY) == "minor"
    assert age_group(date(2008, 10, 28), TODAY, POLICY) == "adult"
    assert age_group(None, TODAY, POLICY) is None


# ---- 單一來源與注入 -----------------------------------------------------------

def test_the_default_policy_has_the_documented_numbers_and_version():
    assert (POLICY.min_register_age, POLICY.min_use_age, POLICY.adult_age) == (13, 13, 18)
    assert CURRENT_AGE_POLICY_VERSION == POLICY.version == "2026-10-28-birthdate-range"
    assert [c["code"] for c in POLICY.range_choices()] == ["under_13", "13_17", "18_24", "25_34", "35_44", "45_54", "55_plus"]
    assert all(c["label"] for c in POLICY.range_choices())


def test_create_app_uses_the_default_policy_and_accepts_an_injected_one():
    custom = AgePolicy(min_register_age=18, min_use_age=18, version="test-18-only")
    for injected, expected in ((None, DEFAULT_AGE_POLICY), (custom, custom)):
        app = create_app(load_config(environ={"APP_ENV": "testing"}), age_policy=injected)
        with app.app_context():
            assert get_age_policy() is expected


def test_a_custom_minimum_age_changes_who_is_eligible_without_touching_anything_else():
    strict = AgePolicy(min_register_age=18, min_use_age=18)
    with pytest.raises(ValidationError) as exc:
        validate_age_input("2011-10-28", "13_17", TODAY, strict)           # 15 歲
    assert exc.value.code == "age_not_eligible" and "18" in exc.value.message
    assert validate_age_input("2008-10-28", "18_24", TODAY, strict).is_adult is True


# ---- 驗證：通過 ---------------------------------------------------------------

def test_a_consistent_birthday_and_range_pass_and_report_the_result():
    verified = validate_age_input("2011-10-28", "13_17", TODAY, POLICY)
    assert verified == VerifiedAge(birth_date=date(2011, 10, 28), age_range="13_17", age=15, is_adult=False)
    assert validate_age_input(" 1996-01-05 ", "25_34", TODAY, POLICY).is_adult is True       # 前後空白會被去除


@pytest.mark.parametrize(("birth", "chosen"), [("2013-10-28", "13_17"), ("2008-10-29", "13_17"), ("2008-10-28", "18_24")])
def test_the_boundary_days_pass_with_the_matching_range(birth, chosen):
    assert validate_age_input(birth, chosen, TODAY, POLICY).age_range == chosen


# ---- 驗證：缺少與格式 ---------------------------------------------------------

@pytest.mark.parametrize("missing", [None, "", "   "])
def test_a_missing_birthday_is_a_field_error_for_the_birthday_only(missing):
    with pytest.raises(ValidationError) as exc:
        validate_age_input(missing, "18_24", TODAY, POLICY)
    assert set(exc.value.fields) == {"birth_date"} and exc.value.fields["birth_date"] == "請輸入生日。"


@pytest.mark.parametrize("missing", [None, "", "everyone", "18-24", 18, ["18_24"]])
def test_a_missing_or_unknown_range_is_a_field_error_for_the_range_only(missing):
    with pytest.raises(ValidationError) as exc:
        validate_age_input("1996-01-05", missing, TODAY, POLICY)
    assert set(exc.value.fields) == {"age_range"}


def test_both_missing_are_reported_at_once():
    with pytest.raises(ValidationError) as exc:
        validate_age_input(None, None, TODAY, POLICY)
    assert set(exc.value.fields) == {"birth_date", "age_range"}


@pytest.mark.parametrize(
    "bad",
    ["2026-02-30", "2026-13-01", "2026-00-10", "1996/01/05", "19960105", "96-01-05", "1996-1-5", "yesterday", "１９９６-０１-０５",
     "2026-10-29",            # 明天
     "2099-01-01",            # 未來
     "1899-12-31",            # 早於 1900 年
     12345678, 1996, True, {"y": 1996}],
)
def test_an_invalid_or_out_of_range_birthday_is_a_birthday_field_error(bad):
    with pytest.raises(ValidationError) as exc:
        validate_age_input(bad, "18_24", TODAY, POLICY)
    assert set(exc.value.fields) == {"birth_date"} and exc.value.code != "age_mismatch"


def test_the_earliest_allowed_birthday_is_the_first_day_of_1900_and_today_is_allowed():
    assert validate_age_input("1900-01-01", "55_plus", TODAY, POLICY).age == 126
    with pytest.raises(ValidationError) as exc:
        validate_age_input("2026-10-28", "under_13", TODAY, POLICY)          # 今天出生：合法的日期，但未滿 13 歲
    assert exc.value.code == "age_not_eligible"


# ---- 驗證：未滿 13 歲 ---------------------------------------------------------

@pytest.mark.parametrize("chosen", ["under_13", "13_17", "18_24", "55_plus"])
def test_a_birthday_under_13_is_not_eligible_whatever_range_was_chosen(chosen):
    with pytest.raises(ValidationError) as exc:
        validate_age_input("2014-10-29", chosen, TODAY, POLICY)              # 差一天才滿 13 歲
    assert exc.value.code == "age_not_eligible"
    assert exc.value.fields == {"birth_date": "本服務僅供 13 歲以上使用，無法註冊。"}


def test_choosing_the_under_13_range_is_not_eligible_even_with_an_adult_birthday():
    with pytest.raises(ValidationError) as exc:
        validate_age_input("1990-01-01", "under_13", TODAY, POLICY)
    assert exc.value.code == "age_not_eligible"


# ---- 驗證：不一致 -------------------------------------------------------------

@pytest.mark.parametrize(
    ("birth", "chosen"),
    [("1996-01-05", "13_17"), ("2011-10-28", "18_24"), ("2008-10-29", "18_24"), ("2008-10-28", "13_17"), ("1960-01-01", "45_54")],
)
def test_a_birthday_that_does_not_fall_in_the_chosen_range_is_a_mismatch_with_a_fixed_message_and_no_field(birth, chosen):
    with pytest.raises(ValidationError) as exc:
        validate_age_input(birth, chosen, TODAY, POLICY)
    assert exc.value.code == "age_mismatch"
    assert exc.value.message == MISMATCH_MESSAGE == "資料有誤，請重新註冊。"
    assert exc.value.fields == {}                                              # 不指出是哪一個欄位


# ---- 失敗的例外不帶輸入 -------------------------------------------------------

@pytest.mark.parametrize(
    ("birth", "chosen"),
    [("2014-10-29", "13_17"), ("1996-01-05", "13_17"), ("2026-02-30", "18_24"), ("1899-12-31", "55_plus"), ("1996-01-05", "nope")],
)
def test_no_failure_carries_the_birthday_in_its_message_fields_or_cause(birth, chosen):
    with pytest.raises(ValidationError) as exc:
        validate_age_input(birth, chosen, TODAY, POLICY)
    error = exc.value
    text = " ".join([str(error), error.message, repr(error.fields), repr(vars(error)), repr(error.__cause__), repr(error.__context__)])
    assert birth not in text and birth.replace("-", "") not in text


# ---- 測試輔助的一致性 ---------------------------------------------------------

def test_the_test_support_today_matches_the_default_fake_clock_in_asia_taipei():
    from tests.support.age import TODAY as SUPPORT_TODAY, TODAY_UTC, age_fields, birth_date_for_age
    from tests.support.auth import START

    assert TODAY_UTC == START and taipei_today(START) == SUPPORT_TODAY
    for age in (13, 17, 18, 30):
        assert age_on(birth_date_for_age(age), SUPPORT_TODAY) == age
        assert validate_age_input(age_fields(age)["birth_date"], age_fields(age)["age_range"], SUPPORT_TODAY, DEFAULT_AGE_POLICY).age == age
