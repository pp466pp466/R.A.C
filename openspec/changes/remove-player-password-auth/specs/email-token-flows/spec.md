# Spec Delta

## MODIFIED Requirements

### Requirement: 權杖只存雜湊且單次使用
重設密碼的權杖 SHALL 為 32 位元組的隨機值，資料庫 SHALL 只保存其 SHA-256 雜湊，SHALL NOT 保存原值。每個權杖 SHALL 只能使用一次，使用的判定與標記 SHALL 為原子操作，使並行的兩次使用最多一次成功。重設密碼權杖 SHALL 於 1 小時後失效。為同一帳號發放新的重設權杖時，舊的未使用權杖 SHALL 立即失效。系統 SHALL NOT 再發放信箱驗證用途的權杖。

#### Scenario: 資料庫不含權杖原值
- **WHEN** 發放權杖後檢視資料庫
- **THEN** 只找得到雜湊，找不到可用於完成動作的原值

#### Scenario: 權杖重複使用
- **WHEN** 同一個權杖被使用兩次
- **THEN** 第一次成功，第二次被拒絕

#### Scenario: 並行使用
- **WHEN** 兩個請求同時使用同一個權杖
- **THEN** 只有一個請求成功

#### Scenario: 權杖過期
- **WHEN** 在失效時間之後使用權杖
- **THEN** 被拒絕，帳號不變

#### Scenario: 新權杖取代舊權杖
- **WHEN** 同一個管理員重新發放重設權杖
- **THEN** 先前發放且未使用的重設權杖不再可用

### Requirement: 提交成功後才寄信，寄信失敗不影響結果
系統 SHALL 在資料庫交易成功提交後才寄信；交易回復時 SHALL NOT 寄信。單次寄送的等待上限 SHALL 為 5 秒。寄信失敗時，系統 SHALL 記錄失敗事件（不含收件人地址與權杖），原操作的回應與已提交的資料 SHALL 不變，使用者可稍後重試。

#### Scenario: 寄信服務無回應
- **WHEN** 寄信服務逾時
- **THEN** 管理員的忘記密碼仍回應通用訊息，日誌記錄寄信失敗事件，不含地址與權杖

#### Scenario: 交易失敗
- **WHEN** 發放重設權杖的流程在提交前發生錯誤而回復
- **THEN** 不寄出任何信件

## ADDED Requirements

### Requirement: 管理員重設密碼
重設密碼 SHALL 只服務管理員：只有在管理員密碼驗證步驟按「忘記密碼」（見 `admin-two-step-login`）才會發放並寄出重設權杖，系統 SHALL NOT 提供匿名的忘記密碼入口。重設權杖的寄送 SHALL 受 60 秒間隔與每小時 5 封的限制（超過限制時回應仍相同，只是不寄信）。重設密碼 SHALL 以有效權杖與符合長度規則的新密碼完成，且權杖所屬帳號 SHALL 為管理員，否則視同無效權杖；成功後 SHALL 使該帳號的 `token_version` 加 1、清除登入失敗計數與鎖定，並寄出密碼已變更的通知信。

#### Scenario: 重設成功
- **WHEN** 管理員以有效權杖與合規的新密碼重設
- **THEN** 密碼更新，所有既有登入狀態失效，鎖定被清除，並寄出通知信

#### Scenario: 沒有匿名的忘記密碼
- **WHEN** 呼叫 `POST /api/auth/forgot-password` 或開啟 `/forgot-password`
- **THEN** 回應 404

#### Scenario: 非管理員的權杖無效
- **WHEN** 以屬於非管理員帳號的重設權杖完成重設（例如遷移前遺留的權杖）
- **THEN** 回應與無效權杖相同的通用錯誤，密碼不變

#### Scenario: 無效權杖
- **WHEN** 以不存在、過期或已用過的權杖重設
- **THEN** 回應相同的通用錯誤，不透露原因差異

### Requirement: 重設密碼信的內容與編碼
系統寄出的信件 SHALL 為 UTF-8 編碼的繁體中文純文字，連結 SHALL 以設定的對外網址組成，SHALL NOT 使用請求的主機標頭。收件人地址含換行等控制字元時 SHALL 拒絕寄送。重設密碼信 SHALL 說明有效期限，並提醒「若非本人操作請忽略」。

#### Scenario: 重設信內容
- **WHEN** 寄出重設密碼信
- **THEN** 信件為 UTF-8，內容為繁體中文，連結以對外網址開頭並帶有權杖片段

#### Scenario: 偽造主機標頭
- **WHEN** 請求的主機標頭被竄改後觸發管理員的忘記密碼
- **THEN** 信件中的連結仍使用設定的對外網址

### Requirement: 寄信設定缺少時停用管理員的重設流程
寄信器或對外網址（`BASE_URL`）缺少時，系統 SHALL 停用管理員的忘記密碼端點與 `/reset-password` 頁面及其完成端點（回應 404），密碼驗證頁 SHALL NOT 顯示「忘記密碼」入口，且 SHALL NOT 寄出任何信件。系統 SHALL NOT 以日誌輸出權杖來取代寄信。

#### Scenario: 沒有寄信設定
- **WHEN** 沒有寄信設定，管理員呼叫忘記密碼端點或開啟 `/reset-password`
- **THEN** 回應 404，沒有寄信

#### Scenario: 日誌不含權杖
- **WHEN** 沒有寄信設定，管理員完成 Google 與密碼登入
- **THEN** 日誌沒有任何權杖

## REMOVED Requirements

### Requirement: 信箱驗證
**Reason**: 玩家的信箱由 Google 回傳的 `email_verified` 驗證，建立帳號時 `email_verified_at` 即有值；沒有信箱驗證信與驗證端點。
**Migration**: 無。`POST /api/auth/verify-email` 與 `/verify-email` 頁面回 404；遺留的未驗證帳號在以同信箱的 Google 登入時依既有合併規則（見 `google-sign-in`）視為已驗證。

### Requirement: 重寄驗證信
**Reason**: 沒有驗證信可重寄。
**Migration**: 無。`POST /api/auth/resend-verification` 回 404。

### Requirement: 信箱驗證關閉時停用相關功能
**Reason**: `EMAIL_VERIFICATION_REQUIRED` 已移除，不再有「驗證開關」；管理員重設流程是否可用改由寄信設定決定。
**Migration**: 見新增的「寄信設定缺少時停用管理員的重設流程」。

### Requirement: 忘記密碼與重設密碼
**Reason**: 玩家不再有忘記密碼；重設密碼只服務管理員，且不再讓純 Google 帳號藉此設定密碼、也不再把重設視為信箱驗證。
**Migration**: 見「管理員重設密碼」。

### Requirement: 信件內容與編碼
**Reason**: 驗證信已移除，信件只剩重設密碼信與密碼已變更通知。
**Migration**: 見「重設密碼信的內容與編碼」。
