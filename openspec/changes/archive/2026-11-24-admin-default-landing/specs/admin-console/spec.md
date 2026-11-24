# Spec Delta

## ADDED Requirements

### Requirement: 後台頂列的導覽連結
`/admin` 頂列 SHALL 依序提供三個通往其他頁面的站內連結：「冒險」（`/game`）、「客服」（`/cs`）與「帳號」（`/account`）。「冒險」排在最前面，讓以 `/admin` 為預設頁面的管理員能進入 RPG 入口測試模型。三者 SHALL 是一般的站內連結，SHALL NOT 使用任何框架內建的互動屬性，也 SHALL NOT 改變 `/admin` 的存取規則。

#### Scenario: 頂列有三個連結
- **WHEN** 管理員開啟 `/admin`
- **THEN** 頂列依序顯示「冒險」「客服」「帳號」三個連結，目標依序為 `/game`、`/cs`、`/account`

#### Scenario: 從後台進入冒險
- **WHEN** 管理員在 `/admin` 按下「冒險」
- **THEN** 開啟 `/game`，不被導回 `/admin`
