# aws-demo-ingress Specification

## Purpose
定義展示環境的對外入口：免費子網域指向固定位址、反向代理終止 HTTPS 並啟用 HTTP/2、把 RPG 串流與其餘請求分流到各自的 worker 池且不緩衝串流，以及 Google 登入的對外網址設定。

## Requirements

### Requirement: 固定位址與子網域
主機 SHALL 使用固定的公開位址（Elastic IP），展示網域（`rac001.duckdns.org`）的 A 記錄 SHALL 指向該位址。更新網域記錄所需的權杖 SHALL 視為機密：不得出現在命令列、日誌、版本庫或腳本輸出。

#### Scenario: 網域指向主機
- **WHEN** 查詢展示網域的 A 記錄
- **THEN** 結果等於主機的 Elastic IP

#### Scenario: 權杖不外洩
- **WHEN** 預覽或執行更新網域記錄的步驟
- **THEN** 輸出與命令列中沒有權杖的值

### Requirement: HTTPS、HTTP/2 與自動憑證
入口 SHALL 對展示網域提供有效的 HTTPS（由憑證機構自動簽發並自動更新）與 HTTP/2；HTTP 請求 SHALL 導向 HTTPS。憑證與帳戶資料 SHALL 存放於持久的主機路徑，容器重建不得造成重新簽發。應用程式 SHALL 仍負責送出 HSTS 與安全標頭，入口不得移除它們。

#### Scenario: 有效憑證
- **WHEN** 以標準憑證驗證連到 `https://rac001.duckdns.org`
- **THEN** TLS 驗證成功且協商為 HTTP/2

#### Scenario: HTTP 導向
- **WHEN** 請求 `http://rac001.duckdns.org/`
- **THEN** 回應導向對應的 https 網址

#### Scenario: 標頭保留
- **WHEN** 檢視經入口回應的首頁標頭
- **THEN** 含應用程式送出的 HSTS 與其餘安全標頭

### Requirement: 客服池與 RPG 串流池依路徑分流
入口 SHALL 把 `/api/game/` 開頭的請求轉到 RPG 串流池（`gunicorn.rpg.conf.py`），其餘請求轉到客服池（`gunicorn.conf.py`）；兩個池 SHALL 是各自獨立的容器，使長連線的串流不佔用客服的執行緒。入口 SHALL 對 RPG 池關閉回應緩衝，使事件與心跳逐塊送達，且 SHALL NOT 在入口層對串流設定短於應用程式心跳與生成上限的閒置逾時。

#### Scenario: 路徑分流
- **WHEN** 分別請求 `/api/game/…` 與 `/api/cs/…`
- **THEN** 前者由 RPG 池處理，後者由客服池處理

#### Scenario: 串流逐塊送達
- **WHEN** 經入口接收一個 RPG 劇情串流
- **THEN** 事件與心跳在產生時即抵達客戶端，而不是在串流結束時一次送達

#### Scenario: 池彼此獨立
- **WHEN** 停止客服池容器
- **THEN** RPG 池仍能回應 `/healthz`

### Requirement: 信任入口的轉送標頭但不開放直連
應用程式 SHALL 只信任單層入口轉送的協定與主機標頭（沿用 staging 既有行為），且兩個池 SHALL 只監聽主機內部位址，SHALL NOT 對外開放連接埠，使請求只能經入口抵達。

#### Scenario: 無法繞過入口
- **WHEN** 從主機外部直接連線應用程式容器的連接埠
- **THEN** 連線被拒絕

### Requirement: Google 登入使用展示網域的回呼網址
`BASE_URL` SHALL 為展示網域的 https 網址（不含路徑），Google 預備環境專用的 OAuth 用戶端 SHALL 只列出該網域的回呼網址（`/auth/google/callback`），SHALL NOT 重用開發用或 Firebase 自動建立的用戶端。Google 是否接受該網域 SHALL 由使用者人工驗收；不接受時 SHALL 標為未通過並改用備用網域，不得改以放寬登入檢查代替。

#### Scenario: 回呼網址
- **WHEN** 發起 Google 登入
- **THEN** 授權請求帶有的回呼網址等於展示網域加 `/auth/google/callback`

#### Scenario: 網域不被接受
- **WHEN** Google 拒絕該網域作為重新導向 URI
- **THEN** 驗收紀錄標為未通過並說明原因，且沒有任何登入檢查被放寬
