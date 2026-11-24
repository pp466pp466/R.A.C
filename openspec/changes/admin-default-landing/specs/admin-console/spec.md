# Spec Delta

## ADDED Requirements

### Requirement: 後台頂列的導覽連結
`/admin` 頂列 SHALL 提供三個通往其他頁面的站內連結：「客服」（`/cs`）、「帳號」（`/account`）與「冒險」（`/game`）。「冒險」讓以 `/admin` 為預設頁面的管理員能進入 RPG 入口測試模型。三者 SHALL 是一般的站內連結，SHALL NOT 使用任何框架內建的互動屬性，也 SHALL NOT 改變 `/admin` 的存取規則。

#### Scenario: 頂列有三個連結
- **WHEN** 管理員開啟 `/admin`
- **THEN** 頂列顯示「客服」「帳號」「冒險」三個連結，目標依序為 `/cs`、`/account`、`/game`

#### Scenario: 從後台進入冒險
- **WHEN** 管理員在 `/admin` 按下「冒險」
- **THEN** 開啟 `/game`，不被導回 `/admin`
