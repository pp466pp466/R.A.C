"""rpg-game-ui 1.3：前端 `notebookLength`（`game-state.js`）與後端 `notebook_policy.length_of` 的字數必須一致。

記事簿的即時字數（編輯時顯示「已用 / 容量」、超過容量時禁用儲存）由前端算，真正的容量檢查在伺服器；兩邊的算法不同就會出現
「畫面說可以存、伺服器卻說太長」。取樣案例（含組合字、表情符號、控制字元、代理字元）寫死在 `tests/js/fixtures/notebook-length-cases.json`：
Node 測試（`tests/js/game-state.test.js`）檢查前端算出同樣的數字，這個測試檢查**後端**算出同樣的數字。任何一邊的算法改變都會有一個測試失敗。
"""
from __future__ import annotations

import json

import pytest

from app.config import PROJECT_ROOT
from app.services.notebook_policy import length_of

FIXTURE = PROJECT_ROOT / "tests" / "js" / "fixtures" / "notebook-length-cases.json"


def load_cases() -> list[dict[str, object]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8-sig"))


def test_the_fixture_is_not_empty_and_covers_the_hard_cases():
    cases = load_cases()
    names = " ".join(str(case["name"]) for case in cases)
    assert len(cases) >= 15
    for needle in ("combining", "emoji", "control", "surrogate", "crlf", "over the free-tier capacity"):
        assert needle in names, needle


@pytest.mark.parametrize("case", load_cases(), ids=lambda case: str(case["name"]))
def test_the_backend_counts_every_golden_case_the_same_way(case):
    assert length_of(str(case["text"])) == case["length"]


def test_the_fixture_file_is_plain_ascii_json_without_a_byte_order_mark():
    raw = FIXTURE.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    raw.decode("ascii")                                  # 非 ASCII 字元一律以 \\u 跳脫，檔案在任何編碼下都讀得一致
    assert raw.endswith(b"\n") and b"\r" not in raw
