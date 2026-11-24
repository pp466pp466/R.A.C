# onboarding-wizard Specification

## Purpose
定義玩家第一次以 Google 登入之後、在 `/age` 完成的分步精靈：三個步驟、使用守則 Modal、各步可前進的條件、一次送出、送出之後才存的故事偏好，以及錯誤、離開與重新整理時的行為。這是純畫面層的規格：年齡確認與暱稱的資料規則（`post-login-age-confirmation`、`age-confirmation`、`player-nickname`）與故事偏好的資料規則（`story-preference`）都不變。

## Requirements

### Requirement: 三個步驟與進度
`/age` 對待確認年齡的玩家 SHALL 以三個依序的步驟呈現，頂端有三段進度條（目前步驟與之前的步驟為已填滿），並有給輔助科技讀的「第 N 步，共 3 步」文字：第 1 步「歡迎」（暱稱與使用守則）、第 2 步「年齡確認」（生日與年齡區間）、第 3 步「故事偏好」（取向）。第 2、3 步 SHALL 有「上一步」，回到上一步時已填的內容 SHALL 保留。第 3 步的主要按鈕 SHALL 是「開始」；其餘步驟是「下一步」。步驟 SHALL 由一份資料驅動的清單定義，日後新增步驟（例如喜歡的類型）不必改動前進、後退與進度的邏輯；v1 SHALL NOT 顯示圖片類型多選、頭像、性別、通知權限或行銷資訊同意。頁面 SHALL 保留「登出」與服務條款的連結。

#### Scenario: 起始畫面
- **WHEN** 待確認年齡的玩家開啟 `/age`
- **THEN** 顯示第 1 步，進度條第一段填滿、其餘兩段未填，沒有「上一步」

#### Scenario: 往前再往回
- **WHEN** 玩家在第 2 步填了生日與區間後按「上一步」再按「下一步」
- **THEN** 回到第 2 步，生日與區間仍是原本填的值

#### Scenario: 步驟清單驅動
- **WHEN** 在步驟清單中加入第四個步驟
- **THEN** 進度條段數、前進與後退都自動涵蓋它，不需要修改狀態機

### Requirement: 使用守則 Modal
第 1 步 SHALL 有一列「使用守則」，點擊開啟以 Vue 狀態控制（`v-if`）的自製 Modal，內容是用白話寫的繁體中文摘要，至少涵蓋：本服務僅限滿 18 歲者使用；暱稱會公開顯示、請勿使用真實姓名；請勿輸入真實個資（角色、劇本與對話會經 AI 處理，且可能被模型供應商留存）；任何模式一律禁止涉及未成年的性內容、性暴力、非自願的性情節與以真人名義的性內容；內容為虛構；並連到服務條款與隱私權政策。Modal SHALL 以純文字渲染（不使用 `v-html`）、可用 Esc 與關閉鈕關閉、開啟時焦點進入 Modal 且關閉後回到觸發的那一列；SHALL NOT 使用 Bootstrap 的 JavaScript 外掛或 `data-bs-*`。按下「我已知悉」SHALL 關閉 Modal 並使該列顯示「已確認使用守則」；只關閉而未按「我已知悉」SHALL NOT 視為已確認。「已確認」只是這次精靈中的前端狀態，SHALL NOT 送到伺服器、存入資料庫或任何瀏覽器儲存。

#### Scenario: 閱讀並確認
- **WHEN** 玩家點「使用守則」、讀完後按「我已知悉」
- **THEN** Modal 關閉，該列顯示「已確認使用守則」，焦點回到該列

#### Scenario: 只關閉
- **WHEN** 玩家開啟 Modal 後按 Esc 或關閉鈕
- **THEN** Modal 關閉，該列仍是未確認

#### Scenario: 純文字
- **WHEN** 檢查使用守則 Modal 的樣板
- **THEN** 內容一律由範本渲染為文字，沒有 `v-html`，也沒有 `data-bs-*` 與 Bootstrap 的 JavaScript

#### Scenario: 不送出確認
- **WHEN** 玩家確認使用守則並完成精靈
- **THEN** 送往伺服器的請求不含任何「已確認使用守則」的欄位，瀏覽器儲存中也沒有它

### Requirement: 各步驟的可前進條件
第 1 步的「下一步」SHALL 在暱稱有內容（去頭尾空白後非空）且已確認使用守則時才可用；暱稱格式（長度、網址與電話樣式、署名）SHALL 只由伺服器在最後送出時判斷，前端 SHALL NOT 另寫一份規則，也 SHALL NOT 為了提早檢查暱稱而新增任何請求。第 2 步的「下一步」SHALL 在生日與年齡區間都已填時才可用，並 SHALL 提示「本服務僅限滿 18 歲者使用」。第 3 步 SHALL 預設選「全讀者」，三個選項互斥，任何時候都可以按「開始」。不可用的按鈕 SHALL 以 `disabled` 呈現並說明缺什麼（文字，不只靠顏色）。

#### Scenario: 缺暱稱
- **WHEN** 第 1 步的暱稱是空白
- **THEN** 「下一步」不可用，並說明需要先填暱稱

#### Scenario: 未確認守則
- **WHEN** 第 1 步已填暱稱但尚未按「我已知悉」
- **THEN** 「下一步」不可用，並說明需要先確認使用守則

#### Scenario: 缺生日或區間
- **WHEN** 第 2 步只填了生日、沒選年齡區間
- **THEN** 「下一步」不可用，並說明缺少年齡區間

#### Scenario: 預設偏好
- **WHEN** 玩家進入第 3 步沒有點任何選項
- **THEN** 「全讀者」是已選的，「開始」可用

### Requirement: 一次送出，成功之後才存故事偏好
按下第 3 步的「開始」SHALL 以**一個**請求（`POST /api/auth/verify-age`）送出暱稱、生日與年齡區間，內容與契約和改版前相同（全有或全無）。請求期間 SHALL 禁用所有按鈕與選項並顯示處理中，無論成功、失敗或中斷 SHALL 在 `finally` 恢復。確認成功之後，若所選偏好**不是**預設的「全讀者」，SHALL 呼叫 `PUT /api/account/story-preference`；偏好請求失敗或逾時 SHALL NOT 使年齡確認失敗、SHALL NOT 阻擋導向，玩家仍導向登入前要去的位置（不合法時為預設頁面），之後可在 `/account` 設定。所選偏好為「全讀者」時 SHALL NOT 發出偏好請求。偏好請求 SHALL NOT 在年齡確認通過之前發出（受限登入狀態本來就會被 403 `age_confirmation_required` 擋下，本變更不放寬這個限制）。

#### Scenario: 選男頻
- **WHEN** 玩家在第 3 步選「男頻」並按「開始」，確認成功
- **THEN** 先有一個 `verify-age` 請求，成功後有一個 `story-preference` 請求（值為 `male`），然後導向預設頁面

#### Scenario: 預設不呼叫
- **WHEN** 玩家保持「全讀者」並按「開始」，確認成功
- **THEN** 只有一個 `verify-age` 請求，沒有 `story-preference` 請求

#### Scenario: 偏好失敗不影響
- **WHEN** 確認成功，但 `story-preference` 請求失敗
- **THEN** 玩家仍被導向預設頁面，年齡確認的結果不變

#### Scenario: 確認失敗不存偏好
- **WHEN** `verify-age` 回應錯誤
- **THEN** 沒有 `story-preference` 請求

#### Scenario: 重複點擊
- **WHEN** 玩家在請求處理中再次按「開始」
- **THEN** 不會送出第二個請求

### Requirement: 錯誤回到對應的步驟
`verify-age` 失敗時，畫面 SHALL 回到與錯誤相關的步驟：暱稱欄位的錯誤回第 1 步並顯示在暱稱欄位（生日與區間保持原樣）；`age_mismatch` 回第 2 步、顯示通用的「資料有誤，請重新填寫」並清空生日與區間（不指出哪一個不對，也不暗示可以改填其他值）；生日或年齡區間欄位的格式錯誤回第 2 步並顯示在該欄位。`age_not_eligible`（未滿 18 歲，帳號已抹除）SHALL 結束精靈，只顯示說明與回到登入頁的連結；登入逾時 SHALL 導向登入頁；帳號被鎖定 SHALL 導向申訴頁；其他錯誤 SHALL 留在第 3 步顯示繁體中文說明。所有錯誤訊息 SHALL 以純文字渲染。

#### Scenario: 暱稱不合格
- **WHEN** 伺服器回應暱稱欄位的錯誤
- **THEN** 回到第 1 步，錯誤顯示在暱稱欄位，生日與區間保持原樣，玩家只改暱稱再走完剩餘步驟即可送出

#### Scenario: 不一致
- **WHEN** 伺服器回應 `age_mismatch`
- **THEN** 回到第 2 步，顯示通用提醒並清空生日與區間

#### Scenario: 未滿 18 歲
- **WHEN** 伺服器回應 `age_not_eligible`
- **THEN** 精靈結束，顯示僅限滿 18 歲的說明與回到登入頁的連結

#### Scenario: 登入逾時
- **WHEN** 送出時伺服器回應 401
- **THEN** 導向登入頁（登入後回到 `/age`）

### Requirement: 離開與重新整理不留下資料
精靈 SHALL NOT 把暱稱、生日、年齡區間、使用守則確認或偏好寫入 `localStorage`、`sessionStorage`、IndexedDB 或 cookie。重新整理或離開後重新開啟 `/age` SHALL 從第 1 步重新開始（帳號的 24 小時期限與清除規則不變，離開並不延長期限）。已完成年齡確認的玩家開啟 `/age` SHALL 照舊導向預設頁面；管理員在 `/account` 補填年齡的流程 SHALL 不受影響（沒有精靈）。

#### Scenario: 重新整理
- **WHEN** 玩家在第 2 步填了生日後重新整理
- **THEN** 回到第 1 步，欄位是空的，瀏覽器儲存中沒有任何先前填的內容

#### Scenario: 已完成者
- **WHEN** 已完成年齡確認的玩家開啟 `/age`
- **THEN** 導向預設頁面

### Requirement: 遵守前端規範與無障礙
精靈 SHALL 以 Vue 3 全域變數模式撰寫，所有資源經 `url_for('static', …)` 載入本機檔案，沒有 CDN、外部字型、`v-html`、Bootstrap 的 JavaScript 外掛與 `data-bs-*`；配色沿用專案的深色極簡規範（主文字不使用純白）。每個欄位 SHALL 有可見標籤，錯誤以 `role="alert"` 呈現並以 `aria-describedby` 關聯；步驟切換後焦點 SHALL 移到該步的標題或第一個欄位；進度條 SHALL 有文字替代。偏好減少動態（`prefers-reduced-motion`）時 SHALL 取消步驟切換的動畫。360px 寬度 SHALL 沒有橫向捲軸，主要按鈕不被遮住。

#### Scenario: 離線資源
- **WHEN** 檢查 `age.html` 與其載入的資源
- **THEN** 全部來自本站的靜態路徑，沒有外部網址

#### Scenario: 步驟切換的焦點
- **WHEN** 玩家按「下一步」進入第 2 步
- **THEN** 焦點移到第 2 步的標題或生日欄位，輔助科技讀到「第 2 步，共 3 步」

#### Scenario: 小螢幕
- **WHEN** 在 360px 寬度開啟任何一步
- **THEN** 沒有橫向捲軸，「上一步」與主要按鈕都可見
