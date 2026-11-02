# Proposal

## Why

C-5a 把部署寫成腳本與測試，但沒有一行對真實的環境執行過；預備環境的應用程式行為（HSTS、`__Host-` cookie、CSRF、Google 登入、營運告警信的真實寄送、容器下的 gunicorn）也都只在測試裡驗證。使用者目前**沒有可計費的 Google Cloud 專案**，且希望先以單人測試的方式使用。依其決定，原本的 C-5b 拆成兩段：

- **本 change（`staging-deployment`，C-5b）**：程式、文件與**本機單人預演**——以 staging 設定在本機啟動、經 ngrok 取得 https 網址，不花任何錢地把應用程式側的預備環境行為驗證到底，並備妥雲端實跑需要的程式。
- **`cloud-staging-run`（C-5c，已規劃，等使用者有可計費的專案再做）**：雲端資源的實際建立、平台告警演練、雲端壓測與還原演練。
- production 與對外開放順延為 C-5d。

## What Changes

- **本機預演（staging 設定）**：`APP_ENV=staging`、SQLite 檔案、機密與一般設定來自被忽略的 `.env.staging` 與 `staging.vars`、經 ngrok 的 https 網址（`BASE_URL`）。啟動仍通過與雲端相同的設定驗證，預演不放寬任何檢查。
- **冒煙驗收腳本** `scripts/staging_smoke.py`：`/healthz` 與 `/readyz`、五個安全標頭且無 CSP、`__Host-` cookie 屬性、無權杖的 POST 被拒、註冊／登入／開新對話（保留網域）、管理員 API 的 403 與 401；預設不呼叫模型；位址守門（https、拒絕 production）。本機預演與日後的雲端共用。
- **Google 一鍵登入的人工驗收（不引入 Firebase）**：「玩家不用輸入帳號密碼、直接連動 Google 帳號登入」**已由 `google-sign-in` 實作**。本 change 補上預備環境專用的 OAuth 用戶端（網頁應用程式型，授權重新導向 URI 為通道網址加 `/auth/google/callback`）與使用者本人的人工驗收（首次登入須勾選條款並提供生日與年齡區間——AGENTS.md 第 10 節的年齡確認不能省；第二次一次點擊）。**不使用 Firebase Authentication、Firebase Hosting 或 Google One Tap**：前者取代我們自建的帳號與簽章 session（與第 10 節衝突），Hosting 轉送到 Cloud Run 時會剝掉 `__session` 以外的 cookie（`__Host-session` 與 `__Host-csrf` 會失效），One Tap 需要載入 `accounts.google.com` 的外部腳本（違反第 1 節）。
- **營運告警信（Brevo）以真實寄送驗證**：玩家端不需要任何信件，但 `check-alerts` 要寄告警信（`ALERT_EMAIL_TO` 在 staging 必填，寄信設定要齊全）。依使用者決定寄給自己，用 **Brevo** SMTP 中繼（`smtp-relay.brevo.com`、587、STARTTLS，SMTP 金鑰）。預演製造一項告警條件並由使用者確認收到信；寄件者若是免費信箱可能有送達風險，不通過就換設定。
- **假模型伺服器與 `GEMINI_BASE_URL`**：新增設定 `GEMINI_BASE_URL`（只允許 development、testing、staging；**production 拒絕**，因為金鑰與提示詞會被送到該位址；必須 https，本機回送位址除外）把 Gemini 轉接器指向 `loadtest/fake_llm/`——Gemini 相容的 `generateContent`、可設延遲與錯誤率、只接受測試金鑰、自己的 Dockerfile，不進主映像。壓測因此走真實的程式路徑，不消耗任何額度。
- **階梯式加壓與每一輪留存**：`loadtest/shapes.py`；每一輪記錄環境、伺服器設定、負載參數與判定；本機結果一律標明「不是容量依據」。
- **本機容器預演（條件式）**：WSL 的 Docker 能啟動時，以主映像（非 root、gunicorn gthread）加假模型伺服器加階梯式加壓預演；起不來就標為未預演，不以 Flask 開發伺服器的數字代替。`WEB_CONCURRENCY` 與 `GUNICORN_THREADS` 納入 `.env.example` 與設定歸類。
- **開發用通道（ngrok）**：使用者暫時以 ngrok 當對外網域。新增開發專用的 `TRUST_PROXY_HEADERS`（只允許 development；staging 與 production 本來就信任入口層，設定它會啟動失敗），讓 development 經 HTTPS 通道的 POST 通過 CSRF 檢查；HSTS 不跟著它走。本機預演用 staging 設定，不需要這個開關。
- **誠實揭露**：驗收紀錄分成「本機預演已驗證」與「必須在雲端驗證（尚未做）」兩欄；上線前關卡不因本機預演而通過。
- **依使用者決定，不在本 change**：所有雲端資源與 `gcloud` 的實際執行（`cloud-staging-run`）；Cloud SQL；Cloud Run；平台告警與演練；production 與對外開放（C-5d）；自有網域與信件的 SPF／DKIM／DMARC；入口層 IP 限流；法務覆核與模型供應商方案；「只留 Google 登入」（另一個規範層級的 change，尚未立案）。

## Capabilities

### New Capabilities
- `local-rehearsal`: 在沒有雲端專案時以 staging 設定在本機預演、冒煙驗收、告警信真實寄送、條件式的容器預演，以及預演範圍的誠實揭露。
- `staging-verification`: 預備環境冒煙驗收（標頭、cookie、CSRF、登入、權限、就緒）、只對 https 且非 production 的位址執行、不使用真實個資、Google 登入的人工驗收、驗收紀錄只記有依據的結果。
- `fake-model-server`: Gemini 相容的假模型伺服器（延遲與錯誤率、只接受測試金鑰、獨立映像）。
- `development-tunnel`: 開發環境經 HTTPS 通道（ngrok）使用時信任轉送標頭、其他環境拒絕設定、通道網址作為對外網址與使用注意事項。

### Modified Capabilities
- `app-configuration`: 新增 `GEMINI_BASE_URL`（僅非正式環境、必須 https、不得含帳密）。
- `load-testing`: 階梯式加壓、每一輪留存環境與設定與結果、本機結果標明不是容量依據。

## Impact

- **新增**：`scripts/staging_smoke.py`、`loadtest/fake_llm/`（伺服器、Dockerfile、requirements）、`loadtest/shapes.py`；`app/config.py` 的 `GEMINI_BASE_URL` 與 `TRUST_PROXY_HEADERS`。
- **修改**：`app/__init__.py`（Gemini 轉接器帶入端點、開發環境的 ProxyFix）、`.env.example`（`GEMINI_BASE_URL`、`TRUST_PROXY_HEADERS`、`WEB_CONCURRENCY`、`GUNICORN_THREADS`）、`scripts/launch_plan.py` 的設定歸類（兩個 gunicorn 變數）。
- **文件**：營運與部署（開發用通道、本機預演、預備環境驗收、負載測試）、README 驗收紀錄與前提、路線圖（C-5b 範圍改為本機預演，新增 C-5c `cloud-staging-run` 與 C-5d `production-launch`）、`tests/test_design_docs.py` 與 `tests/test_launch_docs.py`。
- **風險**：①本機預演可能讓人誤以為雲端也沒問題——驗收紀錄強制分兩欄、關卡不因預演通過；②ngrok 會把本機服務暴露在網際網路上——文件要求通道端存取控制，預演只用 `.invalid` 測試帳號與可丟棄的 SQLite 檔案；③`GEMINI_BASE_URL` 是新的攻擊面——production 拒絕、只允許 https、值不回顯於錯誤訊息；④Brevo 寄件者的送達（免費信箱當寄件者可能進垃圾郵件）——預演一定先真的寄一封；⑤Docker 可能起不來——條件式，不阻擋封存；⑥我對 ngrok 的理解（本機的臨時網域，非雲端預備環境的網域）是依使用者的一句話推得，已記為假設。
