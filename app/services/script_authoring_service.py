"""玩家創作劇本：建立、編輯、新草稿、刪除、撤回、筆名與額度（見 script-authoring 規格與 rpg-script-authoring 的 design D4）。

每個方法都先過帳號與年齡閘門（與其他會員功能同一個入口，`AccessPolicy.require_chat_access`），Repository 的方法一律帶 `user_id`，
別人的劇本與版本得到 `NotFound`（與不存在相同）。固定規格的驗證由純函式 `validate_script` 負責，作者的內容模式上限來自
`AccessPolicy.author_content_cap`（未成年 `safe`，成年到 `adult`）。**每次儲存都先跑規則層**（`script_rules`）：明確命中紅線時拒絕
儲存、被拒絕的文字不寫入（既有草稿維持原樣），回應只有類別；有疑慮（`flag`）照常儲存，疑慮在送審時重新計算。儲存時的拒絕不計入
送審次數與連續被拒。

草稿的編輯以內容雜湊偵測衝突：請求帶開始編輯時的雜湊（`base_hash`），與目前不同就是 `DraftChanged`，不覆寫。
作者在回應裡看不到 `flag` 的類別（那是給人工審核的標示，顯示出來只會教人閃避）。
日誌與事件只有事件名稱、內部編號、整數長度與固定列舉的類別。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from app.models.script import REVIEW_REJECT, VERSION_DRAFT, Script, ScriptVersion
from app.models.user import User
from app.observability import script_events
from app.repositories.errors import (
    AuthorNameLockedError, DraftChangedError, InvalidVersionTransitionError, OpenVersionExistsError, VersionNotDraftError,
)
from app.repositories.script_repository import ScriptRepository
from app.repositories.script_submission_repository import ScriptSubmissionRepository
from app.repositories.user_repository import UserRepository
from app.services.access_policy import AccessPolicy
from app.services.current_user import CurrentUser, iso_utc
from app.services.errors import (
    AuthorNameLocked, DraftChanged, NicknameRequired, NotDraft, NotFound, NotPending, OpenVersionExists, ScriptAutoRejected,
    ScriptLimitReached, Unauthenticated, ValidationError,
)
from app.services.quota_policy import taipei_day_bounds
from app.services.runtime_limits import RuntimeLimits
from app.services.script_rules import check_rules
from app.services import script_spec
from app.services.script_spec import ValidatedScript, validate_script

BUILTIN_EXAMPLE_KEY = "xianxia-qingyun"            # 範例就是內建的首發劇本：單一來源，種子改了範例就跟著改
BASE_HASH_MESSAGE = "請帶入開始編輯時的內容雜湊。"
CONTENT_MESSAGE = "請提供劇本內容。"
LIST_LIMIT = 500


def spec_limits() -> dict[str, Any]:
    """固定規格的欄位長度與數量上限，給編輯器顯示字數與數量（單一來源：驗證器的常數，前端不寫死）。伺服器仍是唯一的驗證者。"""
    s = script_spec
    return {
        "title": s.TITLE_MAX, "summary": s.SUMMARY_MAX, "world_name": s.WORLD_NAME_MAX, "background": s.BACKGROUND_MAX,
        "opening": s.OPENING_MAX, "rules": s.RULES_MAX, "tag_count": s.TAG_MAX_COUNT, "tag": s.TAG_MAX,
        "calendar_name": s.CALENDAR_NAME_MAX, "year_max": s.YEAR_MAX, "ending_text": s.ENDING_TEXT_MAX, "ending_age": s.ENDING_AGE_MAX,
        "character_count": s.CHARACTER_MAX_COUNT, "character_name": s.CHARACTER_NAME_MAX, "character_personality": s.CHARACTER_PERSONALITY_MAX,
        "character_appearance": s.CHARACTER_APPEARANCE_MAX, "character_occupation": s.CHARACTER_OCCUPATION_MAX,
        "character_speech_style": s.CHARACTER_SPEECH_STYLE_MAX, "character_hidden_trait": s.CHARACTER_HIDDEN_TRAIT_MAX,
        "character_age": s.CHARACTER_AGE_MAX, "location_count": s.LOCATION_MAX_COUNT, "location": s.LOCATION_MAX,
        "milestone_count": s.MILESTONE_MAX_COUNT, "milestone_title": s.MILESTONE_TITLE_MAX, "milestone_note": s.MILESTONE_NOTE_MAX,
        "trigger_age_max": s.TRIGGER_AGE_MAX, "total": s.TOTAL_CHARS_MAX,
    }


class ScriptAuthoringService:
    def __init__(
        self,
        *,
        scripts: ScriptRepository,
        users: UserRepository,
        submissions: ScriptSubmissionRepository,
        policy: AccessPolicy,
        limits: RuntimeLimits,
        clock: Callable[[], datetime],
    ) -> None:
        self._scripts = scripts
        self._users = users
        self._submissions = submissions
        self._policy = policy
        self._limits = limits
        self._clock = clock

    # ---- 讀取 -----------------------------------------------------------------------

    def list_mine(self, current: CurrentUser) -> list[dict[str, Any]]:
        """自己的全部劇本，每個劇本帶它的全部版本摘要（新的在前）。"""
        self._policy.require_chat_access(current)
        grouped: dict[int, dict[str, Any]] = {}
        for script, version in self._scripts.list_owned(current.id, limit=LIST_LIMIT):
            entry = grouped.setdefault(script.id, {**self._script_head(script), "versions": []})
            entry["versions"].append(self._version_summary(version))
        return list(grouped.values())

    def get_version(self, current: CurrentUser, version_public_id: str) -> dict[str, Any]:
        """自己的一個版本與它的完整內容。"""
        self._policy.require_chat_access(current)
        version = self._scripts.get_owned_version(current.id, version_public_id) if isinstance(version_public_id, str) else None
        if version is None:
            raise NotFound()
        return self.version_detail(version)

    def example(self, current: CurrentUser) -> dict[str, Any]:
        """內建首發劇本的標準化內容（不含識別碼與雜湊），給編輯器帶入欄位範例。"""
        self._policy.require_chat_access(current)
        builtin = self._scripts.get_builtin(BUILTIN_EXAMPLE_KEY)
        if builtin is None or builtin[1] is None:
            raise NotFound()
        return {"content": self._scripts.content_of(builtin[1])}

    def limits(self, current: CurrentUser) -> dict[str, Any]:
        """作者自己的劇本數與上限、今日送審次數與上限、是否被暫停發佈。"""
        self._policy.require_chat_access(current)
        user = self._require_user(current)
        start, end = taipei_day_bounds(self._clock())
        return {
            "scripts": {"used": self._scripts.count_owned(user.id), "limit": self._limits.script_limit(user.member_tier)},
            "submissions_today": {
                "used": self._submissions.count_counted_between(user.id, start=start, end=end),
                "limit": self._limits.submit_daily(user.member_tier),
            },
            "publish_suspended": user.publish_suspended_at is not None,
            "has_nickname": user.nickname is not None,
            "max_content_tier": self._policy.author_content_cap(current),
            "spec": spec_limits(),
        }

    # ---- 建立與編輯 -------------------------------------------------------------------

    def create(self, current: CurrentUser, raw_content: object) -> dict[str, Any]:
        """建立劇本與第 1 版草稿，筆名取目前暱稱的快照。順序：暱稱、劇本數量上限、固定規格、規則層。"""
        self._policy.require_chat_access(current)
        user = self._require_user(current)
        if user.nickname is None or user.nickname_code is None:
            raise NicknameRequired()
        if self._scripts.count_owned(user.id) >= self._limits.script_limit(user.member_tier):
            raise ScriptLimitReached()
        validated = self._validate(current, raw_content)
        self._check_rules(user, validated)
        version = self._scripts.create_script(
            user.id, author_name=user.nickname, author_code=user.nickname_code, content=validated.content,
            content_hash=validated.content_hash, total_chars=validated.total_chars,
        )
        script_events.log_created(user_id=user.id, total_chars=validated.total_chars)
        return self.version_detail(version)

    def update_draft(self, current: CurrentUser, version_public_id: str, raw_content: object, base_hash: object) -> dict[str, Any]:
        """儲存草稿。`base_hash` 是開始編輯時的內容雜湊；與目前不同（別處已更新）就不覆寫。內容沒變的重送視為成功。"""
        self._policy.require_chat_access(current)
        user = self._require_user(current)
        version = self._scripts.get_owned_version(user.id, version_public_id) if isinstance(version_public_id, str) else None
        if version is None:
            raise NotFound()
        if version.status != VERSION_DRAFT:
            raise NotDraft()
        if not isinstance(base_hash, str) or not base_hash:
            raise ValidationError(fields={"base_hash": BASE_HASH_MESSAGE})
        validated = self._validate(current, raw_content)
        if validated.content_hash == version.content_hash:
            return self.version_detail(version)
        if base_hash != version.content_hash:
            raise DraftChanged()
        self._check_rules(user, validated)
        try:
            self._scripts.update_draft(
                user.id, version_public_id, content=validated.content, content_hash=validated.content_hash,
                total_chars=validated.total_chars, expected_hash=base_hash,
            )
        except VersionNotDraftError:
            raise NotDraft() from None
        except DraftChangedError:
            raise DraftChanged() from None
        script_events.log_updated(user_id=user.id, total_chars=validated.total_chars)
        refreshed = self._scripts.get_owned_version(user.id, version_public_id)
        assert refreshed is not None
        return self.version_detail(refreshed)

    def new_version(self, current: CurrentUser, script_public_id: str) -> dict[str, Any]:
        """以劇本最新的非進行中版本（已發佈、被退回或被取代）的內容建立下一個草稿；已有進行中的版本時拒絕。"""
        self._policy.require_chat_access(current)
        user = self._require_user(current)
        if not isinstance(script_public_id, str) or self._scripts.get_owned_script(user.id, script_public_id) is None:
            raise NotFound()
        if self._scripts.get_open_version(user.id, script_public_id) is not None:
            raise OpenVersionExists()
        source = self._scripts.latest_closed_version(user.id, script_public_id)
        if source is None:
            raise NotFound()
        validated = self._validate(current, self._scripts.content_of(source))          # 沿用里程碑的 id；內容本來就合規
        try:
            version = self._scripts.create_draft_version(
                user.id, script_public_id, content=validated.content, content_hash=validated.content_hash,
                total_chars=validated.total_chars,
            )
        except OpenVersionExistsError:
            raise OpenVersionExists() from None
        if version is None:
            raise NotFound()
        script_events.log_version_created(user_id=user.id, total_chars=validated.total_chars)
        return self.version_detail(version)

    def delete_draft(self, current: CurrentUser, version_public_id: str) -> None:
        self._policy.require_chat_access(current)
        user = self._require_user(current)
        try:
            deleted = self._scripts.delete_draft(user.id, version_public_id) if isinstance(version_public_id, str) else None
        except VersionNotDraftError:
            raise NotDraft() from None
        if deleted is None:
            raise NotFound()
        script_events.log_deleted(user_id=user.id)

    def withdraw(self, current: CurrentUser, version_public_id: str) -> dict[str, Any]:
        """撤回待審的版本，回到草稿可以再編輯。"""
        self._policy.require_chat_access(current)
        user = self._require_user(current)
        try:
            version = self._scripts.withdraw(user.id, version_public_id) if isinstance(version_public_id, str) else None
        except InvalidVersionTransitionError:
            raise NotPending() from None
        if version is None:
            raise NotFound()
        script_events.log_withdrawn(user_id=user.id)
        return self.version_detail(version)

    def refresh_author_name(self, current: CurrentUser, script_public_id: str) -> dict[str, Any]:
        """劇本從未發佈時，重新取用目前的暱稱與短代號作為筆名；已鎖定則拒絕。更改暱稱本身不會自動改變任何筆名。"""
        self._policy.require_chat_access(current)
        user = self._require_user(current)
        if user.nickname is None or user.nickname_code is None:
            raise NicknameRequired()
        try:
            script = (
                self._scripts.update_author_name(user.id, script_public_id, name=user.nickname, code=user.nickname_code)
                if isinstance(script_public_id, str) else None
            )
        except AuthorNameLockedError:
            raise AuthorNameLocked() from None
        if script is None:
            raise NotFound()
        script_events.log_author_name_refreshed(user_id=user.id)
        return self._script_head(script)

    # ---- 內部 -------------------------------------------------------------------------

    def _require_user(self, current: CurrentUser) -> User:
        user = self._users.get_by_id(current.id)
        if user is None:
            raise Unauthenticated()
        return user

    def _validate(self, current: CurrentUser, raw_content: object) -> ValidatedScript:
        if not isinstance(raw_content, dict):
            raise ValidationError(fields={"content": CONTENT_MESSAGE})
        return validate_script(raw_content, max_tier=self._policy.author_content_cap(current))

    def _check_rules(self, user: User, validated: ValidatedScript) -> None:
        """儲存時的規則層：明確命中紅線就拒絕（不寫入、不計次），只告知類別。"""
        result = check_rules(validated.content, tier=validated.content["content_tier"], adult_age=self._policy.adult_age)
        if result.verdict == REVIEW_REJECT:
            script_events.log_auto_rejected(user_id=user.id, stage="save", layer="rules", categories=result.categories)
            raise ScriptAutoRejected(result.categories)

    @staticmethod
    def _script_head(script: Script) -> dict[str, Any]:
        return {
            "id": script.public_id, "author_name": script.author_name, "author_code": script.author_code,
            "author_locked": script.author_locked_at is not None,
        }

    def _version_summary(self, version: ScriptVersion) -> dict[str, Any]:
        return {
            "id": version.public_id, "version_no": version.version_no, "status": version.status, "title": version.title,
            "summary": version.summary, "content_tier": version.content_tier, "content_hash": version.content_hash,
            "total_chars": version.total_chars, "created_at": iso_utc(version.created_at), "updated_at": iso_utc(version.updated_at),
            "published_at": None if version.published_at is None else iso_utc(version.published_at),
        }

    def version_detail(self, version: ScriptVersion) -> dict[str, Any]:
        script = self._scripts.get_script_of(version)
        return {
            **self._version_summary(version), "script": self._script_head(script),
            "content": self._scripts.content_of(version),
        }

