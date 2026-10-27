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
