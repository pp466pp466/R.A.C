# Spec Delta

## Purpose
定義內容程度的三種模式（安全、一般、成人）：它們的順序、玩家偏好與預設、年齡上限、實際生效模式的計算、成人模式的可用條件與成年確認的紀錄、部署層的設定與已淘汰的舊變數，以及讓玩家查詢與切換的 API。取代原本以 `sexual_content_level` 0 至 3 表示的分級。

## ADDED Requirements

### Requirement: 三種內容模式與順序
內容模式 SHALL 只有 `safe`（安全模式）、`standard`（一般模式）、`adult`（成人模式）三種，並依 `safe`、`standard`、`adult` 由保守到寬鬆排序；兩個模式「取較保守者」SHALL 以這個順序決定。未指定或無法判定時 SHALL 取最保守的 `safe`。

#### Scenario: 順序
- **WHEN** 比較 `standard` 與 `adult`
- **THEN** `standard` 較保守

#### Scenario: 無法判定
- **WHEN** 內容模式的輸入缺少或不是三種之一
- **THEN** 視為 `safe`

### Requirement: 年齡上限
一位使用者可用的內容模式上限 SHALL 由集中的權限政策依生日於「當下」計算的年齡決定：未滿 18 歲、或尚未完成年齡確認者，上限 SHALL 固定為 `safe`，不論偏好、劇本宣告、環境設定或角色為何都不得提高；已滿 18 歲者上限為 `standard`，只有在成人模式可用（見下方）時才是 `adult`。管理員與各會員等級 SHALL NOT 豁免。年齡 SHALL 只來自集中的年齡規則，不在這個能力內嵌任何年齡數字。

#### Scenario: 未成年
- **WHEN** 15 歲的使用者詢問可用的模式
- **THEN** 上限為 `safe`

#### Scenario: 尚未完成年齡確認
- **WHEN** 尚未填寫生日的使用者詢問可用的模式
- **THEN** 上限為 `safe`

#### Scenario: 成年
- **WHEN** 25 歲的使用者詢問可用的模式，且成人模式不可用
- **THEN** 上限為 `standard`

#### Scenario: 管理員不豁免
- **WHEN** 15 歲的管理員詢問可用的模式
- **THEN** 上限為 `safe`

#### Scenario: 開關不影響未成年者
- **WHEN** 成人模式的部署開關為開啟，15 歲的使用者詢問可用的模式
- **THEN** 上限仍為 `safe`

### Requirement: 玩家的內容模式偏好與預設
每位使用者 SHALL 有一個內容模式偏好（`safe`、`standard`、`adult` 之一），預設為最保守的 `safe`。年齡確認成功的當下 SHALL 依確認時的年齡設定偏好：已滿 18 歲為 `standard`，未滿 18 歲為 `safe`。既有帳號 SHALL 依其確認當時是否成年回填同樣的值。成年玩家 SHALL 可以自行選擇 `safe`。

#### Scenario: 成年者確認年齡
- **WHEN** 25 歲的新使用者完成年齡確認
- **THEN** 偏好為 `standard`

#### Scenario: 未成年者確認年齡
- **WHEN** 15 歲的新使用者完成年齡確認
- **THEN** 偏好為 `safe`

#### Scenario: 既有帳號回填
- **WHEN** 套用遷移時，一個確認當時已成年的既有帳號與一個確認當時未成年的既有帳號
- **THEN** 前者的偏好為 `standard`，後者為 `safe`

#### Scenario: 成年者選擇安全模式
- **WHEN** 成年玩家把偏好設為 `safe`
- **THEN** 設定成功，生效模式為 `safe`

### Requirement: 生效的內容模式取最保守者
實際生效的內容模式 SHALL 是下列各項中最保守的一個：玩家的偏好、年齡上限、劇本宣告的模式（有劇本時）、存檔記錄的模式（有存檔時）。成人模式不可用時，偏好為 `adult` 的玩家 SHALL 退為 `standard` 而不是出錯；存檔記錄的模式只能在上限之內被調整，SHALL NOT 因玩家輸入曖昧內容而被自動調高。

#### Scenario: 取四者最保守
- **WHEN** 偏好 `adult`、年齡上限 `adult`、劇本宣告 `standard`、存檔 `safe`
- **THEN** 生效模式為 `safe`

#### Scenario: 成人模式不可用時退為一般
- **WHEN** 偏好為 `adult`，但部署開關已關閉
- **THEN** 生效模式為 `standard`，沒有錯誤

#### Scenario: 不自動調高
- **WHEN** 生效模式為 `safe` 的玩家在遊戲中輸入曖昧的內容
- **THEN** 生效模式不變，系統只提醒玩家可以自行調整

### Requirement: 滿 18 歲不自動調高
使用者滿 18 歲後，年齡上限 SHALL 自動從 `safe` 解除，但玩家的偏好與既有存檔記錄的模式 SHALL NOT 自動改變；玩家 SHALL 自行切換才會調高。查詢內容模式的回應 SHALL 讓介面能判斷「現在可以切換為一般模式」並顯示提示。

#### Scenario: 生日當天
- **WHEN** 偏好為 `safe` 的使用者在 18 歲生日之後再次查詢
- **THEN** 偏好仍為 `safe`，生效模式仍為 `safe`，可選的模式包含 `standard`

#### Scenario: 玩家自行切換
- **WHEN** 該使用者把偏好設為 `standard`
- **THEN** 設定成功，生效模式變為 `standard`

#### Scenario: 既有存檔不變
- **WHEN** 使用者滿 18 歲後，他在未成年時開的存檔
- **THEN** 存檔記錄的模式仍是 `safe`

### Requirement: 成人模式的可用條件與成年確認
成人模式 SHALL 同時滿足下列條件才可用：部署層的開關為開啟、使用者已滿 18 歲（由生日於當下計算）、使用者在目前版本的安全規範下完成了成年確認、玩家的偏好為 `adult`。成年確認 SHALL 在玩家切換為成人模式時完成：玩家 SHALL 同時確認「我確定已滿 18 歲」與「同意安全規範」兩項；兩項缺一 SHALL 被拒絕並指出缺哪一項。確認成功 SHALL 記錄確認時間、安全規範的版本與確認方式（目前為自行聲明）。安全規範的版本改變後，先前的確認 SHALL 視為失效，須重新確認。回到 `safe` 或 `standard` SHALL NOT 清除確認紀錄。系統 SHALL NOT 蒐集證件或接第三方驗證。

#### Scenario: 首次切換為成人模式
- **WHEN** 成年玩家在部署開關開啟時送出切換為 `adult`，且兩項確認都為真
- **THEN** 偏好變為 `adult`，並記錄確認時間、規範版本與方式為自行聲明

#### Scenario: 缺少確認
- **WHEN** 送出切換為 `adult`，但沒有確認「同意安全規範」
- **THEN** 回應 400，錯誤指出缺少的那一項，偏好不變

#### Scenario: 部署開關關閉
- **WHEN** 部署開關為關閉時送出切換為 `adult`
- **THEN** 回應 403 與代碼 `adult_mode_unavailable`，偏好不變

#### Scenario: 未成年者
- **WHEN** 未滿 18 歲的使用者送出切換為 `adult` 或 `standard`
- **THEN** 回應 403 與代碼 `content_mode_restricted`，偏好不變

#### Scenario: 已確認後再次切換
- **WHEN** 已在目前規範版本下確認過的玩家，從 `standard` 再切換為 `adult`
- **THEN** 不需要重新勾選，直接成功

#### Scenario: 規範版本改變
- **WHEN** 安全規範的版本更新，先前確認過的玩家的偏好仍是 `adult`
- **THEN** 生效模式退為 `standard`，直到玩家重新確認

#### Scenario: 離開成人模式不清除紀錄
- **WHEN** 玩家從 `adult` 切換回 `standard`
- **THEN** 確認紀錄仍在

### Requirement: 內容模式的查詢與切換 API
系統 SHALL 提供需要登入的 `GET /api/account/content-mode` 與 `PUT /api/account/content-mode`，所有非 GET 請求 SHALL 通過 CSRF 檢查。回應 SHALL 只含：偏好、生效模式、每種模式目前是否可選、成人模式不可用時的原因（`disabled`、`not_adult`、`needs_confirmation` 之一或空）與是否已完成目前版本的成年確認；SHALL NOT 含生日、年齡區間、年齡確認時間或內部編號。尚未完成年齡確認者 SHALL 得到 403 與代碼 `age_confirmation_required`，未登入得到 401，模式不是三種之一得到 400。被鎖定或待刪除的帳號 SHALL 依既有的受限登入規則被拒絕。模式變更成功 SHALL 記錄結構化事件，內容只含事件名稱、使用者編號與新的模式列舉值。

#### Scenario: 查詢
- **WHEN** 成年玩家查詢
- **THEN** 回應含偏好、生效模式與每種模式是否可選，不含生日或年齡區間

#### Scenario: 未登入
- **WHEN** 未登入者查詢
- **THEN** 回應 401

#### Scenario: 尚未完成年齡確認
- **WHEN** 尚未完成年齡確認的使用者查詢或切換
- **THEN** 回應 403 與代碼 `age_confirmation_required`

#### Scenario: 不合法的模式
- **WHEN** 切換為 `mature`
- **THEN** 回應 400，偏好不變

#### Scenario: 缺少 CSRF 權杖
- **WHEN** 沒有 CSRF 權杖的 PUT
- **THEN** 被 CSRF 檢查拒絕

#### Scenario: 變更紀錄不含內容
- **WHEN** 成功切換模式後檢視日誌
- **THEN** 有一筆事件，只含事件名稱、使用者編號與新的模式，沒有確認勾選的文字或其他個資

### Requirement: 成人模式預設關閉且只能以環境設定開啟
環境變數 `ADULT_MODE_ENABLED` SHALL 控制成人模式的部署開關，預設為關閉，SHALL NOT 在程式中寫死為開啟。`development`、`testing`、`staging` 與 `production` 在設定為開啟且其他條件齊備時都可啟動；`staging` 與 `production` SHALL 明確設定才生效。值不是布林 SHALL 使應用程式啟動失敗，訊息只指出變數名稱而不含其值。

#### Scenario: 沒有設定
- **WHEN** 沒有設定 `ADULT_MODE_ENABLED`
- **THEN** 成人模式為關閉

#### Scenario: 開發環境開啟
- **WHEN** `APP_ENV` 為 `development`、開關為開啟且其他條件齊備
- **THEN** 應用程式可啟動，成人模式的部署開關為開啟

#### Scenario: 正式環境明確開啟
- **WHEN** `APP_ENV` 為 `production`、開關為開啟且許可的模型清單涵蓋 RPG 用途的模型
- **THEN** 應用程式可啟動，成人模式的部署開關為開啟

#### Scenario: 值不合法
- **WHEN** 開關的值是 `maybe`
- **THEN** 應用程式啟動失敗，訊息指出 `ADULT_MODE_ENABLED` 而不含其值

### Requirement: 開啟成人模式時須記錄許可涵蓋的模型
開啟成人模式時，`ADULT_MODE_MODELS`（逗號分隔的模型識別碼，代表取得書面許可所涵蓋的模型）SHALL 非空，且 RPG 用途的主要模型與備援模型 SHALL 全部在清單內，RPG 用途 SHALL 已設定；任一條件不成立 SHALL 使應用程式啟動失敗，訊息只指出變數名稱。清單 SHALL NOT 影響開關關閉時的行為。

#### Scenario: 清單涵蓋模型
- **WHEN** 開關開啟，RPG 用途的主要模型與備援模型都在清單內
- **THEN** 應用程式可啟動

#### Scenario: 備援模型不在清單內
- **WHEN** 開關開啟，但 RPG 用途的備援模型不在清單內
- **THEN** 應用程式啟動失敗，訊息指出 `ADULT_MODE_MODELS`

#### Scenario: 清單為空
- **WHEN** 開關開啟且沒有設定 `ADULT_MODE_MODELS`
- **THEN** 應用程式啟動失敗，訊息指出 `ADULT_MODE_MODELS`

#### Scenario: 沒有設定 RPG 用途
- **WHEN** 開關開啟，但沒有設定 RPG 用途的供應商
- **THEN** 應用程式啟動失敗，訊息指出 RPG 用途尚未設定

#### Scenario: 開關關閉時不驗證清單
- **WHEN** 開關關閉，而清單設定了不存在的模型
- **THEN** 應用程式可啟動

### Requirement: 舊的等級 3 環境變數已淘汰且不會開啟任何東西
`SEXUAL_CONTENT_LEVEL3_ENABLED` 與 `SEXUAL_CONTENT_LEVEL3_MODELS` SHALL 被忽略：不論值為何，成人模式都 SHALL NOT 因它們而開啟，也 SHALL NOT 因它們而啟動失敗。環境中出現其中任何一個 SHALL 在啟動時記錄一則警告，只指出變數名稱與已淘汰，SHALL NOT 回顯值。`.env.example` SHALL NOT 再列出它們，部署計畫檔含有它們 SHALL 被拒絕。

#### Scenario: 舊變數為開啟
- **WHEN** 環境設定了 `SEXUAL_CONTENT_LEVEL3_ENABLED=true`，沒有設定 `ADULT_MODE_ENABLED`
- **THEN** 成人模式為關閉，啟動時有一則警告，只含變數名稱

#### Scenario: 舊變數不會讓啟動失敗
- **WHEN** 在 production 環境殘留 `SEXUAL_CONTENT_LEVEL3_ENABLED=maybe`
- **THEN** 應用程式可啟動，警告不含 `maybe`

#### Scenario: 部署計畫檔
- **WHEN** 部署計畫檔含有 `SEXUAL_CONTENT_LEVEL3_MODELS`
- **THEN** 計畫被拒絕並指出該變數已淘汰

### Requirement: 帳號頁對未成年者顯示安全模式的提示
13 至 17 歲的使用者開啟 `/account` 時，頁面 SHALL 顯示目前為安全模式及其原因（年齡限制）。這個提示 SHALL 依生日於當下計算的年齡決定，不依儲存的偏好。

#### Scenario: 帳號設定頁的提示
- **WHEN** 13 至 17 歲的使用者開啟 `/account`
- **THEN** 頁面顯示目前為安全模式及其原因
