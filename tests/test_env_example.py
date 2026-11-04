""".env.example 的測試：必要變數都列出，且沒有任何實際值。"""
import re
from pathlib import Path

from app.config import PROJECT_ROOT, REQUIRED_SECRETS

ENV_EXAMPLE = PROJECT_ROOT / ".env.example"


def parse_assignments() -> dict[str, str]:
    result: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def test_lists_every_required_variable():
    names = parse_assignments()
    for required in ("APP_ENV", *REQUIRED_SECRETS):
        assert required in names, f"{required} 未列在 .env.example"


AUTH_VARIABLES = (
    "SECRET_KEY_FALLBACKS", "BASE_URL",
    "MAIL_SMTP_HOST", "MAIL_SMTP_PORT", "MAIL_SMTP_SECURITY", "MAIL_SMTP_USER", "MAIL_SMTP_PASSWORD", "MAIL_FROM",
    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
)


def test_lists_every_member_auth_variable():
    names = parse_assignments()
    for name in AUTH_VARIABLES:
        assert name in names, f"{name} 未列在 .env.example"


def test_does_not_list_the_retired_email_verification_variable():
    """玩家只用 Google 登入，信箱驗證開關已淘汰（app-configuration 規格「已淘汰的信箱驗證開關被忽略」）。"""
    assert "EMAIL_VERIFICATION_REQUIRED" not in parse_assignments()


def test_lists_the_adult_mode_variables_and_no_longer_the_old_level_3_ones():
    """內容分級改為三種模式：`ADULT_MODE_*` 取代 `SEXUAL_CONTENT_LEVEL3_*`（content-tier-modes 規格「已淘汰的環境變數」）。"""
    names = parse_assignments()
    assert {"ADULT_MODE_ENABLED", "ADULT_MODE_MODELS"} <= set(names)
    assert not {"SEXUAL_CONTENT_LEVEL3_ENABLED", "SEXUAL_CONTENT_LEVEL3_MODELS"} & set(names)
    assert names["ADULT_MODE_ENABLED"] == "" and names["ADULT_MODE_MODELS"] == ""          # 預設不開啟，範例不帶任何值


def test_every_variable_the_config_reads_is_listed():
    """app/config.py 讀取的每個環境變數名稱，都必須出現在 .env.example（避免新增設定卻忘了列出）。"""
    source = (PROJECT_ROOT / "app" / "config.py").read_text(encoding="utf-8-sig")
    # 任何「函式(values, "NAME"」的形式都算（_get、_model_name、_seconds、_bool_setting、_openai_model_name…），避免新的解析函式漏掃
    read_by_config = set(re.findall(r'(?:\w+\(values, |values\.get\(|environ\.get\()"([A-Z][A-Z0-9_]+)"', source))
    read_by_config -= {"APP_ENV"}  # 由行程環境提供，.env.example 另有說明
    assert read_by_config, "沒有掃到任何變數，掃描規則需要更新"
    missing = read_by_config - set(parse_assignments())
    assert not missing, f".env.example 缺少：{sorted(missing)}"


def test_has_no_actual_values():
    for name, value in parse_assignments().items():
        # 只允許空值，或以 <...> 包住的佔位符
        assert value == "" or re.fullmatch(r"<[^<>]+>", value), f"{name} 含有實際值"


def test_connection_string_examples_use_placeholders_only():
    text = ENV_EXAMPLE.read_text(encoding="utf-8-sig")
    for match in re.finditer(r"://([^/\s@]+)@", text):
        credentials = match.group(1)
        assert re.fullmatch(r"<[^<>]+>:<[^<>]+>", credentials), "連線字串範例含有實際帳密"


def test_env_example_is_not_ignored_but_env_is():
    gitignore = Path(PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore.splitlines()
    assert "!.env.example" in gitignore.splitlines()
