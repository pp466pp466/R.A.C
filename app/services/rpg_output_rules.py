"""RPG 劇情輸出的逐句規則（見 rpg-output-review 規格與 rpg-turn-engine 的 design D5；`rpg_output_guard` 的判定核心）。

**純函式**：沒有資料庫、網路、時間與隨機的依賴，不匯入資料模型、Repository 或 Flask。建立在 `script_rules` 的詞表與年齡解析上，不複製任何詞。
結果只有類別列舉，**不含命中的詞、句子或角色名稱**。

一個「單位」是一句（以 `。！？!?；;` 或換行結尾，結尾符號算在句內）；**同段**是自上一個換行以來的所有句；**相鄰句**是前一句與下一句
（緩衝正好看得到的範圍，不受換行影響）。判定（任一成立即擋，不論內容模式）：

| 判定 | 命中條件 | 類別 |
|---|---|---|
| 逐句規則層 | 對「同段累計」跑 `check_text_rules`：必須拒絕的類別、`tier_mismatch`、`minor_suspected`、`real_person_suspected` | 規則層的類別 |
| 條件①年齡紀錄 | 同段出現某位**目前年齡紀錄小於成年門檻**的角色名稱（含主角的姓名），且同段有性內容 | `minor_sexual` |
| 條件②文字線索 | 同段或前一句含明確的未成年用語（詞表 `minor` 或「十五歲」之類的年齡），且同段或前一句有性內容 | `minor_sexual` |
| 洩漏設定 | 文字含階段的英文名稱或資料區塊標題 | `setting_leak` |
| 標記字串 | 文字含系統指令的不公開標記 | `marker_leak` |

單獨出現的暴力或犯罪用語（`other_concern`）放行，只回報類別供記錄。**年齡未知的角色不構成任何條件**（任何模式，含成人模式的露骨段落；使用者 2026-11-09 確認：只擋明確未成年的角色）；「嬌小」「娃娃臉」之類的外貌描述不在未成年詞表中。
性內容 = 詞表的 `sexual_explicit` 或 `sexual_suggestive`（擁抱、牽手、簡短親吻不在詞表中）。

已知限制（列入驗收量測，不假裝沒有）：只以名稱判定，純代名詞的句子（「她」）看不出是誰，只靠同段累計與條件②補一部分；單字的名稱可能誤中其他詞；
與性內容相隔一句以上且已釋放的文字無法收回。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Iterable, Sequence

from app.services.rpg_prompt import LEAK_SETTING_PATTERNS
from app.services.rpg_world import CharacterAge
from app.services.script_rules import (
    CATEGORY_MINOR_SEXUAL, CATEGORY_OTHER_CONCERN, DEFAULT_ADULT_AGE, REVIEW_FLAG, REVIEW_REJECT, Lexicon, check_text_rules, fold_text,
    load_lexicon, matches_any, mentions_minor_age,
)

CATEGORY_SETTING_LEAK = "setting_leak"
CATEGORY_MARKER_LEAK = "marker_leak"


@dataclass(frozen=True)
class RuleOutcome:
    blocked: tuple[str, ...]          # 擋下的類別（排序、不重複）；空代表放行
    noted: tuple[str, ...]            # 放行但要記錄的類別（`other_concern`）


def _has_sexual(folded: str, lex: Lexicon) -> bool:
    return matches_any(lex.sexual_explicit, folded) or matches_any(lex.sexual_suggestive, folded)


def _has_minor_cue(folded: str, lex: Lexicon, adult_age: int) -> bool:
    return matches_any(lex.minor, folded) or mentions_minor_age(folded, adult_age)


def leak_categories(text: str, marker: str) -> tuple[str, ...]:
    """文字是否洩漏了系統指令的標記字串或內部設定（階段的英文名稱、資料區塊標題）。比對前做 NFKC 與不分大小寫；標記另去除空白與格式字元。"""
    categories = []
    if marker and marker.lower() in fold_text(text):
        categories.append(CATEGORY_MARKER_LEAK)
    normalized = unicodedata.normalize("NFKC", text).casefold()
    if any(pattern.search(normalized) for pattern in LEAK_SETTING_PATTERNS):
        categories.append(CATEGORY_SETTING_LEAK)
    return tuple(sorted(categories))


def evaluate(
    paragraph: Sequence[str], previous: str | None, *, tier: str, characters: Iterable[CharacterAge], adult_age: int = DEFAULT_ADULT_AGE,
    lexicon: Lexicon | None = None,
) -> RuleOutcome:
    """判定「同段累計」（`paragraph` 是自上一個換行以來的所有句，最後一句是剛完成的新句）。`previous` 是新句的前一句（可在上一段）。"""
    lex = lexicon or load_lexicon()
    blocked: set[str] = set()
    noted: set[str] = set()

    result = check_text_rules(list(paragraph), tier=tier, adult_age=adult_age, lexicon=lex)
    if result.verdict == REVIEW_REJECT:
        blocked.update(result.categories)
    elif result.verdict == REVIEW_FLAG:
        blocked.update(category for category in result.categories if category != CATEGORY_OTHER_CONCERN)
        noted.update(category for category in result.categories if category == CATEGORY_OTHER_CONCERN)

    folded = [fold_text(sentence) for sentence in paragraph]
    folded_previous = fold_text(previous) if previous else ""
    sexual_in_paragraph = any(_has_sexual(item, lex) for item in folded)
    sexual_in_previous = bool(folded_previous) and _has_sexual(folded_previous, lex)
    if sexual_in_paragraph:                                                              # 條件①：年齡紀錄
        text = "".join(folded)
        for character in characters:
            if character.age is not None and character.age < adult_age:
                name = fold_text(character.name)
                if name and name in text:
                    blocked.add(CATEGORY_MINOR_SEXUAL)
    if sexual_in_paragraph or sexual_in_previous:                                        # 條件②：文字線索
        window = [*folded, folded_previous] if folded_previous else folded
        if any(_has_minor_cue(item, lex, adult_age) for item in window):
            blocked.add(CATEGORY_MINOR_SEXUAL)
    return RuleOutcome(tuple(sorted(blocked)), tuple(sorted(noted)))
