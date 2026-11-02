# Design

## Context

C-5a（`launch-readiness`，已封存）交付了部署腳本、平台告警定義、還原演練與壓測工具，但**都只在預覽輸出與假 runner 下驗證，從未對真實的 Google Cloud 執行**。使用者目前**沒有可計費的 Google Cloud 專案**，所以 C-5b 依其決定拆成兩段：本 change（`staging-deployment`）做程式、文件與**本機單人預演**；雲端實跑另開 `cloud-staging-run`（C-5c，已規劃，等使用者有專案再做）。現況（已查證）：

- `.env` 只有本機開發用的變數；使用者另外放了 Brevo 的 SMTP 金鑰（`SMTP_KEY_NAME`、`SMTP_KEY_VALUE`）。這台電腦**沒有安裝 `gcloud`**，WSL 有 Ubuntu-2204 但 Docker 目前沒在跑（以前用過，能否啟動尚未驗證）。
- `APP_ENV=staging` 加 SQLite 檔案在本機可以啟動，且 `trust_proxy` 為真：本機 staging 設定經 HTTPS 通道可以真實呈現 HSTS、`__Host-` cookie 與 CSRF 行為。
- Google 登入的回呼網址取自 `BASE_URL`，不取自請求；CSRF 檢查比對**請求自己的來源**，只有 staging 與 production 掛 ProxyFix，development 不信任轉送標頭。
- `GeminiProvider` 已有 `base_url` 建構參數，但 `create_app` 沒有把設定傳進去；金鑰在 `x-goog-api-key` 標頭，回應解析 `candidates[0].content.parts[].text` 與 `usageMetadata`。
- 動機與範圍見 proposal.md。

## Goals / Non-Goals

**Goals:**
- 在不花任何錢的前提下，把「預備環境的應用程式行為」盡可能驗證到底，並讓雲端那一半只剩下雲端獨有的問題。
- 本機預演的結果誠實標明範圍：哪些已驗證、哪些必須在雲端驗證。
- 為雲端執行準備好需要的程式（`GEMINI_BASE_URL`、假模型伺服器、階梯式加壓、冒煙腳本），使 `cloud-staging-run` 只需要執行與回寫。

**Non-Goals:**
- 任何雲端資源、`gcloud` 指令的實際執行（`cloud-staging-run`）。
- Cloud SQL、Cloud Run、Secret Manager、Scheduler、平台告警、雲端的還原演練與容量量測。
- 為了通過預演而放寬 staging 的任何檢查。

## Decisions

### D1：預演的順序與範圍

先做不碰外部服務的程式與測試（`GEMINI_BASE_URL`、假模型伺服器、階梯式加壓、冒煙腳本、開發用通道），再依序做本機預演：①以 staging 設定在本機啟動並經 ngrok 取得 https 網址 → ②冒煙驗收 → ③Google 登入人工驗收 → ④`check-alerts` 真實寄出告警信 → ⑤（條件式）本機容器加假模型伺服器加階梯式加壓。每一步的結果記入 README 的驗收紀錄，**分成「本機預演已驗證」與「必須在雲端驗證（尚未做）」兩欄**；後者在 `cloud-staging-run` 完成前不得標為通過，上線前關卡也不因本機預演而通過。

### D2：本機預演用 staging 設定，不是 development

只用 development 加通道會漏掉預備環境真正的行為（HSTS、`__Host-` 前綴、Secure cookie、信任入口層標頭）。`APP_ENV=staging` 本機可以啟動（SQLite 檔案，與雲端同樣禁止 `sa` 與 `TrustServerCertificate=yes`、缺機密即失敗），ProxyFix 本來就生效，所以**預演不需要 `TRUST_PROXY_HEADERS`**；那個開關只服務「用 development 經通道做日常開發」。機密與一般設定來自 `.env.staging` 與 `staging.vars`（皆被版本控制忽略）。SQLite 檔案放在被忽略的位置，預演後可刪除。**預演不能驗證的**：Cloud SQL 的私有連線與伺服器憑證、Cloud Run 與 Secret Manager 的注入方式、Scheduler 與 Job、Cloud Monitoring 的告警。

### D3：壓測走真實程式路徑，只把「模型」換成假的

- 新增設定 `GEMINI_BASE_URL`（`app/config.py`，`create_app` 傳給 `GeminiProvider`）。**production 拒絕**；只允許 https（development 與 testing 另允許回送位址的 http，本機預演的假伺服器就在回送位址），不得含帳密、查詢與片段，錯誤訊息不回顯網址。
- `loadtest/fake_llm/`：小型 Flask 應用（Gemini 相容的 `generateContent`、`x-goog-api-key` 必須等於部署時設定的測試金鑰、延遲與錯誤率環境變數、日誌只有彙總），自己的 `Dockerfile` 與 `requirements.txt`，不進主映像（雲端時以 `gcloud builds submit loadtest/fake_llm` 建置，本機直接以 Python 或容器執行）。
- 為什麼不在映像裡放測試程式或用「載入替身」的環境開關：兩者都擴大正式映像的攻擊面；`GEMINI_BASE_URL` 是一個單純、可驗證、production 拒絕的設定。雲端的隔離壓測服務（自己的資料庫、機密與假金鑰）在 `cloud-staging-run`。

### D4：本機容器預演是條件式的，結果不是容量依據

本機容器環境（WSL 的 Docker）可用時，以主映像（非 root、gunicorn gthread）加假模型伺服器加階梯式加壓預演，目的有二：驗證映像在 Linux 與 gunicorn 下確實能跑、`WEB_CONCURRENCY` 與 `GUNICORN_THREADS`（納入 `.env.example` 與設定歸類）確實生效；以及取得**粗略的相對比較**（例如加倍執行緒是否改善 p95）。本機 SQL Server 只監聽 `127.0.0.1`，WSL 內的容器連不到，所以容器預演使用 SQLite，這與雲端的 SQL Server 行為不同（鎖定、連線池）。**每一輪記錄標明「不是容量依據」**；Docker 起不來就標為未預演，不以 Flask 開發伺服器的數字代替。階梯式加壓情境（`loadtest/shapes.py`）本身與雲端共用。

### D5：冒煙腳本的結構

`scripts/staging_smoke.py` 與 `launch_plan` 同風格：檢查清單是資料，傳輸（HTTP）可注入，所以測試用 Flask 測試客戶端（`https://` 基底網址、staging 設定）驅動同一組檢查；實際執行用標準函式庫的 `urllib` 與 cookie jar。位址守門沿用 `loadtest/guard.py`（正式環境一律拒絕）並額外要求 https。本機預演的通道網址與日後的雲端網址使用同一個腳本。

### D6：開發用通道（ngrok）

`TRUST_PROXY_HEADERS`（development 專用）只讓 `trust_proxy` 為真，使 ProxyFix 生效、CSRF 的來源比對成立；**HSTS 不跟著它走**——`init_security_headers` 的 `hsts` 參數改取 `is_production_like`，開發環境即使經通道也不送 HSTS（瀏覽器會把 HSTS 記在 ngrok 網域，之後的本機開發會被牽連）。`BASE_URL` 設成通道網址，Google 登入的回呼與信件連結自然指向它。ngrok 會把本機服務暴露在網際網路上（開發工具頁、測試帳號），文件要求在通道端加存取控制。這是依使用者「網域暫時使用 ngrok」做出的**假設**：ngrok 是本機的臨時網域，雲端預備環境將使用 Cloud Run 預設網域；若實際意圖不同，這一組（development-tunnel）可以整組拿掉而不影響其他部分。

### D7：Google 登入沿用現有實作，不引入 Firebase

使用者提到 Firebase，要的效果是「Google（Gmail）一鍵登入，玩家不輸入帳號密碼」。現有的 `google-sign-in` 已達成；缺的是預備環境用的 OAuth 用戶端與人工驗收。比較三條路：
- **沿用現有流程（採用）**：只需專用的 OAuth 用戶端與人工驗收。符合第 10 節（簽章 session、Authorization Code 加 PKCE、帳號合併）與第 1 節（無外部腳本）。
- **Firebase Authentication**：登入彈窗走 `*.firebaseapp.com/__/auth/handler`，身分由 Firebase 的 ID token 與它的 session 管理——要取代自建帳號、`token_version` 撤銷、年齡確認與合併規則，等於重寫第 10 節，另一個大 change，且沒有使用者可見的好處。
- **Google One Tap／Identity Services**：需要載入 Google 的外部腳本，違反「嚴禁 CDN、前端庫一律本機」。
- Firebase Hosting 轉送到 Cloud Run 時只保留 `__session` cookie（依我對文件的了解），`__Host-session` 與 `__Host-csrf` 會失效。

OAuth 用戶端：另建一個**網頁應用程式型**用戶端給預備環境（AGENTS.md 要求開發與正式使用不同用戶端）；Firebase 自動建立的用戶端不重用。同意畫面維持「測試中」並把使用者的 Gmail 加為測試使用者；範圍只有 `openid email profile`（非敏感），不需要 Google 審核。本機預演時授權重新導向 URI 是 `<ngrok 網址>/auth/google/callback`，ngrok 的免費網域若固定就不用一直改。

### D8：營運告警信用 Brevo，並以真實寄送驗證

玩家端不需要任何信件（預備環境關閉信箱驗證，Google 登入不寄信），但 `ALERT_EMAIL_TO` 在 staging 必填、設定了它寄信設定就必須齊全。使用者決定告警信**只寄給自己**，用 **Brevo** SMTP 中繼：`MAIL_SMTP_HOST=smtp-relay.brevo.com`、`MAIL_SMTP_PORT=587`、`MAIL_SMTP_SECURITY=starttls`；`MAIL_SMTP_USER` 是 Brevo 後台「SMTP 與 API」頁的 SMTP 登入名稱；`MAIL_SMTP_PASSWORD` 是該頁產生的 **SMTP 金鑰**（不是帳號密碼）；`MAIL_FROM` 必須是 Brevo 已驗證的寄件者。專案的寄信器只做標準 SMTP，不需要改程式。**送達是主要風險**：寄件者若是 `gmail.com` 這類免費信箱，經 Brevo 寄出時 SPF／DMARC 對不上，可能進垃圾郵件甚至被拒；所以預演一定要真的寄一封（製造一項告警條件，例如 5 個被鎖定的 `.invalid` 測試帳號，執行 `check-alerts`），由使用者確認收到。收不到就改用 Gmail SMTP（寄件者等於登入帳號、需要應用程式密碼）或日後自有網域加 DKIM 的 Brevo——只換設定與密碼。收件人地址只放 `staging.vars` 與 `--notify-email`，不進程式庫。

### D9：gunicorn 的兩個旋鈕

`WEB_CONCURRENCY` 與 `GUNICORN_THREADS` 目前只存在 `gunicorn.conf.py`，不在 `.env.example`，部署計畫的設定歸類也不認得它們（放進一般設定檔會被拒絕）。納入 `.env.example` 與歸類測試，讓本機容器預演與日後的雲端調整使用同一組名稱。

## Risks / Trade-offs

- **本機預演可能讓人誤以為雲端也沒問題** → 驗收紀錄強制分兩欄，雲端獨有的項目標為尚未驗證；關卡不因預演通過；README 與營運文件在每個相關位置重複這個限制。
- **Brevo 寄件者送達** → D8 的真實寄送驗證；不通過就換設定，不影響規格。
- **ngrok 暴露開發環境與預演環境** → 文件要求通道端加存取控制；預演使用的資料是 `.invalid` 測試帳號與可丟棄的 SQLite 檔案；結束後關閉通道並刪除資料庫檔案。
- **`GEMINI_BASE_URL` 是新的攻擊面**（金鑰與提示詞送往該位址）→ production 拒絕、只允許 https（本機回送位址除外）、不得含帳密、值不回顯於錯誤訊息；假伺服器只收測試金鑰。
- **Docker 可能起不來** → 條件式，標為未預演；不阻擋封存。
- **使用者不在場時，人工驗收（Google 登入、收信）會停住** → 標為未驗證並記下狀態，下次接續。

## Migration Plan

沒有資料表變動、沒有 migration。程式變更都是新增或限於 development／staging 的設定，對既有環境相容。回復：還原到前一個提交即可；本機預演產生的檔案（SQLite、`.env.staging`、`staging.vars`）都被版本控制忽略，可直接刪除。

## Open Questions

- 本機容器環境（WSL 的 Docker）能否啟動：實作時先試，結果決定容器預演是否進行（不改變規格與其他任務）。
- Brevo 的寄件者地址與 SMTP 登入名稱，以及預備環境專用的 OAuth 用戶端：使用者在預演前提供（寫入本機被忽略的檔案，不貼到對話）。
