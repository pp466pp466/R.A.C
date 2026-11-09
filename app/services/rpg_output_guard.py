"""RPG 劇情輸出的審查狀態機：逐句、保留一句緩衝（見 rpg-output-review 規格與 rpg-turn-engine 的 design D5、D6）。

**純狀態機**：輸入模型串流來的文字片段，輸出「可以釋放給前端的文字」或「命中（類別）」；沒有資料庫、網路、時間與隨機的依賴，不匯入資料模型、
Repository 或 Flask。判定本身在 `rpg_output_rules`。

流程：文字累積在 `tail`；每當出現句子結尾（`。！？!?；;` 與換行），一句完成。一句通過檢查後，**前一句才釋放**，這一句成為新的「保留句」；
串流結束（`finish`）時再檢查尾端並釋放最後一句。所以「前一句十五歲、後一句才露骨」時，前一句尚未釋放、不會送達。
不論片段怎麼切（一次一個字或整段），釋放的文字與判定都相同（以測試鎖住）。

命中時（`GuardResult.categories` 非空）：已確定可釋放的句子隨該次結果一併回傳，保留句與尾端被丟棄，之後的 `feed`／`finish` 一律不再釋放任何文字。
呼叫端（`TurnRunner`）負責關閉上游串流、送出轉場事件；`received_text` 是迄今收到的全部文字（含命中的那句），供被擋回合暫存 14 天。
洩漏標記字串與內部設定（階段的英文名稱、資料區塊標題）在尚未成句時就檢查（尾端每次都檢查），所以不必等到句子結束；因此同時命中洩漏與規則時，
回報的類別可能因片段切法而不同（先偵測到者），但「是否擋下」與已釋放的文字相同。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.services.content_tier import parse_tier
from app.services.rpg_output_rules import RuleOutcome, evaluate, leak_categories
from app.services.rpg_world import CharacterAge
from app.services.script_rules import DEFAULT_ADULT_AGE, SENTENCE_BREAKS, Lexicon, load_lexicon


@dataclass(frozen=True)
class GuardResult:
    """一次 `feed`／`finish` 的結果：`release` 是可以送給前端的文字（可能為空）；`categories` 非空代表命中，這個回合要擋下。"""

    release: str = ""
    categories: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.categories)


class OutputGuard:
    def __init__(
        self, *, tier: str, characters: Iterable[CharacterAge], marker: str, adult_age: int = DEFAULT_ADULT_AGE, lexicon: Lexicon | None = None,
    ) -> None:
        self._tier = parse_tier(tier)                # 無法判定的內容模式視為最保守的安全模式
        self._characters = tuple(characters)
        self._marker = marker
        self._adult_age = adult_age
        self._lexicon = lexicon or load_lexicon()
        self._received: list[str] = []
        self._tail = ""                            # 尚未成句的尾端（也可能含剛收到、還沒切開的完整句）
        self._held: str | None = None              # 保留句：已通過檢查、等下一句通過後才釋放
        self._paragraph: list[str] = []            # 自上一個換行以來的句子（含已釋放與保留句）
        self._noted: set[str] = set()
        self._hit: tuple[str, ...] = ()
        self._finished = False

    # ---- 狀態 ----

    @property
    def hit(self) -> tuple[str, ...]:
        """命中的類別（排序）；沒有命中為空元組。"""
        return self._hit

    @property
    def noted_categories(self) -> tuple[str, ...]:
        """放行但要記錄的類別（目前只有 `other_concern`）。"""
        return tuple(sorted(self._noted))

    @property
    def received_text(self) -> str:
        """迄今收到的全部文字（含尚未釋放與命中的那句）。"""
        return "".join(self._received)

    # ---- 輸入 ----

    def feed(self, chunk: str) -> GuardResult:
        if self._hit or self._finished or not chunk:
            return GuardResult("", self._hit)
        self._received.append(chunk)
        self._tail += chunk
        out: list[str] = []
        while True:
            match = SENTENCE_BREAKS.search(self._tail)
            if match is None:
                break
            unit, self._tail = self._tail[:match.end()], self._tail[match.end():]
            if self._process(unit, out):
                return GuardResult("".join(out), self._hit)
        leaks = leak_categories(self._tail, self._marker)                # 尚未成句的尾端也檢查，不必等到句子結束
        if leaks:
            self._block(leaks)
            return GuardResult("".join(out), self._hit)
        return GuardResult("".join(out), ())

    def finish(self) -> GuardResult:
        """串流正常結束：檢查尾端並釋放最後一句。可重複呼叫（之後不再釋放任何文字）。"""
        if self._hit or self._finished:
            return GuardResult("", self._hit)
        out: list[str] = []
        tail, self._tail = self._tail, ""
        if tail and self._process(tail, out):
            return GuardResult("".join(out), self._hit)
        if self._held is not None:
            out.append(self._held)
            self._held = None
        self._finished = True
        return GuardResult("".join(out), ())

    # ---- 內部 ----

    def _block(self, categories: tuple[str, ...]) -> None:
        self._hit = tuple(sorted(categories))
        self._held = None
        self._tail = ""

    def _process(self, unit: str, out: list[str]) -> bool:
        """處理一個單位；命中時回傳 True（`out` 保留命中之前已確定可釋放的文字）。"""
        if not any(char.isalnum() for char in unit):             # 只有標點或空白：依附在保留句後面，不參與判定
            if self._held is not None:
                self._held += unit
            else:
                out.append(unit)
            if "\n" in unit:
                self._paragraph = []
            return False
        leaks = leak_categories(unit, self._marker)
        if leaks:
            self._block(leaks)
            return True
        paragraph = [*self._paragraph, unit]
        outcome: RuleOutcome = evaluate(
            paragraph, self._held, tier=self._tier, characters=self._characters, adult_age=self._adult_age, lexicon=self._lexicon,
        )
        self._noted.update(outcome.noted)
        if outcome.blocked:
            self._block(outcome.blocked)
            return True
        if self._held is not None:
            out.append(self._held)
        self._held = unit
        self._paragraph = [] if "\n" in unit else paragraph
        return False
