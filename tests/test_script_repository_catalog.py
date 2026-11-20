"""rpg-script-catalog 規格：`ScriptRepository.list_catalog`、`get_published_card`、`popular_tags`（資料層）。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行：關鍵字的大小寫與萬用字元、標籤與頻道篩選、
偏好分層、鍵集游標（分頁串起來等於一次取得、分頁期間上架／下架／恢復）、查詢次數固定、常用標籤的計數。
這裡的 `terms` 已是 Service 正規化後的形式（`casefold`）；解析與驗證在 `test_catalog_query`。
"""
import pytest

from app.models.script import OFFICIAL_AUTHOR_NAME, VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_REMOVED, VERSION_SUPERSEDED
from app.repositories.script_repository import ScriptRepository
from tests.support.query_counter import count_statements
from tests.support.script_data import validated
from tests.support.script_review import at, make_author, put_in_status

PREFERENCES = (None, "all", "male", "female")


@pytest.fixture()
def repo(member_session):
    return ScriptRepository(member_session)


@pytest.fixture()
def author(member_session):
    return make_author(member_session)


def live(repo, author, minutes=0, **changes):
    """上架一個劇本（發佈時間為基準時間加 `minutes` 分鐘），回傳版本。"""
    return put_in_status(repo, author, VERSION_PUBLISHED, now=at(minutes), **changes)


def titles_of(rows) -> list[str]:
    return [version.title for version, _script, _rank in rows]


def read_pages(repo, *, size, preference=None, **filters):
    """依序用游標讀完每一頁（與 Service 同一做法：取 `size + 1` 筆，以當頁最後一筆的排序鍵作為下一頁的起點），回傳 `(各頁串起來的列, 頁數)`。"""
    collected, after, pages, seen = [], None, 0, set()
    for _ in range(100):                                                   # 有上限：游標被破壞成重複同一筆時要失敗，不要卡住
        page = repo.list_catalog(after=after, preference=preference, limit=size + 1, **filters)
        shown = page[:size]
        repeated = [version.title for version, _script, _rank in shown if version.id in seen]
        assert not repeated, f"同一個版本出現在兩頁：{repeated}"
        seen.update(version.id for version, _script, _rank in shown)
        collected += shown
        pages += 1
        if len(page) <= size:
            return collected, pages
        version, _script, rank = shown[-1]
        after = (rank, version.published_at, version.id)
    raise AssertionError("游標一直給出下一頁：分頁沒有走到盡頭")


def six_channels(repo, author, member_session):
    """六個上架劇本，三個頻道各兩個（含一個舊的空值頻道）：由舊到新 甲(男) 乙(女) 丙(全) 丁(空值) 戊(男) 己(女)。"""
    plan = [("甲", "male"), ("乙", "female"), ("丙", "all"), ("丁", None), ("戊", "male"), ("己", "female")]
    made = {}
    for minutes, (title, channel) in enumerate(plan):
        extra = {"audience": channel} if channel in ("male", "female") else {}
        version = live(repo, author, minutes, title=title, **extra)
        if channel is None:
            version.audience = None                                        # 遷移之前的舊版本沒有頻道
        made[title] = version
    member_session.commit()
    return made


# ---- 集合：只含上架 ---------------------------------------------------------------------------

def test_only_published_versions_appear_in_the_catalog(repo, author, member_session):
    draft = put_in_status(repo, author, VERSION_DRAFT, title="草稿")
    pending = put_in_status(repo, author, VERSION_PENDING, title="待審")
    rejected = put_in_status(repo, author, VERSION_REJECTED, title="退回")
    removed = put_in_status(repo, author, VERSION_REMOVED, title="下架")
    superseded = put_in_status(repo, author, VERSION_SUPERSEDED, title="舊版")          # 它的第 2 版是上架的
    kept = live(repo, author, 30, title="上架")
    member_session.commit()
    rows = repo.list_catalog(limit=24)
    shown = {version.id for version, _, _ in rows}
    assert kept.id in shown and not shown & {draft.id, pending.id, rejected.id, removed.id, superseded.id}
    assert all(version.status == VERSION_PUBLISHED for version, _, _ in rows) and len(rows) == 2          # 上架的與舊版的第 2 版


def test_a_removed_version_is_gone_even_when_it_matches_every_filter(repo, author, member_session):
    put_in_status(repo, author, VERSION_REMOVED, title="青雲劍", tags=["仙俠"], audience="male")
    member_session.commit()
    assert repo.list_catalog(terms=["青雲"], tag="仙俠", audience="male") == []


def test_nothing_published_is_an_empty_list(repo, member_session):
    assert repo.list_catalog() == [] and repo.popular_tags() == []


def test_every_declared_mode_is_listed_because_the_catalog_never_filters_by_mode(repo, author, member_session):
    for minutes, tier in enumerate(("safe", "adult")):
        live(repo, author, minutes, title=tier, content_tier=tier)
    member_session.commit()
    assert titles_of(repo.list_catalog()) == ["adult", "safe"]


def test_a_row_carries_its_script_with_the_pen_name_and_the_builtin_is_signed_as_official(repo, author, member_session):
    mine = live(repo, author, 0, title="玩家劇本")
    seed = validated(title="內建劇本")
    repo.publish_builtin("catalog-seed", content=seed.content, content_hash=seed.content_hash, total_chars=seed.total_chars, now=at(1))
    member_session.commit()
    by_title = {version.title: script for version, script, _ in repo.list_catalog()}
    assert (by_title["玩家劇本"].author_name, by_title["玩家劇本"].author_code) == (f"筆名{author.id}", "a1b2c3")
    assert (by_title["內建劇本"].author_name, by_title["內建劇本"].author_code) == (OFFICIAL_AUTHOR_NAME, None)
    assert all(script.id == version.script_id for version, script, _ in repo.list_catalog()) and mine.id


# ---- 順序 -----------------------------------------------------------------------------------

def test_the_default_order_is_newest_first_and_the_same_instant_breaks_ties_by_the_newer_version(repo, author, member_session):
    live(repo, author, 0, title="最舊")
    for name in ("同刻一", "同刻二", "同刻三"):
        live(repo, author, 5, title=name)
    live(repo, author, 10, title="最新")
    member_session.commit()
    assert titles_of(repo.list_catalog()) == ["最新", "同刻三", "同刻二", "同刻一", "最舊"]


def test_the_limit_caps_the_page(repo, author, member_session):
    for minutes in range(5):
        live(repo, author, minutes, title=f"第{minutes}個")
    member_session.commit()
    assert titles_of(repo.list_catalog(limit=2)) == ["第4個", "第3個"]


@pytest.mark.parametrize("preference, expected, ranks", [
    ("male", ["戊", "甲", "丁", "丙", "己", "乙"], [0, 0, 1, 1, 2, 2]),
    ("female", ["己", "乙", "丁", "丙", "戊", "甲"], [0, 0, 1, 1, 2, 2]),
    ("all", ["己", "戊", "丁", "丙", "乙", "甲"], [0] * 6),
    (None, ["己", "戊", "丁", "丙", "乙", "甲"], [0] * 6),
    ("boys", ["己", "戊", "丁", "丙", "乙", "甲"], [0] * 6),                  # 無法辨識的偏好不重排
])
def test_the_preferred_channel_comes_first_then_everyone_then_the_other_channel_and_the_rank_is_returned(
    repo, author, member_session, preference, expected, ranks,
):
    six_channels(repo, author, member_session)
    rows = repo.list_catalog(preference=preference, limit=24)
    assert titles_of(rows) == expected and [rank for _, _, rank in rows] == ranks


def test_the_preference_never_changes_which_versions_are_listed(repo, author, member_session):
    six_channels(repo, author, member_session)
    assert {frozenset(titles_of(repo.list_catalog(preference=p, limit=24))) for p in PREFERENCES} == {frozenset("甲乙丙丁戊己")}


# ---- 關鍵字：只比對標題、簡介、世界名稱與標籤 ---------------------------------------------------

def test_a_keyword_matches_the_title_the_summary_the_world_name_and_a_tag(repo, author, member_session):
    live(repo, author, 0, title="青雲劍訣", summary="普通簡介", world_name="普通世界", tags=["普通"])
    live(repo, author, 1, title="標題二", summary="少年與青雲的故事", world_name="普通世界", tags=["普通"])
    live(repo, author, 2, title="標題三", summary="普通簡介", world_name="青雲大陸", tags=["普通"])
    live(repo, author, 3, title="標題四", summary="普通簡介", world_name="普通世界", tags=["青雲志"])
    live(repo, author, 4, title="都不符合", summary="普通簡介", world_name="普通世界", tags=["普通"])
    member_session.commit()
    assert titles_of(repo.list_catalog(terms=["青雲"])) == ["標題四", "標題三", "標題二", "青雲劍訣"]


def test_a_keyword_never_matches_the_body_characters_locations_milestones_opening_or_rules(repo, author, member_session):
    live(
        repo, author, 0, title="無關標題", summary="無關簡介", world_name="無關世界", tags=["無關"],
        background="祕境青雲圖在此", opening="青雲開場", rules="青雲規則", locations=["青雲山"],
        characters=[{"name": "青雲子", "personality": "青雲", "age": 30, "appearance": "青雲外貌", "occupation": "青雲職業"}],
        milestones=[{"title": "青雲大會", "trigger": {"kind": "age", "value": 20}}],
    )
    member_session.commit()
    assert repo.list_catalog(terms=["青雲"]) == []
    assert titles_of(repo.list_catalog(terms=["無關"])) == ["無關標題"]                # 同一份劇本，一般欄位的詞當然找得到


def test_a_keyword_never_matches_json_columns_even_when_the_text_is_plain_ascii(repo, author, member_session):
    """JSON 欄位把中文存成 `\\uXXXX` 跳脫字串，所以上一個測試的中文詞即使有人把 JSON 欄位加進比對也找不到（會漏判）；
    這裡在角色、地點與里程碑放 ASCII 標記字串，才看得出「比對了 JSON 欄位」。"""
    live(
        repo, author, 0, title="無關標題", summary="無關簡介", world_name="無關世界", tags=["無關"],
        locations=["zzjsonmarker place"],
        characters=[{"name": "zzjsonmarker name", "personality": "zzjsonmarker", "age": 30, "appearance": "zzjsonmarker look"}],
        milestones=[{"title": "zzjsonmarker milestone", "trigger": {"kind": "age", "value": 20}}],
    )
    live(repo, author, 1, title="zzjsonmarker 在標題", summary="無關簡介", world_name="無關世界", tags=["無關"])
    member_session.commit()
    assert titles_of(repo.list_catalog(terms=["zzjsonmarker"])) == ["zzjsonmarker 在標題"]          # 只有一般欄位（標題）找得到


def test_every_term_has_to_match_and_the_fields_may_differ_between_terms(repo, author, member_session):
    live(repo, author, 0, title="青雲劍", world_name="某世界", tags=["仙俠"])             # 預設的世界名稱也含「仙俠」，所以這裡明確改掉
    live(repo, author, 1, title="青雲錄", world_name="某世界", tags=["都市"])
    live(repo, author, 2, title="江湖行", world_name="某世界", tags=["仙俠"])
    member_session.commit()
    assert titles_of(repo.list_catalog(terms=["青雲", "仙俠"])) == ["青雲劍"]           # 一個詞在標題、一個詞在標籤
    assert titles_of(repo.list_catalog(terms=["青雲"])) == ["青雲錄", "青雲劍"]
    assert repo.list_catalog(terms=["青雲", "仙俠", "不存在"]) == []


def test_keywords_ignore_case_in_ascii_titles_and_tags(repo, author, member_session):
    live(repo, author, 0, title="Neon City", tags=["SciFi"])
    live(repo, author, 1, title="Other", tags=["其他"])
    member_session.commit()
    assert titles_of(repo.list_catalog(terms=["scifi"])) == ["Neon City"]          # 標籤 SciFi，詞是 scifi
    assert titles_of(repo.list_catalog(terms=["neon"])) == ["Neon City"]
    assert titles_of(repo.list_catalog(terms=["city"])) == ["Neon City"]


def test_a_keyword_is_a_substring_match_not_a_whole_word_match(repo, author, member_session):
    live(repo, author, 0, title="跨越時空的青雲少年")
    member_session.commit()
    assert titles_of(repo.list_catalog(terms=["時空的青"])) == ["跨越時空的青雲少年"]


@pytest.mark.parametrize("term, matching, others", [
    ("%", "百分百%熱血", ["普通劇本", "一千一百"]),                       # `%` 是一般字元：沒有標題含 `%` 時結果為空，不是全部
    ("_", "底線_劇本", ["普通劇本", "一千一百"]),
    ("100%", "滿分100%", ["100分", "1000"]),                          # `100%` 不會當成「100 開頭的任何字串」
    ("[abc]", "括號[abc]劇", ["abc劇", "a", "b"]),                       # SQL Server 的 `[…]` 字元集合要被跳脫
    ("[", "半個[括號", ["普通劇本", "一千一百"]),
    ("\\", "斜線\\劇本", ["普通劇本", "一千一百"]),
    ("a_c", "a_c劇本", ["abc劇本", "axc劇本"]),
    ("%%", "雙%%百分", ["單%百分", "普通劇本"]),
])
def test_wildcards_and_the_escape_character_are_matched_literally(repo, author, member_session, term, matching, others):
    for minutes, title in enumerate([matching, *others]):
        live(repo, author, minutes, title=title)
    member_session.commit()
    assert titles_of(repo.list_catalog(terms=[term])) == [matching]


def test_a_wildcard_with_no_literal_match_gives_an_empty_page_not_the_whole_catalog(repo, author, member_session):
    live(repo, author, 0, title="普通劇本")
    live(repo, author, 1, title="另一個")
    member_session.commit()
    assert repo.list_catalog(terms=["%"]) == [] and repo.list_catalog(terms=["_"]) == [] and repo.list_catalog(terms=["[a-z]"]) == []


def test_a_wildcard_in_a_tag_is_literal_too(repo, author, member_session):
    live(repo, author, 0, title="有標籤", tags=["50%"])
    live(repo, author, 1, title="沒標籤", tags=["500"])
    member_session.commit()
    assert titles_of(repo.list_catalog(terms=["50%"])) == ["有標籤"]


def test_no_terms_and_no_filters_list_everything(repo, author, member_session):
    for minutes in range(3):
        live(repo, author, minutes, title=f"第{minutes}個")
    member_session.commit()
    assert len(repo.list_catalog(terms=(), tag=None, audience=None)) == 3


# ---- 標籤與讀者頻道 ---------------------------------------------------------------------------

def test_the_tag_filter_is_a_whole_tag_match_that_ignores_case(repo, author, member_session):
    live(repo, author, 0, title="甲", tags=["仙俠", "成長"])
    live(repo, author, 1, title="乙", tags=["仙俠傳奇"])
    live(repo, author, 2, title="丙", tags=["SciFi"])
    live(repo, author, 3, title="丁", tags=["都市"])
    member_session.commit()
    assert titles_of(repo.list_catalog(tag="仙俠")) == ["甲"]                       # 「仙俠傳奇」不是「仙俠」
    assert titles_of(repo.list_catalog(tag="成長")) == ["甲"]
    assert titles_of(repo.list_catalog(tag="scifi")) == ["丙"]
    assert repo.list_catalog(tag="仙") == []


def test_the_channel_filter_selects_one_channel_and_all_means_everyone_including_the_legacy_empty_value(repo, author, member_session):
    six_channels(repo, author, member_session)
    assert titles_of(repo.list_catalog(audience="male")) == ["戊", "甲"]
    assert titles_of(repo.list_catalog(audience="female")) == ["己", "乙"]
    assert titles_of(repo.list_catalog(audience="all")) == ["丁", "丙"]              # 丙是明確的 all，丁是舊的空值
    assert len(repo.list_catalog(audience=None, limit=24)) == 6


def test_keyword_tag_and_channel_combine_with_and(repo, author, member_session):
    live(repo, author, 0, title="青雲甲", tags=["仙俠"], audience="male")
    live(repo, author, 1, title="青雲乙", tags=["仙俠"], audience="female")
    live(repo, author, 2, title="青雲丙", tags=["都市"], audience="male")
    live(repo, author, 3, title="江湖丁", tags=["仙俠"], audience="male")
    member_session.commit()
    assert titles_of(repo.list_catalog(terms=["青雲"], tag="仙俠", audience="male")) == ["青雲甲"]
    assert titles_of(repo.list_catalog(terms=["青雲"], tag="仙俠")) == ["青雲乙", "青雲甲"]


def test_the_filters_coexist_with_the_preference_order(repo, author, member_session):
    for minutes, (title, channel) in enumerate([("青雲一", "male"), ("青雲二", "female"), ("青雲三", "all"), ("江湖", "male")]):
        live(repo, author, minutes, title=title, **({"audience": channel} if channel != "all" else {}))
    member_session.commit()
    assert titles_of(repo.list_catalog(terms=["青雲"], preference="female", limit=24)) == ["青雲二", "青雲三", "青雲一"]


# ---- 游標：串起來等於一次取得 --------------------------------------------------------------------------

@pytest.mark.parametrize("preference", PREFERENCES)
@pytest.mark.parametrize("size", [1, 2, 3, 5, 6, 7])
def test_chained_cursor_pages_equal_one_full_read(repo, author, member_session, preference, size):
    six_channels(repo, author, member_session)
    whole = repo.list_catalog(preference=preference, limit=24)
    paged, pages = read_pages(repo, size=size, preference=preference)
    assert [v.id for v, _, _ in paged] == [v.id for v, _, _ in whole] and len(whole) == 6
    assert pages == -(-6 // size)                                                   # 取 size + 1 筆判斷有無下一頁，所以剛好整除時不會多出空頁


def test_the_pages_chain_across_a_tie_of_the_published_time(repo, author, member_session):
    for name in ("一", "二", "三", "四", "五"):
        live(repo, author, 0, title=name)                                       # 全部同一刻：只靠編號分得出先後
    member_session.commit()
    paged, _ = read_pages(repo, size=2)
    assert titles_of(paged) == ["五", "四", "三", "二", "一"]


@pytest.mark.parametrize("preference", PREFERENCES)
def test_chained_cursor_pages_keep_the_filters(repo, author, member_session, preference):
    for minutes in range(6):
        live(repo, author, minutes, title=f"青雲{minutes}" if minutes % 2 == 0 else f"江湖{minutes}", audience="male" if minutes < 4 else "female")
    member_session.commit()
    whole = repo.list_catalog(terms=["青雲"], preference=preference, limit=24)
    paged, _ = read_pages(repo, size=1, preference=preference, terms=["青雲"])
    assert titles_of(paged) == titles_of(whole) and len(whole) == 3


def test_a_page_of_one_follows_male_then_everyone_then_female_with_each_listed_once(repo, author, member_session):
    live(repo, author, 0, title="女頻", audience="female")
    live(repo, author, 1, title="全讀者")
    live(repo, author, 2, title="男頻", audience="male")
    member_session.commit()
    paged, _ = read_pages(repo, size=1, preference="male")
    assert titles_of(paged) == ["男頻", "全讀者", "女頻"]


def test_a_cursor_from_a_tiered_list_is_ignored_for_the_tier_when_the_preference_became_neutral(repo, author, member_session):
    """玩家在翻頁之間改了偏好：不分層時只看發佈時間與編號，不會因為游標帶著舊的偏好層而得到空頁。"""
    six_channels(repo, author, member_session)
    first = repo.list_catalog(preference="female", limit=2)
    assert titles_of(first) == ["己", "乙"]
    version, _, rank = first[-1]
    assert rank == 0
    after = (2, version.published_at, version.id)                                # 偏好層 2 對現在不分層的清單沒有意義
    assert titles_of(repo.list_catalog(after=after, preference=None, limit=24)) == ["甲"]          # 只看發佈時間與編號：比乙更早的只有甲


def test_a_cursor_past_the_last_item_gives_an_empty_page(repo, author, member_session):
    oldest = live(repo, author, 0, title="唯一")
    member_session.commit()
    assert repo.list_catalog(after=(0, oldest.published_at, oldest.id)) == []


# ---- 游標：分頁期間的變動 ---------------------------------------------------------------------------

def make_five(repo, author, member_session):
    made = [live(repo, author, minutes, title=f"第{minutes}個") for minutes in range(5)]
    member_session.commit()
    return made                                                                 # 由舊到新；一次取得的順序是 4 3 2 1 0


def first_page(repo, size=2):
    page = repo.list_catalog(limit=size + 1)[:size]
    version, _, rank = page[-1]
    return page, (rank, version.published_at, version.id)


def test_a_newer_script_published_between_pages_does_not_show_up_again_on_the_next_page(repo, author, member_session):
    make_five(repo, author, member_session)
    page, after = first_page(repo)
    assert titles_of(page) == ["第4個", "第3個"]
    live(repo, author, 99, title="新上架")
    member_session.commit()
    assert titles_of(repo.list_catalog(after=after, limit=24)) == ["第2個", "第1個", "第0個"]


def test_an_older_script_published_between_pages_appears_at_its_place_on_the_next_page(repo, author, member_session):
    make_five(repo, author, member_session)
    _, after = first_page(repo)
    live(repo, author, -5, title="補上架的舊劇本")                                   # 發佈時間比所有已讀的都早
    member_session.commit()
    assert titles_of(repo.list_catalog(after=after, limit=24)) == ["第2個", "第1個", "第0個", "補上架的舊劇本"]


def test_a_script_taken_down_from_the_first_page_does_not_disturb_the_next_page(repo, author, member_session):
    made = make_five(repo, author, member_session)
    _, after = first_page(repo)
    repo.remove_by_admin(made[4].public_id, "other_policy", "備註", at(200))
    member_session.commit()
    assert titles_of(repo.list_catalog(after=after, limit=24)) == ["第2個", "第1個", "第0個"]


def test_a_script_taken_down_from_a_later_page_simply_disappears_from_it(repo, author, member_session):
    made = make_five(repo, author, member_session)
    _, after = first_page(repo)
    repo.remove_by_admin(made[1].public_id, "other_policy", "備註", at(200))
    member_session.commit()
    assert titles_of(repo.list_catalog(after=after, limit=24)) == ["第2個", "第0個"]


def test_a_restored_script_comes_back_at_its_original_place_because_restoring_keeps_the_published_time(repo, author, member_session):
    made = make_five(repo, author, member_session)
    owner = author
    repo.remove_own(owner.id, made[1].public_id, at(100))
    member_session.commit()
    _, after = first_page(repo)
    assert titles_of(repo.list_catalog(after=after, limit=24)) == ["第2個", "第0個"]
    repo.restore_own(owner.id, made[1].public_id)
    member_session.commit()
    assert titles_of(repo.list_catalog(limit=24)) == ["第4個", "第3個", "第2個", "第1個", "第0個"]      # 回到原本的位置
    assert titles_of(repo.list_catalog(after=after, limit=24)) == ["第2個", "第1個", "第0個"]            # 在已讀位置之後所以出現在下一頁
    assert repo.get_published_card(made[1].public_id) is not None


def test_a_restored_script_that_was_already_read_past_is_not_repeated(repo, author, member_session):
    made = make_five(repo, author, member_session)
    page, after = first_page(repo)
    repo.remove_own(author.id, made[4].public_id, at(100))                       # 第一頁讀完後，第一頁的劇本被下架又恢復
    repo.restore_own(author.id, made[4].public_id)
    member_session.commit()
    assert titles_of(repo.list_catalog(after=after, limit=24)) == ["第2個", "第1個", "第0個"] and titles_of(page)[0] == "第4個"


# ---- 單一版本與標籤 -----------------------------------------------------------------------------

def test_a_published_card_is_the_version_with_its_script_and_anything_else_is_none(repo, author, member_session):
    kept = live(repo, author, 0, title="上架")
    draft = put_in_status(repo, author, VERSION_DRAFT, title="草稿")
    removed = put_in_status(repo, author, VERSION_REMOVED, title="下架")
    pending = put_in_status(repo, author, VERSION_PENDING, title="待審")
    member_session.commit()
    version, script = repo.get_published_card(kept.public_id)
    assert (version.id, script.id, script.author_code) == (kept.id, kept.script_id, "a1b2c3")
    assert [repo.get_published_card(other.public_id) for other in (draft, removed, pending)] == [None, None, None]
    assert repo.get_published_card("00000000-0000-4000-8000-000000000000") is None and repo.get_published_card("not-a-uuid") is None


def test_popular_tags_count_only_published_versions_and_merge_case(repo, author, member_session):
    live(repo, author, 0, title="一", tags=["仙俠", "SciFi"])
    live(repo, author, 1, title="二", tags=["仙俠", "scifi"])
    live(repo, author, 2, title="三", tags=["都市"])
    put_in_status(repo, author, VERSION_REMOVED, title="下架", tags=["仙俠", "已下架"])
    put_in_status(repo, author, VERSION_DRAFT, title="草稿", tags=["仙俠", "草稿"])
    put_in_status(repo, author, VERSION_PENDING, title="待審", tags=["仙俠", "待審"])
    put_in_status(repo, author, VERSION_REJECTED, title="退回", tags=["仙俠", "退回"])
    member_session.commit()
    tags = dict(repo.popular_tags())
    assert tags == {"仙俠": 2, "scifi": 2, "都市": 1}
    assert not set(tags) & {"已下架", "草稿", "待審", "退回"}


def test_popular_tags_are_ordered_by_count_then_by_tag(repo, author, member_session):
    live(repo, author, 0, title="一", tags=["delta", "alpha"])
    live(repo, author, 1, title="二", tags=["delta", "charlie"])
    live(repo, author, 2, title="三", tags=["delta", "bravo"])
    live(repo, author, 3, title="四", tags=["charlie"])
    member_session.commit()
    assert repo.popular_tags() == [("delta", 3), ("charlie", 2), ("alpha", 1), ("bravo", 1)]


def test_popular_tags_return_at_most_the_limit_and_the_default_is_thirty(repo, author, member_session):
    for index in range(20):
        live(repo, author, index, title=f"劇本{index}", tags=[f"標{2 * index:02d}", f"標{2 * index + 1:02d}"])       # 40 種標籤
    member_session.commit()
    assert len(repo.popular_tags()) == 30 and len(repo.popular_tags(limit=5)) == 5 and len(repo.popular_tags(limit=100)) == 40
    assert all(count == 1 for _, count in repo.popular_tags())


def test_a_taken_down_script_stops_counting_and_a_restored_one_counts_again(repo, author, member_session):
    version = live(repo, author, 0, title="一", tags=["仙俠"])
    member_session.commit()
    assert repo.popular_tags() == [("仙俠", 1)]
    repo.remove_own(author.id, version.public_id, at(10))
    member_session.commit()
    assert repo.popular_tags() == []
    repo.restore_own(author.id, version.public_id)
    member_session.commit()
    assert repo.popular_tags() == [("仙俠", 1)]


# ---- 查詢次數 -------------------------------------------------------------------------------------

def test_a_catalog_page_costs_two_queries_whether_it_holds_one_row_or_twelve(repo, author, member_session):
    def page_statements() -> int:
        member_session.expire_all()
        with count_statements(member_session) as statements:
            rows = repo.list_catalog(terms=["劇本"], tag="標籤", preference="male", limit=24)
            tags = repo.tags_of_versions([version.id for version, _, _ in rows])
            for version, script, rank in rows:                                # 卡片需要的欄位全部讀一遍：沒有任何延遲載入的查詢
                _ = (version.public_id, version.title, version.summary, version.world_name, version.content_tier, version.audience,
                     script.author_name, script.author_code, rank, tags[version.id])
        return len(statements)

    live(repo, author, 0, title="劇本零", tags=["標籤", "乙", "丙"])
    member_session.commit()
    one = page_statements()
    for index in range(11):
        live(repo, author, index + 1, title=f"劇本{index + 1}", tags=["標籤", "乙", "丙"])
    member_session.commit()
    assert len(repo.list_catalog(limit=24)) == 12
    assert one == page_statements() == 2


def test_the_popular_tags_cost_one_query(repo, author, member_session):
    for index in range(5):
        live(repo, author, index, title=f"劇本{index}", tags=["甲", "乙"])
    member_session.commit()
    member_session.expire_all()
    with count_statements(member_session) as statements:
        repo.popular_tags()
    assert len(statements) == 1


def test_a_published_card_costs_one_query(repo, author, member_session):
    public_id = live(repo, author, 0, title="一").public_id
    member_session.commit()
    member_session.expire_all()
    with count_statements(member_session) as statements:
        found = repo.get_published_card(public_id)
        assert found is not None and (found[0].title, found[1].author_code) == ("一", "a1b2c3")
    assert len(statements) == 1


# ---- 資料層不寫任何東西 ----------------------------------------------------------------------------

def test_the_catalog_reads_write_nothing(repo, author, member_session):
    version = live(repo, author, 0, title="一", tags=["甲"])
    member_session.commit()
    before = {k: getattr(version, k) for k in ("status", "published_at", "decision_category", "removed_by", "submitted_at")}
    repo.list_catalog(terms=["一"], tag="甲", audience="all", limit=5)
    repo.popular_tags()
    repo.get_published_card(version.public_id)
    assert not member_session.dirty and not member_session.new
    member_session.expire_all()
    assert {k: getattr(version, k) for k in before} == before


def test_a_restored_published_time_is_unchanged_by_the_takedown_round_trip(repo, author, member_session):
    version = live(repo, author, 0, title="一")
    member_session.commit()
    published_at = version.published_at
    repo.remove_own(author.id, version.public_id, at(5))
    repo.restore_own(author.id, version.public_id)
    member_session.commit()
    member_session.expire_all()
    assert repo.get_published_card(version.public_id)[0].published_at == published_at
