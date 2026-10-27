# Spec Delta

## Purpose

定義全站共用的視覺色彩與互動元件：自製 Modal、Toast 與打字機緩衝區，讓客服與 RPG 兩個模組有一致的電影感深色風格，且元件顯示狀態完全由 Vue 接管。

## ADDED Requirements

### Requirement: 全站色彩規範
系統 SHALL 以統一的色彩變數提供全站配色：主背景 `#0F1012`、卡片容器 `rgba(28, 30, 35, 0.75)`、主要文字 `#E2E4E9`、次要文字 `#8A8F99`、焦點與互動色 `#FF6B35`、分隔線 `#2A2D34`（1px）。任何文字 SHALL NOT 使用純白 `#FFFFFF`。

#### Scenario: 預設外觀
- **WHEN** 載入任一頁面
- **THEN** 背景為 `#0F1012`，主要文字為 `#E2E4E9`

#### Scenario: 禁用純白
- **WHEN** 掃描全站樣式
- **THEN** 沒有任何文字顏色為 `#FFFFFF`

### Requirement: Modal 的外觀與狀態
Modal SHALL 由 Vue 的響應式狀態以 `v-if` 或 `v-show` 完全控制顯示與隱藏。全螢幕遮罩 SHALL 為 `rgba(15, 16, 18, 0.85)` 並套用 `blur(8px)`；容器背景 SHALL 為 `rgba(22, 24, 28, 0.95)`，四周為 `1px solid #2A2D34`，左上角顯示小字的系統狀態碼（例如 `[ SYSTEM_PROMPT ]`）。

#### Scenario: 開啟 Modal
- **WHEN** 程式將 Modal 的顯示狀態設為真
- **THEN** 遮罩與容器出現，樣式符合上述規範，狀態碼顯示在左上角

#### Scenario: 關閉 Modal
- **WHEN** 程式將顯示狀態設為假
- **THEN** Modal 從畫面消失，頁面可再次捲動與操作

### Requirement: Modal 的可及性
Modal SHALL 標示為對話框角色並限制焦點在其內，按下 Esc 鍵 SHALL 關閉可關閉的 Modal，關閉後焦點 SHALL 回到開啟前的位置。

#### Scenario: 以鍵盤關閉
- **WHEN** 可關閉的 Modal 開啟時按下 Esc
- **THEN** Modal 關閉，焦點回到原本觸發它的元素

#### Scenario: 焦點限制
- **WHEN** Modal 開啟時反覆按 Tab
- **THEN** 焦點只在 Modal 內的可操作元素之間循環

### Requirement: Toast 的外觀與行為
Toast SHALL 顯示於畫面右下角，左側具有 2px 的 `#FF6B35` 指示線，由下往上淡入；顯示與隱藏 SHALL 由 Vue 響應式狀態控制。Toast SHALL 在預設的時間後自動消失，多個 Toast 同時存在時 SHALL 依序堆疊而不互相遮蓋。

#### Scenario: 顯示 Toast
- **WHEN** 程式送出一則提示訊息
- **THEN** 右下角出現帶有琥珀橙左側線的 Toast，並由下往上淡入

#### Scenario: 自動消失
- **WHEN** Toast 顯示超過預設時間
- **THEN** Toast 自動消失

#### Scenario: 同時多則
- **WHEN** 短時間內送出三則提示
- **THEN** 三則依序堆疊顯示，沒有互相遮蓋

### Requirement: 元件不依賴 Bootstrap 的互動腳本
Modal 與 Toast 的互動 SHALL NOT 使用 Bootstrap 的 JavaScript 外掛，也 SHALL NOT 使用 `data-bs-toggle`、`data-bs-dismiss` 等互動屬性，只可借用其網格與排版樣式。

#### Scenario: 元件原始碼檢查
- **WHEN** 掃描 Modal 與 Toast 的樣板與腳本
- **THEN** 沒有呼叫 Bootstrap 的腳本物件，也沒有 `data-bs-*` 互動屬性

### Requirement: 打字機緩衝區
打字機緩衝區 SHALL 接收陸續到達的文字片段（串流片段，或一次到達的完整文字），並以平穩的節奏逐字顯示。積壓的文字越多顯示速度 SHALL 越快，以免在輸入結束後還要等待很久。顯示 SHALL 以純文字處理，不得解析為 HTML。

#### Scenario: 完整文字逐字顯示
- **WHEN** 一次交給緩衝區一段完整的回覆文字
- **THEN** 文字逐字出現，最終顯示內容與原文完全相同

#### Scenario: 片段陸續到達
- **WHEN** 文字以數個片段陸續交給緩衝區
- **THEN** 顯示內容依序連續增加，順序不錯亂

#### Scenario: 不切開字元
- **WHEN** 文字含有中文與需要兩個編碼單元的表情符號
- **THEN** 每一步顯示的都是完整字元，沒有出現亂碼或半個字元

#### Scenario: 含 HTML 標記的文字
- **WHEN** 文字包含 `<script>` 之類的標記
- **THEN** 標記以原樣文字顯示，不被當作 HTML 執行

#### Scenario: 結束時補完
- **WHEN** 呼叫端表示文字已全部到達
- **THEN** 緩衝區把尚未顯示的文字加速顯示完畢，並通知呼叫端已完成

### Requirement: 元件文字為繁體中文
共用元件內建的所有文字（按鈕、提示、無障礙標籤） SHALL 為繁體中文（zh-TW）。

#### Scenario: 預設按鈕文字
- **WHEN** 開啟含確認與取消按鈕的 Modal
- **THEN** 按鈕文字為繁體中文
