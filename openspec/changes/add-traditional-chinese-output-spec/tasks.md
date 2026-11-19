# Tasks

> 這個 change 只寫規格，不改程式與行為。實作與大部分測試已存在（`app/services/zh_text.py`、`tests/test_zh_text.py`）；tasks 是確認規格與程式一致、補上缺的測試、更新文件。

## 1. 規格與現有測試對照

- [x] 1.1 逐一對照規格的 18 個情境與 `tests/test_zh_text.py`：每個情境列出對應的測試名稱；預期只有「含英文單字的繁體句原樣送達」沒有測試。對照結果記在本檔最下方的「對照結果」。若發現規格寫的行為與程式實際不符，以程式與測試為準修正規格，不改程式。
- [x] 1.2 補缺的測試（`tests/test_zh_text.py`）：含英文單字的繁體句（例如「我感覺你需要 help」「他說了聲 okay 就走了」）經過轉換原樣不變；劇情串流（不同片段切法）與結構化輸出各驗證一次。驗證：暫時讓轉換吃掉 ASCII 字母，確認新測試失敗，再還原並比對。
- [x] 1.3 執行 `tests/test_zh_text.py` 與相關測試（`test_rpg_output_guard`、`test_llm_service`、`test_rpg_prompt`），全部通過；`openspec validate add-traditional-chinese-output-spec --strict` 通過。

## 2. 文件

- [x] 2.1 更新 `docs/模型呼叫層-llm-layer.md` 第 12 節（加上對照規格 `rpg-output-traditional-chinese` 的說明，標頭「對應 change」加入這個 change），並更新 `docs/路線圖-roadmap.md` 的狀態；讓 `tests/test_design_docs.py`、`tests/test_readme.py` 通過。

## Workflow follow-up

- 完成後以 `/openspec-archive-change add-traditional-chinese-output-spec` 封存（delta 同步成新的主規格 `rpg-output-traditional-chinese`）；封存後更新路線圖並複查設計文件標頭的「對應 change」。
- 簡報（`docs/2.0簡報.md`、圖解、產生器、PDF）不受影響，沒有要改的地方。

## 對照結果

### 1.1 規格情境與測試的對照（2026-11-19）

基準線：`tests/test_zh_text.py` 55 項全過（design.md 的 Context 寫 54 項，是撰寫時的數字，不影響規格）。測試名稱都在 `tests/test_zh_text.py`。

| # | 需求 / 情境 | 對應測試 |
|---|---|---|
| 1 | 繁體保證 / 簡體劇情轉成繁體 | `test_simplified_text_becomes_traditional_with_taiwan_wording`（轉換與台灣慣用詞）、`test_the_guard_releases_traditional_text_whatever_way_the_chunks_are_cut`（釋放給玩家）。**缺：寫入資料庫** |
| 2 | 繁體保證 / 不受串流片段切法影響 | `test_the_guard_releases_traditional_text_whatever_way_the_chunks_are_cut`（整段、1、2、5、13 字一組，並驗 `received_text` 仍是原文）、`test_a_simplified_sentence_that_follows_an_earlier_one_is_still_buffered_and_converted_in_order` |
| 3 | 繁體保證 / 審查層看到的是繁體 | `test_a_blocked_sentence_is_blocked_the_same_way_in_simplified_form` |
| 4 | 繁體保證 / 結構化輸出先轉換再解析 | `test_structured_output_that_players_read_is_converted_before_it_is_parsed`（狀態、記事簿、主角生成） |
| 5 | 繁體保證 / 客服與審查類輸出不轉 | `test_customer_service_and_review_outputs_are_not_touched`（chat、script_review）。**缺：protagonist_review** |
| 6 | 只在有簡體字時才轉 / 已經正確的繁體原樣保留 | `test_text_that_is_already_traditional_is_kept_as_it_is`、`test_correct_traditional_sentences_with_characters_that_both_scripts_use_are_left_alone`（祕、咸陽、里長、台灣、余、曲） |
| 7 | 只在有簡體字時才轉 / 簡繁共用字不當作簡體證據 | `test_names_and_ambiguous_characters_are_not_damaged`（皇后、太后、于謙） |
| 8 | 只在有簡體字時才轉 / 有明確簡體字時整句轉換 | `test_a_sentence_is_converted_when_it_has_a_clear_simplified_character_even_next_to_a_both_script_one` |
| 9 | 只在有簡體字時才轉 / 重複轉換結果不變 | `test_text_that_is_already_traditional_is_kept_as_it_is`（第 3 個斷言）、`test_jie_is_kept_even_when_the_sentence_is_converted_for_another_simplified_character`（第 3 個斷言） |
| 10 | 異體字 / 人名中的「杰」保留 | `test_the_variant_character_jie_used_in_taiwanese_names_is_kept`（5 句） |
| 11 | 異體字 / 同句有其他簡體字時只轉其餘 | `test_jie_is_kept_even_when_the_sentence_is_converted_for_another_simplified_character`（第 1 個斷言） |
| 12 | 異體字 / 連續出現與位於句首句尾 | 同上（第 2 個斷言） |
| 13 | 異體字 / 「杰」不讓正確的繁體句整句被轉換 | `test_jie_alone_does_not_make_a_traditional_sentence_count_as_simplified` |
| 14 | 異體字 / 標準字「傑」不受影響 | `test_the_standard_form_jie_is_untouched_too` |
| 15 | 異體字 / 串流與結構化輸出一致 | `test_the_guard_keeps_the_name_jie_whatever_way_the_chunks_are_cut`、`test_structured_output_keeps_the_name_jie_while_converting_the_rest` |
| 16 | 不破壞結構 / JSON 結構與非中文字元不變 | `test_json_structure_and_ascii_survive_so_converted_model_json_still_parses` |
| 17 | 不破壞結構 / 沒有中文字元的文字原樣 | `test_text_without_chinese_characters_is_returned_unchanged`（空字串、ASCII、標記字串、標點、換行） |
| 18 | 英文不翻譯 / 含英文單字的繁體句原樣送達 | **缺** |

對照程式（規格需求文字裡的行為性敘述，逐項讀過原始碼確認一致，沒有發現規格與程式不符）：

- 結構化輸出在解析之前轉換、只限三種 kind：`llm_service.py` 的 `TRADITIONAL_KINDS`（狀態、記事簿、主角生成）與第 168 行 `to_traditional(response.text)` 在 `parse_llm_json` 之前；客服與兩種審查類（`script_review`、`protagonist_review`）不在清單內。
- 劇情逐句轉換、先於洩漏與規則層檢查：`rpg_output_guard.py` 的 `_process` 第一行就是 `to_traditional(unit)`。
- 寫入資料庫的是釋放出去（已轉換）的文字、`received_text` 保留原文：`rpg_runner_base.py` 的 `_attempt` 中 `self._narration = "".join(released)`；回合、第 0 幕、重新生成與編輯的三個 runner（`TurnRunner`、`OpeningRunner`、`RevisionRunner`）都繼承同一個 `RunnerBase`。

結論：18 個情境中 15 個有完整測試；3 個有缺口，在 1.2 補：

1. 情境 1「寫入資料庫」：只驗到狀態機釋放的文字，沒有任何測試在 runner 層驗證存進資料庫的內容（整個 `tests/` 只有 `test_zh_text.py` 含簡體字）。
2. 情境 5：規格寫「劇本、主角的審查類輸出」，測試只涵蓋 `script_review`，沒有 `protagonist_review`。
3. 情境 18：預期中的缺口。

### 1.2 補測試與變異驗證（2026-11-19）

只動 `tests/test_zh_text.py`（55 項 → 75 項，其中用到資料庫的測試在 SQLite 與真實 SQL Server 各跑一次），程式與行為沒有變動。

| 補的缺口 | 新測試 |
|---|---|
| 情境 18（英文原樣）：轉換本身 | `test_english_words_in_a_traditional_sentence_are_neither_translated_nor_changed`（「我感覺你需要 help」「他說了聲 okay 就走了」「Hello」引號句與兩句合併，共 4 句）、`test_english_words_next_to_simplified_characters_survive_while_the_chinese_is_converted`（簡體句中的英文也不動，中文照轉） |
| 情境 18：劇情串流（不同片段切法） | `test_the_guard_releases_english_words_as_they_are_whatever_way_the_chunks_are_cut`（整段、1、2、5、13 字一組，沒有被擋） |
| 情境 18：結構化輸出 | `test_structured_output_keeps_english_words_as_they_are`（狀態、記事簿、主角生成） |
| 情境 1（寫入資料庫） | `test_a_simplified_story_is_streamed_and_stored_as_traditional`：用 `RunnerKit` 跑完整的一回合，驗證 token 事件、資料庫的 `llm_raw_response` 與玩家重播看到的都是繁體 |
| 情境 5（主角審查） | 既有的 `test_customer_service_and_review_outputs_are_not_touched` 加入 `protagonist_review` |

變異驗證（暫時破壞實作 → 只跑新測試 → 還原並以 `git diff app/` 比對，三個檔案都已還原、無殘留）：

| 變異 | 失敗的新測試 | 結果 |
|---|---|---|
| `zh_text.to_traditional` 吃掉 ASCII 字母 | 英文原樣的 4 組測試（轉換、簡體旁的英文、串流、結構化），共 16 項 | 失敗，符合預期 |
| `rpg_runner_base._attempt` 改存原文（`self._received`）而不是釋放出去的文字 | `test_a_simplified_story_is_streamed_and_stored_as_traditional`（2 項） | 失敗，符合預期 |
| `llm_service.TRADITIONAL_KINDS` 加入 `protagonist_review` | `test_customer_service_and_review_outputs_are_not_touched[…protagonist_review]`（2 項） | 失敗，符合預期 |
