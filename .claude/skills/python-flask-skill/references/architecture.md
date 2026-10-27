# 後端架構（關注點分離）

對應 AGENTS.md 第 3 節。

## 分層

專案結構劃分為四層：MVC Controller 層、Web API Controller 層、Service 層、Repository 層。

## Controller 分流

- 【MVC Page Controller】：僅負責渲染基本 HTML 頁面外殼與初始狀態傳遞（包含 /game 與 /cs 兩套視圖，以及 /login、/register 等帳號頁面）。
- 【Web API Controller】：一律回傳 JSON 格式，專門處理 Ajax 請求與 API 服務（SSE 串流端點除外，見 [llm-streaming.md](llm-streaming.md)）。包含 /api/auth/*、/api/cs/*，以及不需登入的 /healthz（存活）與 /readyz（含資料庫檢查），兩者不得回傳內部細節。

## Service 層

負責：
- RPG 核心邏輯
- 強約束事件錨點 (Milestones) 驗證
- 客服 NLP 意圖解析 (Intent/Entity)
- 對話摘要壓縮
- Gemini API 呼叫
- 身分驗證與資料存取政策檢查 (AccessPolicy，見 [auth-security.md](auth-security.md))

兩個模組共用的 Service 工具（Gemini 封裝、JSON 解析、滑動視窗與摘要、用量限制）應放在共用位置，避免客服與 RPG 各自實作。

## Repository 層

負責 SQLAlchemy CRUD 操作，封裝 ORM Session 生命週期與 JSON 序列化（見 [database.md](database.md)）。對話相關方法必須接收 user_id。
