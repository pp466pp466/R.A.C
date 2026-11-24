# Proposal

## Why

管理員的日常工作都在 `/admin`（審核劇本、處理檢舉與申訴、看統計），但登入後與打開首頁時一律落在玩家的入口 `/game`，每次都要手動改網址或從別的頁面繞過去。使用者要求：**管理員登入後預設進入 `/admin`**。

現況的預設頁面由單一常數 `DEFAULT_AFTER_LOGIN`（`/game`）決定，首頁、登入頁、Google 回呼與管理員的密碼驗證步驟都共用 `landing_for`，所以這是一個集中、範圍很小的規則變更；但它修訂了 AGENTS.md 第 10 節與 `auth-pages` 規格的文字（「單一常數、目前為 `/game`」），屬於規範層級，因此走 OpenSpec。

## What Changes

- **預設頁面依角色決定**：`landing_for` 對「角色為 `admin` 且帳號啟用中」的使用者回傳 `/admin`，其餘使用者仍是 `/game`。新增常數 `ADMIN_AFTER_LOGIN = "/admin"`，與 `DEFAULT_AFTER_LOGIN` 並列；兩個常數都只定義在 `app/security/redirects.py`。
- **生效範圍**：管理員通過 Google 與密碼兩步驗證之後（`POST /api/auth/admin-password` 回傳的 `next`）、已登入的管理員打開 `/` 或 `/login`、以及其他「沒有特殊狀態就導向預設頁面」的頁面（`/age`、`/appeal` 對沒有該狀態的人）都導向 `/admin`。玩家與受限登入狀態（待確認年齡、被鎖定、等待刪除）的導向完全不變。
- **`next` 優先權不變，但「沒有指定」的判斷要補上**：有合法的 `next` 就回到那裡（例如管理員被擋在 `/game/<存檔>` 之後登入）。因為發起登入時伺服器在沒有 `next` 時會補上 `/game`（`safe_next` 的預設值，並存進交易 cookie 與管理員中間狀態），所以 `landing_for` 必須把「等於玩家預設頁面的 `next`」視為「沒有指定」，否則管理員永遠拿到 `/game`。這個既有的作法已用在待確認年齡的導向（預設頁面不必帶 `next`）。
- **管理員的密碼驗證頁**（`login-password.js`）的備援導向由 `/game` 改為 `/admin`（伺服器平常一定會回 `next`，備援只在回應壞掉時用）；`auth-flows.js` 給玩家用的 `DEFAULT_PAGE` 維持 `/game`。
- **後台頂列加「冒險」連結**：`/admin` 頂列目前只有「客服」「帳號」，預設改成 `/admin` 後管理員（用 RPG 測試模型、不消耗遊戲幣）就沒有入口進 `/game`，所以加一個 `href="/game"`、文字「冒險」的連結，與客服頁頂列、帳號頁導覽列的「冒險」連結一致。
- **規範與規格**：修訂 AGENTS.md 第 10 節「登入與 session」一條（預設頁面不再是單一常數，而是依角色的兩個常數）；`auth-pages` 規格修改「登入後的預設頁面與首頁導向」；`admin-console` 規格新增「後台頂列的導覽連結」。
- **不變**：玩家的預設頁面仍是 `/game`；客服 `/cs` 仍可直接開啟；`/admin` 的存取規則（非管理員 404、未登入導向登入頁並保留返回位置）、年齡確認規則（管理員未確認年齡仍可登入、只是不能用會呼叫模型的功能）、Google 與密碼兩步驗證流程、API、資料表與遷移都不動。

## Capabilities

### New Capabilities

（無）

### Modified Capabilities

- `auth-pages`：「登入後的預設頁面與首頁導向」改為依角色決定預設頁面（管理員 `/admin`、玩家 `/game`），並規定「沒有指定返回位置」包含等於玩家預設頁面的值；新增管理員情境。
- `admin-console`：新增「後台頂列的導覽連結」（客服、帳號、冒險）。

## Impact

- **程式**：`app/security/redirects.py`（新常數、`landing_for` 依角色與「沒有指定」判斷）、`app/static/js/auth/login-password.js`（備援導向）、`app/templates/admin/console.html`（頂列連結，必要時 `admin.css` 無需改）。`auth_api_controller`、`auth_page_controller`、`page_controller` 都只呼叫 `landing_for`，不需修改。
- **測試**：`tests/test_landing.py`、`tests/test_auth_pages.py`（前端備援與伺服器常數的比對）、`tests/test_admin_page.py`、涉及管理員登入結果的 Google 與密碼驗證測試；其餘以 `DEFAULT_AFTER_LOGIN` 為期望值的測試都是玩家情境，維持不變。
- **規範與文件**：AGENTS.md 第 10 節；`openspec/specs/auth-pages`、`admin-console`；`docs/帳號與安全-auth-security.md`、`docs/系統架構-architecture.md`、`docs/前端結構-frontend.md`、`docs/路線圖-roadmap.md`；README 的登入與預設頁面說明；2.0 簡報的「網站入口」與登入章節及其圖解產生器（`build_diagrams.py`、`build_diagrams_system.py` 寫死「否則 `/game`」「`DEFAULT_AFTER_LOGIN`＝`/game`」）。
- **不涉及**：資料庫、遷移、新端點、模型呼叫、設定與環境變數、個資與保存期限。
