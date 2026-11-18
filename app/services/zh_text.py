"""模型輸出的繁體中文保證：把簡體字轉成繁體（台灣慣用詞）。

AGENTS.md 第 8 節要求全系統的內容一律是繁體中文。提示詞已經要求模型用繁體（`rpg_prompt.LANGUAGE_INSTRUCTIONS`），但免費模型不一定遵守，
所以伺服器端在文字送達玩家、寫入資料庫之前再轉一次，不依賴模型守規矩：

- 用 OpenCC 的 `s2twp`（簡體轉台灣繁體，連同慣用詞：軟件→軟體、鼠標→滑鼠）；套件是純 Python、離線可用，只在伺服器端執行。
- **只在文字裡真的有簡體字時才轉換**：純繁體的句子原樣保留（轉換器會把繁體異體字「祕」改成「秘」、把「咸陽」改成「鹹陽」，不能動已經正確的繁體）。
  判斷用 `s2t`（只換字、不換慣用詞）：有字被換掉就是有簡體字；但「里、咸、台、余、后、于…」這類在繁體裡也是正確用字的字，單獨出現
  不算證據（見 `_BOTH_FORMS`），所以「里長」「咸陽」「台灣」不會被誤判。
- 只改 CJK 字元；ASCII、標點、JSON 結構與標記字串不受影響，所以轉換後的 JSON 仍可解析。
- 純函式、沒有狀態，可在多執行緒下使用。

套用的地方（見 `rpg_output_guard.OutputGuard._process` 與 `llm_service.run_structured`）：RPG 的劇情串流（逐句、在洩漏與規則層檢查**之前**轉換，
所以規則層看到的是繁體）、狀態呼叫、記事簿抓取與主角生成的結構化輸出。客服（Gemini）不經過這裡。
"""
from __future__ import annotations

import re

from opencc import OpenCC

_CJK = re.compile(r"[㐀-鿿]")
_S2T = OpenCC("s2t")                 # 偵測用：只換字
_S2TWP = OpenCC("s2twp")             # 轉換用：換字加台灣慣用詞

# 簡體與繁體都有、在繁體裡也是正確用字的字：單獨被 s2t 換掉不能當作「這是簡體」的證據（里長、咸陽、台灣、余姓、曲、皇后、于謙、面子、只…）。
# 句子裡若還有別的簡體字（例如「咸阳」的「阳」），整句仍會轉換，這些字由轉換器依上下文處理。
_BOTH_FORMS = frozenset("里咸余曲台干后于面只系范周征折谷采郁卜象松向制冲划合出复")


def _has_simplified(text: str) -> bool:
    converted = _S2T.convert(text)
    if len(converted) != len(text):                          # s2t 是逐字對應；長度不同代表有詞組被改寫，保守地當作有簡體字
        return True
    return any(old != new and old not in _BOTH_FORMS for old, new in zip(text, converted))


def to_traditional(text: str) -> str:
    """簡體轉台灣繁體；沒有 CJK 字元、或沒有簡體字的文字原樣回傳。"""
    if not text or not _CJK.search(text) or not _has_simplified(text):
        return text
    return _S2TWP.convert(text)
