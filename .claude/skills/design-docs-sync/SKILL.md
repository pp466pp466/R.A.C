---
name: design-docs-sync
description: 在 OpenSpec 的 propose、apply、archive 之後，或修改架構、分層、資料表、migration、端點、設定、環境變數、模型呼叫流程、前端模組、維運指令時使用；更新 docs/ 底下的 Mermaid 設計文件，使圖、表與程式保持一致，並讓 tests/test_design_docs.py 通過。也用於新增設計文件或回答「設計文件要更新哪一份」。
---

# 設計文件同步（design-docs-sync）

`docs/` 底下的設計文件（`系統架構-architecture.md`、`資料模型-data-model.md`、`帳號與安全-auth-security.md`、`客服對話-chat-service.md`、`模型呼叫層-llm-layer.md`、`前端結構-frontend.md`、`營運與部署-operations.md`、`路線圖-roadmap.md`，索引與規則在 `docs/README.md`）用 Mermaid 描述**現在的設計**。它們不是歷史紀錄：每次 change 的 propose、apply、archive 之後都要跟著更新，否則會與程式漂移。

## 什麼時候用

- **propose**：判斷這個 change 會動到哪些設計文件（見下表），把「更新 docs 對應的設計文件並讓 `tests/test_design_docs.py` 通過」寫成 `tasks.md` 的最後一項。
- **apply**：實作完成、勾選最後一項任務之前，更新受影響的文件。
- **archive**：封存前確認文件已更新；封存後更新 `docs/路線圖-roadmap.md` 的狀態與日期，並複查各文件標頭的「對應 change」。
- **平時**：只要改了架構、資料表、端點、設定、流程或前端模組，即使沒有走 OpenSpec，也要同步更新。

## 步驟

1. 讀 `docs/README.md` 第 2 節的對照表，找出受影響的文件；用 `git diff` 或該 change 的 `tasks.md` 確認這次實際改了什麼。
2. **對照程式更新，不要憑記憶**：資料表與欄位看 `app/models/`、路由看 `app/controllers/`、環境變數看 `.env.example` 與 `app/config.py`、流程看對應的 Service。圖與表要與程式的名稱完全一致。
3. 修改圖與表：被取代的設計直接改掉（理由寫在該 change 的 `design.md`，不留在圖上）；規劃中但未實作的部分標明「（C-4）」「規劃中」。
4. 更新該文件標頭的「最後更新」日期與「對應 change」；有新文件時加進 `docs/README.md` 的索引與 `tests/test_design_docs.py` 的 `DESIGN_DOCS`。
5. 執行 `.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_design_docs.py -q -p no:cacheprovider`。失敗訊息會指出哪張資料表、哪個路由、哪個環境變數或哪個前端檔案沒有寫進文件。
6. 在 VS Code 或 GitHub 的預覽確認圖能渲染（測試不驗證 Mermaid 語法本身，只擋常見的坑：引號沒成對、序列圖訊息含分號、HTML 實體、不支援的圖類型）。
7. 文件與程式放在同一個提交；不要留到「之後再補」。

## 規則

- **AGENTS.md 是規範的唯一權威**：設計文件只描述實作如何符合它，**不修改它**；文件與它衝突時，以 AGENTS.md 為準並回報。
- 決策的來龍去脈與備選方案留在 `openspec/` 與 `docs/report.md`、`docs/RPG.md`；設計文件只寫現況，不複製整份決策紀錄。
- 逐項的環境變數與端點表在根目錄 `README.md`；設計文件引用它而不重複。
- Mermaid 慣例：只用 `flowchart`、`sequenceDiagram`、`erDiagram`、`stateDiagram-v2`；節點文字放在雙引號內、換行用 `<br/>`；一張圖只講一件事、節點盡量不超過 15 個。
- 日誌與文件都不得出現金鑰、密碼、使用者的 email 與生日等個資。

## 受影響文件的快速對照

| 改了什麼 | 更新 |
|---|---|
| 分層、禁止匯入、`create_app` 注入參數、請求生命週期 | `系統架構-architecture.md` |
| 資料表、欄位、約束、migration、保存與刪除 | `資料模型-data-model.md`（保存期限的實作狀態同步 `營運與部署-operations.md`） |
| 註冊、登入、session、CSRF、Google、年齡規則、權限、`/api/auth/*` | `帳號與安全-auth-security.md` |
| `/api/cs/*`、送出流程、冪等、上下文、摘要、降級文案 | `客服對話-chat-service.md` |
| `LlmService`、轉接器、斷路、預算、帳本、提示詞結構、`GEMINI_*`、`LLM_*` | `模型呼叫層-llm-layer.md` |
| 前端模組、載入順序、Sidebar、暫存與同步 | `前端結構-frontend.md` |
| 環境、容器、日誌白名單與事件、維運指令、測試與驗收分層 | `營運與部署-operations.md` |
| 任何 change 的 propose、apply、archive；待決定事項有結論 | `路線圖-roadmap.md` |
