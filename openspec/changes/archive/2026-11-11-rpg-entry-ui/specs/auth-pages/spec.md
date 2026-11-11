# Spec Delta

## MODIFIED Requirements

### Requirement: 登入後的預設頁面與首頁導向
登入後的預設頁面 SHALL 由單一常數決定（目前為 `/game`，也就是選擇劇本的首頁，不直接進入聊天；客服 `/cs` 改由導覽列進入）。已登入者開啟 `/` 或 `/login` SHALL 被導向預設頁面（受限登入狀態各導向自己的頁面：待確認年齡 → `/age`，被鎖定 → `/appeal`，等待刪除 → `/account`）；`/register` SHALL 一律導向 `/login`。未登入者開啟 `/` 仍看到首頁。

#### Scenario: 已登入者開啟首頁
- **WHEN** 已登入的一般玩家開啟 `/`
- **THEN** 導向 `/game`

#### Scenario: 受限狀態開啟首頁
- **WHEN** 待確認年齡的玩家開啟 `/`
- **THEN** 導向 `/age`

#### Scenario: 舊的註冊網址
- **WHEN** 任何人開啟 `/register`
- **THEN** 導向 `/login`

#### Scenario: 完成年齡確認之後
- **WHEN** 新玩家完成年齡確認與暱稱，且沒有指定返回位置
- **THEN** 導向 `/game`

#### Scenario: 客服仍可直接開啟
- **WHEN** 已登入的玩家直接開啟 `/cs`
- **THEN** 顯示客服頁面，不被導向 `/game`
