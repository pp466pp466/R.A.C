# Spec Delta

## Purpose

定義玩家把草稿送審的流程：送審前的檢查順序、每日送審次數、被自動拒絕時只留類別與時間的送審紀錄、連續被自動拒絕時的「暫停發佈」旗標，以及管理員對暫停的檢視與解除。核可與退回由後續的發佈 change 處理；在那之前待審的版本停在待審。

## ADDED Requirements

### Requirement: 送審前依固定順序檢查
`POST /api/scripts/versions/<id>/submit` SHALL 依序檢查並在第一個不通過處停止：擁有者（否則 404）、版本為 `draft`（否則 409 `not_draft`）、未被暫停發佈（否則 403 `publish_suspended`）、今日送審次數未達上限（否則 429 `submission_limit`）、固定規格與規則層、模型層。全部通過 SHALL 才把版本轉為 `pending_review`。

#### Scenario: 全部通過
- **WHEN** 作者送審一份合格、審查通過的草稿
- **THEN** 版本轉為 `pending_review`，回應含新狀態

#### Scenario: 被暫停發佈
- **WHEN** 被暫停發佈的作者送審
- **THEN** 回應 403 與代碼 `publish_suspended`，沒有呼叫模型

#### Scenario: 達到每日上限
- **WHEN** 作者今日送審次數已等於其上限
- **THEN** 回應 429 與代碼 `submission_limit`，沒有呼叫模型

#### Scenario: 並行重送
- **WHEN** 同一份草稿同時收到兩個送審請求
- **THEN** 只有一個成功，另一個得到 409 `not_draft`

### Requirement: 自動拒絕只告知類別並留下紀錄
規則層或模型層的結果為 `reject` 時，送審 SHALL 回應 422 與代碼 `script_auto_rejected` 和類別列舉，版本維持 `draft`。每次自動拒絕與每次成功送審 SHALL 在 `script_submissions` 留下一筆紀錄：使用者、結果（`sent` 或 `auto_rejected`）、類別列舉與時間，SHALL NOT 含標題、內容、版本識別碼或任何劇本文字。結果為 `flag` 的送審 SHALL 成功，並把類別留在版本上供人工審核。

#### Scenario: 被自動拒絕
- **WHEN** 模型層對送審的內容回傳 `reject` 與 `non_consensual`
- **THEN** 回應 422 與類別，版本仍是草稿，有一筆 `auto_rejected` 紀錄

#### Scenario: 紀錄不含內容
- **WHEN** 檢查送審紀錄的所有欄位
- **THEN** 沒有任何劇本文字、標題或版本識別碼

#### Scenario: 有疑慮的送審
- **WHEN** 模型層回傳 `flag`
- **THEN** 送審成功，版本記有該類別

### Requirement: 每日送審次數依會員等級
每位作者每個台灣日（Asia/Taipei，資料庫存 UTC）的送審次數 SHALL 不超過其會員等級的上限，上限來自 `runtime-limits`。次數 SHALL 只計 `sent` 與 `auto_rejected` 的紀錄；模型層不可用導致的失敗與解除暫停的紀錄 SHALL NOT 計入。儲存時被規則層拒絕 SHALL NOT 計入。

#### Scenario: 日界
- **WHEN** 作者在台灣時間 23:59 用完上限，00:01 再送審
- **THEN** 00:01 的送審不被次數上限擋下

#### Scenario: 不計入的情形
- **WHEN** 作者的送審因 `review_unavailable` 失敗
- **THEN** 當日次數不增加

#### Scenario: 依等級
- **WHEN** 免費與尊榮的作者各送審到自己的上限
- **THEN** 兩者的上限不同，各依 `runtime-limits` 的值

### Requirement: 連續被自動拒絕時暫停發佈
作者最近連續達門檻次數（`runtime-limits` 的 `script_reject_streak`，初始 3）的送審紀錄皆為 `auto_rejected` 時，系統 SHALL 以條件式更新設定 `publish_suspended_at`，並寫入 `publish.suspend` 稽核（管理員欄位為空）與事件。成功送審（`sent`）或解除暫停（`lifted`）的紀錄 SHALL 使連續中斷。並行時 SHALL 只有一個請求成功暫停並寫稽核。管理員 SHALL NOT 被暫停。

#### Scenario: 連續三次
- **WHEN** 作者連續三次送審都被自動拒絕
- **THEN** 帳號被設定暫停發佈，有一筆 `publish.suspend` 稽核

#### Scenario: 成功一次即中斷
- **WHEN** 作者被拒絕兩次、成功送審一次、又被拒絕一次
- **THEN** 不暫停

#### Scenario: 管理員
- **WHEN** 管理員帳號連續被自動拒絕
- **THEN** 不被暫停

### Requirement: 暫停發佈只停止送審
被暫停發佈的帳號 SHALL 仍能登入、使用客服、建立與編輯草稿、撤回、刪除與用自己的草稿開存檔；只有送審 SHALL 被拒絕。`/api/auth/me` 與 `/api/scripts/limits` SHALL 讓前端知道目前被暫停，頁面 SHALL 顯示說明與連到申訴頁的「前往申訴」按鈕（見 `appeal-shortcut`）。

#### Scenario: 其他功能照常
- **WHEN** 被暫停發佈的作者編輯草稿
- **THEN** 儲存成功

#### Scenario: 前端得知狀態
- **WHEN** 被暫停的作者開啟 `/scripts`
- **THEN** 頁面顯示已暫停發佈的說明，送審按鈕不可用

### Requirement: 管理員檢視與解除暫停發佈
管理員 SHALL 能檢視被暫停發佈的帳號（只含對外識別碼、顯示名稱、暫停時間），並以 `POST /api/admin/users/<public_id>/lift-publish-suspension` 解除。解除 SHALL 要求管理員重新輸入密碼，目標 SHALL 為一般使用者，未被暫停 SHALL 得到 409，成功時 SHALL 清除旗標、把該帳號待處理的申訴標為已處理、在 `script_submissions` 補一筆 `lifted` 並寫入 `publish.lift` 稽核。

#### Scenario: 解除
- **WHEN** 管理員以正確的密碼解除
- **THEN** 旗標清除、有一筆 `publish.lift` 稽核，作者可再次送審

#### Scenario: 解除後不立即再次暫停
- **WHEN** 解除後作者再被自動拒絕一次
- **THEN** 不暫停，因為連續已被 `lifted` 中斷

#### Scenario: 沒有被暫停
- **WHEN** 對未被暫停的帳號解除
- **THEN** 回應 409，沒有新的稽核

#### Scenario: 密碼錯誤
- **WHEN** 管理員以錯誤的密碼解除
- **THEN** 被拒絕，旗標不變
