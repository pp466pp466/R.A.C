# Spec Delta

## MODIFIED Requirements

### Requirement: 登入後的預設頁面與首頁導向
登入後的預設頁面 SHALL 依角色決定：玩家為 `/game`（選擇劇本的首頁，不直接進入聊天；客服 `/cs` 改由導覽列進入），管理員見「管理員的預設頁面」。已登入者開啟 `/` 或 `/login` SHALL 被導向預設頁面（受限登入狀態各導向自己的頁面：待確認年齡 → `/age`，被鎖定 → `/appeal`，等待刪除 → `/account`）；`/register` SHALL 一律導向 `/login`。未登入者開啟 `/` 仍看到首頁。

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

## ADDED Requirements

### Requirement: 管理員的預設頁面
角色為 `admin` 且帳號啟用中的使用者，其預設頁面 SHALL 是 `/admin`，所有使用預設頁面的情況都一致：通過 Google 與密碼兩步驗證而建立登入之後、已登入的管理員開啟 `/` 或 `/login`、以及沒有特殊狀態的人被導向預設頁面的頁面。登入前指定的返回位置 SHALL 仍然優先；「沒有指定」包含未帶返回位置、返回位置不合法，以及返回位置恰好等於玩家的預設頁面 `/game`（發起登入時沒有指定的補值），這些情況 SHALL 導向 `/admin`。受限登入狀態的導向 SHALL NOT 改變。

#### Scenario: 管理員完成密碼驗證且沒有指定返回位置
- **WHEN** 管理員通過 Google 與密碼驗證，登入前沒有指定返回位置（登入頁的返回位置是伺服器補上的 `/game`）
- **THEN** 密碼驗證的成功回應與之後的導向都是 `/admin`

#### Scenario: 已登入的管理員開啟首頁或登入頁
- **WHEN** 已登入的管理員開啟 `/` 或 `/login`
- **THEN** 導向 `/admin`

#### Scenario: 管理員有指定合法的返回位置
- **WHEN** 管理員被擋在 `/game/<存檔識別碼>` 而導向登入頁，登入完成
- **THEN** 導向 `/game/<存檔識別碼>`，不導向 `/admin`

#### Scenario: 返回位置不合法
- **WHEN** 管理員登入時的返回位置是外部網址或以 `//` 開頭的值
- **THEN** 導向 `/admin`

#### Scenario: 管理員尚未完成年齡確認
- **WHEN** 沒有生日與年齡區間的管理員登入完成，沒有指定返回位置
- **THEN** 導向 `/admin`，不導向 `/age`

#### Scenario: 玩家不受影響
- **WHEN** 已登入的一般玩家開啟 `/` 或登入完成且沒有指定返回位置
- **THEN** 導向 `/game`，不會被導向 `/admin`

#### Scenario: 管理員仍可直接開啟玩家頁面
- **WHEN** 已登入的管理員直接開啟 `/game` 或 `/cs`
- **THEN** 顯示該頁面，不被導向 `/admin`
