# Spec Delta

## Purpose

定義以 Google 帳號註冊與登入的行為：授權碼流程的防護、ID token 的驗證、Google 身分與本機帳號共用同一個會員帳號的合併規則，以及 Google 登入為選用功能。

## ADDED Requirements

### Requirement: Google 登入為選用功能
只有當 Google 用戶端識別碼與密鑰兩者都設定時，系統 SHALL 啟用 Google 登入並在登入與註冊頁顯示入口；兩者都未設定時，SHALL 隱藏入口且相關端點回應 404。只設定其中一個時，系統 SHALL 啟動失敗。開發與正式環境 SHALL 使用不同的 Google 用戶端。

#### Scenario: 未設定
- **WHEN** 沒有設定任何 Google 用戶端資訊
- **THEN** 登入頁不顯示 Google 入口，啟動 Google 登入的 API 回應 404

#### Scenario: 只設定一半
- **WHEN** 只設定了用戶端識別碼而沒有密鑰
- **THEN** 啟動失敗，訊息指出缺少的變數名稱

### Requirement: 授權碼流程的防護
啟動 Google 登入時，系統 SHALL 產生一次性的 `state`、`nonce` 與 PKCE 驗證碼，並綁定在發起者的瀏覽器上（短效、10 分鐘、只供此流程使用）。回呼時，系統 SHALL 驗證 `state` 與該綁定一致後才處理授權碼，且無論成功或失敗都 SHALL 清除該綁定。`state` 不符、綁定遺失或已過期時 SHALL 拒絕並回到登入頁顯示通用錯誤。授權請求 SHALL 只要求 `openid email profile`。

#### Scenario: state 不符
- **WHEN** 回呼帶來的 `state` 與發起時的不同
- **THEN** 流程被拒絕，不建立登入狀態，並導向登入頁顯示錯誤

#### Scenario: 沒有綁定
- **WHEN** 瀏覽器沒有發起過流程就直接開啟回呼網址
- **THEN** 流程被拒絕

#### Scenario: 綁定逾時
- **WHEN** 發起後超過 10 分鐘才完成回呼
- **THEN** 流程被拒絕

#### Scenario: 綁定不可重複使用
- **WHEN** 同一個回呼網址被送出第二次
- **THEN** 第二次被拒絕

### Requirement: ID token 驗證
系統 SHALL 驗證 Google 回傳的 ID token：簽章須由 Google 的公開金鑰驗證通過（只接受 RS256）、發行者須為 Google、受眾須為本系統的用戶端識別碼、未過期、`nonce` 與發起時一致、並要求信箱已被 Google 驗證且有穩定的使用者識別。任何一項不符 SHALL 拒絕登入，且不建立或變更任何帳號。呼叫 Google 的等待上限 SHALL 為 5 秒，逾時或失敗時導向登入頁顯示「Google 登入暫時無法使用」。

#### Scenario: 信箱未被 Google 驗證
- **WHEN** ID token 的信箱驗證標記不為真
- **THEN** 拒絕登入，不建立帳號

#### Scenario: 受眾不符
- **WHEN** ID token 是發給其他用戶端的
- **THEN** 拒絕登入

#### Scenario: nonce 不符
- **WHEN** ID token 內的 `nonce` 與發起時的不同
- **THEN** 拒絕登入

#### Scenario: 簽章無效或演算法不是 RS256
- **WHEN** ID token 簽章驗證失敗，或宣告使用其他簽章演算法
- **THEN** 拒絕登入

#### Scenario: Google 無法連線
- **WHEN** 向 Google 交換授權碼逾時
- **THEN** 導向登入頁並顯示服務暫時無法使用，不建立登入狀態

### Requirement: Google 身分與帳號的對應
系統 SHALL 以 Google 的穩定使用者識別（而非信箱）辨識已綁定的身分：身分已綁定時直接登入該帳號，不因 Google 端信箱變更而重新比對。身分尚未綁定時，系統 SHALL 以信箱的重複判斷正規形式尋找本機帳號。每個帳號 SHALL 最多綁定一個 Google 身分，同一個 Google 身分 SHALL 只能綁定一個帳號。

#### Scenario: 已綁定的身分
- **WHEN** 已綁定的 Google 身分登入，且其信箱已改成別的地址
- **THEN** 仍登入原本綁定的帳號

#### Scenario: 帳號已綁定另一個 Google 身分
- **WHEN** 本機帳號已綁定某 Google 身分，另一個 Google 身分以相同信箱登入
- **THEN** 拒絕登入，不新增綁定

### Requirement: 同信箱帳號合併規則
當 Google 身分尚未綁定且信箱已有本機帳號時：若該帳號的信箱已驗證，系統 SHALL 將 Google 身分綁定到該帳號並登入；若該帳號的信箱未驗證（包含信箱驗證關閉期間註冊的帳號），系統 SHALL 清除其密碼憑證、將信箱視為已驗證、使其 `token_version` 加 1（撤銷任何以舊密碼建立的登入狀態），再綁定並登入。信箱沒有任何帳號時，系統 SHALL 建立新帳號（已驗證、沒有密碼）並綁定。

#### Scenario: 合併到已驗證帳號
- **WHEN** 信箱已驗證的本機帳號用同信箱的 Google 登入
- **THEN** Google 身分被綁定，原有密碼仍可使用

#### Scenario: 預先佔用信箱
- **WHEN** 攻擊者先以他人的信箱註冊並設定密碼（未驗證），該信箱的真正擁有者之後用 Google 登入
- **THEN** 該帳號的密碼憑證被清除、信箱視為已驗證、攻擊者既有的登入狀態失效，攻擊者無法再以原密碼登入

#### Scenario: 全新信箱
- **WHEN** 信箱沒有任何帳號，使用者以 Google 登入
- **THEN** 建立啟用狀態的新帳號，信箱已驗證、沒有密碼，並綁定 Google 身分

#### Scenario: 信箱已有純 Google 帳號時以信箱註冊
- **WHEN** 信箱已有 Google 帳號，有人以該信箱註冊
- **THEN** 不建立新帳號，回應通用訊息

### Requirement: 首次以 Google 註冊須同意條款
需要建立新帳號時，系統 SHALL 要求發起流程時已明確同意服務條款與隱私權政策，並記錄條款版本與同意時間；未同意時 SHALL 不建立帳號，導向登入頁提示須先勾選同意。已存在帳號的登入不需要再次勾選。

#### Scenario: 新使用者未勾選同意
- **WHEN** 沒有帳號的使用者未勾選同意就以 Google 登入
- **THEN** 不建立帳號，導向登入頁並提示須先同意條款

#### Scenario: 新使用者已勾選同意
- **WHEN** 沒有帳號的使用者勾選同意後以 Google 登入
- **THEN** 建立帳號並記錄條款版本與同意時間

### Requirement: Google 登入的帳號狀態與結果
透過 Google 登入的帳號若為 `suspended` 或 `deletion_pending`，系統 SHALL 拒絕登入並顯示說明，且不建立登入狀態。登入成功時，登入狀態的建立方式與屬性 SHALL 與密碼登入相同，並更新最後登入時間。顯示名稱 SHALL 取自 Google 提供的名稱（最長 50 字），缺少時以信箱的本機部分代替。

#### Scenario: 停權帳號
- **WHEN** 已停權的帳號以 Google 登入
- **THEN** 導向登入頁並顯示帳號已停用，不建立登入狀態

#### Scenario: 缺少名稱
- **WHEN** Google 沒有提供名稱
- **THEN** 顯示名稱使用信箱的本機部分
