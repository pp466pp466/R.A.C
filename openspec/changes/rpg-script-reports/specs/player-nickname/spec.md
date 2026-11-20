# Spec Delta

## ADDED Requirements

### Requirement: 暱稱不得冒用已註銷的作者署名
暱稱 SHALL NOT 冒用「已註銷的作者」這個系統署名（比對時 NFC 正規化、不分大小寫並忽略空白，與「官方」的比對方式相同；暱稱包含該字串即視為冒用），被拒絕時 SHALL 以暱稱欄位的錯誤回應並說明不可冒用系統署名，不保存任何資料。

#### Scenario: 完全相同
- **WHEN** 玩家設定暱稱為「已註銷的作者」
- **THEN** 被拒絕並指出暱稱欄位

#### Scenario: 夾雜空白
- **WHEN** 暱稱為「已註銷 的作者」
- **THEN** 同樣被拒絕

#### Scenario: 包含該字串
- **WHEN** 暱稱為「我是已註銷的作者」
- **THEN** 被拒絕

#### Scenario: 一般暱稱
- **WHEN** 暱稱為「小註銷」
- **THEN** 不被這條規則拒絕
