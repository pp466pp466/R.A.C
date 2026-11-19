# Spec Delta

## Purpose

定義個人頁（`/account`）讓玩家自行開啟或關閉安全模式的畫面與流程：目前狀態的顯示、關閉時的同意 Modal、緊急煞車啟用時的停用說明，以及從劇本目錄的通知跳轉過來、改完回到原劇本的返回規則。伺服器端的模式規則與 API 見 `content-tier-modes`。

## ADDED Requirements

### Requirement: 帳號頁的安全模式區塊
已完成年齡確認的玩家開啟 `/account` SHALL 看到「安全模式」區塊：以一個開關顯示目前是開或關，並以一句話說明兩種狀態的差異（開：不出現性內容與露骨的暴力描寫；關：成年角色之間合意的露骨內容可能出現，但絕對禁止項仍不允許）。開關與 Modal 的顯示狀態 SHALL 由 Vue 響應式變數接管，區塊 SHALL NOT 使用 Bootstrap 的 JavaScript 外掛或 `data-bs-*` 互動屬性。

#### Scenario: 預設為開
- **WHEN** 新玩家第一次開啟 `/account`
- **THEN** 安全模式區塊顯示為開，並有兩種狀態的說明

#### Scenario: 顯示目前的設定
- **WHEN** 已關閉安全模式的玩家開啟 `/account`
- **THEN** 開關顯示為關

#### Scenario: 尚未完成年齡確認
- **WHEN** 尚未完成年齡確認的管理員開啟 `/account`
- **THEN** 不顯示安全模式區塊

### Requirement: 關閉安全模式需先同意安全規範
玩家把開關從開切到關、且尚未在目前的安全規範版本下同意時，頁面 SHALL 先以自製 Modal 顯示安全規範的重點（含「服務含露骨內容」「絕對禁止項」「敏感內容可能被模型供應商留存或審閱」）與一個「我同意安全規範」勾選框；勾選之前確認鈕 SHALL 為停用。確認後頁面 SHALL 送出 `mode` 為 `adult` 與 `confirm_rules` 為真的請求，成功以 Toast 回報並更新開關。Modal 內 SHALL NOT 有「我確定已滿 18 歲」之類的年齡勾選。

#### Scenario: 第一次關閉
- **WHEN** 玩家把開關切到關，尚未同意目前版本的規範
- **THEN** 顯示 Modal，未勾選時確認鈕停用，沒有送出請求

#### Scenario: 同意後送出
- **WHEN** 玩家勾選同意並按下確認
- **THEN** 送出關閉安全模式的請求，成功後 Modal 關閉、開關為關並顯示 Toast

#### Scenario: 取消
- **WHEN** 玩家在 Modal 按取消或 Esc
- **THEN** 沒有送出請求，開關維持為開

#### Scenario: 沒有年齡勾選
- **WHEN** 檢查 Modal 的內容
- **THEN** 只有安全規範的勾選框，沒有「已滿 18 歲」的勾選

#### Scenario: 已同意目前版本
- **WHEN** 已在目前規範版本下同意過的玩家把開關切到關
- **THEN** 不再顯示 Modal，直接送出請求

#### Scenario: 請求失敗
- **WHEN** 關閉請求回應錯誤
- **THEN** 開關維持原狀，顯示繁體中文的錯誤說明，按鈕恢復可用

### Requirement: 開啟安全模式不需確認
玩家把開關從關切到開 SHALL 立即送出請求，不顯示 Modal；成功以 Toast 回報。請求期間開關 SHALL 禁用，無論成功或失敗結束後 SHALL 恢復。

#### Scenario: 切回安全模式
- **WHEN** 已關閉安全模式的玩家把開關切到開
- **THEN** 直接送出請求，成功後開關為開並顯示 Toast，沒有 Modal

#### Scenario: 請求期間
- **WHEN** 請求尚未回應
- **THEN** 開關為禁用，回應後恢復

### Requirement: 緊急煞車啟用時停用開關
緊急煞車啟用時，`/account` 的安全模式區塊 SHALL 顯示為開且開關為禁用，並以一句話說明「目前系統暫時只提供安全模式」；SHALL NOT 顯示關閉用的 Modal。玩家先前保存的設定 SHALL 保留，煞車解除後恢復。

#### Scenario: 煞車啟用
- **WHEN** 緊急煞車啟用，玩家的設定為關
- **THEN** 區塊顯示為開並禁用，附說明，點擊開關沒有任何請求

#### Scenario: 煞車解除
- **WHEN** 煞車解除後玩家再次開啟 `/account`
- **THEN** 開關顯示玩家先前保存的設定

### Requirement: 從目錄通知跳轉與返回
`/account` SHALL 接受選填的返回位置（查詢參數）：只有以 `/game/` 開頭的站內相對路徑才有效，其他值（含絕對網址、`//` 開頭、含反斜線或控制字元）SHALL 被忽略。帶有效返回位置時，安全模式區塊 SHALL 提示「改完後回到劇本」並提供「完成並返回」連結；玩家成功改變設定後 SHALL 以連結（或自動導向）回到該位置，沒有修改而直接按返回 SHALL 也能回去。

#### Scenario: 有效的返回位置
- **WHEN** 玩家開啟 `/account` 並帶 `/game/start/<版本識別碼>` 為返回位置
- **THEN** 區塊顯示返回的提示與連結，連結指向該路徑

#### Scenario: 成功修改後返回
- **WHEN** 玩家在帶返回位置的頁面成功關閉安全模式
- **THEN** 頁面導向返回位置

#### Scenario: 無效的返回位置
- **WHEN** 返回位置為 `https://example.com/` 或 `//example.com/` 或 `/account`
- **THEN** 被忽略，頁面照一般模式顯示，沒有任何外部連結

### Requirement: 設定頁不顯示個資與內部欄位
安全模式區塊與其初始狀態 SHALL NOT 含生日、年齡區間、年齡確認時間、同意的時間戳記或內部編號；只可含開／關、是否已同意目前版本與緊急煞車是否啟用。文字 SHALL 以純文字渲染，嚴禁 `v-html`。

#### Scenario: 初始狀態
- **WHEN** 檢查 `/account` 的初始狀態與區塊內容
- **THEN** 沒有生日、年齡區間與同意時間戳記

#### Scenario: 純文字渲染
- **WHEN** 檢查區塊的樣板
- **THEN** 沒有 `v-html`
