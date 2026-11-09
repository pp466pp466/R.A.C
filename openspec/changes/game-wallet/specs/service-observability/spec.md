# Spec Delta

## ADDED Requirements

### Requirement: 遊戲幣事件只含白名單欄位
錢包相關的日誌事件（`wallet.signup_granted`、`wallet.checkin`、`wallet.checkin_overflow`、`wallet.hold`、`wallet.settle`、`wallet.refund`、`wallet.stale_released`、`wallet.insufficient`、`wallet.adjust`）SHALL 只含：事件名稱、`user_id`（內部整數）、整數的幣量與餘額、固定列舉（例如桶是 `free` 或 `paid`、模型鍵）與時間；SHALL NOT 含冪等鍵、存檔識別碼、管理員的理由、email、暱稱或任何玩家文字。設定缺值或不合法的警告 SHALL 只含設定的鍵名。

#### Scenario: 簽到事件
- **WHEN** 玩家簽到成功
- **THEN** 日誌有 `wallet.checkin`，只含使用者內部編號、入帳量與作廢量

#### Scenario: 調整事件不含理由
- **WHEN** 管理員調整餘額並附理由
- **THEN** 日誌事件沒有理由文字，也沒有目標玩家的對外識別碼

#### Scenario: 日誌掃描
- **WHEN** 完整走過贈幣、簽到、預扣、結算、退回與調整後掃描全部日誌輸出
- **THEN** 找不到冪等鍵、存檔識別碼、理由與任何玩家文字
