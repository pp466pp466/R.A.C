# 部署與正式環境 (Deployment)

對應 AGENTS.md 第 9 節。

- 部署方式：正式環境以 Docker 映像部署於雲端 PaaS（容器型），並使用託管的 SQL Server；雲端平台為 Google Cloud（Cloud Run、Cloud SQL for SQL Server、Secret Manager）。映像檔須包含 static/vendor/ 與 static/fonts/，以非 root 使用者執行，且不得包含 .env。
- 正式環境不得使用 Flask 開發伺服器（app.run）。正式環境以 gunicorn（gthread worker）運行，worker 數與執行緒數依負載測試結果設定；客服與 RPG 串流使用獨立的 worker 池。（RPG 適用）SSE 端點須運行於支援長連線並發的正式伺服器配置。
- 入口層須啟用 HTTPS 與 HTTP/2 並設定 HSTS；Flask 以 ProxyFix 信任轉發標頭，否則 Secure cookie 與 OAuth 回呼網址會判斷錯誤。
- 多 worker 運行時，應用程式不得依賴行程內記憶體保存狀態（如快取、串流進度、限流計數）；狀態須存放於資料庫或其他共用儲存。限流第一道使用入口層（如 Nginx limit_req），帳號層級的計數與鎖定存放於資料庫。
- 模型供應商的 API Key 只能由後端從環境變數讀取，嚴禁寫入程式碼、模板或版本庫：客服使用 Gemini，變數名稱 GEMINI_API_KEY（只有客服使用）；RPG 用途使用 OpenAI 相容端點，變數名稱 OPENROUTER_API_KEY（與 OPENROUTER_MODEL 成對，不使用 GEMINI_API_KEY）。本機開發以 `.env` 檔注入（`.env` 不進版本庫），正式環境以平台機密功能注入。
- 環境與機密管理：
  - 以環境變數 APP_ENV（development、testing、staging、production）選擇設定類別；未設定時拒絕啟動，不預設為 development。staging 為部署於雲端的預備環境，設定與 production 相同（Secure cookie、禁用除錯、不得使用 sa 與 TrustServerCertificate），唯一的例外是 staging 可設定 GEMINI_BASE_URL，把 Gemini 轉接器指向隔離壓測環境的假模型伺服器（production 設定了即啟動失敗，值須為 https 且不得含帳密）。TRUST_PROXY_HEADERS（開發環境經 HTTPS 通道時信任轉送標頭）只有 development 可以設定，其餘環境設定了即啟動失敗。staging 與 production 缺少必要機密時，應用程式啟動即失敗 (fail-fast)。
  - 所有機密（Gemini 金鑰、OpenRouter 金鑰、SECRET_KEY、資料庫連線、OAuth、寄信服務）採同一套注入機制，不得另設例外；機密不得出現在日誌、錯誤訊息或前端回應中。
  - 金鑰至少每 90 天輪替，疑似外洩時立即輪替；輪替流程為「建立新金鑰 → 更新機密 → 重新部署 → 驗證 → 停用舊金鑰」。SECRET_KEY 輪替可暫用 SECRET_KEY_FALLBACKS（Flask 3.1 以上）。
- 資料庫連線：連線帳號與密碼僅存於 DATABASE_URL（`.env` 或平台機密）。本機開發的 SQL Server 連線可設定 TrustServerCertificate=yes 以信任自簽憑證；正式環境嚴禁使用，須驗證伺服器憑證，且不得使用 sa 帳號。
- 專案提供 `.env.example` 列出所需變數名稱，不含實際值。
- 開發與正式設定須分離，正式環境不得沿用開發設定（如除錯模式、本機資料庫路徑）。
