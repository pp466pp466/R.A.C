# Spec Delta

## ADDED Requirements

### Requirement: 匯出納入錢包與帳本
個人資料匯出 SHALL 包含使用者的錢包餘額（免費與付費兩個整數）與**全部**帳本列（含已退回的預扣與其退回列）。每筆 SHALL 含：種類、免費與付費各自的增減量、異動後的兩個餘額、預扣的結算結果（若有）、關聯的存檔對外識別碼（若有）與時間；SHALL NOT 含冪等鍵與任何內部編號。JSON 匯出 SHALL 放在 `wallet` 區塊；CSV 匯出 SHALL 在 zip 內新增 `wallet_entries.csv`（UTF-8-sig，字串以公式字元開頭者依既有規則中和）。從未存取過錢包的使用者 SHALL 得到空的帳本與兩個為 0 的餘額，且匯出本身 SHALL NOT 因此建立錢包或發放贈幣。

#### Scenario: JSON 含錢包
- **WHEN** 有 3 筆帳本的使用者匯出 JSON
- **THEN** `wallet` 區塊含兩個餘額與 3 筆帳本

#### Scenario: 含已退回的預扣
- **WHEN** 使用者有一筆已退回的預扣
- **THEN** 匯出含該預扣與其退回列（與記錄頁不同，匯出包含全部）

#### Scenario: CSV 含帳本
- **WHEN** 使用者匯出 CSV
- **THEN** zip 內有 `wallet_entries.csv`，每筆帳本一列

#### Scenario: 不含內部資料
- **WHEN** 檢查匯出內容
- **THEN** 沒有冪等鍵與內部整數編號

#### Scenario: 沒有錢包的使用者
- **WHEN** 從未存取錢包的使用者匯出
- **THEN** 餘額為 0、帳本為空，且沒有因此建立錢包
