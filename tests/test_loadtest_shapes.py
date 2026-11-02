"""load-testing 規格「階梯式加壓」：各階段的使用者數與持續時間、結束條件、設定不合法時的處理。

`loadtest/shapes.py` 是純邏輯，不匯入 Locust（匯入 Locust 會對標準函式庫做 gevent 猴子補丁，不能發生在 pytest 行程裡）。
"""
import ast
import math
import subprocess
import sys

import pytest

from app.config import PROJECT_ROOT
from loadtest import shapes
from loadtest.shapes import StairConfigError, StairShape, parse_stairs


def shape(**overrides) -> StairShape:
    values = dict(start_users=20, step_users=20, steps=5, step_seconds=120)
    values.update(overrides)
    return StairShape(**values)


# ---- 階梯：逐步增加使用者數、每階段持續時間相同 ---------------------------------------------------


def test_the_users_rise_step_by_step_and_every_stage_lasts_the_same_time():
    stages = shape().stages
    assert [stage.users for stage in stages] == [20, 40, 60, 80, 100]
    assert [stage.index for stage in stages] == [1, 2, 3, 4, 5]
    assert {stage.seconds for stage in stages} == {120.0}
    assert shape().total_seconds == 600 and shape().peak_users == 100


def test_tick_returns_the_users_for_the_stage_the_elapsed_time_falls_in():
    s = shape(start_users=10, step_users=5, steps=3, step_seconds=60)
    assert s.tick(0)[0] == 10 and s.tick(59.9)[0] == 10
    assert s.tick(60)[0] == 15 and s.tick(119.9)[0] == 15
    assert s.tick(120)[0] == 20 and s.tick(179.9)[0] == 20


def test_the_run_ends_after_the_last_stage():
    s = shape(steps=3, step_seconds=30)
    assert s.tick(89.9) is not None
    assert s.tick(90) is None and s.tick(10_000) is None


def test_new_users_are_ramped_in_not_added_all_at_once():
    stages = shape(start_users=20, step_users=20, step_seconds=120).stages
    assert all(stage.spawn_rate >= 1 for stage in stages)
    assert stages[1].spawn_rate == pytest.approx(20 / 10)             # 每階段開頭最多用 10 秒把新增的 20 位加進來
    short = shape(step_users=20, step_seconds=10).stages
    assert short[1].spawn_rate == pytest.approx(20 / 5)               # 階段很短時，加入時間不超過階段的一半


@pytest.mark.parametrize("elapsed", [-1, math.inf, math.nan])
def test_a_nonsensical_elapsed_time_is_refused(elapsed):
    with pytest.raises(ValueError):
        shape().tick(elapsed)


def test_describe_is_a_one_line_summary_with_only_numbers():
    line = shape(start_users=10, step_users=10, steps=3, step_seconds=60).describe()
    assert line == "3 個階段（虛擬使用者 10、20、30），每階段 60 秒，共 180 秒"


# ---- 設定的解析與驗證 ---------------------------------------------------------------------------


def test_the_default_spec_is_a_ten_minute_ramp_to_one_hundred_users():
    s = parse_stairs(None)
    assert (s.start_users, s.step_users, s.steps, s.step_seconds) == (20, 20, 5, 120) and parse_stairs("").peak_users == 100


def test_a_partial_spec_overrides_only_the_named_fields():
    s = parse_stairs("steps=3, seconds=45")
    assert (s.start_users, s.step_users, s.steps, s.step_seconds) == (20, 20, 3, 45)


@pytest.mark.parametrize(
    "spec, field, leaked",
    [
        ("start=0", "start", None),
        ("step=-5", "step", None),
        ("steps=1", "steps", None),
        ("seconds=5", "seconds", None),
        ("start=abc", "start", "abc"),
        ("seconds=lots", "seconds", "lots"),
        ("seconds=inf", "seconds", "inf"),
        ("seconds=nan", "seconds", "nan"),
        ("start=4000,step=1000,steps=3", "最高", None),
        ("users=50", "LOADTEST_STAIRS", "users=50"),
        ("start", "LOADTEST_STAIRS", None),
    ],
)
def test_an_invalid_spec_is_refused_with_the_field_rule_and_without_echoing_the_value(spec, field, leaked):
    with pytest.raises(StairConfigError) as error:
        parse_stairs(spec)
    assert field in str(error.value)
    if leaked:
        assert leaked not in str(error.value)


def test_the_environment_variable_is_the_source():
    assert shapes.stairs_from_environment({"LOADTEST_STAIRS": "start=5,step=5,steps=2,seconds=10"}).peak_users == 10
    assert shapes.stairs_from_environment({}).peak_users == 100


# ---- 不匯入 Locust，薄包裝在另一個檔案 -------------------------------------------------------------


def test_the_shape_module_never_imports_locust_or_the_application():
    tree = ast.parse((PROJECT_ROOT / "loadtest" / "shapes.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"locust", "gevent", "app", "flask"}, imported
    assert "locust" not in sys.modules and "gevent" not in sys.modules


def test_the_locust_wrapper_exists_and_only_delegates_to_the_shape():
    source = (PROJECT_ROOT / "loadtest" / "locustfile_stairs.py").read_text(encoding="utf-8")
    assert "LoadTestShape" in source and "stairs_from_environment" in source and "STAIRS.tick(self.get_run_time())" in source


def test_the_wrapper_refuses_an_invalid_spec_in_a_subprocess_with_a_clear_message():
    """在子行程執行：匯入 Locust 會對標準函式庫做 gevent 猴子補丁，不能發生在 pytest 行程裡。"""
    import os

    environ = {**os.environ, "LOADTEST_STAIRS": "steps=1", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [sys.executable, "-c", "import loadtest.locustfile_stairs"], cwd=PROJECT_ROOT, env=environ, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode != 0 and "階梯設定不合法" in result.stderr and "steps" in result.stderr
