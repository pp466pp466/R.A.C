# 雙核心業務邏輯、語系與編碼規範

對應 AGENTS.md 第 7、8 節。

## 雙核心業務邏輯 (Dual-Core Business Spec)

### 開放世界 RPG 模組

- 無硬性結局 (Endless Open World)：廢除傳統死亡死局，採用「無窮盡開放世界 + 狀態轉移框架 (State Transition Framework)」。
- 動態階段錨點：透過 milestones_json 驅動階段性事件（如 20 歲宗門大測），觸發後由 LLM 動態延伸新目標，玩家離線時對話 Session 自動存檔。
- 原始劇本（`scripts.milestones_json`）為唯讀。LLM 動態延伸的目標不得寫回原始劇本，應另存於 session 層，完成後歸檔。

### 會員制智能客服機器人模組

- 類 ChatGPT 介面：具備可開關的左側歷史對話 Sidebar (寬度 280px) 與會員等級標籤；響應式行為與建議動作 chips 見 [frontend.md](frontend.md)。
- 會員制：以 email 為帳號識別，一組 email 只能對應一個帳號。會員等級為 free（一般）、plus（進階）、vip（尊榮），僅影響額度、不影響權限；初始額度為每日對話輪數 30 / 100 / 300、每分鐘訊息數 6 / 10 / 20、對話數上限 20 / 100 / 500，單則訊息上限皆為 1000 字。
- NLP 意圖與實體解析：Service 層呼叫 Gemini API 時強制要求以非串流請求輸出標準 JSON（至少包含 intent, confidence, entities, reply_text, suggested_actions）（見 [llm-streaming.md](llm-streaming.md)）。
- 客服僅由 AI 提供，不設真人客服通道。使用者要求真人客服時，AI 須以繁體中文說明並引導以意見回饋留下紀錄，供管理員於後台檢視。
- 知識範圍：v1 僅回答平台自身資訊（登入與帳號、會員等級與額度、意見回饋流程）；超出範圍或沒有資料來源的問題歸為 unknown，並引導以意見回饋留下紀錄。有 FAQ 文件時再另行擴充，不採檢索式 (RAG)。

## 語系與字元編碼規範（繁體中文與 UTF-8 強約束）

### 全系統語言統一（Traditional Chinese）

- 所有 UI 介面文字、系統 Prompt、錯誤訊息、註解、選單、Modal/Toast 提示、預設劇本內容與客服回應，一律強制採用繁體中文（zh-TW，台灣常用術語）；唯一例外是必須純 ASCII 的檔案（見下方檔案編碼規範）。
- 要求 Gemini API 生成內容時，必須在 System Prompt 中明確約束回傳繁體中文。
- 系統寄出的信件（信箱驗證、重設密碼）一律為 UTF-8 編碼與繁體中文內容。

### 檔案編碼與 I/O 規範

- 後端檔案讀寫：Python 處理文字、JSON、CSV、檔案讀寫（open()）或匯出/匯入操作時，必須明確指定 encoding，嚴禁使用 ANSI 或未指定之編碼。
  - 讀取：一律使用 encoding='utf-8-sig'（可自動去除 BOM）。
  - 寫入 JSON 與一般文字檔：使用 encoding='utf-8'，避免 BOM 導致 JavaScript 的 JSON.parse 解析失敗。
  - 匯出供 Windows/Excel 開啟的 CSV：使用 encoding='utf-8-sig'，確保繁體中文絕不亂碼。
- 資料庫與 HTML 標籤：SQLAlchemy 連線與文字欄位必須支援 Unicode（見 [database.md](database.md)）；HTML 模板標註 <meta charset="UTF-8"> 與 <html lang="zh-Hant-TW">。
- 以系統地區編碼讀取的檔案與腳本：
  - alembic.ini 與 script.py.mako 必須純 ASCII（見 [database.md](database.md)）。
  - Windows PowerShell 5.1 以系統編碼讀取沒有 BOM 的 .ps1；內含繁體中文或其他非 ASCII 字元的 .ps1 須存成 UTF-8 with BOM，否則會亂碼。
  - 需要輸出繁體中文訊息，或要把字串傳給外部程式（例如 wsl、docker）的維運與驗收腳本，一律以 Python 撰寫（UTF-8、參數以清單傳遞）；.ps1 只作為純 ASCII 的薄包裝（例如容器驗收的 scripts/verify_container.py 與 scripts/verify-container.ps1）。
