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

詞表另有 `romantic_relation`（戀愛與婚姻關係的用語）、`betrothal`（婚約類的用語）與 `marriage_relation`（婚姻與配偶類，`romantic_relation` 的子集）三個群組，
**不屬於本模組的判定**：它們只供玩家主角的錨定事實守衛使用
（`protagonist.validate_anchor`，見 rpg-protagonist 規格），所以改動它不會改變任何劇本的審查結果。
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

# 公開 API（劇本審查與 RPG 輸出審查共用，行為以本檔為準）：`SENTENCE_BREAKS`（切句符號）、`fold_text`（摺疊）、`split_sentences`
# （切句並摺疊）、`parse_age_number`／`AGE_PATTERN`／`mentions_minor_age`（年齡數字）、`matches_any`（詞表比對）。
SENTENCE_BREAKS = re.compile(r"[。！？!?；;\n]+")
# 拆字用的符號：夾在詞中間的這些字元會被去除（逗號、頓號等真正的標點保留，句內的詞仍可能共現）
_SPLITTERS = re.compile(r"[\s*·・._\-—~`'\"|/\\]+")
AGE_PATTERN = re.compile(r"([0-9]{1,2}|[零〇一二兩三四五六七八九十]{1,3})(?:歲|岁)")
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
    romantic_relation: tuple[Term, ...]    # 只供玩家主角的錨定事實守衛使用（未成年不得有戀愛或婚姻的聯繫）；`check_rules` 不使用
    betrothal: tuple[Term, ...]            # 同上；純婚約類（娃娃親、指腹為婚、未婚夫妻…），只在成年與未成年之間才擋
    marriage_relation: tuple[Term, ...]    # 同上；婚姻與配偶類（結婚、成親、夫妻、童養媳…）：romantic_relation 的子集，兩位都未成年時只有它仍擋


def fold_text(text: str) -> str:
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
            text = fold_text(spec["term"])
            if text:
                result.append(Term(
                    text, frozenset(fold_text(char) for char in spec.get("not_after", ())), frozenset(fold_text(char) for char in spec.get("not_before", ())),
                ))
        return tuple(result)

    return Lexicon(
        version=str(data["version"]), sexual_explicit=terms("sexual_explicit"), sexual_suggestive=terms("sexual_suggestive"),
        minor=terms("minor"), rape=terms("rape"), sexual_violence=terms("sexual_violence"), non_consent=terms("non_consent"),
        real_person=terms("real_person"), romantic_relation=terms("romantic_relation"), betrothal=terms("betrothal"),
        marriage_relation=terms("marriage_relation"),
    )


def parse_age_number(text: str) -> int | None:
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


def mentions_minor_age(sentence: str, adult_age: int) -> bool:
    """一句話是否明確寫出小於 `adult_age` 的年齡（「十五歲」「16歲」）。`sentence` 應已摺疊（`fold_text`）；純函式。"""
    for match in AGE_PATTERN.finditer(sentence):
        value = parse_age_number(match.group(1))
        if value is not None and value < adult_age:
            return True
    return False


def matches_any(terms: Iterable[Term], sentence: str) -> bool:
    """一句話（已摺疊）是否含詞表群組中的任一詞（含「前一個字／後一個字」的排除）。純函式，不回傳命中的詞。"""
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


def split_sentences(texts: Iterable[str]) -> list[str]:
    """每段文字各自以 `SENTENCE_BREAKS` 切句，並逐句摺疊（`fold_text`）；摺疊後為空的句子略過。不跨段串接。"""
    folded = []
    for text in texts:
        for part in SENTENCE_BREAKS.split(text):
            compact = fold_text(part)
            if compact:
                folded.append(compact)
    return folded


def mentions_romantic_relation(text: str, *, lexicon: Lexicon | None = None) -> bool:
    """一句話是否含戀愛或婚姻關係的用語（詞表群組 `romantic_relation`；玩家主角的錨定事實守衛使用）。純函式，不回傳命中的詞。"""
    lex = lexicon or load_lexicon()
    return any(matches_any(lex.romantic_relation, sentence) for sentence in split_sentences([text]))


def mentions_betrothal(text: str, *, lexicon: Lexicon | None = None) -> bool:
    """一句話是否含婚約類的用語（詞表群組 `betrothal`：娃娃親、指腹為婚、訂婚、未婚夫妻…）。古代的婚約是歷史題材常見的背景設定，
    所以它和戀愛關係分開判定（玩家主角的錨定事實守衛只在成年與未成年之間才擋）。純函式，不回傳命中的詞。"""
    lex = lexicon or load_lexicon()
    return any(matches_any(lex.betrothal, sentence) for sentence in split_sentences([text]))


def mentions_marriage(text: str, *, lexicon: Lexicon | None = None) -> bool:
    """一句話是否含婚姻與配偶類的用語（詞表群組 `marriage_relation`：結婚、成親、夫妻、童養媳…，是 `romantic_relation` 的子集）。
    玩家主角的錨定事實守衛用它區分「戀愛」與「婚姻」：兩位都未成年時允許戀愛、不允許婚姻。純函式，不回傳命中的詞。"""
    lex = lexicon or load_lexicon()
    return any(matches_any(lex.marriage_relation, sentence) for sentence in split_sentences([text]))


def mentions_forbidden_relation(text: str, *, lexicon: Lexicon | None = None) -> bool:
    """一句話是否含性奴等禁斷關係的用語（詞表群組 `sexual_violence`：性奴、性虐、強暴、凌辱…）。玩家主角的錨定事實不論年齡都不得是這類關係。
    純函式，不回傳命中的詞。"""
    lex = lexicon or load_lexicon()
    return any(matches_any(lex.sexual_violence, sentence) for sentence in split_sentences([text]))


def mentions_sexual(text: str, *, lexicon: Lexicon | None = None) -> bool:
    """一句話是否含性相關的用語（詞表群組 `sexual_explicit` 或 `sexual_suggestive`）。純函式，不回傳命中的詞。"""
    lex = lexicon or load_lexicon()
    return any(matches_any(lex.sexual_explicit, s) or matches_any(lex.sexual_suggestive, s) for s in split_sentences([text]))


def check_rules(content: dict[str, Any], *, tier: str, adult_age: int = DEFAULT_ADULT_AGE, lexicon: Lexicon | None = None) -> RulesResult:
    """對標準化後的劇本內容做規則層審查。`tier` 是劇本宣告的內容模式；`adult_age` 是成年門檻（由年齡規則提供）。"""
    return _evaluate(
        _text_fields(content), content.get("characters") or [], tier=tier, adult_age=adult_age, lex=lexicon or load_lexicon(),
        extra_own_texts=(), minor_explicit_rejects=False,
    )


def check_protagonist_rules(
    character: dict[str, Any], anchor_text: str | None, intro: str | None, *, tier: str, adult_age: int = DEFAULT_ADULT_AGE,
    lexicon: Lexicon | None = None,
) -> RulesResult:
    """對玩家主角做規則層審查（rpg-protagonist 規格）：角色卡的文字欄位、錨定事實與自我介紹各自切句檢查，不跨欄位串接。

    與劇本共用同一個引擎，所以四類紅線、誤擋防線與詞表版本都一樣。**唯一更嚴的地方**：主角年齡低於成年門檻，而主角自己的文字
    （角色卡、錨定事實、自我介紹）出現露骨的性用語時直接拒絕 `minor_sexual`——劇本只標示（`minor_suspected`），因為劇本有人工複審，
    主角沒有人工審核佇列，而無論模式一律禁止涉及未成年角色的性內容（AGENTS.md 第 7 節）。
    """
    own_extra = [text for text in (anchor_text, intro) if isinstance(text, str) and text]
    return _evaluate(
        _character_texts(character) + own_extra, [character], tier=tier, adult_age=adult_age, lex=lexicon or load_lexicon(),
        extra_own_texts=own_extra, minor_explicit_rejects=True,
    )


def check_text_rules(texts: Iterable[str], *, tier: str, adult_age: int = DEFAULT_ADULT_AGE, lexicon: Lexicon | None = None) -> RulesResult:
    """對一串自由文字（例如玩家的自我介紹）做規則層審查：沒有角色年齡，其餘判定與劇本相同。每段各自切句，不跨段串接。"""
    return _evaluate(
        [text for text in texts if isinstance(text, str) and text], [], tier=tier, adult_age=adult_age, lex=lexicon or load_lexicon(),
        extra_own_texts=(), minor_explicit_rejects=False,
    )


def _evaluate(
    texts: Iterable[str], characters: Iterable[dict[str, Any]], *, tier: str, adult_age: int, lex: Lexicon,
    extra_own_texts: Iterable[str], minor_explicit_rejects: bool,
) -> RulesResult:
    """規則層的核心。`texts` 是會被模型讀到的文字（每段各自切句）；`characters` 是有年齡的角色；`extra_own_texts` 是主角額外的自述
    （錨定事實、自我介紹），計入該角色自己的文字。"""
    rejected: set[str] = set()
    flagged: set[str] = set()
    saw_explicit = saw_suggestive = saw_real_person = False

    for sentence in split_sentences(texts):
        explicit = matches_any(lex.sexual_explicit, sentence)
        suggestive = matches_any(lex.sexual_suggestive, sentence)
        real_person = matches_any(lex.real_person, sentence)
        saw_explicit |= explicit
        saw_suggestive |= suggestive
        saw_real_person |= real_person
        if explicit and (matches_any(lex.minor, sentence) or mentions_minor_age(sentence, adult_age)):
            rejected.add(CATEGORY_MINOR_SEXUAL)
        if explicit and (matches_any(lex.rape, sentence) or matches_any(lex.non_consent, sentence)):
            rejected.add(CATEGORY_NON_CONSENSUAL)
        elif matches_any(lex.rape, sentence):               # 強姦用語單獨出現（犯罪劇情、新聞、防治課）不直接拒絕，標示後交給模型層與人工
            flagged.add(CATEGORY_OTHER_CONCERN)
        if matches_any(lex.sexual_violence, sentence):
            if explicit:
                rejected.add(CATEGORY_SEXUAL_VIOLENCE)
            else:
                flagged.add(CATEGORY_OTHER_CONCERN)
        if explicit and real_person:
            rejected.add(CATEGORY_REAL_PERSON_SEXUAL)

    extra = list(extra_own_texts)
    if minor_explicit_rejects:
        for character in characters:
            age = character.get("age")
            if isinstance(age, int) and not isinstance(age, bool) and age < adult_age:
                own = split_sentences(_character_texts(character) + extra)
                if any(matches_any(lex.sexual_explicit, s) for s in own):
                    rejected.add(CATEGORY_MINOR_SEXUAL)

    if rejected:
        return RulesResult(REVIEW_REJECT, tuple(sorted(rejected)), lex.version)

    if tier in (CONTENT_TIER_STANDARD, CONTENT_TIER_ADULT):
        for character in characters:
            age = character.get("age")
            if isinstance(age, int) and not isinstance(age, bool) and age < adult_age:
                own = split_sentences(_character_texts(character) + extra)
                if any(matches_any(lex.sexual_explicit, s) or matches_any(lex.sexual_suggestive, s) for s in own):
                    flagged.add(CATEGORY_MINOR_SUSPECTED)
    if (saw_explicit or saw_suggestive) and saw_real_person:
        flagged.add(CATEGORY_REAL_PERSON_SUSPECTED)
    if (tier == CONTENT_TIER_SAFE and (saw_explicit or saw_suggestive)) or (tier == CONTENT_TIER_STANDARD and saw_explicit):
        flagged.add(CATEGORY_TIER_MISMATCH)

    if flagged:
        return RulesResult(REVIEW_FLAG, tuple(sorted(flagged)), lex.version)
    return RulesResult(REVIEW_PASS, (), lex.version)
