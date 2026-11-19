# Proposal

## Why

RPG 的模型輸出在送達玩家之前會先做簡體轉繁體（`app/services/zh_text.py`），這是展示時為了確保「全系統內容一律繁體中文」（AGENTS.md 第 8 節）加上的保險，行為有測試（`tests/test_zh_text.py`）與設計文件（`docs/模型呼叫層-llm-layer.md` 第 12 節），但**沒有任何一份主規格**描述它。`safe-mode-toggle` 驗收時發現「杰」被轉成「傑」，修正成「保留台灣仍在使用的異體字」之後，這條規則同樣沒有規格可以放；`safe-mode-toggle` 封存時只能把它記成既有缺口。現在補上規格，讓這個行為有明文依據：之後有人改動它（例如增加要保留的字、改變套用位置）必須走 change，測試也能對應到規格的情境。

## What Changes

- 新增能力規格 `rpg-output-traditional-chinese`，**記錄既有、已實作且已測試的行為**，不新增或改變任何行為：
  - RPG 的劇情與結構化輸出在送達玩家與寫入資料庫之前，轉成台灣用語的繁體中文，並說明套用的位置與不套用的範圍（客服的 Gemini 與審查類輸出不轉）。
  - 只在文字裡真的有簡體字時才轉換，已經正確的繁體（含「祕」、「咸陽」、「里長」這類簡繁都用的字）原樣保留。
  - 台灣仍在使用的異體字不轉：目前只有人名常用的「杰」，不算簡體的證據，句子因其他簡體字而轉換時也原樣保留。
  - 只改 CJK 字元，ASCII、標點與 JSON 結構不變，轉換後的 JSON 仍可解析；轉換是純函式，只依文字本身。
  - 英文夾雜只靠提示詞約束（規則已在 `rpg-turn-context`），伺服器端不翻譯。
- **不改程式、不改資料庫、不改既有主規格**；沒有 **BREAKING**。
- 在 `docs/模型呼叫層-llm-layer.md` 第 12 節加上指向新規格的說明（設計文件與規格互相對照）。

## Capabilities

### New Capabilities
- `rpg-output-traditional-chinese`: RPG 輸出的繁體中文保證——簡轉繁的套用位置與範圍、只在有簡體字時才轉、保留台灣仍在使用的異體字（杰）、只改 CJK 字元且 JSON 仍可解析、英文不在伺服器端翻譯。

### Modified Capabilities
<!-- 沒有：回合提示詞的「不夾雜英文」與「繁體中文」規則已在主規格 `rpg-turn-context` 的「系統指令只放固定規則」，這個 change 不重複也不修改它。 -->

## Impact

- 規格：新增 `openspec/specs/rpg-output-traditional-chinese/spec.md`（封存時由 delta 同步產生）。
- 程式與測試：**無變動**。規格的每個情境對應 `tests/test_zh_text.py` 既有的測試；若發現哪個情境沒有測試，在 tasks 補測試，不改行為。
- 文件：`docs/模型呼叫層-llm-layer.md` 第 12 節加規格的對照；`docs/路線圖-roadmap.md` 記錄狀態。
- 風險：低。只是把既有行為寫成規格；唯一的風險是規格寫得與程式不一致，所以每個情境都要對照測試與實際輸出。
