---
name: python-flask-skill
description: 開發本專案（Python Flask「開放世界文字 RPG」與「會員制智能客服機器人」雙核心平台）時使用。觸發情境包含：Flask MVC Page Controller / Web API Controller / Service / Repository 分層、Vue 3 全域變數模式前端（離線 vendor、自製 Modal/Toast/Sidebar、深色電影感配色）、SQLAlchemy SQLite 與 SQL Server 雙軌資料庫與 Alembic migration、會員註冊登入（信箱驗證、Google 登入、CSRF、資料隔離與權限）、Gemini LLM 串流（SSE）與客服結構化 JSON 輸出、對話滑動視窗與摘要記憶、用量與限流、繁體中文與 UTF-8 編碼、個資保存與日誌規範，以及 Docker 部署與機密管理。
---

# python-flask-skill

> 權威來源為專案根目錄的 `AGENTS.md`。本 skill 與其衝突時，一律以 `AGENTS.md` 為準，並在回報中指出衝突。
> SKILL.md 僅放總綱、核心規則與索引；細節請依任務讀取對應的 `references/` 檔案。

## 1. 專案定位

本專案為一個以 LLM 驅動的「開放世界文字 RPG 平台」與「會員制智能客服機器人系統」之雙核心平台 (Dual-Core Platform)。

## 2. 核心規則摘要（不可違反，細節以 reference 為準）

1. 前端無 CDN，資源存於 static/vendor/（字型存於 static/fonts/），引入一律用 url_for。
2. 禁止 .vue 單檔元件與 Vite；採 Vue 3 全域變數模式。
3. 禁止 Bootstrap 原生 JS 與 data-bs-* 互動屬性，僅允許 data-bs-theme="dark"。
4. 禁止外部彈窗套件；Modal、Toast 與 Sidebar 狀態由 Vue 3 的 v-if / v-show 接管。
5. 業務與 Repository 層禁止 Raw SQL；唯一例外為 SQLite 的連線初始化事件。
6. 全系統模型統一為 gemini-3.8-flash。
7. SSE 僅用於 RPG 劇情；客服意圖解析必須非串流輸出 JSON。
8. 上下文 = 摘要 + 最近 10 輪原文 + 當前世界狀態；一輪 = 玩家訊息 + AI 回應。
9. 全系統使用繁體中文（zh-TW）。
10. 檔案讀取用 utf-8-sig，寫入 JSON 用 utf-8，給 Excel 的 CSV 用 utf-8-sig。
11. 正式環境禁止 Flask 開發伺服器；SSE 須運行於支援長連線並發的正式配置。
12. Gemini API Key 只從環境變數讀取，嚴禁寫入程式碼或版本庫。
13. 驗證採簽章 session cookie 加 CSRF，密碼用 Argon2id；一組 email 只能對應一個帳號。
14. 對話相關的 Repository 方法必須帶 user_id，非擁有者一律回 404。
15. 限流與鎖定計數不得放行程內記憶體，須存資料庫或入口層。
16. 日誌不得記錄密碼、權杖、完整 API Key、email、對話全文。
17. 資料表變更用 Alembic，正式環境禁用 create_all()，migration 不含方言專屬原生 SQL。
18. reply_text 一律純文字渲染，禁用 v-html；LLM 指令只放 system_instruction。
19. SSE、心跳、串流逾時等條文僅適用 RPG；客服為非串流，逾時以 axios 搭配 AbortController 處理。
20. 可為空的唯一欄位必須用部分唯一索引（unique_when_not_null），不可直接 unique=True（SQL Server 只允許一個 NULL）。
21. alembic.ini 與 script.py.mako 必須純 ASCII；含中文的 .ps1 須 UTF-8 with BOM，維運與驗收腳本一律用 Python 撰寫。

## 3. 參考索引（依任務類型讀取）

| 任務類型 | 讀取的 reference | 對應 AGENTS.md |
| --- | --- | --- |
| 前端畫面、Modal、Toast、Sidebar、色彩 | [references/frontend.md](references/frontend.md) | 第 1、2、7 節 |
| Controller、Service、Repository 分層 | [references/architecture.md](references/architecture.md) | 第 3 節 |
| 資料表、SQLAlchemy 模型、SQLite / SQL Server 切換、migration | [references/database.md](references/database.md) | 第 4 節 |
| LLM 呼叫、SSE、客服、對話記憶 | [references/llm-streaming.md](references/llm-streaming.md) | 第 5、6 節 |
| RPG 劇情機制、客服業務、語系、檔案編碼 | [references/business-encoding.md](references/business-encoding.md) | 第 7、8 節 |
| 部署、正式環境、機密管理 | [references/deployment.md](references/deployment.md) | 第 9 節 |
| 登入註冊、Google 登入、CSRF、權限、用量限制 | [references/auth-security.md](references/auth-security.md) | 第 10 節 |
| 日誌、保存期限、刪除帳號、備份、告警 | [references/privacy-observability.md](references/privacy-observability.md) | 第 11 節 |

跨領域任務（例如新增一個客服 API）請同時讀取多個 reference。

## 4. 產出前自查清單

- [ ] 是否已讀取任務對應的 reference，且內容未與 `AGENTS.md` 衝突？
- [ ] 前端是否沒有任何 CDN、Google Fonts、.vue、Vite、SweetAlert2、Bootstrap 原生 JS、data-bs-*（除 data-bs-theme="dark"）？
- [ ] 資源引入是否一律使用 `{{ url_for('static', filename='...') }}`？
- [ ] 是否沒有 Raw SQL（連線初始化事件除外），且 PRAGMA 僅在 `engine.dialect.name == "sqlite"` 時於 connect 事件中執行？
- [ ] 是否所有 LLM 呼叫都使用 gemini-3.8-flash，且 SSE 只用於 RPG 劇情、客服為非串流 JSON？
- [ ] 送進 Gemini 的 Context 是否為「摘要 + 最近 10 輪原文 + 當前世界狀態」？
- [ ] UI 文字、Prompt、錯誤訊息、註解是否皆為繁體中文？System Prompt 是否明確約束回傳繁體中文？
- [ ] 所有 open() 是否明確指定 encoding（讀取 utf-8-sig；寫入 JSON/文字 utf-8；Excel CSV utf-8-sig）？HTML 是否含 `<meta charset="UTF-8">` 與 `<html lang="zh-Hant-TW">`？
- [ ] 是否沒有使用 app.run 作為正式環境、沒有依賴行程內記憶體保存狀態（含限流計數）、沒有把 API Key 或資料庫密碼寫入程式碼／模板／版本庫？
- [ ] 每個讀寫端點是否都檢查擁有者（Repository 帶 user_id，非擁有者回 404），且有對應的 IDOR 測試？
- [ ] 日誌是否未含黑名單欄位（密碼、權杖、完整金鑰、email、對話全文）？
- [ ] reply_text 是否以純文字渲染（無 v-html）？
- [ ] 資料表變更是否透過 Alembic migration，且不含方言專屬原生 SQL？alembic.ini 是否仍為純 ASCII？
- [ ] 可為空的唯一欄位是否使用部分唯一索引，而不是 unique=True？
- [ ] 新增的腳本是否以 Python 撰寫（或 .ps1 為純 ASCII 薄包裝；含中文的 .ps1 是否為 UTF-8 with BOM）？
