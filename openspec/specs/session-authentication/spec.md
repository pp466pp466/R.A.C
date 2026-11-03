# session-authentication Specification

## Purpose
定義會員如何登入並維持登入狀態：密碼雜湊與長度規則、登入失敗的通用訊息與鎖定、簽章 session cookie 與撤銷機制、登出、改密碼，以及帳號狀態對登入的影響。

> 注意（2026-11-02）：AGENTS.md 第 10 節已修訂（Google 登入改版），change ① `google-first-login` 已實作（登入、年齡確認、登入有效期、帳號鎖定、申訴與管理員二階段登入），其需求已於 2026-11-03 封存時同步進本規格；本規格中與玩家端密碼註冊、密碼登入、忘記密碼、信箱驗證有關的部分描述的是目前的實作，待 change ② 更新。

## Requirements


### Requirement: 密碼以 Argon2id 雜湊
系統 SHALL 以 Argon2id 雜湊密碼後才儲存，SHALL NOT 儲存或記錄明文密碼。密碼 SHALL 先經 Unicode NFKC 正規化再雜湊，設定與驗證使用同一規則。登入成功時，若既有雜湊的參數已低於目前設定，系統 SHALL 以新參數重新雜湊。

#### Scenario: 儲存的是雜湊
- **WHEN** 會員註冊後檢視資料庫中的密碼欄位
- **THEN** 內容為 Argon2id 雜湊字串，不含明文

#### Scenario: 全形與半形等價
- **WHEN** 以全形數字組成的密碼註冊，之後以對應的半形數字登入
- **THEN** 登入成功

#### Scenario: 舊參數的雜湊
- **WHEN** 雜湊以較弱的參數產生，且使用者以正確密碼登入成功
- **THEN** 資料庫中的雜湊被更新為目前參數

### Requirement: 登入失敗回應一致
信箱不存在、密碼錯誤、帳號被鎖定時，登入 SHALL 回應相同的狀態碼與繁體中文通用訊息，SHALL NOT 洩漏帳號是否存在或是否被鎖定。信箱不存在時，系統 SHALL 仍執行與驗證密碼相當的運算，使回應時間不致明顯短於密碼錯誤的情況。

#### Scenario: 信箱不存在
- **WHEN** 以不存在的信箱登入
- **THEN** 回應 401 與通用訊息

#### Scenario: 密碼錯誤
- **WHEN** 以存在的信箱與錯誤密碼登入
- **THEN** 回應與「信箱不存在」完全相同的狀態碼與訊息

#### Scenario: 帳號被鎖定時以正確密碼登入
- **WHEN** 帳號處於鎖定期間，使用者以正確密碼登入
- **THEN** 回應與密碼錯誤相同的通用訊息，且不建立登入狀態

### Requirement: 連續失敗鎖定帳號
同一帳號連續登入失敗 5 次，系統 SHALL 鎖定該帳號 15 分鐘。失敗計數與鎖定時間 SHALL 存放於資料庫，使多個 worker 共用。登入成功 SHALL 將失敗計數歸零。鎖定期間的嘗試 SHALL NOT 延長鎖定時間。並行的失敗嘗試 SHALL NOT 造成計數被低估。

#### Scenario: 第五次失敗
- **WHEN** 同一帳號連續第 5 次輸入錯誤密碼
- **THEN** 帳號被鎖定 15 分鐘

#### Scenario: 鎖定到期
- **WHEN** 鎖定期間已過，使用者以正確密碼登入
- **THEN** 登入成功，失敗計數歸零

#### Scenario: 成功登入重設計數
- **WHEN** 連續失敗 4 次後以正確密碼登入成功，接著再輸入 1 次錯誤密碼
- **THEN** 帳號不會被鎖定，失敗計數為 1

#### Scenario: 多個 worker 並行失敗
- **WHEN** 兩個請求同時對同一帳號送出錯誤密碼
- **THEN** 失敗計數增加 2

#### Scenario: 失敗的請求結束後計數仍在
- **WHEN** 同一帳號透過一連串各自獨立的 HTTP 請求送出 5 次錯誤密碼（每次回應 401）
- **THEN** 失敗計數隨之累積到 5，帳號被鎖定，之後以正確密碼登入仍被拒絕

### Requirement: 登入狀態使用簽章 Cookie
登入成功後，系統 SHALL 以簽章的 session cookie 維持登入，cookie 內容 SHALL 只包含使用者內部編號與 `token_version`（不放簽發時間或其他欄位），SHALL NOT 使用 JWT。cookie SHALL 設定 `HttpOnly`、`SameSite=Lax`；有效期為閒置 30 天、每次使用就續期，且 SHALL NOT 設絕對上限：每個辨識出登入者的回應 SHALL 以新的簽章與新的到期時間重新發出 cookie，沒有碰過登入狀態的回應（例如靜態檔、存活檢查）SHALL NOT 重發。staging 與 production SHALL 另設 `Secure` 並使用 `__Host-` 前綴名稱。登入 SHALL 使用全新的 session 內容，不沿用登入前的任何內容。撤銷 SHALL 只靠 `token_version` 與帳號狀態（cookie 被竊取後只要持續被使用就一直有效，這是已接受的取捨）。

#### Scenario: 登入成功
- **WHEN** 使用者通過登入
- **THEN** 回應設定了符合上述屬性的 session cookie，內容只含使用者編號與 `token_version`

#### Scenario: 正式環境的 cookie
- **WHEN** 在 production 登入成功
- **THEN** cookie 名稱以 `__Host-` 開頭，並帶有 `Secure`、`HttpOnly`、`SameSite=Lax`，且沒有 Domain 屬性

#### Scenario: 每次使用就續期
- **WHEN** 登入 20 天後的使用者再次請求需要登入的頁面
- **THEN** 回應重新發出 cookie，新的到期時間是回應當下起算的 30 天，之後第 45 天仍視為已登入

#### Scenario: 超過七天
- **WHEN** 登入已超過 7 天、但期間持續使用的 cookie 被送回
- **THEN** 仍視為已登入（不再有 7 天的絕對期限）

#### Scenario: 閒置超過 30 天
- **WHEN** 最後一次使用在 31 天前的 cookie 被送回
- **THEN** 系統視為未登入

#### Scenario: 沒有碰登入狀態的回應
- **WHEN** 帶著有效登入 cookie 請求 `/healthz` 或靜態檔
- **THEN** 回應沒有重發 cookie，也沒有查詢資料庫

#### Scenario: 被竄改的 cookie
- **WHEN** 送回內容被修改或簽章無效的 cookie
- **THEN** 系統視為未登入

#### Scenario: 沒有絕對上限
- **WHEN** 使用者連續每天使用超過 100 天
- **THEN** 仍維持登入，直到登出、`token_version` 改變或帳號被停用

### Requirement: 以 token_version 撤銷登入狀態
每次請求辨識登入狀態時，系統 SHALL 比對 cookie 內的 `token_version` 與資料庫中的值，不一致即視為未登入。改密碼、登出所有裝置、重設密碼、綁定 Google 身分與申請刪除帳號時，系統 SHALL 使 `token_version` 加 1。帳號狀態為 `suspended` 時，既有的登入狀態 SHALL 立即失效。狀態為 `deletion_pending` 的帳號，申請當下的既有登入狀態因 `token_version` 加 1 而失效；之後重新登入得到的受限登入狀態 SHALL 有效。

#### Scenario: 登出所有裝置
- **WHEN** 使用者在裝置 A 與 B 皆已登入，於 A 執行「登出所有裝置」
- **THEN** 裝置 B 下一次請求被視為未登入

#### Scenario: 改密碼後
- **WHEN** 使用者改密碼
- **THEN** 其他裝置的登入狀態失效，目前裝置以新的 `token_version` 維持登入

#### Scenario: 帳號被停權
- **WHEN** 已登入使用者的帳號狀態變為 `suspended`
- **THEN** 其下一次請求被視為未登入

#### Scenario: 申請刪除後所有裝置登出
- **WHEN** 使用者在裝置 A 與 B 皆已登入，於 A 申請刪除帳號
- **THEN** 兩個裝置下一次請求都被視為未登入

#### Scenario: 待刪除帳號重新登入後的狀態
- **WHEN** 待刪除的使用者重新登入成功
- **THEN** 取得的受限登入狀態在之後的請求中有效，直到被撤銷、登出或帳號被刪除

### Requirement: 登出
系統 SHALL 提供登出，清除目前裝置的 session cookie，且不影響其他裝置。

#### Scenario: 登出目前裝置
- **WHEN** 已登入使用者登出
- **THEN** 目前裝置之後的請求被視為未登入，其他裝置不受影響

### Requirement: 帳號狀態對登入的影響
密碼驗證成功後，狀態為 `active` 與 `pending_verification` 的帳號 SHALL 可登入；`pending_verification` 的帳號登入後 SHALL 只能使用驗證相關功能，不得使用客服。狀態為 `suspended` 的帳號 SHALL 被拒絕登入，回應 403 與繁體中文說明。狀態為 `deletion_pending` 的帳號 SHALL 可登入，但只得到受限登入狀態：回應中的帳號狀態為 `deletion_pending`，且除了查詢自己、撤銷刪除、登出與開啟帳號設定頁之外，其餘功能 SHALL 被拒絕。

#### Scenario: 待驗證帳號登入
- **WHEN** 狀態為 `pending_verification` 的使用者以正確密碼登入
- **THEN** 登入成功，帳號資訊顯示尚未驗證信箱

#### Scenario: 停權帳號登入
- **WHEN** 狀態為 `suspended` 的使用者以正確密碼登入
- **THEN** 回應 403 與說明，且不建立登入狀態

#### Scenario: 待刪除帳號登入
- **WHEN** 狀態為 `deletion_pending` 的使用者以正確密碼登入
- **THEN** 登入成功，回應中的帳號狀態為 `deletion_pending`，之後只能撤銷刪除或登出

#### Scenario: 待刪除帳號密碼錯誤
- **WHEN** 狀態為 `deletion_pending` 的使用者以錯誤密碼登入
- **THEN** 與一般帳號相同，回應通用的失敗訊息並計入失敗次數

### Requirement: 查詢目前登入者
系統 SHALL 提供查詢目前登入者的 API：已登入時回傳該會員自己的資料（對外識別碼、email、顯示名稱、會員等級、角色、帳號狀態、信箱是否已驗證、是否已設定密碼、是否已綁定 Google、是否已完成年齡確認、年齡群組 `minor` 或 `adult`），未登入時回應 401。尚未完成年齡確認者的年齡群組 SHALL 為空值。狀態為 `deletion_pending` 時回應 SHALL 另含預定刪除的時間。回應 SHALL NOT 包含密碼雜湊、內部編號或 `token_version`，也 SHALL NOT 包含生日、年齡區間、年齡確認的時間與政策版本。

#### Scenario: 已登入
- **WHEN** 已登入使用者查詢目前登入者
- **THEN** 回應其帳號資料，且不含密碼雜湊、內部編號與 `token_version`

#### Scenario: 未登入
- **WHEN** 未登入者查詢目前登入者
- **THEN** 回應 401 的 JSON 錯誤

#### Scenario: 年齡確認狀態
- **WHEN** 已完成與尚未完成年齡確認的使用者分別查詢目前登入者
- **THEN** 回應中的「是否已完成」分別為真與假，年齡群組分別為 `minor` 或 `adult`、以及空值，且都不含生日、年齡區間、確認時間與政策版本

#### Scenario: 生日到了之後
- **WHEN** 註冊時 17 歲的使用者在 18 歲生日之後查詢目前登入者
- **THEN** 年齡群組為 `adult`，不需要重新登入或補填

#### Scenario: 待刪除帳號
- **WHEN** 狀態為 `deletion_pending` 的使用者查詢目前登入者
- **THEN** 回應含帳號狀態與預定刪除的時間（申請時間加 7 天）

### Requirement: 變更密碼
已登入且已設定密碼的使用者 SHALL 能在提供目前密碼的前提下變更密碼；新密碼須符合長度規則且不得與目前密碼相同。目前密碼錯誤時，系統 SHALL 回應 400 並計入登入失敗次數。變更成功後，系統 SHALL 使其他裝置的登入狀態失效；信箱驗證開啟時 SHALL 寄出密碼已變更的通知信。

#### Scenario: 目前密碼錯誤
- **WHEN** 使用者提供錯誤的目前密碼
- **THEN** 回應 400，密碼不變，且失敗次數增加

#### Scenario: 變更成功
- **WHEN** 使用者提供正確的目前密碼與合規的新密碼
- **THEN** 密碼更新，其他裝置被登出，目前裝置維持登入

#### Scenario: 沒有密碼的帳號
- **WHEN** 純 Google 帳號（沒有密碼）呼叫變更密碼
- **THEN** 回應 400，說明目前無法以此方式設定密碼

### Requirement: 認證回應不可被快取
認證相關的 API 回應與含權杖的頁面 SHALL 禁止被瀏覽器或中介快取。

#### Scenario: 回應標頭
- **WHEN** 請求任一 `/api/auth/` 端點，或開啟信箱驗證與重設密碼的確認頁
- **THEN** 回應標頭指示不得快取

### Requirement: 登入狀態的辨識不影響存活檢查
存活檢查與靜態資源的請求 SHALL NOT 因辨識登入狀態而存取資料庫。

#### Scenario: 帶著登入 cookie 請求存活檢查
- **WHEN** 資料庫停止，且請求帶有有效的登入 cookie 並呼叫存活檢查
- **THEN** 回應 200

