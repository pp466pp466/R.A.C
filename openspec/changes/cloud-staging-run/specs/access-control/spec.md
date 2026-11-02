# Spec Delta

## ADDED Requirements

### Requirement: 非互動的雲端工作以機密環境變數提供管理員密碼
建立管理員的指令 SHALL 另提供 `--password-env NAME`：從名稱為 `NAME` 的環境變數讀取密碼，供雲端一次性工作以機密注入（雲端工作沒有互動輸入，也不能餵標準輸入）。使用此選項時，信箱與顯示名稱 SHALL 以參數提供；此選項與 `--password-stdin` SHALL 互斥；該環境變數不存在或為空 SHALL 使指令失敗且訊息只指出變數名稱。密碼的處理 SHALL 與其他方式相同（符合長度限制、以雜湊儲存、去除 UTF-8 BOM），SHALL NOT 出現在輸出或日誌。

#### Scenario: 從環境變數建立
- **WHEN** 以 `--password-env` 指定的環境變數含合規的密碼，並提供信箱與顯示名稱
- **THEN** 建立管理員帳號，密碼以雜湊儲存，輸出與日誌沒有密碼

#### Scenario: 變數不存在或為空
- **WHEN** 指定的環境變數不存在或為空
- **THEN** 指令失敗，訊息只指出變數名稱，沒有建立任何帳號

#### Scenario: 與標準輸入互斥
- **WHEN** 同時指定 `--password-env` 與 `--password-stdin`
- **THEN** 指令以用法錯誤結束

#### Scenario: 缺少信箱或顯示名稱
- **WHEN** 使用 `--password-env` 但沒有提供信箱或顯示名稱
- **THEN** 指令以用法錯誤結束（非互動環境不詢問）
