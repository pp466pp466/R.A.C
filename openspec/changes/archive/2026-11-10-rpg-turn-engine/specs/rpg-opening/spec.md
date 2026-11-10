# Spec Delta

## Purpose

定義 RPG 的第 0 幕（開場）：主角建立後由模型依角色卡、錨定事實與來歷生成開場劇情，選定後存於存檔、不重抽；它不是一輪，不扣遊戲幣；失敗或被擋時退回劇本的初始對話。確保開場不干擾輪數與記事簿的計數。

## ADDED Requirements

### Requirement: 第 0 幕的狀態與儲存
存檔 SHALL 記錄第 0 幕的文字與狀態（`pending`、`generating`、`ready`、`fallback`）及開始生成的時間，SHALL NOT 放進回合表。新存檔的狀態 SHALL 為 `pending`。`ready` 與 `fallback` 之後 SHALL NOT 重新生成：重新整理或換裝置只取得既有文字。第 0 幕 SHALL NOT 計輪數、SHALL NOT 計入每日對話輪數或記事簿的 10 輪計數，也 SHALL NOT 出現在回合同步的訊息序列中。

#### Scenario: 新存檔
- **WHEN** 開出新存檔
- **THEN** 第 0 幕狀態為 `pending`，沒有回合列

#### Scenario: 不計輪
- **WHEN** 第 0 幕完成、尚未有玩家回合
- **THEN** 已完成輪數為 0

#### Scenario: 不重抽
- **WHEN** 第 0 幕為 `ready` 後再次呼叫開場端點
- **THEN** 回傳既有文字，不呼叫模型

### Requirement: 生成開場
生成 SHALL 以串流呼叫 RPG 用途的模型，提示詞帶入主角角色卡、錨定事實（可為空）與來歷，沿用上下文組裝的規則（系統指令只放固定規則、玩家文字只放資料區塊）、輸出審查與內容分級上限。開場 SHALL 不扣遊戲幣；token 與帳本 SHALL 記入全站每日用量。第 0 幕 SHALL NOT 可被玩家重新生成。

#### Scenario: 帶入主角
- **WHEN** 生成開場
- **THEN** 使用者內容區含主角角色卡、錨定事實與來歷，系統指令不含其中任何文字

#### Scenario: 不扣幣
- **WHEN** 開場生成成功
- **THEN** 錢包沒有任何異動，帳本有用途為 `rpg_opening` 的紀錄

### Requirement: 同時只有一個裝置生成
開始生成 SHALL 以條件式更新把 `pending` 改為 `generating` 並記錄開始時間；失敗者 SHALL NOT 重複生成。另一個裝置在 `generating` 且未逾時時呼叫開場端點 SHALL 回應衝突（409，代碼 `opening_in_progress`），由前端稍後再讀取。`generating` 超過 90 秒 SHALL 可被接手重新生成。

#### Scenario: 兩個裝置同時
- **WHEN** 兩個請求同時嘗試開始生成
- **THEN** 只有一個取得生成權，另一個得到 `opening_in_progress`

#### Scenario: 接手
- **WHEN** 狀態停在 `generating` 超過 90 秒
- **THEN** 新的請求可以取得生成權

### Requirement: 失敗與被擋時退回初始對話
生成失敗（主要與備援模型皆失敗）或輸出被擋且自動重生一次仍被擋時，狀態 SHALL 為 `fallback`，內容取劇本的初始對話；劇本沒有初始對話時 SHALL 只顯示背景故事（文字為空）。串流因客戶端中斷而結束時，狀態 SHALL 退回 `pending`（不是 `fallback`），部分文字 SHALL 丟棄，下次進入再生成。

#### Scenario: 兩次都被擋
- **WHEN** 開場輸出被擋，自動重生仍被擋
- **THEN** 狀態為 `fallback`，文字為劇本的初始對話

#### Scenario: 沒有初始對話
- **WHEN** 劇本沒有初始對話，開場生成失敗
- **THEN** 狀態為 `fallback`，文字為空，介面只顯示背景故事

#### Scenario: 中途斷線
- **WHEN** 客戶端在開場串流中斷線
- **THEN** 狀態退回 `pending`，沒有保存部分文字

### Requirement: 開場是歷史最前面的一則訊息
完成輪數未滿 10 輪時，第 0 幕的文字（非空時）SHALL 作為歷史最前面的一則 AI 訊息放入上下文，並隨最近 10 輪的視窗滑出；它 SHALL NOT 改變「最近 10 輪原文」的定義。

#### Scenario: 早期
- **WHEN** 完成 2 輪，第 0 幕為 `ready`
- **THEN** 上下文的歷史由第 0 幕與 2 輪組成

#### Scenario: 滑出
- **WHEN** 完成 10 輪
- **THEN** 歷史只含最近 10 輪
