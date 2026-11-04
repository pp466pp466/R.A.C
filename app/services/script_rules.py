"""劇本設定的規則層審查（見 script-review 規格與 rpg-script-authoring 的 design D5）。

純函式：不碰資料庫、網路與模型，相同輸入永遠得到相同輸出。詞表是版本化的資料檔（`app/data/review/lexicon.json`，UTF-8），
程式不內嵌任何詞；結果記錄詞表版本。

**只在「同一句」內共現時才直接拒絕**（`reject`）：
- 性相關詞與明確的未成年詞（或小於成年門檻的年齡數字）同句 -> `minor_sexual`；
- 性相關詞與性暴力專用詞同句 -> `sexual_violence`；性相關詞與非自願用語（或強姦用語）同句 -> `non_consensual`；
- 性相關詞與真人用語同句 -> `real_person_sexual`。
一般的未成年角色（帶著三歲寶寶的成年主角、校園日常、親子與育兒）不含性相關詞，所以不會命中；更寬鬆的跨句判斷只標示（`flag`，
進人工），交給模型層與人工審核：宣告一般或成人模式的劇本裡，年齡小於成年門檻的角色其文字出現性相關詞（`minor_suspected`）、
性相關詞與真人用語分散在不同句（`real_person_suspected`）、宣告安全模式卻出現性相關詞，或宣告一般模式卻出現露骨的用語
（`tier_mismatch`）、性暴力或強姦用語單獨出現（`other_concern`）。

**直接拒絕的主軸是「露骨的性用語與未成年用語同句」（不分先後）**；其餘的組合都要求同一句裡有露骨的性用語，單獨出現的暴力用語
（例如犯罪劇情、新聞、性侵害防治課）只標示。

比對前先去除空白、零寬字元與常見的拆字符號並做字元摺疊（NFKC、不分大小寫），所以以空白或全形字拆開的詞仍會命中。
結果與日誌只有類別列舉，**不含命中的詞、句子或欄位**。
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from app.models.script import (
    CATEGORY_MINOR_SEXUAL, CATEGORY_MINOR_SUSPECTED, CATEGORY_NON_CONSENSUAL, CATEGORY_OTHER_CONCERN, CATEGORY_REAL_PERSON_SEXUAL,
    CATEGORY_REAL_PERSON_SUSPECTED, CATEGORY_SEXUAL_VIOLENCE, CATEGORY_TIER_MISMATCH, REVIEW_FLAG, REVIEW_PASS, REVIEW_REJECT,
)
from app.models.user import CONTENT_TIER_ADULT, CONTENT_TIER_SAFE, CONTENT_TIER_STANDARD

LEXICON_PATH = Path(__file__).resolve().parent.parent / "data" / "review" / "lexicon.json"
DEFAULT_ADULT_AGE = 18

_SENTENCE_SPLIT = re.compile(r"[。！？!?；;\n]+")
# 拆字用的符號：夾在詞中間的這些字元會被去除（逗號、頓號等真正的標點保留，句內的詞仍可能共現）
_SPLITTERS = re.compile(r"[\s*·・._\-—~`'\"|/\\]+")
_AGE = re.compile(r"([0-9]{1,2}|[零〇一二兩三四五六七八九十]{1,3})(?:歲|岁)")
_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


@dataclass(frozen=True)
class RulesResult:
    verdict: str                          # REVIEW_PASS、REVIEW_FLAG 或 REVIEW_REJECT
    categories: tuple[str, ...]           # 固定列舉；`reject` 時只有可拒絕的類別，`flag` 時是疑慮的類別
    lexicon_version: str


@dataclass(frozen=True)
class Term:
    """詞表裡的一個詞。`not_after`／`not_before` 列出「前一個字」與「後一個字」的排除：例如「性愛」前面是「生」「天」「個」時是
    「生性愛玩」「天性愛哭」之類的一般句子，不算命中。排除的字也先摺疊好。"""

    text: str
    not_after: frozenset[str] = frozenset()
    not_before: frozenset[str] = frozenset()

    def found_in(self, sentence: str) -> bool:
        start = sentence.find(self.text)
        while start != -1:
            end = start + len(self.text)
            previous = sentence[start - 1] if start > 0 else ""
            following = sentence[end] if end < len(sentence) else ""
            if previous not in self.not_after and following not in self.not_before:
                return True
            start = sentence.find(self.text, start + 1)
        return False


@dataclass(frozen=True)
class Lexicon:
    version: str
    sexual_explicit: tuple[Term, ...]
    sexual_suggestive: tuple[Term, ...]
    minor: tuple[Term, ...]
    rape: tuple[Term, ...]
    sexual_violence: tuple[Term, ...]
    non_consent: tuple[Term, ...]
    real_person: tuple[Term, ...]


def _fold(text: str) -> str:
    """比對用的形式：NFKC、不分大小寫，去除格式字元、空白與拆字符號。"""
    folded = unicodedata.normalize("NFKC", text).casefold()
    folded = "".join(char for char in folded if unicodedata.category(char) not in ("Cf", "Cc"))
    return _SPLITTERS.sub("", folded)


@lru_cache(maxsize=1)
def load_lexicon(path: Path = LEXICON_PATH) -> Lexicon:
    """讀取詞表（UTF-8，可含 BOM）。詞在載入時就摺疊好，比對時不必每次重算。"""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    groups = data["groups"]

    def terms(name: str) -> tuple[Term, ...]:
        result = []
        for entry in groups[name]:
            spec = entry if isinstance(entry, dict) else {"term": entry}
            text = _fold(spec["term"])
            if text:
                result.append(Term(
                    text, frozenset(_fold(char) for char in spec.get("not_after", ())), frozenset(_fold(char) for char in spec.get("not_before", ())),
                ))
        return tuple(result)

    return Lexicon(
        version=str(data["version"]), sexual_explicit=terms("sexual_explicit"), sexual_suggestive=terms("sexual_suggestive"),
        minor=terms("minor"), rape=terms("rape"), sexual_violence=terms("sexual_violence"), non_consent=terms("non_consent"),
        real_person=terms("real_person"),
    )


def _number(text: str) -> int | None:
    """年齡數字：阿拉伯數字或一至九十九的中文數字；看不懂回傳 None。"""
    if text.isdigit():
        return int(text)
    if "十" in text:
        before, _, after = text.partition("十")
        tens = 1 if before == "" else _DIGITS.get(before)
        ones = 0 if after == "" else _DIGITS.get(after)
        if tens is None or ones is None or len(before) > 1 or len(after) > 1:
            return None
        return tens * 10 + ones
    if len(text) == 1 and text in _DIGITS:
        return _DIGITS[text]
    return None


def _has_minor_age(sentence: str, adult_age: int) -> bool:
    for match in _AGE.finditer(sentence):
        value = _number(match.group(1))
        if value is not None and value < adult_age:
            return True
    return False


def _any(terms: Iterable[Term], sentence: str) -> bool:
    return any(term.found_in(sentence) for term in terms)


def _text_fields(content: dict[str, Any]) -> list[str]:
    """內容裡所有會被模型讀到的文字欄位（每個欄位各自一段，不跨欄位串接）。"""
    values: list[Any] = [
        content.get("title"), content.get("summary"), content.get("world_name"), content.get("background"), content.get("opening"),
        content.get("rules"), content.get("ending_text"),
    ]
    values.extend(content.get("tags") or [])
    values.extend(content.get("locations") or [])
    calendar = content.get("calendar") or {}
    values.append(calendar.get("name"))
    for milestone in content.get("milestones") or []:
        values.append(milestone.get("title"))
    for character in content.get("characters") or []:
        values.extend(_character_texts(character))
    return [value for value in values if isinstance(value, str) and value]


def _character_texts(character: dict[str, Any]) -> list[str]:
    keys = ("name", "personality", "appearance", "occupation", "speech_style", "hidden_trait")
    return [character[key] for key in keys if isinstance(character.get(key), str) and character[key]]


def _sentences(texts: Iterable[str]) -> list[str]:
    folded = []
    for text in texts:
        for part in _SENTENCE_SPLIT.split(text):
            compact = _fold(part)
            if compact:
                folded.append(compact)
    return folded


def check_rules(content: dict[str, Any], *, tier: str, adult_age: int = DEFAULT_ADULT_AGE, lexicon: Lexicon | None = None) -> RulesResult:
    """對標準化後的劇本內容做規則層審查。`tier` 是劇本宣告的內容模式；`adult_age` 是成年門檻（由年齡規則提供）。"""
    lex = lexicon or load_lexicon()
    rejected: set[str] = set()
    flagged: set[str] = set()
    saw_explicit = saw_suggestive = saw_real_person = False

    for sentence in _sentences(_text_fields(content)):
        explicit = _any(lex.sexual_explicit, sentence)
        suggestive = _any(lex.sexual_suggestive, sentence)
        real_person = _any(lex.real_person, sentence)
        saw_explicit |= explicit
        saw_suggestive |= suggestive
        saw_real_person |= real_person
        if explicit and (_any(lex.minor, sentence) or _has_minor_age(sentence, adult_age)):
            rejected.add(CATEGORY_MINOR_SEXUAL)
        if explicit and (_any(lex.rape, sentence) or _any(lex.non_consent, sentence)):
            rejected.add(CATEGORY_NON_CONSENSUAL)
        elif _any(lex.rape, sentence):               # 強姦用語單獨出現（犯罪劇情、新聞、防治課）不直接拒絕，標示後交給模型層與人工
            flagged.add(CATEGORY_OTHER_CONCERN)
        if _any(lex.sexual_violence, sentence):
            if explicit:
                rejected.add(CATEGORY_SEXUAL_VIOLENCE)
            else:
                flagged.add(CATEGORY_OTHER_CONCERN)
        if explicit and real_person:
            rejected.add(CATEGORY_REAL_PERSON_SEXUAL)

    if rejected:
        return RulesResult(REVIEW_REJECT, tuple(sorted(rejected)), lex.version)

    if tier in (CONTENT_TIER_STANDARD, CONTENT_TIER_ADULT):
        for character in content.get("characters") or []:
            age = character.get("age")
            if isinstance(age, int) and age < adult_age:
                own = _sentences(_character_texts(character))
                if any(_any(lex.sexual_explicit, s) or _any(lex.sexual_suggestive, s) for s in own):
                    flagged.add(CATEGORY_MINOR_SUSPECTED)
    if (saw_explicit or saw_suggestive) and saw_real_person:
        flagged.add(CATEGORY_REAL_PERSON_SUSPECTED)
    if (tier == CONTENT_TIER_SAFE and (saw_explicit or saw_suggestive)) or (tier == CONTENT_TIER_STANDARD and saw_explicit):
        flagged.add(CATEGORY_TIER_MISMATCH)

    if flagged:
        return RulesResult(REVIEW_FLAG, tuple(sorted(flagged)), lex.version)
    return RulesResult(REVIEW_PASS, (), lex.version)
