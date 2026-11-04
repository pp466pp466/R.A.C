"""內建（首發）劇本的種子（見 builtin-script-seed 規格、rpg-data-and-script 的 design D10）。

資料檔放在 `app/data/scripts/*.json`（UTF-8，讀取一律 `utf-8-sig`，隨映像封裝），每份是
`{"seed_key": "...", "content": {…固定規格的內容…}}`。`flask seed-scripts` 由部署流程執行（不排程、不在應用程式啟動時執行）：

1. **全部先驗證**：讀檔、解析 JSON、檢查種子鍵、以 `validate_script` 驗證內容，宣告的內容模式不得高於 `BUILTIN_MAX_TIER`。
   任何一份不合規就丟出 `SeedDataError`（只含檔名與欄位路徑與規則說明，**不回顯劇本文字**），而且還沒有寫入任何東西（全有或全無）。
2. 以種子鍵與標準化內容的雜湊判斷每一份是「建立」「更新」或「沒有變動」：只有排版差異（標準化後相同）視為沒有變動；
   內容改變時新增一個版本並取代舊的上架版本，從不修改已發佈的版本，已經開出的存檔也不受影響（它們有自己的複本）。
3. 預覽模式只計算結果，不寫入。正式執行在單一交易內寫入全部，失敗時整批回復。

內建劇本沒有作者，筆名「官方」並在建立時鎖定；它們直接為 `published`，是唯一繞過審核的路徑（`ScriptRepository.publish_builtin`）。
輸出與日誌只含種子鍵、檔名與筆數。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from app.models.user import CONTENT_TIER_STANDARD
from app.repositories.script_repository import ScriptRepository
from app.services.errors import ValidationError
from app.services.script_spec import ValidatedScript, validate_script

SEED_DIRECTORY = Path(__file__).resolve().parent.parent / "data" / "scripts"

# 內建劇本允許宣告的最高內容模式。首發劇本宣告 `safe`；內建劇本不宣告成人模式（成人模式需要書面許可與法務覆核，見 AGENTS.md 第 7 節）。
BUILTIN_MAX_TIER = CONTENT_TIER_STANDARD

SEED_KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,58}")          # scripts.seed_key 為 60 字，保守取 59 字
FILE_FIELDS = frozenset({"seed_key", "content"})

SEED_CREATED = "created"
SEED_UPDATED = "updated"
SEED_UNCHANGED = "unchanged"


class SeedDataError(Exception):
    """種子資料不合規。`problems` 的每一項是「檔名：欄位路徑（規則說明）」，不含劇本文字；此時沒有寫入任何資料。"""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("內建劇本資料不合規")
        self.problems = problems


@dataclass(frozen=True)
class SeedEntry:
    file_name: str
    seed_key: str
    validated: ValidatedScript


@dataclass(frozen=True)
class SeedResult:
    seed_key: str
    action: str


@dataclass(frozen=True)
class SeedReport:
    results: tuple[SeedResult, ...]
    dry_run: bool

    def count(self, action: str) -> int:
        return sum(1 for result in self.results if result.action == action)

    @property
    def created(self) -> int:
        return self.count(SEED_CREATED)

    @property
    def updated(self) -> int:
        return self.count(SEED_UPDATED)

    @property
    def unchanged(self) -> int:
        return self.count(SEED_UNCHANGED)


class ScriptSeedService:
    def __init__(
        self, *, scripts: ScriptRepository, clock: Callable[[], datetime], commit: Callable[[], None], rollback: Callable[[], None],
        directory: Path = SEED_DIRECTORY, forbidden_markers: Iterable[str] = (),
    ) -> None:
        self._scripts = scripts
        self._clock = clock
        self._commit = commit
        self._rollback = rollback
        self._directory = directory
        self._markers = tuple(forbidden_markers)

    # ---- 讀取與驗證（沒有任何寫入）-----------------------------------------------------

    def load(self) -> list[SeedEntry]:
        """讀取並驗證全部資料檔。任何一份不合規就丟出 `SeedDataError`（含全部的問題，不只第一個）。"""
        if not self._directory.is_dir():
            raise SeedDataError(["找不到內建劇本的資料夾（映像可能沒有封裝資料檔）"])
        files = sorted(self._directory.glob("*.json"))
        if not files:
            raise SeedDataError(["內建劇本的資料夾裡沒有任何資料檔"])
        problems: list[str] = []
        entries: list[SeedEntry] = []
        seen: dict[str, str] = {}
        for path in files:
            entry = self._load_file(path, problems)
            if entry is None:
                continue
            if entry.seed_key in seen:
                problems.append(f"{path.name}：seed_key 與另一個資料檔重複")
                continue
            seen[entry.seed_key] = path.name
            entries.append(entry)
        if problems:
            raise SeedDataError(problems)
        return entries

    def _load_file(self, path: Path, problems: list[str]) -> SeedEntry | None:
        name = path.name
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            problems.append(f"{name}：無法讀取（需要 UTF-8 文字檔）")
            return None
        try:
            data = json.loads(text)
        except ValueError as error:
            line = getattr(error, "lineno", None)
            problems.append(f"{name}：不是合法的 JSON" + (f"（第 {line} 行）" if line else ""))
            return None
        if not isinstance(data, dict):
            problems.append(f"{name}：最外層必須是物件")
            return None
        before = len(problems)
        for key in data:
            if key not in FILE_FIELDS:
                problems.append(f"{name}：{str(key)[:50]}（規格沒有這個欄位）")
        seed_key = data.get("seed_key")
        if not isinstance(seed_key, str) or not SEED_KEY_PATTERN.fullmatch(seed_key):
            problems.append(f"{name}：seed_key（只能是小寫英數字與連字號，最多 59 字）")
        try:
            validated = validate_script(data.get("content"), max_tier=BUILTIN_MAX_TIER, forbidden_markers=self._markers)
        except ValidationError as error:
            for path_in_content, message in error.fields.items():
                problems.append(f"{name}：content.{path_in_content}（{message}）")
            return None
        if len(problems) != before:
            return None
        return SeedEntry(name, seed_key, validated)

    # ---- 執行 -------------------------------------------------------------------------

    def run(self, *, dry_run: bool = False) -> SeedReport:
        """驗證全部資料，再建立或更新。預覽（`dry_run`）只回報將發生的結果，不寫入也不提交。"""
        entries = self.load()
        plan: list[tuple[SeedEntry, str]] = []
        for entry in entries:
            existing = self._scripts.get_builtin(entry.seed_key)
            if existing is None:
                action = SEED_CREATED
            else:
                _script, live = existing
                same = live is not None and live.content_hash == entry.validated.content_hash and live.spec_version == entry.validated.spec_version
                action = SEED_UNCHANGED if same else SEED_UPDATED
            plan.append((entry, action))
        report = SeedReport(tuple(SeedResult(entry.seed_key, action) for entry, action in plan), dry_run)
        if dry_run:
            return report
        try:
            now = self._clock()
            for entry, action in plan:
                if action != SEED_UNCHANGED:
                    self._scripts.publish_builtin(
                        entry.seed_key, content=entry.validated.content, content_hash=entry.validated.content_hash,
                        total_chars=entry.validated.total_chars, now=now,
                    )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return report
