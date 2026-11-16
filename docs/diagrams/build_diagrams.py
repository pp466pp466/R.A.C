"""產生兩張以 Azure 圖示設計的架構圖解（PNG）：

    docs/登入與註冊架構圖解.png    使用者註冊與登入：流程、程式模組、資料庫存取
    docs/MCP架構圖解.png           聊天介面 → NLP → 函式與資料存取 → 回應的完整週期（含 MCP 的規劃位置）

用法（專案根目錄）：  python docs/diagrams/build_diagrams.py [login|mcp]
需要 Chrome 或 Edge（見 render_tools.find_chrome）。圖中的函式、端點與資料表名稱取自程式碼，架構改變時請同步修改本檔。
Azure 圖示的使用條款見 docs/diagrams/README.md。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from render_tools import DOCS_DIR, FONTS_CSS, FONT_STACK, PALETTE, file_url, html_to_png, write_text
from svg_kit import Svg, tw, wrap

P = PALETTE
W = 1800

LOGIN_PNG = DOCS_DIR / "登入與註冊架構圖解.png"
MCP_PNG = DOCS_DIR / "MCP架構圖解.png"


# ---- 共用版面 ---------------------------------------------------------------------------------


def header(svg: Svg, *, tag: str, title: str, subtitle: str, title_size: int = 40) -> None:
    svg.text(40, 36, tag, size=14, fill=P["accent"], mono=True)
    svg.text(40, 82, title, size=title_size, weight=700)
    svg.text(40, 116, subtitle, size=19, fill=P["muted"])
    svg.text(W - 40, 40, "圖示：Azure Architecture Icons（僅作元件符號；實際部署為 Google Cloud）", size=14, fill=P["muted"], anchor="end")
    svg.text(W - 40, 62, "卡片內「Azure 圖示：…」是圖示名稱，並不代表使用該 Azure 服務", size=13, fill=P["muted"], anchor="end")
    svg.text(W - 40, 84, "橘色圓圈的數字＝流程步驟，對應下方的「功能對應」表", size=13, fill=P["muted"], anchor="end")


def mapping_table(svg: Svg, y: float, *, title: str, headers: tuple[str, str, str, str], rows: list[tuple[str, str, str, str]],
                  widths: tuple[int, int, int] = (400, 760, 480)) -> float:
    """底部的「功能對應」表：步驟徽章 ｜ 動作 ｜ 程式 ｜ 資料表。回傳表格下方的 y。"""
    svg.text(40, y + 8, title, size=26, weight=700)
    y += 30
    x_badge, x_action = 56, 100
    x_code = x_action + widths[0] + 20
    x_db = x_code + widths[1] + 20
    svg.rect(30, y, W - 60, 40, fill=P["panel"], stroke=P["line"], rx=8)
    svg.text(x_action, y + 26, headers[0], size=16, fill=P["accent"], weight=700)
    svg.text(x_code, y + 26, headers[1], size=16, fill=P["accent"], weight=700)
    svg.text(x_db, y + 26, headers[2], size=16, fill=P["accent"], weight=700)
    svg.text(x_badge, y + 26, headers[3], size=16, fill=P["accent"], weight=700, anchor="middle")
    y += 48
    size, lh = 15, 22
    for index, (number, action, code, db) in enumerate(rows):
        a = wrap(action, size, widths[0], mono=False)
        c = wrap(code, size, widths[1], mono=False)
        d = wrap(db, size, widths[2], mono=False)
        height = max(len(a), len(c), len(d)) * lh + 20
        if index % 2 == 0:
            svg.rect(30, y, W - 60, height, fill="#14161A", rx=8)
        svg.badge(x_badge, y + 20, number, r=13, size=15)
        svg.lines(x_action, y + 25, a, size=size, lh=lh, weight=600)
        svg.lines(x_code, y + 25, c, size=size, lh=lh, fill=P["text"])
        svg.lines(x_db, y + 25, d, size=size, lh=lh, fill=P["muted"])
        y += height + 4
    return y


def page_html(svg_markup: str, width: int, height: int) -> str:
    return (
        f'<!doctype html><html lang="zh-Hant-TW"><head><meta charset="UTF-8">'
        f'<link rel="stylesheet" href="{file_url(FONTS_CSS)}">'
        f'<style>html,body{{margin:0;padding:0;background:{P["bg"]};font-family:{FONT_STACK}}}svg{{display:block}}</style>'
        f"</head><body>{svg_markup}</body></html>"
    )


def render_png(svg: Svg, png: Path) -> None:
    markup = svg.render()
    with tempfile.TemporaryDirectory(prefix="diagram-") as folder:
        page = Path(folder) / "diagram.html"
        write_text(page, page_html(markup, svg.width, svg.height))
        html_to_png(page, png, width=svg.width, height=svg.height, scale=2)
    print(f"已輸出 {png}（版面 {svg.width} x {svg.height}）")


def band(svg: Svg, x: float, y: float, w: float, *, label: str, chips: list, size: float = 14, mono: bool = False) -> float:
    """分層的色帶：標籤 + 膠囊標籤。先量高度再畫底，回傳色帶高度。"""
    tmp = Svg(W, 10)
    bottom = tmp.chips(x + 14, y + 36, w - 28, chips, size=size, mono=mono)
    height = bottom - y + 12
    svg.rect(x, y, w, height, fill=P["panel_alt"], stroke=P["line"], rx=10)
    svg.text(x + 14, y + 22, label, size=12, fill=P["accent"], mono=True)
    svg.parts.extend(tmp.parts)
    return height


# ---- 登入與註冊 -------------------------------------------------------------------------------


def login_diagram() -> Svg:
    svg = Svg(W, 100)
    header(svg, tag="[ AUTH_FLOW ]", title="登入與註冊架構圖解",
           subtitle="Google 登入即註冊：玩家走 Authorization Code ＋ PKCE → 年齡確認 → 贈送遊戲幣；管理員在 Google 之後再輸入密碼（二階段）")

    top = 140
    # -- 使用者端（A 欄）
    ax, aw = 30, 225
    y = top + 56
    h = svg.card(ax + 10, y, aw - 20, icon="users", azure="Users", title="玩家", badges=(1,),
                 body=["首次登入＝註冊，須勾選「同意服務條款與隱私權政策」", "回訪者一鍵登入，不必勾選"])
    y += h + 12
    browser_y = y
    h = svg.card(ax + 10, y, aw - 20, icon="browser", azure="Browser", title="瀏覽器", badges=(3, 2),
                 body=["Vue 3 全域變數模式＋axios", "/login、/age、/appeal、/account", "auth/login.js、age.js、auth-common.js", "meta csrf-token → X-CSRF-Token", "③ 導向 Google 授權頁（不經過伺服器）"])
    browser_mid = browser_y + h / 2
    y += h + 12
    admin_y = y
    h = svg.card(ax + 10, y, aw - 20, icon="users", azure="Users", title="管理員", badges=(8,),
                 body=["先通過 Google，再到 /login/password 輸入密碼", "login-password.js", "不能只靠密碼登入"])
    y += h + 12
    h = svg.card(ax + 10, y, aw - 20, icon="app-registrations", azure="App Registrations", title="登入狀態 cookie", badges=(9, 6),
                 body=["簽章 session cookie：HttpOnly、SameSite=Lax；staging 與 production 加 Secure 與 __Host- 前綴",
                       "內容只有 uid 與 tv（token_version），不使用 JWT", "閒置 30 天、每次使用就續期；撤銷靠 token_version ＋1"], body_size=14)
    a_bottom = y + h + 12

    # -- 入口層（B 欄）
    bx, bw = 290, 210
    h = svg.card(bx + 10, top + 56, bw - 20, icon="front-door", azure="Front Door and CDN Profiles", title="HTTPS",
                 body=["HTTPS／HTTP/2、HSTS", "ProxyFix 信任轉發標頭", "security_headers.py", "Cloud Run 容器"], body_size=14)
    edge_mid = top + 56 + 40
    y = top + 56 + h + 12
    h = svg.card(bx + 10, y, bw - 20, icon="conditional-access", azure="Conditional Access", title="受限登入", badges=(9, 6),
                 body=["預設拒絕，只有明確允許的端點能通過", "locked → 403 account_locked，導向 /appeal", "age_pending → 403 age_confirmation_required，導向 /age",
                       "deletion_pending → 403 account_deletion_pending，導向 /account", "tests/test_restricted_session.py 掃描全部路由"], body_size=13)
    b_bottom = y + h + 12

    # -- Flask 應用程式（C 欄）
    cx, cw = 530, 790
    app_top = top
    svg.rect(cx, app_top, cw, 100, fill="#14161A", stroke=P["accent"], rx=14, sw=1.5)   # 高度稍後補上
    app_rect_index = len(svg.parts) - 1
    svg.text(cx + 16, app_top + 24, "[ FLASK APP ]", size=12, fill=P["accent"], mono=True)
    svg.icon("app-services", cx + 16, app_top + 32, 40)
    svg.text(cx + 66, app_top + 56, "Flask 應用程式（create_app）", size=21, weight=700)
    svg.text(cx + 66, app_top + 76, "Azure 圖示：App Services　｜　gunicorn gthread、非 root 容器；多 worker 不依賴行程內狀態", size=12, fill=P["muted"])
    rx, rw = cx + 16, cw - 32
    ry = app_top + 96

    rows = [
        ("page", "MVC PAGE CONTROLLER ‧ auth_page_controller.py（只回 HTML 外殼與初始狀態）", [
            ("GET /login", (1,)), ("GET /auth/google/callback", (4, 6)), ("GET /age", (7,)), ("GET /login/password", (8,)),
            ("GET /appeal", (6,)), ("GET /account", (6,)), ("GET /terms・/privacy", (1,)),
        ], True),
        ("api", "WEB API CONTROLLER ‧ auth_api_controller.py（只回 JSON）", [
            ("POST /api/auth/google/start", (2,)), ("POST /api/auth/verify-age", (7,)), ("POST /api/auth/admin-password", (8,)),
            ("POST …/admin-password/forgot", (8,)), ("GET /api/auth/me", (9,)), ("POST /logout・/logout-all", (9,)),
        ], True),
        ("sec", "SECURITY ‧ app/security/（HTTP 安全輔助，不碰資料庫）", [
            ("SlidingSessionInterface 閒置 30 天續期", (6, 9)), ("login_user／current_user／require_login", (6, 9)),
            ("oauth_cookie 交易 cookie 10 分", (2, 4)), ("admin_step 中間狀態 10 分", (6, 8)),
            ("csrf：X-CSRF-Token＋Origin", (2, 7, 8)), ("redirects：safe_next／landing_for", (6,)),
        ], False),
        ("svc", "SERVICE ‧ app/services/（業務邏輯與 AccessPolicy；不匯入 flask）", [
            ("GoogleSignInService begin／complete／_resolve", (2, 4, 5)), ("GoogleOidcClient 換 token、驗 ID token", (4,)),
            ("AuthService verify_age／verify_admin_password／resolve_session", (7, 8, 9)),
            ("AccessPolicy／CurrentUser.restriction", (6, 9)), ("AccountLockService.register_strike", (5, 7)),
            ("AccountErasureService erase_underage／清除", (7, 10)), ("WalletService.ensure_signup_grant", (7,)),
            ("EmailFlowService 管理員重設信", (8,)), ("AppealService", (6,)),
        ], False),
        ("repo", "REPOSITORY ‧ app/repositories/（唯一碰 ORM；條件式 UPDATE 與擁有者條件）", [
            ("UserRepository", (5, 6, 7, 8, 9, 10)), ("UserIdentityRepository", (5,)), ("LockStrikeRepository", (5, 7)),
            ("WalletRepository", (7,)), ("DeletionTombstoneRepository", (7, 10)), ("AuthTokenRepository", (8,)), ("LockAppealRepository", (6,)),
        ], False),
    ]
    spans: dict[str, tuple[float, float]] = {}
    for index, (key, label, chips, mono) in enumerate(rows):
        h = band(svg, rx, ry, rw, label=label, chips=chips, size=14, mono=mono)
        spans[key] = (ry, ry + h)
        if index < len(rows) - 1:
            svg.arrow([(rx + rw / 2, ry + h + 1), (rx + rw / 2, ry + h + 17)], color=P["muted"], sw=2)
        ry += h + 18
    c_bottom = ry - 18 + 14
    svg.parts[app_rect_index] = (
        f'<rect x="{cx:.1f}" y="{app_top:.1f}" width="{cw:.1f}" height="{c_bottom - app_top:.1f}" rx="14" fill="#14161A" stroke="{P["accent"]}" stroke-width="1.5"/>'
    )
    svg.text(rx + rw - 4, app_top + 24, "上層只呼叫下一層，tests/architecture 以 AST 守住", size=12, fill=P["muted"], anchor="end")

    # -- 資料與外部服務（D 欄）
    dx, dw = 1370, 400
    d1 = top
    h1 = svg.card(dx, d1, dw, icon="scheduler", azure="Scheduler", title="排程器（平台排程）", badges=(10,),
                  body=["觸發 flask 指令，不使用行程內計時器", "purge-unconfirmed-accounts：每小時", "purge-inactive-accounts、execute-account-deletions：每日"], body_size=14)
    svg.text(dx + dw / 2, d1 + h1 + 26, "圖例：實線＝同步呼叫　虛線＝設定或旁路", size=13, fill=P["muted"], anchor="middle")
    # 對齊各層
    log_y = spans["api"][0]
    h2 = svg.card(dx, log_y, dw, icon="log-analytics", azure="Log Analytics Workspaces", title="結構化 JSON 日誌",
                  body=["security_events：只記事件列舉與內部編號", "不含 email、授權碼、cookie、生日、暱稱"], body_size=14)
    kv_y = max(spans["sec"][0], log_y + h2 + 12)
    h3 = svg.card(dx, kv_y, dw, icon="key-vaults", azure="Key Vaults", title="機密注入",
                  body=["SECRET_KEY（簽章 cookie）、GOOGLE_CLIENT_SECRET、DATABASE_URL", "本機 .env；正式環境 Secret Manager"], body_size=14)
    g_y = max(spans["svc"][0], kv_y + h3 + 12)
    h4 = svg.card(dx, g_y, dw, icon="external-identities", azure="External Identities", title="Google OIDC", badges=(4, 3),
                  body=["accounts.google.com：授權、token、JWKS", "要求 email_verified 為真", "開發與正式使用不同的 OAuth 用戶端"], body_size=14)
    s_y = g_y + h4 + 12
    h5 = svg.card(dx, s_y, dw, icon="communication-services", azure="Communication Services", title="SMTP 寄信", badges=(8,),
                  body=["只寄管理員的重設密碼信與營運告警信", "玩家不收任何信"], body_size=14)
    sql_y = max(spans["repo"][0], s_y + h5 + 12)
    sql_tables = [
        ("users", (5, 6, 7, 8, 9, 10)), ("user_identities", (5,)), ("lock_strikes", (5, 7)), ("lock_appeals", (6,)),
        ("wallets", (7,)), ("wallet_entries", (7,)), ("deletion_tombstones", (7, 10)), ("auth_tokens", (8,)), ("admin_audit_logs", (5,)),
    ]
    tmp = Svg(W, 10)
    chips_bottom = tmp.chips(dx + 14, sql_y + 66, dw - 28, sql_tables, size=14, mono=True)
    sql_h = chips_bottom - sql_y + 14
    svg.rect(dx, sql_y, dw, sql_h, fill=P["panel"], stroke=P["line"], rx=12)
    svg.icon("sql-database", dx + 14, sql_y + 14, 44)
    svg.text(dx + 70, sql_y + 38, "SQL Server project", size=20, weight=700)
    svg.text(dx + 70, sql_y + 57, "Azure 圖示：SQL Database", size=11, fill=P["muted"])
    svg.parts.extend(tmp.parts)
    d_bottom = sql_y + sql_h

    # -- 箭頭
    svg.arrow([(ax + aw, edge_mid), (bx, edge_mid)], label="HTTPS", label_at=((ax + aw + bx) / 2, edge_mid - 8), both=True, color=P["text"])
    svg.arrow([(bx + bw, edge_mid), (cx, edge_mid)], label="請求", label_at=((bx + bw + cx) / 2, edge_mid - 8), color=P["text"], both=True)
    svg.arrow([(cx + cw, (spans["svc"][0] + spans["svc"][1]) / 2 - 20), (dx, g_y + h4 / 2)], color=P["accent"], both=True, label="OIDC", label_at=((cx + cw + dx) / 2, g_y + h4 / 2 - 24))
    svg.arrow([(cx + cw, (spans["svc"][0] + spans["svc"][1]) / 2 + 30), (dx, s_y + h5 / 2)], color=P["muted"], dash="6 5", label="SMTP", label_at=((cx + cw + dx) / 2, s_y + h5 / 2 - 6))
    svg.arrow([(cx + cw, (spans["repo"][0] + spans["repo"][1]) / 2), (dx, sql_y + 40)], color=P["accent"], both=True, label="ORM", label_at=((cx + cw + dx) / 2, sql_y + 24))
    svg.arrow([(dx, kv_y + h3 / 2), (cx + cw, (spans["sec"][0] + spans["sec"][1]) / 2)], color=P["muted"], dash="6 5", label="機密", label_at=((cx + cw + dx) / 2, kv_y + h3 / 2 - 8))
    svg.arrow([(cx + cw, (spans["api"][0] + spans["api"][1]) / 2), (dx, log_y + h2 / 2)], color=P["muted"], dash="6 5", label="日誌", label_at=((cx + cw + dx) / 2, log_y + h2 / 2 - 8))
    svg.arrow([(dx, d1 + h1 / 2), (cx + cw, d1 + 60)], color=P["muted"], dash="6 5", label="指令", label_at=((cx + cw + dx) / 2, d1 + h1 / 2 - 8))

    # 左側兩欄的外框與標題（內容畫完才知道高度；外框不填色，所以後畫也不會蓋住卡片）
    panel_bottom = max(a_bottom, b_bottom, c_bottom, d_bottom)
    for lane_x, lane_w, tag, title in ((ax, aw, "[ CLIENT ]", "使用者端"), (bx, bw, "[ EDGE ]", "入口層")):
        svg.rect(lane_x, top, lane_w, panel_bottom - top, fill="none", stroke=P["line"], rx=14)
        svg.text(lane_x + 16, top + 24, tag, size=12, fill=P["accent"], mono=True)
        svg.text(lane_x + 16, top + 50, title, size=21, weight=700)

    rows_data = [
        ("1", "開啟登入頁；新使用者勾選條款",
         "GET /login → auth_page_controller.login_page（已登入者以 landing_for 導向）；前端 auth/login.js",
         "已登入者：current_user() 讀 users（token_version、status）；匿名：不碰資料庫"),
        ("2", "按「使用 Google 登入」：發起授權",
         "POST /api/auth/google/start → GoogleSignInService.begin(accept_terms)：產生 state、nonce、PKCE verifier → oauth_cookie.store_transaction（簽章 cookie，10 分鐘）→ 回傳授權網址；先通過 CSRF（X-CSRF-Token＋Origin）",
         "不碰資料庫（條款旗標只放在簽章 cookie）"),
        ("3", "瀏覽器導向 Google 授權頁並同意",
         "Authorization Code ＋ PKCE（S256），不經過伺服器",
         "—（外部：Google OIDC）"),
        ("4", "Google 導回：驗證 state、換取並驗證 ID token",
         "GET /auth/google/callback → google_callback → GoogleSignInService.complete：_check_state（hmac）→ GoogleOidcClient.exchange_code（驗 iss、aud、exp、nonce；email_verified 為真）",
         "不碰資料庫；失敗只導回 /login?error=固定代碼並記日誌"),
        ("5", "帳號對應：已綁定登入、同信箱合併、全新信箱註冊、被拒計次",
         "GoogleSignInService._resolve → UserIdentityRepository.get_by_subject → UserRepository.get_by_canonical_email → add_user＋add_identity（須勾選條款；age_deadline_at＝建立後 24 小時）；被拒 → AccountLockService.register_strike",
         "R users、user_identities；W users（新帳號）、user_identities；被拒 W lock_strikes（達門檻 W users.locked_at、admin_audit_logs）"),
        ("6", "建立登入狀態並依狀態導向",
         "玩家：login_user（session cookie 只含 uid、tv；換發 CSRF）→ landing_for：待確認年齡 /age、被鎖定 /appeal、等待刪除 /account，否則 /game；管理員：store_admin_step → /login/password（此時尚無 session）",
         "W users.last_login_at（_finish）"),
        ("7", "年齡確認（玩家登入後的第一步）",
         "POST /api/auth/verify-age → AuthService.verify_age：check_nickname、validate_age_input；未滿 13 歲 → AccountErasureService.erase_underage；不一致 → register_strike；成功 → UserRepository.complete_age_verification_if_unset → 回呼 WalletService.ensure_signup_grant（贈 200 幣）",
         "W users（birth_date、age_range、nickname、age_confirmed_at）；W wallets、wallet_entries；未滿 13 歲 D users（CASCADE）＋W deletion_tombstones；不一致 W lock_strikes"),
        ("8", "管理員第二道：輸入密碼",
         "POST /api/auth/admin-password → AuthService.verify_admin_password（Argon2id；連續 5 次失敗鎖定 15 分鐘）→ login_user；忘記密碼：EmailFlowService.request_admin_password_reset（SMTP）",
         "R／W users（failed_login_count、locked_until、last_login_at）；W auth_tokens（只存 SHA-256 雜湊）"),
        ("9", "之後每個請求：辨識、續期與撤銷",
         "current_user → AuthService.resolve_session(uid, tv)；require_login（受限狀態預設 403）；SlidingSessionInterface 每次重發 cookie；logout-all → UserRepository.bump_token_version",
         "R users（token_version、status）；logout-all：W users.token_version ＋1"),
        ("10", "排程清理未完成與閒置帳號",
         "flask purge-unconfirmed-accounts（每小時；24 小時內未完成年齡確認）、purge-inactive-accounts、execute-account-deletions（每日）→ AccountErasureService（單一抹除出口）",
         "D users（其餘資料表 CASCADE）；W deletion_tombstones"),
    ]
    end_y = mapping_table(svg, panel_bottom + 36, title="功能對應：流程步驟 ↔ 程式模組 ↔ 資料表存取",
                          headers=("流程動作", "程式模組與函式（Controller → Service → Repository）", "資料表存取（R 讀、W 寫、D 刪）", "步驟"), rows=rows_data)
    svg.text(40, end_y + 24, "依據：docs/帳號與安全-auth-security.md、docs/project資料結構文件.md；資料表名稱與欄位以後者為準。", size=14, fill=P["muted"])
    svg.height = int(end_y + 50)
    print(f"登入圖：架構區高度 {panel_bottom:.0f}／全圖 {svg.height}（架構區占 {panel_bottom / svg.height:.3f}）")
    return svg


# ---- MCP 與客服週期 --------------------------------------------------------------------------


def mcp_diagram() -> Svg:
    svg = Svg(W, 100)
    header(svg, tag="[ CS_NLP_CYCLE ]", title="MCP 架構圖解：聊天介面 → NLP → 函式與資料存取 → 回應",
           subtitle="客服對話的完整週期（已實作），以及 MCP 在本平台的規劃位置（RC-6 external-data-gateway，尚未實作）", title_size=36)

    # 現況聲明
    by = 138
    svg.rect(30, by, W - 60, 70, fill=P["panel"], stroke=P["line"], rx=10)
    svg.rect(30, by, 6, 70, fill=P["accent"], rx=3)
    svg.text(54, by + 28, "現況聲明", size=17, fill=P["accent"], weight=700)
    svg.text(140, by + 28, "本專案的客服 v1 不提供工具呼叫（AGENTS.md 第 10 節），目前沒有實作 MCP：模型只做 NLP（輸出結構化 JSON），所有資料存取都由 Service 層函式完成。", size=17)
    svg.text(140, by + 54, "實線＝已實作；虛線＝規劃中（ExternalDataProvider ＋ MCP 轉接器，由 Service 發動、模型不選工具、不傳參數；見 docs/RPG.md R-16）。", size=17, fill=P["muted"])

    # 六欄
    n, gap = 6, 34
    cw = (W - 60 - gap * (n - 1)) / n
    top = 282
    xs = [30 + i * (cw + gap) for i in range(n)]

    columns = [
        dict(icon="browser", azure="Browser", title="聊天介面", badges=(1,), body=[
            "Vue 3 全域變數模式（/cs 頁面）", "chat.js：送出與取消（AbortController；axios 逾時 35 秒）",
            "cs-store.js：IndexedDB 暫存對話與待送訊息", "cs-sync.js：since_seq 增量補齊",
            "typewriter.js：逐字渲染 reply_text", "建議動作 chips（最多 4 個）"]),
        dict(icon="api-management", azure="API Management Services", title="Web API Controller", badges=(2,), body=[
            "POST /api/cs/conversations/<public_id>/messages", "cs_api_controller.send_message",
            "require_login：受限狀態預設 403", "CSRF：X-CSRF-Token＋Origin",
            "→ ChatService.send_message(user, public_id, client_msg_id, content)", "回應 JSON，Cache-Control: no-store"]),
        dict(icon="bot-services", azure="Bot Services", title="ChatService（編排者）", badges=(3,), body=[
            "AccessPolicy.require_chat_access：年齡閘門", "輸入清理：NFC、去控制字元、1000 字",
            "冪等：find_message，已有回覆直接回傳", "UsageService.check_send：暫停、每分鐘、每日輪數、全站用量",
            "acquire_processing：處理標記（60 秒）", "append_message（使用者）→ commit，之後不持有交易",
            "build_context：摘要＋最近 10 輪＋狀態"]),
        dict(icon="language-understanding", azure="Language Understanding", title="NLP 意圖與實體解析", badges=(4,), body=[
            "LlmService.run_structured(kind=chat)", "GeminiProvider.generate：非串流、response schema、單次 20 秒",
            "輸出 JSON：intent、confidence、entities、reply_text、suggested_actions",
            "parse_llm_json＋semantic_rules：去除 ```json、schema 與語意驗證",
            "失敗：同模型重試 1 次；429／5xx／逾時切備援；總預算 30 秒；被擋或設定錯誤降級"]),
        dict(icon="function-apps", azure="Function Apps", title="Service 函式與資料存取", badges=(6, 5), body=[
            "to_reply：信心 < 0.6 → 改為 unknown", "_write_reply：助理訊息（intent、entities、suggested_actions）",
            "_advance_state：next_state → write_state（state_version 條件式）", "release_processing → commit（連同最後一筆帳本）",
            "record_block_outcome：被擋累計暫停", "after_reply → SummaryService.compact：補償式摘要",
            "模型從不直接呼叫這些函式"]),
        dict(icon="sql-database", azure="SQL Database", title="SQL Server project", badges=(), body=[
            "ChatRepository：每個方法以 user_id 為擁有者條件", "LlmCallLogRepository：用量、斷路與全站 token 的唯一來源"]),
    ]
    tables = [("chat_conversations", (3, 5)), ("chat_messages", (3, 5)), ("llm_call_logs", (3, 4, 5)), ("lock_strikes", (6,))]

    def draw_column(target: Svg, index: int, min_h: float = 0) -> float:
        spec = columns[index]
        h = target.card(xs[index], top, cw, icon=spec["icon"], azure=spec["azure"], title=spec["title"], body=spec["body"], badges=spec["badges"],
                        body_size=14, title_size=19, min_h=min_h)
        return h

    base_h = [draw_column(Svg(W, 10), i) for i in range(n)]
    tables_y = top + base_h[-1] + 8
    tables_bottom = Svg(W, 10).chips(xs[-1] + 14, tables_y, cw - 28, tables, size=13, mono=True)      # 資料欄：內文下方還要放資料表標籤
    natural = base_h[:-1] + [tables_bottom - top + 14]
    natural[3] += 70                                                                                    # NLP 欄底部留給 Gemini 迷你卡
    col_h = max(natural)
    for i in range(n):
        draw_column(svg, i, min_h=col_h)
    svg.chips(xs[-1] + 14, tables_y, cw - 28, tables, size=13, mono=True)
    cards_bottom = top + col_h

    # NLP 欄底部的 Gemini 迷你卡
    gy = cards_bottom - 64
    svg.rect(xs[3] + 10, gy, cw - 20, 54, fill=P["panel_alt"], stroke=P["line"], rx=10)
    svg.icon("cognitive-services", xs[3] + 20, gy + 8, 36)
    svg.text(xs[3] + 64, gy + 26, "Gemini API（外部）", size=15, weight=700)
    svg.text(xs[3] + 64, gy + 44, "模型名稱取自環境變數", size=11, fill=P["muted"])
    svg.badge(xs[3] + cw - 24, gy + 16, "4", r=10, size=12)

    # 上方的流程箭頭（水平管線）
    labels = ["POST messages { client_msg_id, content }", "send_message(user, …)", "run_structured(contents, schema)",
              "結構化 JSON → CsReply", "Repository 的 ORM 讀寫"]
    pipe_y = top - 20
    for i in range(n - 1):
        x1 = xs[i] + cw / 2 + 50
        x2 = xs[i + 1] + cw / 2 - 50
        svg.arrow([(x1, top - 2), (x1, pipe_y), (x2, pipe_y), (x2, top - 2)], color=P["accent"], sw=2.2)
        svg.text((x1 + x2) / 2, pipe_y - 8, labels[i], size=13, fill=P["text"], anchor="middle", halo=True)

    # 回應箭頭（U 形）
    ret_y = cards_bottom + 38
    x_from, x_to = xs[4] + cw / 2 - 70, xs[0] + cw / 2
    svg.arrow([(x_from, cards_bottom + 2), (x_from, ret_y), (x_to, ret_y), (x_to, cards_bottom + 2)], color=P["ok"], sw=2.4)
    svg.badge(x_to + 50, ret_y, "7", r=12, fill=P["ok"], size=14)
    svg.text((x_from + x_to) / 2 + 20, ret_y - 9, "回應 JSON：user_message、assistant_message、conversation（只有文字與建議動作；intent、entities、信心與摘要不離開伺服器）",
             size=14, fill=P["text"], anchor="middle", halo=True)

    # 規劃中的 MCP
    py = ret_y + 58
    # 角色對照
    role = ("Host（編排）：現況＝ChatService／GameService（Service 主導）；規劃＝不變",
            "Client：現況＝無；規劃＝ExternalDataProvider 的 MCP 轉接器",
            "Server／Tool：現況＝無（程序內的 Python 函式與 Repository 代替）；規劃＝白名單外部資料",
            "模型：現況與規劃都只輸出結構化 JSON，不呼叫工具（AGENTS.md 第 10 節；RPG.md T-5 暫擱置）")
    lane_x, lane_w = 760, W - 30 - 760
    pw = (lane_w - 32 - 2 * 40) / 3
    px = [lane_x + 16 + i * (pw + 40) for i in range(3)]
    pcards = [
        dict(icon="api-connections", azure="API Connections", title="ExternalDataProvider",
             body=["介面：比照寄信器與 Google 身分供應商，create_app() 建立，Service 經 runtime 取用", "測試用假的實作"]),
        dict(icon="api-connections", azure="API Connections", title="MCP 轉接器（MCP Client）",
             body=["自行以 mcp 套件撰寫用戶端", "短逾時；失敗降級，不擋住對話或遊戲"]),
        dict(icon="cognitive-search", azure="Cognitive Search", title="白名單外部資料（MCP Server）",
             body=["偏百科類來源，只回受限結構的摘要", "只送標準化標籤，不送玩家原文"]),
    ]
    def draw_pcard(target: Svg, i: int, min_h: float = 0) -> float:
        spec = pcards[i]
        return target.card(px[i], py + 40, pw, icon=spec["icon"], azure=spec["azure"], title=spec["title"], body=spec["body"],
                           body_size=13, title_size=16, dashed=True, min_h=min_h)

    role_h = Svg(W, 10).card(30, py, 700, title="MCP 角色對照（概念對應，非現況實作）", title_size=19, body=list(role), body_size=14)
    card_h = max(draw_pcard(Svg(W, 10), i) for i in range(3))
    rh = max(role_h, 40 + card_h + 16)                                   # 虛線框要包得住三張規劃卡片
    svg.card(30, py, 700, title="MCP 角色對照（概念對應，非現況實作）", title_size=19, body=list(role), body_size=14, min_h=rh)
    svg.rect(lane_x, py, lane_w, rh, fill="#14161A", stroke=P["muted"], rx=14, dash="9 7")
    svg.text(lane_x + lane_w - 16, py + 24, "[ PLANNED ‧ RC-6 external-data-gateway ‧ 尚未實作 ]", size=12, fill=P["accent"], mono=True, anchor="end")
    for i in range(3):
        draw_pcard(svg, i, min_h=card_h)
    for i in range(2):
        svg.arrow([(px[i] + pw, py + 40 + card_h / 2), (px[i + 1], py + 40 + card_h / 2)], color=P["muted"], dash="6 5")
    # 從 Service 欄發動
    sx = xs[4] + cw / 2 + 60
    mid_y = ret_y + 30
    entry_x = px[0] + pw / 2
    svg.arrow([(sx, cards_bottom + 2), (sx, mid_y), (entry_x, mid_y), (entry_x, py + 40)], color=P["muted"], dash="6 5")
    svg.text((sx + entry_x) / 2, mid_y - 8, "由 Service 發動（模型不選工具、不傳參數）", size=13, fill=P["muted"], anchor="middle", halo=True)
    panel_bottom = py + rh

    rows_data = [
        ("1", "聊天介面：輸入與送出",
         "chat.js 送出（axios 35 秒逾時、AbortController）；cs-store.js 把對話與待送訊息存 IndexedDB；cs-sync.js 以 since_seq 增量補齊；前端中止不代表後端中止",
         "不碰 SQL Server（瀏覽器端 IndexedDB 只是快取，衝突時以後端為準）"),
        ("2", "API 入口",
         "POST /api/cs/conversations/<public_id>/messages → cs_api_controller.send_message → require_login → ChatService.send_message；CSRF 於 before_request 統一檢查",
         "—（Controller 不碰資料庫）"),
        ("3", "編排：閘門、冪等、用量、標記、寫入、組上下文",
         "AccessPolicy.require_chat_access → _clean_content → _owned → find_message → UsageService.check_send → acquire_processing → append_message → commit → build_context／build_user_contents",
         "R chat_conversations、chat_messages、llm_call_logs；W chat_messages（使用者訊息，seq 遞增）、chat_conversations（處理標記、預設標題）"),
        ("4", "NLP：意圖與實體解析",
         "LlmService.run_structured → GeminiProvider.generate（非串流、response schema）→ parse_llm_json＋cs_intents.semantic_rules；重試、備援、斷路與降級見模型呼叫層文件",
         "R llm_call_logs（斷路判斷）；每次呼叫 W llm_call_logs（模型、token、counted；不含對話全文）"),
        ("5", "Service 函式：把解析結果變成資料存取",
         "to_reply → _write_reply（append_message）→ _advance_state（next_state、write_state）→ release_processing → commit → record_block_outcome → after_reply → SummaryService.compact",
         "W chat_messages（助理訊息）、chat_conversations（state_json、summary、state_version）；被擋次數由 llm_call_logs 推得"),
        ("6", "每分鐘訊息數上限（洗版）",
         "UsageService._check_rate → AccountLockService.register_strike（15 分鐘內 10 次 → 鎖定帳號，只有管理員能解除）",
         "W lock_strikes；達門檻 W users.locked_at、admin_audit_logs"),
        ("7", "回應與渲染",
         "MessageView.to_dict（只有文字與建議動作）→ 前端以純文字渲染（嚴禁 v-html）→ typewriter.js → 建議動作 chips，點擊後以其 text 作為使用者訊息送出",
         "—"),
        ("8", "管理員旁路：意見回饋",
         "intent 為 complaint_feedback 或 human_handoff 的對話，管理員可在後台讀取：AdminService → AdminRepository，先寫稽核再回傳全文",
         "R chat_messages、chat_conversations；W admin_audit_logs（conversation.read）"),
        ("9", "規劃：外部資料（尚未實作）",
         "ExternalDataProvider → MCP 轉接器 → 白名單來源（RC-6）；由 Service 發動、短逾時、可降級；客服 v1 不啟用",
         "規劃：以標籤為鍵的快取表；目前沒有對應的資料表或程式"),
    ]
    end_y = mapping_table(svg, panel_bottom + 36, title="功能對應：週期階段 ↔ 程式函式 ↔ 資料存取",
                          headers=("階段與動作", "程式模組與函式", "資料表存取（R 讀、W 寫）", "階段"), rows=rows_data, widths=(370, 800, 470))
    svg.text(40, end_y + 24, "依據：docs/客服對話-chat-service.md、docs/模型呼叫層-llm-layer.md、docs/RPG.md R-16、AGENTS.md 第 10 節。", size=14, fill=P["muted"])
    svg.height = int(end_y + 50)
    print(f"MCP 圖：架構區高度 {panel_bottom:.0f}／全圖 {svg.height}（架構區占 {panel_bottom / svg.height:.3f}）")
    return svg


def main(argv: list[str]) -> int:
    which = argv[1] if len(argv) > 1 else "all"
    if which in ("all", "login"):
        render_png(login_diagram(), LOGIN_PNG)
    if which in ("all", "mcp"):
        render_png(mcp_diagram(), MCP_PNG)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
