"""年齡規則的單一來源，以及生日與年齡區間的驗證（見 AGENTS.md 第 7、10 節與 age-confirmation 規格）。

所有年齡數字（最低註冊年齡、最低使用年齡、成年門檻）、年齡區間選項與政策版本都只在這裡；頁面、端點與其他 Service
不得內嵌年齡數字，需要時一律向 `AgePolicy` 取用。日後改規則（例如換 LLM 供應商而要把最低年齡改為 18）只需要換一個
`AgePolicy` 並提高 `version`，不需要改資料結構或頁面。

驗證規則（`validate_age_input`）：
1. 缺少或格式不合法 → `ValidationError`，欄位 `birth_date`、`age_range` 各自說明；
2. 年齡低於最低註冊年齡（或所選區間整段都低於它）→ `ValidationError`，代碼 `age_not_eligible`；
3. 生日算出的年齡不在所選區間 → `ValidationError`，代碼 `age_mismatch`，**不指出是哪個欄位**，訊息固定。
失敗的例外一律不帶輸入的內容（訊息、屬性、`__cause__` 都沒有生日），日誌也只記事件名稱，因此未滿最低年齡者填寫的
生日不會留在任何地方。

年齡一律由生日於「當下」計算，不依賴儲存的結果；日曆日期以 Asia/Taipei 計。台灣自 1979 年起沒有夏令時間，固定
UTC+8 即正確，所以不依賴 `zoneinfo`（Windows 預設沒有 tz 資料庫，也不想為此新增相依套件）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.services.errors import ValidationError

TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")
EARLIEST_BIRTH_DATE = date(1900, 1, 1)

GROUP_MINOR = "minor"
GROUP_ADULT = "adult"

MISMATCH_MESSAGE = "資料有誤，請重新註冊。"
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class AgeRange:
    """一個年齡區間選項。`max_age` 為 None 代表沒有上限。"""

    code: str
    label: str
    min_age: int
    max_age: int | None

    def contains(self, age: int) -> bool:
        return age >= self.min_age and (self.max_age is None or age <= self.max_age)


DEFAULT_RANGES = (
    AgeRange("under_13", "未滿 13 歲", 0, 12),
    AgeRange("13_17", "13 至 17 歲", 13, 17),
    AgeRange("18_24", "18 至 24 歲", 18, 24),
    AgeRange("25_34", "25 至 34 歲", 25, 34),
    AgeRange("35_44", "35 至 44 歲", 35, 44),
    AgeRange("45_54", "45 至 54 歲", 45, 54),
    AgeRange("55_plus", "55 歲以上", 55, None),
)


@dataclass(frozen=True)
class AgePolicy:
    min_register_age: int = 13        # 低於它不得註冊
    min_use_age: int = 13             # 低於它不得使用任何會呼叫 Gemini 的功能（既有帳號亦同）
    adult_age: int = 18               # 達到它才不受安全模式限制
    ranges: tuple[AgeRange, ...] = DEFAULT_RANGES
    version: str = "2026-10-28-birthdate-range"

    def range_by_code(self, code: object) -> AgeRange | None:
        return next((r for r in self.ranges if r.code == code), None) if isinstance(code, str) else None

    def range_choices(self) -> list[dict[str, str]]:
        """給頁面的下拉選項（代碼與繁體中文標籤）。"""
        return [{"code": r.code, "label": r.label} for r in self.ranges]


DEFAULT_AGE_POLICY = AgePolicy()


@dataclass(frozen=True)
class VerifiedAge:
    """通過驗證的結果。`is_adult` 只用於紀錄；判斷成年一律重新由生日計算。"""

    birth_date: date
    age_range: str
    age: int
    is_adult: bool


# ---- 年齡計算 -----------------------------------------------------------------

def taipei_today(now: datetime) -> date:
    """時鐘的時間（UTC，沒有時區資訊者視為 UTC）對應到 Asia/Taipei 的日曆日期。"""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(TAIPEI).date()


def age_on(birth_date: date, today: date) -> int:
    """滿幾歲。生日當天就滿一歲；2 月 29 日出生者，在沒有 2 月 29 日的年份於 3 月 1 日滿歲（民法第 121 條）。"""
    return today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))


def age_group(birth_date: date | None, today: date, policy: AgePolicy) -> str | None:
    """`minor`、`adult`；沒有生日（尚未完成年齡確認）為 None。"""
    if birth_date is None:
        return None
    return GROUP_ADULT if age_on(birth_date, today) >= policy.adult_age else GROUP_MINOR


def range_for_age(age: int, policy: AgePolicy) -> AgeRange | None:
    return next((r for r in policy.ranges if r.contains(age)), None)


# ---- 驗證 ---------------------------------------------------------------------

def _parse_birth_date(raw: object, today: date) -> tuple[date | None, str | None]:
    """回傳（日期, None）或（None, 錯誤說明）。說明不含輸入的內容。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, "請輸入生日。"
    invalid = "請輸入有效的生日（年-月-日）。"
    if not isinstance(raw, str) or not _ISO_DATE.fullmatch(raw.strip()):
        return None, invalid
    try:
        parsed = date.fromisoformat(raw.strip())
    except ValueError:
        return None, invalid
    if parsed < EARLIEST_BIRTH_DATE or parsed > today:
        return None, invalid
    return parsed, None


def validate_age_input(raw_birth_date: object, raw_range: object, today: date, policy: AgePolicy) -> VerifiedAge:
    fields: dict[str, str] = {}
    birth_date, problem = _parse_birth_date(raw_birth_date, today)
    if problem:
        fields["birth_date"] = problem
    chosen = policy.range_by_code(raw_range)
    if chosen is None:
        fields["age_range"] = "請選擇年齡區間。"
    if fields:
        raise ValidationError(fields=fields)
    assert birth_date is not None and chosen is not None

    age = age_on(birth_date, today)
    if age < policy.min_register_age or (chosen.max_age is not None and chosen.max_age < policy.min_register_age):
        message = f"本服務僅供 {policy.min_register_age} 歲以上使用，無法註冊。"
        raise ValidationError(message, fields={"birth_date": message}, code="age_not_eligible")
    if not chosen.contains(age):
        raise ValidationError(MISMATCH_MESSAGE, code="age_mismatch")
    return VerifiedAge(birth_date=birth_date, age_range=chosen.code, age=age, is_adult=age >= policy.adult_age)
