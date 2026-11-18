"""產生四張系統層級的 Azure 圖示架構圖解（4K PNG），補足登入與 MCP 兩張之外的重要架構：

    docs/網站入口架構圖解.png        網址 → DNS 與憑證 → 入口層 → 兩個 worker 池 → Flask 請求管線 → 頁面與導向
    docs/RPG回合引擎架構圖解.png     一輪 RPG：玩家行動 → 預扣遊戲幣 → SSE 串流與逐句審查 → 狀態驗證與結算 → 記事簿
    docs/內容安全與審查架構圖解.png  年齡閘門、內容模式、輸入規則層、創作審查、輸出審查與被擋回合
    docs/部署與營運架構圖解.png      映像、遷移、兩個池、AWS 展示環境、排程、告警與 Google Cloud 的 production 規劃

用法（專案根目錄）：  python -X utf8 docs/diagrams/build_diagrams_system.py [entry|rpg|safety|deploy|all]
版面元件在 diagram_common.py，與登入、MCP 兩張圖（build_diagrams.py）共用。圖中的函式、端點與資料表名稱取自程式碼與設計文件，
架構改變時請同步修改本檔。Azure 圖示的使用條款見 docs/diagrams/README.md。
"""
from __future__ import annotations

import sys

from diagram_common import W, band, header, mapping_table, render_png
from render_tools import DOCS_DIR, PALETTE
from svg_kit import Svg

P = PALETTE

ENTRY_PNG = DOCS_DIR / "網站入口架構圖解.png"
RPG_PNG = DOCS_DIR / "RPG回合引擎架構圖解.png"
SAFETY_PNG = DOCS_DIR / "內容安全與審查架構圖解.png"
DEPLOY_PNG = DOCS_DIR / "部署與營運架構圖解.png"


# ---- 小工具 -----------------------------------------------------------------------------------


def lane(svg: Svg, x: float, w: float, top: float, bottom: float, tag: str, title: str) -> None:
    """欄位外框與標題（不填色，所以內容畫完才補也不會蓋住卡片）。"""
    svg.rect(x, top, w, bottom - top, fill="none", stroke=P["line"], rx=14)
    svg.text(x + 16, top + 24, tag, size=12, fill=P["accent"], mono=True)
    svg.text(x + 16, top + 50, title, size=21, weight=700)


def stack(svg: Svg, x: float, y: float, w: float, specs: list[dict], *, gap: float = 12) -> list[tuple[float, float]]:
    """由上往下堆疊卡片，回傳每張卡片的 (y, 高度)。"""
    spots: list[tuple[float, float]] = []
    for spec in specs:
        h = svg.card(x, y, w, **spec)
        spots.append((y, h))
        y += h + gap
    return spots


def bottom_of(spots: list[tuple[float, float]]) -> float:
    return spots[-1][0] + spots[-1][1] if spots else 0


def tall_band(svg: Svg, x: float, y: float, w: float, bands: list[tuple[str, list, bool]], *, size: float = 14, gap: float = 18) -> list[tuple[float, float]]:
    """把數個色帶由上往下疊在同一欄，之間畫向下的箭頭。bands 為 (標籤, 膠囊清單, 是否等寬字)。回傳每條色帶的 (上, 下)。"""
    spans: list[tuple[float, float]] = []
    for index, (label, chips, mono) in enumerate(bands):
        h = band(svg, x, y, w, label=label, chips=chips, size=size, mono=mono)
        spans.append((y, y + h))
        if index < len(bands) - 1:
            svg.arrow([(x + w / 2, y + h + 1), (x + w / 2, y + h + gap - 1)], color=P["muted"], sw=2)
        y += h + gap
    return spans


def note_box(svg: Svg, x: float, y: float, w: float, title: str, lines: list[str], *, size: int = 16) -> float:
    """左側有琥珀色線條的說明框，回傳高度。"""
    from svg_kit import wrap

    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(wrap(line, size, w - 150))
    height = 22 + len(wrapped) * round(size * 1.5) + 14
    svg.rect(x, y, w, height, fill=P["panel"], stroke=P["line"], rx=10)
    svg.rect(x, y, 6, height, fill=P["accent"], rx=3)
    svg.text(x + 24, y + 30, title, size=size + 1, fill=P["accent"], weight=700)
    svg.lines(x + 130, y + 30, wrapped, size=size, lh=round(size * 1.5))
    return height


# ---- 網站入口 ---------------------------------------------------------------------------------


def entry_diagram() -> Svg:
    svg = Svg(W, 100)
    header(svg, tag="[ SITE_ENTRY ]", title="網站入口架構圖解",
           subtitle="一個網址如何走到一個頁面：DNS 與憑證 → 入口層 → 兩個 worker 池 → Flask 請求管線 → 頁面、API 與導向（展示環境為 AWS 雪梨；production 規劃為 Google Cloud）")
    top = 140
    ax, aw = 30, 225
    bx, bw = 285, 285
    cx, cw = 655, 280
    dx, dw = 985, 400
    ex, ew = 1435, 335
    y0 = top + 56

    # -- A 使用者端
    a = stack(svg, ax + 10, y0, aw - 20, [
        dict(icon="browser", azure="Browser", title="瀏覽器", badges=(1,), body_size=14, body=[
            "輸入 https://rac001.duckdns.org（展示環境）", "HTTP 80 一律 308 導向 HTTPS", "只載入本機資產：static/vendor、static/fonts，沒有 CDN",
            "頁面 meta csrf-token → axios 帶 X-CSRF-Token", "RPG 劇情以原生 fetch＋ReadableStream 接收 SSE"]),
        dict(icon="browser", azure="Browser", title="本機暫存", badges=(1,), body_size=14, body=[
            "IndexedDB 暫存客服對話與 RPG 世界線，連線時背景同步", "同步衝突以後端為準：客服用 since_seq、RPG 用 version", "localStorage 只存介面偏好，讀寫包 try/catch"]),
        dict(icon="users", azure="Users", title="維運者", badges=(9,), body_size=14, body=[
            "AWS Systems Manager Session Manager", "主機沒有 SSH，不開 22", "只在自己的終端機建立管理員、部署與查詢"]),
    ])
    a_bottom = bottom_of(a)

    # -- B 網路與入口層
    b = stack(svg, bx + 10, y0, bw - 20, [
        dict(icon="api-connections", azure="API Connections", title="DNS 與固定位址", badges=(2,), body_size=14, body=[
            "DuckDNS 網域 rac001.duckdns.org → Elastic IP", "安全群組只開 TCP 80 與 443", "8081、8082、1433、22 從外部連不上（逾時被丟棄）"]),
        dict(icon="front-door", azure="Front Door and CDN Profiles", title="入口：Caddy（rac-caddy）", badges=(3,), body_size=14, body=[
            "憑證由 Caddy 自動簽發與管理（Let's Encrypt），存在 /var/lib/rac/caddy", "HTTPS 與 HTTP/2；80 導向 308",
            "/api/game/* → RPG 串流池：flush_interval -1，不壓縮、不緩衝", "其餘所有路徑 → 客服池", "不改寫標頭：HSTS 與安全標頭由應用程式送"]),
        dict(icon="conditional-access", azure="Conditional Access", title="production 規劃（尚未建立）", dashed=True, body_size=13, body=[
            "Google Cloud：Cloud Run 當入口層（HTTPS、HTTP/2）", "Secret Manager 注入機密、Cloud SQL for SQL Server",
            "入口層 IP 限流、路徑分流是上線前關卡；沒有可計費的專案，目前未建立"]),
    ])
    b_bottom = bottom_of(b)

    # -- C 兩個 worker 池
    c = stack(svg, cx + 10, y0, cw - 20, [
        dict(icon="app-services", azure="App Services", title="客服池 rac-cs", badges=(4,), body_size=14, body=[
            "127.0.0.1:8081，只發布在回送位址", "gunicorn.conf.py：gthread、2 worker × 8 執行緒、timeout 60 秒",
            "處理頁面、/api/auth、/api/cs、/api/admin、/api/account、/api/scripts 等所有非 /api/game/ 路徑", "非 root（uid 10001）、唯讀根檔案系統、cap-drop ALL"]),
        dict(icon="app-services", azure="App Services", title="RPG 串流池 rac-rpg", badges=(4,), body_size=14, body=[
            "127.0.0.1:8082，只發布在回送位址", "gunicorn.rpg.conf.py：gthread、2 worker × 16 執行緒、timeout 180 秒",
            "每個進行中的串流佔一條請求執行緒（外加一條上游讀取執行緒）", "SSE 心跳每 15 秒；客戶端斷線即停止生成、關閉上游"]),
        dict(icon="scheduler", azure="Scheduler", title="排程（一次性容器）", badges=(9,), body_size=14, body=[
            "rac-job-*.timer × 5，時區 Asia/Taipei", "check-alerts 每 5 分鐘；purge-unconfirmed-accounts 每小時", "purge-inactive-accounts、execute-account-deletions、purge-expired-data 每日"]),
    ])
    c_bottom = bottom_of(c)

    # -- D Flask 請求管線
    app_top = top
    svg.rect(dx, app_top, dw, 100, fill="#14161A", stroke=P["accent"], rx=14, sw=1.5)
    app_rect_index = len(svg.parts) - 1
    svg.text(dx + 16, app_top + 24, "[ FLASK APP ]", size=12, fill=P["accent"], mono=True)
    svg.icon("app-services", dx + 16, app_top + 32, 40)
    svg.text(dx + 66, app_top + 56, "Flask 應用程式（create_app）", size=21, weight=700)
    svg.text(dx + 66, app_top + 76, "Azure 圖示：App Services　｜　多 worker 不依賴行程內狀態", size=12, fill=P["muted"])
    spans = tall_band(svg, dx + 16, app_top + 96, dw - 32, [
        ("REQUEST PIPELINE ‧ 依註冊順序（after_request 相反）", [
            ("ProxyFix：信任轉送標頭", (5,)), ("request_logging：request_id", (5,)), ("session：滑動續期 cookie", (5,)),
            ("csrf：Token＋Origin", (5,)), ("no_store", (5,)), ("security_headers：HSTS 等", (5,))], False),
        ("ROUTES ‧ 18 個 Blueprint（development 另有 dev）", [
            ("頁面：只回 HTML 外殼", (6,)), ("API：只回 JSON", (6,)), ("SSE：/api/game/*", (6,)), ("/healthz・/readyz", (6,))], False),
        ("ACCESS ‧ 誰可以進來", [
            ("login_required_page", (7,)), ("CurrentUser.restriction", (7,)),
            ("AccessPolicy：年齡、擁有者、管理員", (7,)), ("landing_for／safe_next", (7,))], False),
        ("SERVICE → REPOSITORY", [("Service 業務邏輯", (8,)), ("Repository：擁有者條件", (8,)), ("SQLAlchemy ORM", (8,))], False),
    ])
    d_bottom = spans[-1][1] + 14
    svg.parts[app_rect_index] = (
        f'<rect x="{dx:.1f}" y="{app_top:.1f}" width="{dw:.1f}" height="{d_bottom - app_top:.1f}" rx="14" fill="#14161A" stroke="{P["accent"]}" stroke-width="1.5"/>'
    )

    # -- E 資料與外部服務
    e = stack(svg, ex, top, ew, [
        dict(icon="sql-database", azure="SQL Database", title="資料庫", badges=(8,), body_size=14, body=[
            "展示環境：RDS for SQL Server Express（不公開、加密、只允許主機連入）", "開發與驗收：SQLite 或本機 SQL Server；程式碼只透過 ORM，沒有 Raw SQL"]),
        dict(icon="key-vaults", azure="Key Vaults", title="機密注入", badges=(9,), body_size=14, body=[
            "SSM Parameter Store（SecureString）→ 啟動前取出，寫到 tmpfs /run/rac（0600）", "production 規劃：Secret Manager；機密不進映像、日誌與前端"]),
        dict(icon="cognitive-services", azure="Cognitive Services", title="模型供應商（外部）", badges=(8,), body_size=14, body=[
            "客服：Gemini，非串流結構化 JSON", "RPG：OpenAI 相容端點（目前 OpenRouter），SSE 串流", "名稱取自環境變數，備援模型自動切換"]),
        dict(icon="external-identities", azure="External Identities", title="Google OIDC", badges=(8,), body_size=14, body=[
            "accounts.google.com：授權、token、JWKS", "玩家唯一的身分來源（見登入與註冊架構圖解）"]),
        dict(icon="log-analytics", azure="Log Analytics Workspaces", title="結構化 JSON 日誌", badges=(5,), body_size=14, body=[
            "標準輸出；只記事件、內部編號與整數", "不記 email、cookie、授權碼、生日與玩家文字"]),
    ])
    e_bottom = bottom_of(e)

    # -- 箭頭
    mid_y = a[0][0] + 44
    svg.arrow([(ax + aw, mid_y), (bx + 10, mid_y)], label="HTTPS", label_at=((ax + aw + bx + 10) / 2, mid_y - 10), both=True, color=P["text"], label_size=12)
    svg.arrow([(bx + bw / 2, b[0][0] + b[0][1] + 1), (bx + bw / 2, b[1][0] - 1)], color=P["text"], sw=2)
    x1 = bx + bw - 10
    y_cs, y_rpg = c[0][0] + 60, c[1][0] + 60
    svg.arrow([(x1, b[1][0] + 70), (x1 + 34, b[1][0] + 70), (x1 + 34, y_cs), (cx + 10, y_cs)], color=P["muted"], label="其餘路徑", label_at=(x1 + 66, y_cs - 8), label_size=12)
    svg.arrow([(x1, b[1][0] + 120), (x1 + 54, b[1][0] + 120), (x1 + 54, y_rpg), (cx + 10, y_rpg)], color=P["accent"], label="/api/game/*", label_at=(x1 + 88, y_rpg - 8), label_size=12)
    gap_x = (cx + cw + dx) / 2
    svg.arrow([(cx + cw - 10, c[0][0] + 30), (dx, c[0][0] + 30)], color=P["text"], label="WSGI", label_at=(gap_x, c[0][0] + 22), label_size=12)
    svg.arrow([(cx + cw - 10, c[1][0] + 30), (dx, c[1][0] + 30)], color=P["accent"], label="WSGI", label_at=(gap_x, c[1][0] + 22), label_size=12)
    svg.arrow([(cx + cw - 10, c[2][0] + 30), (dx, c[2][0] + 30)], color=P["muted"], dash="6 5", label="指令", label_at=(gap_x, c[2][0] + 22), label_size=12)
    gap_e = (dx + dw + ex) / 2
    for index, (label, color, dash, both, reverse) in enumerate([
        ("ORM", P["accent"], None, True, False), ("機密", P["muted"], "6 5", False, True), ("模型", P["muted"], "6 5", False, False),
        ("OIDC", P["accent"], None, True, False), ("日誌", P["muted"], "6 5", False, False),
    ]):
        ey = e[index][0] + 30
        points = [(ex, ey), (dx + dw, ey)] if reverse else [(dx + dw, ey), (ex, ey)]
        svg.arrow(points, color=color, dash=dash, both=both, label=label, label_at=(gap_e, ey - 8), label_size=12)

    panel_bottom = max(a_bottom, b_bottom, c_bottom, d_bottom, e_bottom) + 4
    for lane_x, lane_w, tag, title in ((ax, aw, "[ CLIENT ]", "使用者端"), (bx, bw, "[ EDGE ]", "網路與入口層"), (cx, cw, "[ POOLS ]", "兩個 worker 池")):
        lane(svg, lane_x, lane_w, top, panel_bottom, tag, title)

    # -- 頁面與導向（全寬）
    y = panel_bottom + 34
    svg.text(40, y, "入口頁面與導向決策", size=26, weight=700)
    y += 16
    gw = (W - 60 - 2 * 20) / 3
    groups = [
        ("ANONYMOUS ‧ 匿名可進", [("GET /（已登入者導向 landing_for）", ()), ("/login", ()), ("/auth/google/callback", ()), ("/terms", ()), ("/privacy", ()), ("/healthz", ()), ("/readyz", ()),
                                  ("/favicon.ico（204）", ()), ("/register → /login（舊書籤）", ()), ("/reset-password（管理員重設信的連結）", ())]),
        ("SIGNED IN ‧ login_required_page", [("/game（RPG 入口，DEFAULT_AFTER_LOGIN）", ()), ("/game/start/<version>", ()), ("/game/<session>", ()), ("/scripts（劇本工作坊）", ()),
                                           ("/cs（客服）", ()), ("/account", ()), ("/admin（非管理員回 404）", ())]),
        ("RESTRICTED ‧ 受限登入狀態（預設拒絕）", [("待確認年齡 → /age", ()), ("被鎖定或暫停發佈 → /appeal", ()), ("等待刪除 → /account", ()),
                                                  ("其餘頁面一律導向上述頁面", ()), ("API 一律 403", ()), ("age_confirmation_required", ()), ("account_locked", ()), ("account_deletion_pending", ())]),
    ]
    heights = []
    for index, (label, chips) in enumerate(groups):
        heights.append(band(Svg(W, 10), 30 + index * (gw + 20), y, gw, label=label, chips=chips, size=14, mono=True))
    group_h = max(heights)
    for index, (label, chips) in enumerate(groups):
        gx = 30 + index * (gw + 20)
        svg.rect(gx, y, gw, group_h, fill=P["panel_alt"], stroke=P["line"], rx=10)
        svg.text(gx + 14, y + 22, label, size=12, fill=P["accent"], mono=True)
        svg.chips(gx + 14, y + 36, gw - 28, chips, size=14, mono=True)
    y += group_h + 22

    c1 = svg.card(30, y, 330, icon="browser", azure="Browser", title="GET /　或　Google 回呼", badges=(7,), body_size=14,
                  body=["已登入者打開首頁或登入頁，直接導向；登入完成（回呼）也走同一個函式"])
    c2 = svg.card(410, y, 360, icon="app-registrations", azure="App Registrations", title="current_user()", badges=(7,), body_size=14,
                  body=["讀簽章 session cookie（uid、tv）→ resolve_session：比對 users 的 token_version 與狀態 → CurrentUser.restriction"])
    c3 = svg.card(820, y, 360, icon="conditional-access", azure="Conditional Access", title="landing_for(user, next)", badges=(7,), body_size=14,
                  body=["受限狀態各去自己的頁面，不依 next；其餘取 safe_next(next)，不合法就退回 /game（只擋站外與控制字元）"])
    flow_h = max(c1, c2, c3)
    svg.arrow([(360, y + flow_h / 2), (410, y + flow_h / 2)], color=P["accent"])
    svg.arrow([(770, y + flow_h / 2), (820, y + flow_h / 2)], color=P["accent"])
    outs = [
        ("匿名", "首頁 home.html，只有進入登入頁的入口"),
        ("待確認年齡", "/age?next=…，補填成功才回原處"),
        ("被鎖定／等待刪除", "/appeal 或 /account，不依 next"),
        ("一般玩家", "/game：選劇本與我的存檔；客服 /cs 由導覽列進入"),
    ]
    ox, ow = 1230, W - 30 - 1230
    row_h = (flow_h - 3 * 6) / 4
    for index, (name, text) in enumerate(outs):
        oy = y + index * (row_h + 6)
        svg.rect(ox, oy, ow, row_h, fill=P["panel"], stroke=P["line"], rx=8)
        svg.text(ox + 10, oy + row_h / 2 + 5, name, size=13, fill=P["accent"], weight=700)
        svg.text(ox + 118, oy + row_h / 2 + 5, text, size=12, fill=P["text"])
        svg.arrow([(1180, y + flow_h / 2), (ox, oy + row_h / 2)], color=P["muted"], sw=1.6)
    panel2_bottom = y + flow_h

    rows = [
        ("1", "瀏覽器輸入網址、載入頁面與資產",
         "https://rac001.duckdns.org/（展示環境）；HTML 模板以 url_for('static', …) 引入本機資產，不使用 CDN 與外部字型；頁面 meta 帶 csrf-token",
         "—（不碰資料庫）"),
        ("2", "DNS 與網路邊界",
         "DuckDNS 網域指向 Elastic IP；EC2 安全群組只開 TCP 80、443；主機沒有 SSH（scripts/aws_demo_plan.py 的 network 組）",
         "—"),
        ("3", "入口層：憑證、HTTPS、導向與路徑分流",
         "rac-caddy.service（caddyfile()、caddy_unit()）：自動簽發憑證、HTTP/2、80 → 308；/api/game/* → 127.0.0.1:8082（flush_interval -1、不壓縮），其餘 → 127.0.0.1:8081",
         "—"),
        ("4", "兩個 gunicorn（gthread）池",
         "客服池 gunicorn.conf.py（2 × 8 執行緒、timeout 60 秒）；RPG 池 gunicorn.rpg.conf.py（2 × 16 執行緒、timeout 180 秒）；非 root、--read-only、--cap-drop=ALL；正式環境不得使用 app.run()",
         "—"),
        ("5", "Flask 請求管線",
         "create_app：ProxyFix（x_for、x_proto、x_host 各 1，只在 staging、production 或明確設定 TRUST_PROXY_HEADERS）→ init_request_logging → init_session → init_csrf → init_no_store → init_security_headers（HSTS 只在 staging、production）",
         "session 只含 uid 與 tv；日誌不含個資"),
        ("6", "路由：頁面、API 與串流",
         "頁面 Controller 只回 HTML 外殼與初始狀態；API Controller 只回 JSON；RPG 串流端點回 text/event-stream；/healthz 只表示行程存活，/readyz 會確認資料庫，兩者不回內部細節",
         "/readyz：讀資料庫連線（HealthRepository）"),
        ("7", "誰可以進來：身分辨識、受限狀態與導向",
         "current_user → AuthService.resolve_session；login_required_page／require_login；CurrentUser.restriction → RESTRICTION_PAGES；landing_for、safe_next、DEFAULT_AFTER_LOGIN＝/game",
         "R users（token_version、status、locked_at、age 與刪除狀態）"),
        ("8", "Service → Repository → 外部服務",
         "AccessPolicy 集中授權（非擁有者一律 404）；Repository 唯一碰 ORM；模型呼叫經 LlmService；Google 登入經 GoogleOidcClient",
         "依功能而定：見登入與註冊、MCP、RPG 回合引擎三張圖"),
        ("9", "機密、排程與維運",
         "SSM → 啟動前取出到 /run/rac（rac-render-env-*）；五個 rac-job-*.timer 觸發 flask 指令；維運者只經 SSM Session Manager 進入主機",
         "排程指令寫入 deletion_tombstones、alert_states 等（見部署與營運架構圖解）"),
    ]
    end_y = mapping_table(svg, panel2_bottom + 36, title="功能對應：入口步驟 ↔ 程式與設定 ↔ 資料存取",
                          headers=("流程動作", "程式模組、函式與設定", "資料存取", "步驟"), rows=rows, widths=(380, 860, 400))
    svg.text(40, end_y + 24, "依據：docs/營運與部署-operations.md 第 2、8 節、docs/系統架構-architecture.md、app/__init__.py、app/security/redirects.py。展示環境不是 production（單機單點）。", size=14, fill=P["muted"])
    svg.height = int(end_y + 50)
    print(f"入口圖：架構區高度 {panel_bottom:.0f}、導向決策 {panel_bottom + 20:.0f} 至 {panel2_bottom + 8:.0f}／全圖 {svg.height}")
    return svg


# ---- RPG 回合引擎 -----------------------------------------------------------------------------


def columns_layout(n: int = 6, gap: int = 34) -> tuple[float, list[float]]:
    cw = (W - 60 - gap * (n - 1)) / n
    return cw, [30 + i * (cw + gap) for i in range(n)]


def rpg_diagram() -> Svg:
    svg = Svg(W, 100)
    header(svg, tag="[ RPG_TURN_ENGINE ]", title="RPG 回合引擎架構圖解",
           subtitle="一輪＝一則玩家訊息＋一則完整的 AI 回應（含狀態呼叫與記事簿抓取），消耗 30 遊戲幣；沒有成功生成（失敗、中斷、被擋）不扣，重送與自動重生不再次扣")

    by = 138
    svg.rect(30, by, W - 60, 70, fill=P["panel"], stroke=P["line"], rx=10)
    svg.rect(30, by, 6, 70, fill=P["accent"], rx=3)
    svg.text(54, by + 28, "現況聲明", size=17, fill=P["accent"], weight=700)
    svg.text(140, by + 28, "全部已實作並封存：rpg-turn-engine、rpg-notebook、rpg-turn-revision、game-wallet、rpg-character-creation、rpg-game-ui；劇情以 SSE 串流。", size=17)
    svg.text(140, by + 54, "回合分兩個階段：請求階段（GameService，請求的資料庫工作階段內）與產生器階段（TurnRunner，after_request 之後、自己管理短交易）；串流與狀態呼叫期間沒有任何資料庫交易。", size=17, fill=P["muted"])

    n = 6
    cw, xs = columns_layout(n)
    top = 282
    columns = [
        dict(icon="browser", azure="Browser", title="/game 畫面", badges=(1,), body=[
            "game/*.js：entry、wizard、turn-stream、game-sync", "turn-stream.js：原生 fetch＋ReadableStream；AbortController 逾時；收到任何位元組就重設閒置計時器",
            "打字機緩衝區逐字渲染；被擋時丟棄未完成內容，改顯示一行轉場文字", "IndexedDB 暫存世界線；version 不一致時以後端為準",
            "事件：meta、token、ping、transition、state、done、error"]),
        dict(icon="api-management", azure="API Management Services", title="Web API Controller", badges=(2,), body=[
            "POST /api/game/sessions/<id>/turns/stream", "POST …/opening/stream（第 0 幕）", "POST …/turns/<client_msg_id>/regenerate／edit/stream",
            "GET …/<id>、…/turns；GET／PUT …/notebook", "標頭 text/event-stream、no-cache、X-Accel-Buffering: no",
            "先確認有 RPG 供應商（沒有就在預扣前回 503）", "已完成的重送由 rpg_replay 回放，不碰錢包", "不做權限判斷、不匯入資料層"]),
        dict(icon="bot-services", azure="Bot Services", title="GameService（請求階段）", badges=(3,), body=[
            "begin_turn：閘門（AccessPolicy）→ client_msg_id 與行動驗證 → 擁有者（非擁有者 404）", "已完成的重送（TurnReplay）；完結與第 0 幕閘門",
            "行動的規則層：只擋確定違規，422 input_blocked，預扣之前就拒絕", "全站每日 token 預算（503 system_busy）",
            "SAVEPOINT：start_turn＋wallet.hold 30 幣（餘額不足 409 insufficient_coins，整段復原）", "凍結 TurnPlan：記事簿＋最近 10 輪＋世界狀態（含在場角色的角色卡切片）"]),
        dict(icon="function-apps", azure="Function Apps", title="TurnRunner（串流與審查）", badges=(4,), body=[
            "StreamRun＋OutputGuard：逐句審查，保留一句緩衝，通過才釋放", "ping 心跳每 15 秒；客戶端斷線 → 停止生成並關閉上游",
            "被擋：transition，同一筆預扣自動重生一次；仍被擋 → blocked 並退回預扣", "標記字串洩漏視為驗證失敗；模型拒答視為生成失敗，不繞過",
            "主要模型 429、5xx、單次逾時 20 秒 → 切備援（不切到 Gemini）"]),
        dict(icon="language-understanding", azure="Language Understanding", title="狀態驗證與結算", badges=(5, 6, 8), body=[
            "收尾不 yield：正常與斷線路徑共用", "狀態呼叫 LlmService.run_structured（rpg_state）：模型只提議", "apply_proposal：Service 驗證 NPC 弧線階段、年齡、里程碑（依年齡或年份）與結局（須是自訂結局的劇本）",
            "finish_turn＋settle：完成回合、寫世界狀態（版本比對）與結算預扣，同一個短交易", "失敗、被擋、中斷：退回預扣；狀態呼叫失敗不影響文字與結算",
            "回應關閉後：NotebookCapturer（每完成 10 輪）"]),
        dict(icon="sql-database", azure="SQL Database", title="SQL Server project", badges=(), body=[
            "SessionRepository：條件式 UPDATE（attempt、revision、版本），不信任先讀後寫", "WalletRepository：預扣、結算、退回；冪等鍵＝存檔、client_msg_id、attempt", "LlmCallLogRepository：用量與全站 token 的唯一來源"]),
    ]
    tables = [("memory_logs", (3, 4, 5, 6)), ("sessions", (3, 5, 8)), ("wallets", (3, 5)), ("wallet_entries", (3, 5, 6)), ("turn_regenerations", ()), ("llm_call_logs", (3, 4, 5, 8))]

    def draw_column(target: Svg, index: int, min_h: float = 0) -> float:
        spec = columns[index]
        return target.card(xs[index], top, cw, icon=spec["icon"], azure=spec["azure"], title=spec["title"], body=spec["body"], badges=spec["badges"],
                           body_size=14, title_size=19, min_h=min_h)

    base_h = [draw_column(Svg(W, 10), i) for i in range(n)]
    tables_y = top + base_h[-1] + 8
    tables_bottom = Svg(W, 10).chips(xs[-1] + 14, tables_y, cw - 28, tables, size=13, mono=True)
    natural = base_h[:-1] + [tables_bottom - top + 14]
    natural[3] += 70                                                              # 串流欄底部留給模型供應商的迷你卡
    col_h = max(natural)
    for i in range(n):
        draw_column(svg, i, min_h=col_h)
    svg.chips(xs[-1] + 14, tables_y, cw - 28, tables, size=13, mono=True)
    cards_bottom = top + col_h

    gy = cards_bottom - 64
    svg.rect(xs[3] + 10, gy, cw - 20, 54, fill=P["panel_alt"], stroke=P["line"], rx=10)
    svg.icon("cognitive-services", xs[3] + 20, gy + 8, 36)
    svg.text(xs[3] + 64, gy + 26, "OpenAI 相容端點（外部）", size=14, weight=700)
    svg.text(xs[3] + 64, gy + 44, "OPENROUTER_MODEL 與備援", size=11, fill=P["muted"])
    svg.badge(xs[3] + cw - 24, gy + 16, "4", r=10, size=12)

    labels = ["POST turns/stream { client_msg_id, action }", "begin_turn(user, session, …)", "凍結的 TurnPlan → 串流", "文字完整 → 收尾", "短交易：回合、世界、預扣與結算"]
    pipe_y = top - 20
    for i in range(n - 1):
        x1 = xs[i] + cw / 2 + 50
        x2 = xs[i + 1] + cw / 2 - 50
        svg.arrow([(x1, top - 2), (x1, pipe_y), (x2, pipe_y), (x2, top - 2)], color=P["accent"], sw=2.2)
        svg.text((x1 + x2) / 2, pipe_y - 8, labels[i], size=13, fill=P["text"], anchor="middle", halo=True)

    ret_y = cards_bottom + 38
    x_from, x_to = xs[3] + cw / 2, xs[0] + cw / 2
    svg.arrow([(x_from, cards_bottom + 2), (x_from, ret_y), (x_to, ret_y), (x_to, cards_bottom + 2)], color=P["ok"], sw=2.4)
    svg.badge(x_to + 50, ret_y, "7", r=12, fill=P["ok"], size=14)
    svg.text((x_from + x_to) / 2 + 20, ret_y - 9, "SSE：meta → token（逐句審查後釋放）／ping → state → done（最後一個事件，帶 revision 與 free_left）", size=14, fill=P["text"], anchor="middle", halo=True)

    # 四個旁路與周邊能力
    py = ret_y + 52
    svg.text(40, py, "周邊能力：同一個回合引擎的另外四條路徑", size=24, weight=700)
    py += 18
    pw = (W - 60 - 3 * 20) / 4
    side = [
        dict(icon="function-apps", azure="Function Apps", title="修訂：重新生成與編輯最後一輪", body=[
            "GameService.begin_revision＋RevisionRunner；回合保持 complete，用認領欄位表示修訂中", "每回合前 2 次免費（兩者合計），第 3 次起 30 幣", "重新生成須選分類並填 5 至 50 字理由：只進 turn_regenerations，不進模型、日誌與暫存",
            "通過審查與狀態驗證才原子替換；失敗保留舊內容並退回預扣"]),
        dict(icon="bot-services", azure="Bot Services", title="第 0 幕（開場）", body=[
            "OpeningRunner：依主角角色卡與錨定事實產生，沿用串流與審查", "不是一輪：不計輪數、不扣幣，token 計入全站每日用量", "選定後存於存檔，重新整理或換裝置不重抽",
            "失敗或兩次被擋 → 退回劇本的初始對話；斷線 → 退回 pending"]),
        dict(icon="key-vaults", azure="Key Vaults", title="遊戲幣（錢包與帳本）", body=[
            "一輪 30 幣；先扣免費幣、再扣付費幣；餘額不足 409 insufficient_coins", "回合開始原子預扣、完成結算、失敗退回；卡住逾時由系統退回",
            "首次登入贈 200、每日簽到 200（台灣時間 0 點刷新）；免費幣上限 1000", "金額、上限存資料庫設定表，每次現讀，flask 指令修改並寫稽核"]),
        dict(icon="cognitive-search", azure="Cognitive Search", title="記事簿（長期記憶）", badges=(8,), body=[
            "每個存檔一本，不使用摘要；容量 free 2000、plus 5000、vip 10000 字，另有 25% 預備空間（只供 AI 寫入）", "每完成 10 輪，回應關閉之後由 NotebookCapturer 抓重點、只在末尾追加（總預算 60 秒）",
            "已滿則不呼叫模型；玩家可檢視與編輯（不得超量），純文字渲染", "notebook_upto_turn 標記進度，失敗由下一輪補做"]),
    ]
    heights = [Svg(W, 10).card(30 + i * (pw + 20), py, pw, icon=c["icon"], azure=c["azure"], title=c["title"], body=c["body"], body_size=14, title_size=19) for i, c in enumerate(side)]
    side_h = max(heights)
    for i, c in enumerate(side):
        svg.card(30 + i * (pw + 20), py, pw, icon=c["icon"], azure=c["azure"], title=c["title"], body=c["body"], body_size=14, title_size=19, min_h=side_h, badges=c.get("badges", ()))
    panel_bottom = py + side_h

    rows = [
        ("1", "玩家送出行動（或編輯、重新生成）",
         "turn-stream.js：fetch 串流、AbortController 逾時、任何位元組重設閒置計時器；打字機緩衝區渲染；finally 恢復按鈕與載入狀態；以 client_msg_id 冪等重送",
         "不碰 SQL Server（IndexedDB 只是快取，衝突時以後端為準）"),
        ("2", "API 入口",
         "game_api_controller.stream_turn → require_login、CSRF → 確認有 RPG 供應商 → GameService.begin_turn；回應 text/event-stream，事件產生器在 after_request 之後才執行",
         "—（Controller 不碰資料庫）"),
        ("3", "請求階段（交易 1）：閘門、輸入規則層、預算與預扣",
         "AccessPolicy → clean_action → _owned → TurnReplay → 規則層 → 全站預算 → SAVEPOINT：SessionRepository.start_turn、WalletService.hold；凍結 TurnPlan（rpg_context：記事簿、最近 10 輪、世界狀態）",
         "R sessions、memory_logs、llm_call_logs、wallets；W memory_logs（新回合列，狀態 streaming）、wallets、wallet_entries（預扣）"),
        ("4", "產生器階段：串流、審查、心跳與重生",
         "TurnRunner／RunnerBase → StreamRun＋OutputGuard（rpg_output_rules）→ OpenAICompatibleProvider；事件 token、ping、transition；被擋 save_blocked_text 後同一筆預扣自動重生一次，仍被擋 block_turn＋refund",
         "W llm_call_logs（串流帳本，緩衝後在短交易寫入）；被擋時 W memory_logs（被擋的部分內容，14 天後清除）"),
        ("5", "文字完整後的收尾：狀態呼叫、驗證與結算",
         "state_phase → LlmService.run_structured（rpg_state）→ apply_proposal（rpg_world）→ SessionRepository.finish_turn＋WalletService.settle，同一個短交易；done 事件之後串流結束",
         "W memory_logs（回合完成）、sessions（世界狀態 entities_json 等，版本加 1）、wallet_entries（結算）、llm_call_logs"),
        ("6", "失敗、中斷與卡住的回合",
         "GeneratorExit 時：文字未完成 → interrupted 並退回預扣；文字已完整 → 收尾照常完成，以相同 client_msg_id 重送取得既有結果；worker 重啟的回合逾時後被接手（attempt 加 1、新的預扣鍵）",
         "W memory_logs（interrupted、blocked）、wallet_entries（退回）；過時的產生器因 attempt 不符而寫回失敗"),
        ("7", "回應與渲染",
         "事件 state、done 回到前端；前端以純文字渲染（嚴禁 v-html）；done 帶 revision 與 free_left；GET …/turns 補齊與重播",
         "—"),
        ("8", "回應關閉之後：記事簿抓取（每完成 10 輪）",
         "response.call_on_close → TurnRunner.capture_target → NotebookCapturer.run：短交易 A 讀視窗 → rpg_notebook 抓取（無交易）→ 規則層與長度 → 短交易 B 鎖定後追加；不碰錢包、不計輪數、不拋出",
         "R memory_logs；R／W sessions（notebook_text、notebook_version、notebook_upto_turn）；W llm_call_logs"),
    ]
    end_y = mapping_table(svg, panel_bottom + 36, title="功能對應：回合階段 ↔ 程式函式 ↔ 資料存取",
                          headers=("階段與動作", "程式模組與函式", "資料表存取（R 讀、W 寫）", "階段"), rows=rows, widths=(370, 800, 470))
    svg.text(40, end_y + 24, "依據：docs/系統架構-architecture.md「RPG 回合引擎」「RPG 修訂」、docs/RPG.md、AGENTS.md 第 5、6、7 節。", size=14, fill=P["muted"])
    svg.height = int(end_y + 50)
    print(f"RPG 圖：架構區高度 {panel_bottom:.0f}／全圖 {svg.height}")
    return svg


# ---- 內容安全與審查 ---------------------------------------------------------------------------


def safety_diagram() -> Svg:
    svg = Svg(W, 100)
    header(svg, tag="[ CONTENT_SAFETY ]", title="內容安全與審查架構圖解",
           subtitle="全站只限滿 18 歲；內容安全由伺服器端的多道防線共同執行，不依賴模型自行判斷使用者的年齡，也不依賴模型拒絕")

    by = 138
    svg.rect(30, by, W - 60, 96, fill=P["panel"], stroke=P["line"], rx=10)
    svg.rect(30, by, 6, 96, fill=P["accent"], rx=3)
    svg.text(54, by + 28, "不變的底線", size=17, fill=P["accent"], weight=700)
    svg.text(170, by + 28, "無論哪一種模式，一律禁止：涉及未成年（含看似未成年）的性內容、性暴力、非自願的性情節，以及以真人名義的性內容。", size=17)
    svg.text(170, by + 54, "判定依年齡紀錄與文字的明確線索（年齡紀錄未滿 18 歲，或文字明確寫出未成年的年齡或身分）；沒有明確年齡的角色，露骨場景也不因此被擋。", size=17, fill=P["muted"])
    svg.text(170, by + 80, "規範（AGENTS.md，2026-11-17 修訂）：內容模式為「安全模式開（預設）／關」兩種；程式現況仍是 safe／standard／adult 三種，改為兩種與緊急煞車屬 safe-mode-toggle（尚未實作）。", size=17, fill=P["muted"])

    n = 6
    cw, xs = columns_layout(n)
    top = 310
    columns = [
        dict(icon="conditional-access", azure="Conditional Access", title="① 帳號閘門", badges=(1,), body=[
            "全站只限滿 18 歲：AgePolicy 最低註冊與使用年齡都是 18，年齡由 birth_date 於當下計算", "未滿 18 歲：立即抹除帳號與全部個資（erase_underage）；管理員只拒絕、不抹除",
            "AccessPolicy.chat_gate：age_confirmation_required、age_restricted（呼叫模型的功能）", "新帳號的內容模式偏好一律 safe，之後不自動調高"]),
        dict(icon="app-registrations", azure="App Registrations", title="② 生效的內容模式", badges=(2,), body=[
            "effective_tier ＝ 玩家偏好、年齡上限、劇本宣告、存檔記錄中最保守者（most_conservative）", "無法判定一律視為 safe，寧可少給",
            "劇本宣告的模式也參與：宣告 safe 的劇本一律以安全模式遊玩（規範的「綁定安全模式」與創作頁提示屬 safe-mode-toggle）", "現況的成人模式須部署開關、模型許可清單與玩家確認；規範的緊急煞車由 safe-mode-toggle 取代"]),
        dict(icon="function-apps", azure="Function Apps", title="③ 輸入規則層", badges=(3,), body=[
            "script_rules（詞表 app/data/review/lexicon.json），伺服器端、不呼叫模型", "玩家的行動：只擋確定違規，預扣與呼叫模型之前就拒絕（422 input_blocked，不消耗遊戲幣）",
            "劇本、主角、記事簿與對話人設：每次儲存或送出都過規則層", "拒絕只告知類別，不指出內容；性關係與性奴等禁斷關係不得作為錨定事實"]),
        dict(icon="cognitive-services", azure="Cognitive Services", title="④ 創作內容的模型層審查", badges=(4,), body=[
            "劇本：script_review_service，開存檔或送審且內容有變動時；pass、flag（進人工並標示類別）、reject", "主角：protagonist_review_service，開存檔時",
            "模型不可用、預算用盡、降級或被擋，一律不放行", "連續被自動拒絕達門檻 → 暫停發佈（獨立於帳號鎖定，只停止送審）"]),
        dict(icon="bot-services", azure="Bot Services", title="⑤ 輸出審查（逐句）", badges=(5,), body=[
            "OutputGuard：逐句檢查，保留一句緩衝，通過才釋放給玩家", "明確違規一律擋；安全模式開時，依安全模式界線不明確者也擋（取保守）",
            "跨句的未成年判定納入在場角色的年齡紀錄", "單獨出現的暴力或犯罪用語不擋，只記錄類別；不另設每回合的模型層審查"]),
        dict(icon="log-analytics", azure="Log Analytics Workspaces", title="⑥ 被擋之後", badges=(6,), body=[
            "RPG：前端丟棄未完成內容，以一行轉場文字取代；回合標記為被擋、不進上下文", "自動重新生成一次（不另扣幣）；仍被擋再顯示轉場、退回預扣並解鎖輸入",
            "客服：同一使用者一日內被內容安全過濾 3 次 → 暫停對話 1 小時（只計玩家輸入被擋）", "日誌只記事件與類別，不記任何內容"]),
    ]

    def draw_column(target: Svg, index: int, min_h: float = 0) -> float:
        spec = columns[index]
        return target.card(xs[index], top, cw, icon=spec["icon"], azure=spec["azure"], title=spec["title"], body=spec["body"], badges=spec["badges"],
                           body_size=14, title_size=19, min_h=min_h)

    col_h = max(draw_column(Svg(W, 10), i) for i in range(n))
    for i in range(n):
        draw_column(svg, i, min_h=col_h)
    cards_bottom = top + col_h

    labels = ["年齡確認通過", "取較保守者", "通過才送審或組上下文", "通過才呼叫模型", "逐句釋放或擋下"]
    pipe_y = top - 20
    for i in range(n - 1):
        x1 = xs[i] + cw / 2 + 50
        x2 = xs[i + 1] + cw / 2 - 50
        svg.arrow([(x1, top - 2), (x1, pipe_y), (x2, pipe_y), (x2, top - 2)], color=P["accent"], sw=2.2)
        svg.text((x1 + x2) / 2, pipe_y - 8, labels[i], size=13, fill=P["text"], anchor="middle", halo=True)

    # 帳號層的後果與稽核
    ay = cards_bottom + 34
    svg.text(40, ay, "帳號層的後果：鎖定、暫停發佈、申訴與稽核", size=24, weight=700)
    ay += 18
    aw3 = (W - 60 - 2 * 20) / 3
    side = [
        dict(icon="conditional-access", azure="Conditional Access", title="帳號鎖定", badges=(6,), body=[
            "15 分鐘內 Google 登入被拒 5 次，或被每分鐘訊息數上限擋下 10 次 → 自動鎖定", "只有管理員能解除；被鎖定者仍可登入，但只能進申訴頁（其他一律 403 account_locked）",
            "申訴：單則上限 1000 字、每小時最多 3 則、同時最多 1 則待處理"]),
        dict(icon="key-vaults", azure="Key Vaults", title="暫停發佈與劇本審核", badges=(4,), body=[
            "連續被自動拒絕達門檻 → 暫停發佈（只停止送審，其餘功能照常）；暫停者可使用申訴頁", "解除須管理員重新輸入密碼並寫入稽核", "公開劇本先審後發：每個送審公開的劇本須管理員核可才上架（RC-8，規劃中）"]),
        dict(icon="app-services", azure="App Services", title="管理員的可見範圍與稽核", badges=(6,), body=[
            "預設只能看統計、使用者主動提交的意見回饋、被鎖定或暫停者的申訴、被內容安全過濾標記的對話、送審或被檢舉的劇本", "不得任意讀取其他對話全文；每次讀取都寫入 admin_audit_logs",
            "停權、刪除內容、調整等級與遊戲幣等高風險操作須重新輸入密碼"]),
    ]
    heights = [Svg(W, 10).card(30 + i * (aw3 + 20), ay, aw3, icon=c["icon"], azure=c["azure"], title=c["title"], body=c["body"], body_size=14, title_size=19) for i, c in enumerate(side)]
    side_h = max(heights)
    for i, c in enumerate(side):
        svg.card(30 + i * (aw3 + 20), ay, aw3, icon=c["icon"], azure=c["azure"], title=c["title"], body=c["body"], body_size=14, title_size=19, min_h=side_h, badges=c["badges"])
    panel_bottom = ay + side_h

    matrix = [
        ("A", "玩家的 RPG 行動（每一輪）", "每次送出：規則層只擋確定違規，在預扣與呼叫模型之前拒絕", "不另設模型層；劇情輸出由 OutputGuard 逐句審查後才釋放"),
        ("B", "劇本設定（含狀態轉移規則、結局說明、初始對話）", "每次儲存：未成年的性內容、性暴力、非自願與真人名義的性內容直接拒絕，有疑慮者進人工", "開存檔或送出公開且內容有變動時：模型層審查；未通過者不得用來開存檔"),
        ("C", "玩家主角（角色卡、自我介紹、錨定事實）", "每次生成與送出：同劇本設定的規則層；錨定事實不得為性的關係或禁斷關係", "開存檔時：模型層；未通過者不得開存檔；自我介紹不寫入資料庫"),
        ("D", "記事簿與對話人設", "玩家儲存時過規則層（只擋必須拒絕的類別，不呼叫模型）；AI 抓取的條目只用規則層", "不設模型層；內容一律純文字渲染"),
        ("E", "客服對話", "輸入清理（NFC、去控制字元、1000 字）；system_instruction 只放固定指令，使用者文字不得串接進指令", "Gemini 內容安全過濾被擋：降級文案、不重試；一日內 3 次暫停 1 小時"),
    ]
    y = mapping_table(svg, panel_bottom + 36, title="審查矩陣：哪一種內容在哪個時機被檢查",
                      headers=("內容", "規則層（伺服器端，不呼叫模型）", "模型層與輸出審查", "代號"), rows=matrix, widths=(370, 760, 510))

    rows = [
        ("1", "帳號閘門：年齡與抹除",
         "AgePolicy（app/services/age_policy.py）→ AuthService.verify_age／_age_step_error → AccountErasureService.erase_underage；AccessPolicy.chat_gate、require_chat_access",
         "R users（birth_date 於當下計算年齡）；未滿 18 歲 D users（CASCADE）＋W deletion_tombstones"),
        ("2", "生效的內容模式",
         "AccessPolicy.content_access、effective_tier；content_tier.most_conservative、parse_tier；ContentModeService；GET／PUT /api/account/content-mode",
         "R／W users.content_tier_preference；R sessions.content_tier（開存檔當下的生效模式，只能往保守調）、script_versions.content_tier（作者宣告）"),
        ("3", "輸入規則層",
         "script_rules.check_rules、check_text_rules、check_protagonist_rules（詞表載入 load_lexicon）；rpg_output_rules.evaluate；GameService.begin_turn 的行動檢查",
         "不碰資料庫；被拒的送審只記類別與時間（script_submissions，保存 12 個月）"),
        ("4", "創作內容的模型層審查",
         "script_review_service、script_review_prompt；protagonist_review_service；LlmService.run_structured；script_submission_service（送審次數與暫停發佈）",
         "W llm_call_logs；W script_submissions；暫停發佈寫 users 與 admin_audit_logs"),
        ("5", "輸出審查（逐句）",
         "rpg_output_guard.OutputGuard、rpg_output_rules（標記字串洩漏、性內容、未成年線索）；StreamRun 在 TurnRunner 內逐句呼叫",
         "不碰資料庫；命中時 W memory_logs（被擋的部分內容，14 天後清除）"),
        ("6", "被擋之後與帳號層後果",
         "TurnRunner：save_blocked_text、自動重生、block_turn＋refund；UsageService（客服暫停）；AccountLockService.register_strike；AppealService；AdminService（讀取前先寫稽核）",
         "W memory_logs、wallet_entries（退回）；W lock_strikes、users（locked_at）、lock_appeals、admin_audit_logs"),
    ]
    end_y = mapping_table(svg, y + 26, title="功能對應：防線 ↔ 程式函式 ↔ 資料存取",
                          headers=("防線與動作", "程式模組與函式", "資料表存取（R 讀、W 寫、D 刪）", "防線"), rows=rows, widths=(370, 800, 470))
    svg.text(40, end_y + 24, "依據：AGENTS.md 第 7、10 節、docs/帳號與安全-auth-security.md 第 5 節、docs/系統架構-architecture.md。詞表缺口交由模型層查核；法務覆核仍待進行。", size=14, fill=P["muted"])
    svg.height = int(end_y + 50)
    print(f"內容安全圖：架構區高度 {panel_bottom:.0f}／全圖 {svg.height}")
    return svg


# ---- 部署與營運 -------------------------------------------------------------------------------


def deploy_diagram() -> Svg:
    svg = Svg(W, 100)
    header(svg, tag="[ DEPLOY_OPS ]", title="部署與營運架構圖解",
           subtitle="從提交到上線：映像、獨立的遷移步驟、兩個 worker 池、AWS 雪梨展示環境、排程與告警，以及尚未建立的 Google Cloud production 規劃")
    top = 140
    ax, aw = 30, 250
    bx, bw = 315, 330
    cx, cw = 680, 560
    dx, dw = 1275, 495
    y0 = top + 56

    # -- A 開發與驗證
    a = stack(svg, ax + 10, y0, aw - 20, [
        dict(icon="users", azure="Users", title="開發者", body_size=14, body=[
            "本機 development：Flask 開發伺服器（正式環境不得使用 app.run）", "SQLite 或本機 SQL Server；.env 不進版本庫，.env.example 只列變數名稱"]),
        dict(icon="function-apps", azure="Function Apps", title="自動測試", body_size=14, body=[
            "pytest 11,448 案例（含連 project_test 的 SQL Server 整合測試）", "Node 1,100 案例", "tests/architecture：分層、無 Raw SQL、前端規則", "test_design_docs：文件與程式一致"]),
        dict(icon="app-services", azure="App Services", title="容器驗收", body_size=14, body=[
            "scripts/verify_container.py（WSL Docker）", "非 root、映像沒有 .env、各環境啟動、缺機密只列變數名稱", "會員與客服流程、保存期限指令（160／160）"]),
        dict(icon="app-registrations", azure="App Registrations", title="OpenSpec 規格流程", body_size=14, body=[
            "propose → apply → archive；87 份主規格、25 個 change 已封存", "每批做完才 commit，驗收後才封存"]),
    ])
    a_bottom = bottom_of(a)

    # -- B 部署流程
    steps = [
        ("① 預檢與找主機", ["deploy-find-host：確認 AWS 帳號就是預期帳號，以名稱與標籤找出主機（必須剛好一台）"]),
        ("② 遷移前手動快照", ["snapshot-db：每個提交短碼一個 RDS 快照，已存在就沿用，等到 available（執行遷移前先備份）"]),
        ("③ 在主機上建置映像", ["git archive → S3 → docker build：基底映像 rac-app-base，再疊一層 RDS 憑證的衍生映像 rac-app:<提交短碼>", "check-image：非 root、沒有 .env 與 .git"]),
        ("④ 遷移（獨立步驟）", ["一次性容器 alembic upgrade head → 確認已是最新版 → seed-scripts", "不在應用程式啟動時執行；失敗即停止、不重啟"]),
        ("⑤ 換版並重啟兩個池", ["switch-release：release.env 換標籤，前一版存成 release.previous.env；容器一律 --pull=never"]),
        ("⑥ 驗證", ["兩個池 /readyz 回 200；非 root；各自只跑自己的 gunicorn 設定；埠只在 127.0.0.1"]),
        ("⑦ 第一次部署的一次性群組", ["db-init、create-admin、ingress、routing、schedule：以 --group 單獨執行，例行部署不含"]),
        ("⑧ 回復與拆除", ["rollback_plan：換回前一版標籤；不遷移、不降級、不還原資料庫（expand／contract）", "teardown_plan：整批拆除，RDS 須明確選擇最終快照；尚未對真實 AWS 執行"]),
    ]
    b_specs = [dict(title=title, body=body, body_size=14, title_size=17, badges=(i + 1,)) for i, (title, body) in enumerate(steps)]
    b = stack(svg, bx + 10, y0, bw - 20, b_specs, gap=10)
    b_bottom = bottom_of(b)

    # -- C AWS 展示環境
    c_x, c_w = cx + 10, cw - 20
    svg.text(c_x + 4, y0 + 12, "AWS 雪梨 ap-southeast-2 ‧ APP_ENV=staging ‧ Free plan 抵用金，展示期到 2026-12-31", size=13, fill=P["muted"])
    c_top = y0 + 34
    ec2_chips = [("rac-caddy（入口）", (7,)), ("rac-cs :8081", (5, 6)), ("rac-rpg :8082", (5, 6)), ("rac-job-*.timer × 5", (7,)), ("/run/rac/*.env（tmpfs 0600）", ()), ("/etc/rac/release.env（映像標籤）", (5,))]
    tmp = Svg(W, 10)
    ec2_body = ["沒有 SSH、只開 80 與 443；IMDSv2 強制；2 GB 交換檔；映像與前一版都留在本機", "Docker 容器全部非 root、--read-only、--cap-drop=ALL、no-new-privileges"]
    h_est = tmp.card(c_x, c_top, c_w, icon="app-services", azure="App Services", title="EC2 t3.small（Amazon Linux 2023）", body=ec2_body, body_size=14)
    chips_bottom = tmp.chips(c_x + 14, c_top + h_est + 2, c_w - 28, ec2_chips, size=13, mono=True)
    ec2_h = chips_bottom - c_top + 14
    svg.card(c_x, c_top, c_w, icon="app-services", azure="App Services", title="EC2 t3.small（Amazon Linux 2023）", body=ec2_body, body_size=14, min_h=ec2_h, badges=(3, 5, 6))
    svg.chips(c_x + 14, c_top + h_est + 2, c_w - 28, ec2_chips, size=13, mono=True)
    ec2_top = c_top
    gy = c_top + ec2_h + 12
    half = (c_w - 12) / 2
    grid = [
        dict(icon="sql-database", azure="SQL Database", title="RDS for SQL Server Express", badges=(2, 4), body=[
            "db.t3.micro、單一可用區、20 GB gp3；不公開、加密、只允許主機連入", "主要使用者 racmaster（不是 sa）；應用程式用專屬登入", "連線只有 Encrypt=yes，憑證由映像的系統憑證庫驗證", "備份只保留 1 天（規範要求 30 天）"]),
        dict(icon="key-vaults", azure="Key Vaults", title="SSM Parameter Store", body=[
            "/rac-demo/staging/*（SecureString）", "啟動前由 rac-render-env-* 取出，寫到 tmpfs", "應用程式單元不含 RDS 主要使用者密碼", "機密不進映像、日誌與前端"]),
        dict(icon="api-connections", azure="API Connections", title="S3 傳檔儲存貯體", badges=(3,), body=[
            "git archive 的壓縮檔與 ops/ 維運腳本（主機下載後比對 SHA-256）", "封鎖公開存取、加密、標籤", "拆除時連同物件一併刪除"]),
        dict(icon="communication-services", azure="Communication Services", title="預算與告警", badges=(7,), body=[
            "Budgets 季度 US$75：實際花費 60% 與 90% 寄信", "CloudWatch：EC2 StatusCheckFailed → SNS 電子郵件", "收件人由呼叫者提供，沒有預設值"]),
    ]
    row_heights = []
    for index in range(0, 4, 2):
        hh = max(Svg(W, 10).card(c_x + (j * (half + 12)), gy, half, icon=grid[index + j]["icon"], azure=grid[index + j]["azure"], title=grid[index + j]["title"], body=grid[index + j]["body"], body_size=14, title_size=18) for j in range(2))
        for j in range(2):
            spec = grid[index + j]
            svg.card(c_x + (j * (half + 12)), gy, half, icon=spec["icon"], azure=spec["azure"], title=spec["title"], body=spec["body"], body_size=14, title_size=18, min_h=hh, badges=spec.get("badges", ()))
        row_heights.append(hh)
        gy += hh + 12
    c_bottom = gy
    svg.rect(cx, y0 + 20, cw, c_bottom - y0 - 14, fill="none", stroke=P["accent"], rx=14, sw=1.5)

    # -- D 營運與 production 規劃
    d_half = (dw - 12) / 2
    ops = [
        dict(icon="log-analytics", azure="Log Analytics Workspaces", title="日誌", badges=(7,), body=[
            "結構化 JSON 到標準輸出；Docker 循環日誌（單檔 10 MB、最多 3 個）", "規範要求保存 30 天：展示環境依大小、不依時間，未達"]),
        dict(icon="scheduler", azure="Scheduler", title="排程與告警", badges=(7,), body=[
            "check-alerts 每 5 分鐘：用量預算、對話錯誤率、設定錯誤、延遲 p95、帳號鎖定激增", "狀態變化寄信：進入、升級、每 6 小時提醒、恢復各一封"]),
        dict(icon="key-vaults", azure="Key Vaults", title="機密輪替", body=[
            "金鑰至少每 90 天輪替，疑似外洩立即輪替", "建立新金鑰 → 更新機密 → 重新部署 → 驗證 → 停用舊金鑰；SECRET_KEY 可暫用 SECRET_KEY_FALLBACKS"]),
        dict(icon="sql-database", azure="SQL Database", title="備份與還原", body=[
            "規範：託管 SQL Server 自動備份與時間點還原，加密、不同帳號或區域、保留 30 天、每季還原演練", "還原後、對外開放前：replay-deletions 依 deletion_tombstones 重新抹除"]),
    ]
    dy = y0 + 8
    for index in range(0, 4, 2):
        hh = max(Svg(W, 10).card(dx + (j * (d_half + 12)), dy, d_half, icon=ops[index + j]["icon"], azure=ops[index + j]["azure"], title=ops[index + j]["title"], body=ops[index + j]["body"], body_size=14, title_size=18) for j in range(2))
        for j in range(2):
            spec = ops[index + j]
            svg.card(dx + (j * (d_half + 12)), dy, d_half, icon=spec["icon"], azure=spec["azure"], title=spec["title"], body=spec["body"], body_size=14, title_size=18, min_h=hh, badges=spec.get("badges", ()))
        dy += hh + 12
    # 缺口
    gap_chips = ["5xx 比率告警", "外部存活與就緒探測", "寄信失敗告警", "排程失敗告警", "備份 30 天與跨區", "日誌保存 30 天", "信件投遞品質"]
    from svg_kit import wrap

    note_lines = wrap("每項寫明原因、風險與重新評估時機；升級 production 前逐項處理。原第 8 項「一般與成人模式不得用免費模型」已隨 AGENTS.md 修訂不再成立。", 12, dw - 28)
    chips_y = dy + 40 + len(note_lines) * 16
    tmp = Svg(W, 10)
    chips_bottom = tmp.chips(dx + 14, chips_y, dw - 28, gap_chips, size=13)
    gap_h = chips_bottom - dy + 14
    svg.rect(dx, dy, dw, gap_h, fill=P["panel_alt"], stroke=P["muted"], rx=12, dash="7 5")
    svg.text(dx + 14, dy + 26, "展示環境未達 production 的缺口（7 項）", size=18, weight=700)
    svg.lines(dx + 14, dy + 46, note_lines, size=12, lh=16, fill=P["muted"])
    svg.chips(dx + 14, chips_y, dw - 28, gap_chips, size=13)
    dy += gap_h + 12
    prod = dict(icon="conditional-access", azure="Conditional Access", title="production 規劃：Google Cloud（尚未建立）", dashed=True, body_size=14, body=[
        "Cloud Run（gunicorn gthread，客服與 RPG 串流使用獨立的 worker 池）＋ Cloud SQL for SQL Server ＋ Secret Manager", "入口層：HTTPS、HTTP/2、HSTS、IP 限流（上線前關卡）；排程由平台排程器觸發 flask 指令",
        "沒有可計費的 Google Cloud 專案：cloud-staging-run、production-launch 規劃中；另待法務覆核、模型供應商方案與付費模型決定"])
    ph = svg.card(dx, dy, dw, **prod)
    d_bottom = dy + ph

    # -- 箭頭
    svg.arrow([(ax + aw, b[0][0] + 40), (bx + 10, b[0][0] + 40)], color=P["text"], label="部署", label_at=((ax + aw + bx + 10) / 2, b[0][0] + 28), label_size=12)
    svg.arrow([(bx + bw - 10, b[3][0] + 40), (cx + 10, b[3][0] + 40)], color=P["accent"], label="SSM", label_at=((bx + bw + cx) / 2, b[3][0] + 28), label_size=12)
    svg.arrow([(cx + cw, ec2_top + 40), (dx, ec2_top + 40)], color=P["muted"], dash="6 5", label="日誌", label_at=((cx + cw + dx) / 2, ec2_top + 28), label_size=12)

    panel_bottom = max(a_bottom, b_bottom, c_bottom, d_bottom) + 4
    lane(svg, ax, aw, top, panel_bottom, "[ DEV ]", "開發與驗證")
    lane(svg, bx, bw, top, panel_bottom, "[ PIPELINE ]", "部署流程（deploy_plan）")
    lane(svg, dx, dw, top, panel_bottom, "[ OPS ]", "營運與 production 規劃")
    svg.text(cx + 16, top + 24, "[ AWS DEMO ]", size=12, fill=P["accent"], mono=True)
    svg.text(cx + 16, top + 50, "AWS 展示環境（RC-4 前半）", size=21, weight=700)

    rows = [
        ("1", "預檢與找主機", "deploy_plan 的 prepare 組：deploy-find-host（build_plan 另有 check-identity）", "確認帳號與主機；不符即停止，不放寬"),
        ("2", "遷移前手動快照", "snapshot-db、wait-snapshot-db", "每個提交短碼一個 RDS 快照；2026-11-17 已對真實 RDS 執行並驗證"),
        ("3", "建置映像", "archive-source、upload-source、build-image、check-image（derived_dockerfile）", "主機上分兩段建置；檢查非 root（uid 10001）、沒有 .env 與 .git、憑證庫張數；已驗證"),
        ("4", "遷移與種子", "migrate-db、check-migration-head、seed-scripts（alembic upgrade head；展示環境的 RDS 已於 2026-11-18 由 0018 升到 0019，adult-only-signup，已驗證）", "遷移是部署的獨立步驟，不在應用程式啟動時執行；舊版程式必須在新結構上可運作（expand／contract）"),
        ("5", "換版與重啟", "switch-release、wait-switch-release（release.env、release.previous.env）", "只重啟兩個池；標籤不存在就失敗；已驗證"),
        ("6", "驗證", "check-pools、check-pool-hardening（容器內 /readyz、行程 UID、gunicorn 設定、埠只在回送位址）", "已對真實主機驗證；公開 /healthz 與 /readyz 回 200，本文只有 {\"status\":\"ok\"}"),
        ("7", "一次性群組與營運", "db-init、create-admin、ingress、routing、schedule；告警與寄信（task 7.x）", "Caddy 憑證簽發、路徑分流、五個計時器、EC2 狀態告警演練與營運告警信已驗證；寄信進垃圾郵件匣"),
        ("8", "回復與拆除", "rollback_plan（rollback-switch-release 等）、teardown_plan（td-* 步驟）", "尚未對真實 AWS 執行；回復不降級資料庫"),
    ]
    end_y = mapping_table(svg, panel_bottom + 36, title="功能對應：部署步驟 ↔ 計畫步驟與腳本 ↔ 驗證與限度",
                          headers=("部署動作", "計畫步驟與腳本（scripts/aws_demo_plan.py、aws_demo.py）", "驗證結果與限度", "步驟"), rows=rows, widths=(300, 900, 440))
    svg.text(40, end_y + 24, "依據：docs/營運與部署-operations.md 第 2、3、8 節與「展示環境未達 production 的缺口」；展示環境不是 production，單機單點，數字不是容量依據。", size=14, fill=P["muted"])
    svg.height = int(end_y + 50)
    print(f"部署圖：架構區高度 {panel_bottom:.0f}／全圖 {svg.height}")
    return svg


def main(argv: list[str]) -> int:
    which = argv[1] if len(argv) > 1 else "all"
    builders = {"entry": (entry_diagram, ENTRY_PNG), "rpg": (rpg_diagram, RPG_PNG), "safety": (safety_diagram, SAFETY_PNG), "deploy": (deploy_diagram, DEPLOY_PNG)}
    for key, (build, target) in builders.items():
        if which in ("all", key):
            render_png(build(), target)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
