# Design

## Context

動機與範圍見 `proposal.md`；行為契約見 `specs/auth-pages/spec.md`（管理員的預設頁面）與 `specs/admin-console/spec.md`（頂列連結）。

現況（已讀程式確認）：

- `app/security/redirects.py` 的 `landing_for(user, next_url)` 是唯一決定「登入者該去哪裡」的函式：受限登入狀態各去自己的頁面；待確認年齡的玩家去 `/age`（`next` 等於預設頁面時不帶）；其餘去 `safe_next(next_url, DEFAULT_AFTER_LOGIN)`。呼叫者有五處：`page_controller.home`（`/`）、`auth_page_controller` 的 `login_page`、`age_page`、`appeal_page` 與 Google 回呼（玩家），以及 `auth_api_controller.admin_password`（管理員第二步成功後回傳 `next`）。它們都只呼叫 `landing_for`，不自己判斷角色。
- **預設值的補法**是這次最重要的發現：登入頁的初始狀態 `next` 是 `safe_next(request.args.get("next"))`（沒有就是 `/game`）；前端 `googleStartBody` 把它送到 `POST /api/auth/google/start`，伺服器再以 `safe_next(body.get("next"))` 存進簽章的交易 cookie（鍵 `x`）；回呼時管理員的中間狀態 cookie 也存 `safe_next(...)` 的結果。所以到了 `landing_for` 時，「玩家沒有指定返回位置」與「返回位置就是 `/game`」**已經分不開**，`next_url` 一律是 `/game` 或一個明確的路徑。
- `CurrentUser`（`app/services/current_user.py`）已有 `is_active_admin`（角色為 `admin` 且帳號啟用中；後台權限與遊戲幣豁免共用的唯一定義）。管理員的 `restriction` 不會是待確認年齡或被鎖定（只可能是等待刪除），所以管理員不會被導向 `/age` 或 `/appeal`。
- `/admin` 頁面本身已要求「角色為 admin 且啟用中，否則 404；未登入導向登入頁並帶返回位置」（`admin-console` 規格）。
- 前端 `auth-flows.js` 的 `DEFAULT_PAGE = '/game'` 與 `tests/test_auth_pages.py` 比對伺服器常數；`login-password.js` 在管理員密碼驗證成功後 `Auth.safeDestination(response.data.next, '/game')`，備援值只在回應缺 `next` 時才用。
- 後台頂列（`templates/admin/console.html`）只有「客服」「帳號」兩個連結；客服頁頂列與帳號頁導覽列都已有「冒險」連結（`/game`）。

## Goals / Non-Goals

**Goals:**
- 管理員的預設頁面在每個使用預設頁面的地方一致為 `/admin`，**規則只有一處**（`landing_for`），其他呼叫者不改。
- 玩家與受限登入狀態的導向、`next` 的安全檢查（開放轉址防護）完全不變。
- 不改變任何人的權限：導向只是便利，`/admin` 與所有 `/api/admin/*` 仍由既有的管理員檢查把關。

**Non-Goals:**
- 不改交易 cookie、管理員中間狀態 cookie 的內容與簽章格式、登入頁的初始狀態與 `POST /api/auth/google/start` 的契約。
- 不讓前端自行判斷角色或決定預設頁面（伺服器決定，前端只用回應的 `next`）。
- 不做管理員專屬的導覽列改版，只加一個連結。

## Decisions

**1. 兩個並列的常數，由 `landing_for` 依伺服器端的角色選擇。**
新增 `ADMIN_AFTER_LOGIN = "/admin"`，與 `DEFAULT_AFTER_LOGIN = "/game"` 並列；`landing_for` 以 `user.is_active_admin` 選預設頁面。判斷用 `is_active_admin` 而不是單看 `role`，與後台權限同一個定義：停權的管理員不會被導向一個會回 404 的頁面。角色只取自伺服器端的使用者紀錄，不讀任何請求內容。
替代方案：把 `/admin` 的判斷散在各控制器（違反 `landing_for` 集中判斷、容易漏掉某一條路徑）；或讓 `safe_next` 的預設值依角色變（`safe_next` 在登入頁就被呼叫，那時還不知道是誰，且它是純字串檢查，不該認識使用者）。

**2. 「等於玩家預設頁面的 `next`」視為沒有指定。**
`landing_for` 對一般使用者的行為不變；對管理員，`safe_next` 的結果若恰為 `DEFAULT_AFTER_LOGIN`，就改回 `ADMIN_AFTER_LOGIN`。這與待確認年齡的既有作法一致（預設頁面不必帶 `next`，見 `tests/test_landing.py`「預設頁面不必帶」）。
替代方案：讓「沒有指定」一路保持為空值（登入頁初始狀態、`googleStartBody`、`google/start`、交易 cookie、管理員中間狀態都改成可為空）。語意最精確，但要改前端兩個腳本、兩個 cookie 的內容與約十個比對 `x == DEFAULT_AFTER_LOGIN`、`state.next == DEFAULT_AFTER_LOGIN` 的測試，且 cookie 格式變動要考慮已發出的舊 cookie（10 分鐘內的中間狀態）。為了一個只影響「從裸的 `/game` 被擋下來的管理員」的邊角，不值得。
接受的代價：管理員被擋在**恰好是 `/game`** 之後登入，會落在 `/admin` 而不是 `/game`（一鍵可回，頂列有「冒險」）；`/game/<存檔>`、`/game/start/<版本>`、`/cs`、`/account` 等其他任何明確的返回位置都照原樣回去。

**3. 判斷只在 `landing_for` 內，其餘呼叫者不改。**
`/`、`/login`、`/age`（已完成者）、`/appeal`（沒有申訴狀態者）、Google 回呼與 `admin_password` 本來就共用它，所以一處修改涵蓋所有情況，也自動保持與規格「所有使用預設頁面的情況一致」相符。管理員走 Google 回呼時會先進密碼驗證頁（中間狀態），登入 session 在 `admin_password` 才建立，回傳的 `next` 由 `landing_for(user, step.next_url)` 算出，所以前端不需要知道角色。

**4. 前端：`login-password.js` 的備援改為 `/admin`，其餘不動。**
備援值與伺服器常數用同一個測試鎖住（把 `tests/test_auth_pages.py` 現有的比對對象由 `DEFAULT_AFTER_LOGIN` 改為 `ADMIN_AFTER_LOGIN`，並新增「`auth-flows.js` 的 `DEFAULT_PAGE` 仍等於 `DEFAULT_AFTER_LOGIN`」維持原狀）。`auth-flows.js` 的 `DEFAULT_PAGE`（玩家情境的備援）仍是 `/game`。
替代方案：不改備援（它平常不會用到）。但備援值寫死 `/game` 會讓「管理員第二步的後備導向」與規格不一致，且測試已經在鎖這類值，順手對齊成本很低。

**5. 後台頂列加一個一般的 `<a href="/game">冒險</a>`。**
沿用 `admin-topbar__link` 樣式，不新增 CSS、不用 `data-bs-*`，位置在「客服」之前（與「入口」的重要性一致：管理員新增的是通往玩家頁面的入口）。連結目標是純站內路徑，不需要任何新的存取判斷。

**6. AGENTS.md 第 10 節的修訂只動「預設頁面」那一句。**
由「單一常數；目前為 /game，……客服 /cs 仍可直接開啟」改為「依角色各一個常數：玩家為 /game（……），管理員為 /admin；登入前指定的合法返回位置優先；客服 /cs 仍可直接開啟」。其他句子（待確認年齡一律導向年齡確認頁等）不動。文件裡凡寫「單一常數 `DEFAULT_AFTER_LOGIN`」之處同步改成兩個常數與依角色選擇。

## Risks / Trade-offs

- [管理員從裸的 `/game` 被擋下登入後落在 `/admin`] → 已在決策 2 接受；規格以「沒有指定包含恰等於 `/game`」明寫，測試涵蓋；頂列「冒險」一鍵回去。
- [管理員未確認年齡也會被導向 `/admin`] → `/admin` 的頁面外殼與 `/api/admin/*` 不呼叫模型，與「管理員未確認年齡之前不得使用會呼叫模型的功能」不衝突；規格有情境鎖住。要用 RPG 時仍由既有的 `age_confirmation_required` 閘門擋下。
- [前端備援與伺服器常數漂移] → 既有的「前端備援等於伺服器常數」測試改為兩組比對（玩家 `/game`、管理員 `/admin`）。
- [簡報與圖解寫死「否則 `/game`」「`DEFAULT_AFTER_LOGIN`＝`/game`」] → 依 `deck-conflict-watch` 只通知使用者，由使用者決定是否重產（本 change 的任務不自行修改簡報、圖解、PDF、pptx）。
- [已發出的 cookie] → 不改任何 cookie 的內容與格式，所以沒有相容問題；部署只是換版，不需要遷移，回復只換回前一版映像。
