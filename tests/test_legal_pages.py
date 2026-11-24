"""auth-pages 規格「隱私權政策如實描述資料權利」：政策頁描述已提供的功能，保存期限表與清除指令使用的數值一致。"""
import re
from datetime import timedelta
from html.parser import HTMLParser

import pytest

from app import create_app
from app.config import load_config
from app.models import Base
from app.services import data_retention_service as retention
from app.services.legal import CURRENT_TERMS_VERSION
from tests.support.auth import FakeClock


@pytest.fixture()
def client():
    app = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(app.extensions["db_engine"])
    return app.test_client()


class _TableRows(HTMLParser):
    """把頁面上的表格抽成（第一欄文字，第二欄文字）。"""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None
        self._row: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def retention_table(html: str) -> dict[str, str]:
    parser = _TableRows()
    parser.feed(html)
    return {row[0]: row[1] for row in parser.rows if len(row) == 2}


def period_text(period: timedelta) -> str:
    """與政策頁相同的寫法：365 天 = 12 個月、730 天 = 24 個月，其餘用天。"""
    months = {365: 12, 730: 24}
    return f"{months[period.days]} 個月" if period.days in months else f"{period.days} 天"


def test_period_text_uses_months_only_for_the_two_long_periods():
    assert period_text(timedelta(days=365)) == "12 個月" and period_text(timedelta(days=730)) == "24 個月"
    assert period_text(timedelta(days=14)) == "14 天" and period_text(timedelta(days=7)) == "7 天"


def test_the_retention_table_uses_the_same_periods_as_the_purge_command(client):
    table = retention_table(client.get("/privacy").get_data(as_text=True))
    expectations = {
        "對話與訊息": retention.CONVERSATION_RETENTION,
        "用量紀錄（不含對話全文）": retention.LEDGER_RETENTION,
        "模型回覆失敗時的原始輸出": retention.ERROR_DETAIL_RETENTION,
        "管理員重設密碼的一次性權杖": retention.AUTH_TOKEN_RETENTION,
        "管理稽核紀錄": retention.ADMIN_AUDIT_RETENTION,
    }
    for label, period in expectations.items():
        assert label in table, label
        assert period_text(period) in table[label], (label, table[label])
    # 每一列只含自己的期限，沒有把別類的數字誤植進來
    assert "12 個月" not in table["管理稽核紀錄"] and "24 個月" not in table["對話與訊息"]


def test_the_table_also_discloses_the_tombstone_that_is_kept_without_a_limit(client):
    table = retention_table(client.get("/privacy").get_data(as_text=True))
    (label,) = [key for key in table if key.startswith("帳號刪除紀錄")]
    assert "不含個人資料" in label and "不設期限" in table[label]


def test_the_rights_section_describes_the_real_features_and_no_longer_says_they_are_coming(client):
    html = client.get("/privacy").get_data(as_text=True)
    assert "後續版本提供" not in html and "將提供" not in html and "將於" not in html
    section = html.split("<h2>6. 您的權利</h2>", 1)[1].split("<h2>7.", 1)[0]
    for needle in ("匯出我的資料", "下載 JSON", "CSV（zip）", "含生日與年齡區間", "刪除帳號", "7 天寬限期", "撤銷刪除", "永久刪除",
                   "匿名化保留統計", "備份最長 30 天", "管理員帳號無法自行刪除"):
        assert needle in section, needle


def test_the_privacy_page_numbers_for_age_still_come_from_the_policy_not_the_text(client):
    html = client.get("/privacy").get_data(as_text=True)
    assert re.search(r"發現未滿 \d+ 歲者", html)


def test_the_terms_version_names_the_new_draft_and_the_registration_records_it():
    # rpg-turn-revision 補了重新生成理由與遊戲幣帳本的告知（draft-6），之後又把編輯改為與重新生成共用免費次數（draft-7）；
    # adult-only-signup 加入僅限滿 18 歲、註冊時的年滿 18 歲聲明與謊報責任（draft-8，2026-11-18）；
    # safe-mode-toggle 加入露骨內容、安全模式兩種狀態與供應商留存或審閱的揭露（draft-9，2026-11-19）；
    # rpg-script-reports 如實說明公開分享已開放、檢舉與刪除帳號時匿名保留劇本的選項（draft-10，2026-11-22）；
    # admin-delete-account 在使用規範加入「管理員得刪除違規者的帳號與其全部資料」（draft-11，2026-11-24）；
    # 版本隨內容提高（只影響之後新註冊者記錄的版本）
    assert CURRENT_TERMS_VERSION == "2026-11-24-draft-11" and len(CURRENT_TERMS_VERSION) <= 40
    assert CURRENT_TERMS_VERSION not in ("2026-11-16-draft-7", "2026-11-18-draft-8", "2026-11-19-draft-9", "2026-11-22-draft-10")   # 提高過：不是先前的版本


# ---- Google 登入改版（google-first-login）新增的揭露 ------------------------------------------------------

def test_the_privacy_policy_says_the_account_is_created_at_the_first_google_sign_in_with_the_google_email_and_name(client):
    html = client.get("/privacy").get_data(as_text=True)
    assert "第一次以 Google 登入" in html and "建立帳號" in html and "Google 提供的電子郵件與名稱" in html
    assert "玩家帳號沒有密碼" in html and "只有管理員帳號有密碼" in html


def test_the_privacy_policy_says_the_age_is_declared_after_signing_in_and_unfinished_or_underage_accounts_are_deleted_at_once(client):
    html = client.get("/privacy").get_data(as_text=True)
    assert "登入之後" in html and "年齡確認" in html
    assert "24 小時內沒有完成年齡確認，帳號與資料會立即刪除" in html
    assert re.search(r"發現未滿 \d+ 歲者，帳號與全部資料也會(?:<strong>)?立即刪除", html)
    table = retention_table(html)
    (account_row,) = [value for key, value in table.items() if key.startswith("帳號資料")]
    assert "24 小時內未完成年齡確認" in account_row and "立即刪除" in account_row


def test_the_retention_table_lists_the_lock_strikes_with_the_purge_period_and_the_appeals_with_their_account(client):
    table = retention_table(client.get("/privacy").get_data(as_text=True))
    (strike_label,) = [key for key in table if key.startswith("帳號鎖定計次")]
    assert period_text(retention.LOCK_STRIKE_RETENTION) in table[strike_label] and "只含帳號編號" in strike_label
    (appeal_label,) = [key for key in table if key.startswith("申訴")]
    assert "帳號刪除時一併刪除" in table[appeal_label] and "與所屬帳號相同" in table[appeal_label]


def test_the_privacy_policy_discloses_appeals_as_personal_data_only_the_admin_can_read_while_locked(client):
    html = client.get("/privacy").get_data(as_text=True)
    assert "申訴" in html and "只有管理員在您的帳號被鎖定期間看得到" in html and "不會出現在日誌" in html
    assert "鎖定的計次" in html                                                     # 使用紀錄裡也揭露了計次
    assert "以及您送出的申訴" in html                                               # 匯出的內容包含申訴


def test_the_terms_describe_the_google_first_flow_the_age_step_the_deadline_and_the_automatic_lock(client):
    html = client.get("/terms").get_data(as_text=True)
    assert "第一次以 Google 登入時會建立您的帳號" in html and "不另外設定密碼" in html
    assert "24 小時內沒有完成年齡確認，帳號與其資料會被刪除" in html and "立即刪除" in html
    assert "自動鎖定" in html and "只有管理員能解除" in html and "帳號申訴" in html
    assert "閒置 30 天" in html


def test_the_terms_no_longer_point_to_a_registration_page(client):
    html = client.get("/terms").get_data(as_text=True)
    assert 'href="/register"' not in html and "前往註冊" not in html
    assert 'href="/login"' in html and 'href="/privacy"' in html
    assert "請到「帳號設定」補填" not in html                                       # 舊的補填說法已不存在


def test_the_privacy_policy_no_longer_points_to_a_registration_page_either(client):
    """沒有 /register：隱私權政策與條款一樣，底部連結是登入頁（登入與註冊合一）與條款。"""
    html = client.get("/privacy").get_data(as_text=True)
    assert 'href="/register"' not in html and "前往註冊" not in html
    assert 'href="/login"' in html and 'href="/terms"' in html


def test_the_terms_say_every_account_starts_with_the_safe_mode_on_and_there_is_no_middle_mode(client):
    """safe-mode-toggle：只有安全模式開與關兩種；所有帳號一開始都是開；沒有「一般模式」，也沒有成年者預設一般模式的說法。"""
    html = client.get("/terms").get_data(as_text=True)
    section = html.split("<h2>5. RPG、劇本創作與內容規範</h2>", 1)[1].split("<h2>6.", 1)[0]
    assert "所有帳號一開始都是<strong>安全模式開</strong>" in section
    assert "只有兩種狀態" in section and "安全模式開" in section and "安全模式關" in section
    for stale in ("預設使用一般模式", "成年的使用者預設", "調整為一般模式", "成人模式預設關閉", "安全模式、一般模式與成人模式"):
        assert stale not in section, stale


# ---- RPG、劇本與主角（rpg-script-authoring、rpg-character-creation）新增的揭露 ----------------------------------

def test_the_retention_table_lists_the_rpg_rows_with_the_same_periods_as_the_purge_command(client):
    table = retention_table(client.get("/privacy").get_data(as_text=True))
    (save_label,) = [key for key in table if key.startswith("RPG 存檔")]
    assert "主角" in save_label and period_text(retention.SESSION_RETENTION) in table[save_label] and "刪除帳號" in table[save_label]
    (submission_label,) = [key for key in table if key.startswith("劇本送審紀錄")]
    assert "只含結果、類別與時間" in submission_label and period_text(retention.SCRIPT_SUBMISSION_RETENTION) in table[submission_label]
    (script_label,) = [key for key in table if key.startswith("劇本（")]
    assert "刪除帳號時一併刪除" in table[script_label]
    (intro_label,) = [key for key in table if "自我介紹" in key]
    assert table[intro_label] == "不保存"
    # 每一列只含自己的期限
    assert "12 個月" not in table["管理稽核紀錄"]


def test_the_privacy_policy_says_what_is_sent_to_the_rpg_model_service_and_that_it_is_separate_from_gemini(client):
    html = client.get("/privacy").get_data(as_text=True)
    section = html.split("<h2>4. RPG 與劇本的資料會傳送至 RPG 模型服務</h2>", 1)[1].split("<h2>5.", 1)[0]
    for needle in ("OpenRouter", "與客服使用的 Google Gemini 分開", "劇本設定", "主角", "自我介紹", "不含隱藏特質", "免費模型的輸入輸出可能被供應商留存",
                   "勿在劇本、主角與自我介紹中填寫真實的個人資料", "零保存", "信箱、生日與年齡區間", "不會", "只告知類別"):
        assert needle in section, needle


def test_the_privacy_policy_lists_the_new_personal_data_and_says_the_intro_is_never_stored_and_admins_cannot_read_scripts_by_default(client):
    html = client.get("/privacy").get_data(as_text=True)
    section = html.split("<h2>1. 我們蒐集哪些資料</h2>", 1)[1].split("<h2>2.", 1)[0]
    for needle in ("暱稱與短代號", "筆名", "管理員預設看不到", "稽核紀錄", "只保留<strong>類別與時間</strong>", "存檔與主角", "不會儲存"):
        assert needle in section, needle


def test_the_rights_section_covers_the_exported_and_deleted_rpg_data(client):
    html = client.get("/privacy").get_data(as_text=True)
    section = html.split("<h2>6. 您的權利</h2>", 1)[1].split("<h2>7.", 1)[0]
    for needle in ("暱稱與短代號", "您創作的劇本（全部版本）", "存檔（含主角與聯繫）", "送審紀錄", "自我介紹不保存", "以及您的暱稱、劇本、存檔與主角"):
        assert needle in section, needle


def test_the_privacy_policy_does_not_promise_features_that_do_not_exist_yet(client):
    html = client.get("/privacy").get_data(as_text=True)
    assert "目前沒有公開分享" not in html and "現階段沒有公開分享" not in html and "公開分享尚未開放" not in html      # 公開分享已開放（rpg-script-reports）
    for forbidden in ("將提供", "後續版本提供", "將於"):
        assert forbidden not in html


def test_the_terms_describe_the_rpg_the_content_modes_and_the_absolute_prohibitions(client):
    html = client.get("/terms").get_data(as_text=True)
    section = html.split("<h2>5. RPG、劇本創作與內容規範</h2>", 1)[1].split("<h2>6.", 1)[0]
    for needle in ("只有兩種狀態", "本服務含露骨內容", "謊報年齡者須自行承擔", "無論哪一種模式，一律禁止", "未成年（含看似未成年的角色）的性內容",
                   "性暴力", "非自願的性情節", "以真人名義的性內容", "我們只告知類別", "暫停而不是放行", "暫停送審權限", "申訴頁",
                   "筆名", "先審後發", "授權本平台保存", "不含您的署名", "可能由管理員審閱", "AI 生成的主角與聯繫只是建議", "性奴等禁斷關係", "隨帳號一併刪除"):
        assert needle in section, needle
    assert "RPG" in html.split("<h2>2.", 1)[0] and "劇本創作" in html.split("<h2>2.", 1)[0]          # 服務內容也提到了


def test_the_age_numbers_in_the_new_terms_section_come_from_the_age_policy(client):
    html = client.get("/terms").get_data(as_text=True)
    section = html.split("<h2>5. RPG、劇本創作與內容規範</h2>", 1)[1].split("<h2>6.", 1)[0]
    assert "13 至 17 歲" not in section and "我確定已滿" not in section           # 數字來自年齡政策：最低年齡 18；關閉安全模式時沒有另外的年齡勾選
    assert "本服務僅限滿 18 歲者使用（見第 2 節）" in section and "年滿 18 歲聲明" in section


# ---- 重新生成與編輯最後一輪（rpg-turn-revision）新增的揭露 ----------------------------------------------------

def test_the_privacy_policy_says_the_regeneration_reason_goes_to_the_operators_never_to_the_ai_and_is_kept_with_the_save(client):
    html = client.get("/privacy").get_data(as_text=True)
    section = html.split("<h2>1. 我們蒐集哪些資料</h2>", 1)[1].split("<h2>2.", 1)[0]
    for needle in ("重新生成時填寫的分類與理由", "會提供營運團隊檢視", "不含您的帳號資料", "不會傳送給 AI", "不會出現在日誌", "請勿在理由中填寫真實的個人資料"):
        assert needle in section, needle
    table = retention_table(html)
    (label,) = [key for key in table if key.startswith("重新生成時填寫的分類與理由")]
    assert "與所屬存檔相同" in table[label] and "存檔刪除或帳號刪除時一併刪除" in table[label]


def test_the_model_service_section_says_the_reason_is_not_sent_to_the_rpg_model(client):
    html = client.get("/privacy").get_data(as_text=True)
    section = html.split("<h2>4. RPG 與劇本的資料會傳送至 RPG 模型服務</h2>", 1)[1].split("<h2>5.", 1)[0]
    assert "分類與理由不會</strong>傳送至這個服務" in section and "不會成為 AI 的指令" in section


def test_the_privacy_policy_says_the_wallet_and_ledger_stay_with_the_account_and_are_exported_and_deleted(client):
    html = client.get("/privacy").get_data(as_text=True)
    section = html.split("<h2>1. 我們蒐集哪些資料</h2>", 1)[1].split("<h2>2.", 1)[0]
    for needle in ("遊戲幣與帳本", "免費與付費兩種", "每一筆的種類、增減的金額與關聯的存檔", "隨帳號保存"):
        assert needle in section, needle
    table = retention_table(html)
    (label,) = [key for key in table if key.startswith("遊戲幣餘額與帳本")]
    assert "每筆的種類、金額與關聯的存檔" in label and "帳號存續期間" in table[label] and "刪除帳號時一併刪除" in table[label]
    rights = html.split("<h2>6. 您的權利</h2>", 1)[1].split("<h2>7.", 1)[0]
    for needle in ("遊戲幣的餘額與帳本", "重新生成時填寫的分類與理由", "遊戲幣錢包與帳本，無法復原", "含重新生成時填寫的理由"):
        assert needle in rights, needle


def test_the_privacy_policy_still_does_not_promise_payment_features(client):
    html = client.get("/privacy").get_data(as_text=True)
    for forbidden in ("儲值", "訂閱", "付款紀錄", "退款"):
        assert forbidden not in html, forbidden


def test_the_terms_describe_the_regeneration_and_edit_rules(client):
    html = client.get("/terms").get_data(as_text=True)
    section = html.split("<h2>5. RPG、劇本創作與內容規範</h2>", 1)[1].split("<h2>6.", 1)[0]
    for needle in ("重新生成與編輯最後一輪", "最後一輪", "AI 的輸出不能直接修改", "下一輪開始後這一輪就不能再動",
                   # 2026-11-16 規則修訂：兩種操作共用同一組免費次數；免費次數內編輯不扣幣也不需要餘額；編輯不使次數歸零
                   "前幾次重新生成或編輯免費", "兩種操作共用同一組次數", "免費次數內不扣遊戲幣，也不需要有餘額",
                   "每次重新生成或編輯都消耗與一輪劇情相同的遊戲幣", "原本那一輪已扣的幣不退", "餘額不足時無法送出", "編輯不會使免費次數歸零",
                   "必須選擇分類並填寫", "理由會提供營運團隊檢視、不會送給 AI", "完整通過內容檢查", "原本的回應會保留", "不扣遊戲幣，也不消耗免費次數",
                   "遊戲幣帳本會隨帳號一併刪除"):
        assert needle in section, needle
    assert "沒有成功產生回應" in section and "以遊戲內顯示為準" in section


def test_the_terms_no_longer_carry_the_old_edit_always_paid_and_resets_the_free_count_rule(client):
    """舊規則（編輯一律消耗幣並使免費次數歸零）已於 2026-11-16 作廢；條款不得再留下這兩句。"""
    html = client.get("/terms").get_data(as_text=True)
    section = html.split("<h2>5. RPG、劇本創作與內容規範</h2>", 1)[1].split("<h2>6.", 1)[0]
    for stale in ("編輯同樣消耗一輪的遊戲幣", "使該回合的免費重新生成次數歸零", "免費重新生成次數歸零"):
        assert stale not in section, stale
    # 「歸零」只允許出現在「編輯不會使免費次數歸零」這一句
    assert section.count("歸零") == 1 and "編輯不會使免費次數歸零" in section


def test_the_reason_length_range_in_both_pages_comes_from_the_service_constants_not_from_the_text(client, monkeypatch):
    from app.services.game_service import REASON_MAX, REASON_MIN

    for path in ("/privacy", "/terms"):
        assert f"{REASON_MIN} 至 {REASON_MAX} 字" in client.get(path).get_data(as_text=True), path
    monkeypatch.setattr("app.controllers.auth_page_controller.REASON_MIN", 7)
    monkeypatch.setattr("app.controllers.auth_page_controller.REASON_MAX", 63)
    for path in ("/privacy", "/terms"):
        html = client.get(path).get_data(as_text=True)
        assert "7 至 63 字" in html and f"{REASON_MIN} 至 {REASON_MAX} 字" not in html, path


def test_the_new_terms_text_hardcodes_no_coin_or_free_regeneration_numbers(client):
    """幣價與免費次數存在設定表、可用指令調整；條款只講規則，數字以遊戲內顯示為準，所以不會與設定脫節。"""
    html = client.get("/terms").get_data(as_text=True)
    section = html.split("<h2>5. RPG、劇本創作與內容規範</h2>", 1)[1].split("<h2>6.", 1)[0]
    revision = section.split("<strong>遊戲幣</strong>", 1)[1].split("<strong>刪除帳號</strong>", 1)[0]
    assert not re.search(r"\d+\s*(?:幣|枚)|前\s*\d+\s*次|第\s*\d+\s*次", revision)


# ---- 安全模式（safe-mode-toggle）新增的揭露 --------------------------------------------------------------------------

def terms_section_five(client) -> str:
    html = client.get("/terms").get_data(as_text=True)
    return html.split("<h2>5. RPG、劇本創作與內容規範</h2>", 1)[1].split("<h2>6.", 1)[0]


def test_the_terms_disclose_explicit_content_the_two_states_the_bans_the_age_duty_and_provider_retention(client):
    html = client.get("/terms").get_data(as_text=True)
    section = terms_section_five(client)
    for needle in (
        "本服務含露骨內容", "成年角色之間合意的露骨內容可能出現", "是否出現取決於模型，我們不保證",          # 露骨內容（不保證結果）
        "性行為與性暗示", "露骨的暴力血腥與虐待描寫", "自殺與自殘方法的描寫", "簡短的親吻",                # 安全模式開的界線
        "在「帳號設定」頁自行開關", "同意安全規範", "記錄同意的時間與安全規範的版本",                        # 如何切換與同意紀錄
        "較保守", "綁定安全模式", "緊急煞車", "不會自動替您關閉安全模式",                                    # 生效模式、綁定、煞車、不自動調高
        "露骨內容僅限成年使用者", "謊報年齡者須自行承擔因此產生的一切責任",                                  # 年滿 18 歲與謊報責任
        "這些敏感內容可能被模型供應商留存或審閱", "請勿輸入真實的個人資料",                                  # 供應商留存或審閱
        "無論哪一種模式，一律禁止", "以真人名義的性內容",                                                    # 絕對禁止項
    ):
        assert needle in section, needle
    assert "草稿" in html and "尚待法務覆核" in html                                                       # 仍標示為草稿


def test_the_terms_hardcode_no_age_number_for_the_explicit_content_duty(client, monkeypatch):
    """年齡數字一律來自年齡政策，不寫死在樣板裡。"""
    monkeypatch.setattr("app.controllers.auth_page_controller.get_age_policy", lambda: type("P", (), {
        "min_register_age": 21, "adult_age": 21})())
    section = terms_section_five(client)
    assert "本服務僅限滿 21 歲者使用（見第 2 節）" in section and "年滿 21 歲聲明" in section


def test_the_privacy_policy_discloses_that_turning_the_safe_mode_off_may_let_the_rpg_provider_keep_or_review_the_content(client):
    html = client.get("/privacy").get_data(as_text=True)
    section = html.split("<h2>4. RPG 與劇本的資料會傳送至 RPG 模型服務</h2>", 1)[1].split("<h2>5.", 1)[0]
    for needle in ("安全模式關閉時", "您輸入的行動，以及模型產生的劇情", "成年角色之間合意的露骨內容可能出現",
                   "敏感內容可能被模型供應商留存或人工審閱", "不以付費模型為前提", "請保持安全模式開啟", "請勿輸入真實的個人資料",
                   "不會把您的信箱、生日與年齡區間傳送至這個服務"):
        assert needle in section, needle
    collected = html.split("<h2>1. 我們蒐集哪些資料</h2>", 1)[1].split("<h2>2.", 1)[0]
    for needle in ("安全模式的設定與同意紀錄", "同意安全規範的時間與規範版本", "不含您寫的任何文字"):
        assert needle in collected, needle


def test_neither_policy_page_has_the_old_three_mode_wording_or_a_minor_exception(client):
    for path in ("/terms", "/privacy"):
        html = client.get(path).get_data(as_text=True)
        for stale in ("一般模式", "成人模式", "13 歲以上", "13 至 17 歲", "我確定已滿", "預設關閉", "僅限安全模式"):
            assert stale not in html, (path, stale)
        assert "草稿" in html and CURRENT_TERMS_VERSION in html


def test_the_privacy_policy_still_does_not_promise_features_that_do_not_exist_after_the_safe_mode_wording(client):
    html = client.get("/privacy").get_data(as_text=True)
    for forbidden in ("將提供", "後續版本提供", "將於", "保證不會被留存", "保證不會被審閱"):
        assert forbidden not in html, forbidden


# ---- 公開分享、檢舉與匿名保留（rpg-script-reports；auth-pages 規格）---------------------------------------------------

def test_neither_policy_page_still_says_public_sharing_is_not_open(client):
    for path in ("/terms", "/privacy"):
        html = client.get(path).get_data(as_text=True)
        for stale in ("公開分享尚未開放", "現階段沒有公開分享", "目前沒有公開分享", "現階段劇本只供您自己使用", "公開分享開放之後"):
            assert stale not in html, (path, stale)


def test_the_privacy_policy_says_the_pen_name_is_shown_publicly_the_script_is_ai_checked_and_may_be_read_by_an_admin(client):
    html = client.get("/privacy").get_data(as_text=True)
    section = html.split("<h2>1. 我們蒐集哪些資料</h2>", 1)[1].split("<h2>2.", 1)[0]
    assert "公開顯示在已發佈的劇本上" in section and "第一次發佈時鎖定" in section
    assert "送審公開的劇本可能由管理員審閱" in section
    purposes = html.split("<h2>2. 使用目的</h2>", 1)[1].split("<h2>3.", 1)[0]
    assert "公開分享" in purposes and "複製進自己的存檔" in purposes and "不含您的署名" in purposes and "授權本平台保存" in purposes
    assert "AI 檢查" in purposes and "稽核紀錄" in purposes


def test_the_privacy_policy_says_the_report_goes_to_the_admin_only_never_to_the_ai_and_the_reporter_is_not_told(client):
    html = client.get("/privacy").get_data(as_text=True)
    section = html.split("<h2>1. 我們蒐集哪些資料</h2>", 1)[1].split("<h2>2.", 1)[0]
    (bullet,) = [item for item in section.split("<li>") if item.startswith("<strong>劇本檢舉</strong>")]
    for needle in ("至多 200 字", "視為個人資料", "只有管理員在被檢舉的劇本下看得到", "不會傳送給 AI", "不會出現在日誌", "看不到檢舉人與檢舉內容", "不會收到處理結果的通知"):
        assert needle in bullet, needle
    model_section = html.split("<h2>4. RPG 與劇本的資料會傳送至 RPG 模型服務</h2>", 1)[1].split("<h2>5.", 1)[0]
    assert "檢舉劇本時填寫的<strong>理由與說明同樣不會</strong>傳送至這個服務" in model_section


def test_the_retention_table_lists_the_script_reports_with_the_period_the_purge_command_uses(client):
    table = retention_table(client.get("/privacy").get_data(as_text=True))
    (label,) = [key for key in table if key.startswith("劇本檢舉")]
    assert f"結案後 {period_text(retention.SCRIPT_REPORT_RETENTION)}" in table[label]
    assert "未結案者與所屬劇本相同" in table[label] and "一併刪除" in table[label]
    assert "12 個月" not in table["管理稽核紀錄"] and "24 個月" not in table[label]                 # 每一列只含自己的期限


def test_the_privacy_policy_describes_the_anonymous_retention_option_its_scope_and_its_known_limit(client):
    html = client.get("/privacy").get_data(as_text=True)
    section = html.split("<h2>6. 您的權利</h2>", 1)[1].split("<h2>7.", 1)[0]
    option = section.split("<h3>刪除帳號時，已發佈的劇本可以匿名保留</h3>", 1)[1].split("<p>其他請求", 1)[0]
    for needle in ("匿名保留我已發佈的劇本", "預設為關閉", "再確認一次", "自己申請刪除帳號", "連續 12 個月未登入", "目前上架的版本", "已註銷的作者",
                   "暱稱與短代號都不會留下", "審核備註也會清除", "草稿、送審中、被退回、已下架與舊的版本一律刪除", "不論是您自己或管理員下架",
                   "仍然可以被其他玩家檢舉", "被管理員依規範下架", "一律全部刪除，不看這個偏好", "已知限制", "不區分這個偏好", "會被一併刪除"):
        assert needle in option, needle
    assert re.search(r"未滿 \d+ 歲而被抹除", option)                                                # 年齡數字來自年齡政策


def test_the_rights_section_also_covers_the_exported_reports_and_the_exception_to_the_deletion(client):
    html = client.get("/privacy").get_data(as_text=True)
    section = html.split("<h2>6. 您的權利</h2>", 1)[1].split("<h2>7.", 1)[0]
    assert "您送出的劇本檢舉" in section and "例外：您若開啟「匿名保留我已發佈的劇本」" in section


def test_the_script_row_of_the_retention_table_points_to_the_option(client):
    table = retention_table(client.get("/privacy").get_data(as_text=True))
    (label,) = [key for key in table if key.startswith("劇本（")]
    assert "刪除帳號時一併刪除" in table[label] and "匿名保留" in table[label] and "第 6 節" in table[label]


def test_the_terms_describe_public_sharing_the_admin_review_the_report_rules_and_the_retention_option(client):
    section = terms_section_five(client)
    assert "先審後發" in section and "可能由管理員審閱" in section and "授權本平台保存" in section and "不含您的署名" in section
    (report,) = [item for item in section.split("<li>") if item.startswith("<strong>檢舉</strong>")]
    for needle in ("不能檢舉自己的劇本與內建劇本", "至多 200 字", "每人每天的檢舉次數有上限", "不會自動下架", "是否下架由管理員決定", "不會送給 AI",
                   "不會收到處理結果的通知", "看不到檢舉人"):
        assert needle in report, needle
    (deletion,) = [item for item in section.split("<li>") if item.startswith("<strong>刪除帳號</strong>")]
    for needle in ("選擇匿名保留已發佈的劇本", "預設為關閉", "自己申請刪除帳號", "連續 12 個月未登入", "已註銷的作者", "其餘劇本與版本一律刪除", "仍可被檢舉",
                   "被管理員依規範下架", "逾時未完成年齡確認的帳號，一律全部刪除", "不區分這個偏好", "會被一併刪除", "隨帳號一併刪除"):
        assert needle in deletion, needle


def terms_section_four(client) -> str:
    html = client.get("/terms").get_data(as_text=True)
    return html.split("<h2>4. 使用規範</h2>", 1)[1].split("<h2>5.", 1)[0]


def test_the_terms_say_an_administrator_may_delete_an_account_that_breaks_the_rules_and_it_cannot_be_undone(client):
    section = terms_section_four(client)
    (clause,) = [item for item in section.split("<li>") if "管理員得刪除您的帳號與其全部資料" in item]
    for needle in ("違反內容規範或本條款", "刪除後無法復原", "立即執行", "7 天寬限期", "匿名保留已發佈的劇本", "劇本會一併刪除"):
        assert needle in clause, needle
    for path in ("/terms", "/privacy"):                                                     # 兩頁沒有留下與之矛盾的說法
        html = client.get(path).get_data(as_text=True)
        assert "管理員不會刪除" not in html and "管理員無法刪除" not in html, path


def test_the_admin_deletion_clause_makes_no_promise_about_a_warning_a_notice_or_a_way_back(client):
    clause = terms_section_four(client)
    for forbidden in ("事先通知", "會先警告", "可以申訴後恢復", "可恢復", "可復原。"):
        assert forbidden not in clause, forbidden


def test_the_age_number_in_the_terms_retention_sentence_comes_from_the_age_policy(client, monkeypatch):
    monkeypatch.setattr("app.controllers.auth_page_controller.get_age_policy", lambda: type("P", (), {
        "min_register_age": 21, "adult_age": 21})())
    html = client.get("/terms").get_data(as_text=True)
    (deletion,) = [item for item in terms_section_five(client).split("<li>") if item.startswith("<strong>刪除帳號</strong>")]
    assert "未滿 21 歲而被抹除" in deletion and "未滿 18 歲而被抹除" not in html


def test_both_pages_show_the_current_terms_version_and_it_is_not_the_previous_one(client):
    for path in ("/terms", "/privacy"):
        html = client.get(path).get_data(as_text=True)
        assert f"版本：{CURRENT_TERMS_VERSION}" in html and "2026-11-19-draft-9" not in html and "2026-11-22-draft-10" not in html, path


def test_the_new_policy_text_makes_no_promise_of_future_features_and_keeps_the_draft_notice(client):
    for path in ("/terms", "/privacy"):
        html = client.get(path).get_data(as_text=True)
        for forbidden in ("將提供", "後續版本提供", "將於", "保證不會被留存", "保證不會被審閱", "保證匿名"):
            assert forbidden not in html, (path, forbidden)
        assert "草稿" in html
