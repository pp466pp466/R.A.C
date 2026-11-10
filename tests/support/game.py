"""RPG 回合引擎測試共用的組裝：真的 Repository、真的錢包與設定表，只有時鐘與「全站預算是否用盡」是假的。

`kit.game` 是被測的 `GameService`；`kit.player(n)` 建立已完成年齡確認的玩家（第一次存取錢包時依規範補發 200 幣）；
`kit.open_save(user)` 開一個第 0 幕已完成（`ready`）的存檔，讓測試可以直接開始回合。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.models import RpgSession
from app.models.rpg_session import OPENING_READY
from app.repositories.runtime_limit_repository import RuntimeLimitRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.wallet_repository import WalletRepository
from app.services.access_policy import AccessPolicy
from app.services.current_user import CurrentUser
from app.services.game_service import GameService
from app.services.runtime_limits import RuntimeLimits
from app.services.wallet_service import WalletService
from tests.support.auth import FakeClock, add_user
from tests.support.protagonist import protagonist
from tests.support.script_data import validated

MARKER = "rpgtestmarker0123456789ab"        # 與真實標記一樣只含英數字（審查比對前會去除標點）
MODEL = "rpg-primary-model"


class GameKit:
    def __init__(self, session: Session, *, adult_mode: bool = False) -> None:
        self.session = session
        self.clock = FakeClock()
        self.sessions = SessionRepository(session)
        self.wallets = WalletRepository(session)
        self.limits = RuntimeLimits(limits=RuntimeLimitRepository(session), clock=self.clock)
        self.wallet = WalletService(wallets=self.wallets, limits=self.limits, clock=self.clock)
        self.policy = AccessPolicy(clock=self.clock, adult_mode_enabled=adult_mode)
        self.budget_exhausted = False
        self.game = GameService(
            sessions=self.sessions, wallet=self.wallet, policy=self.policy, limits=self.limits,
            budget_exhausted=lambda: self.budget_exhausted, marker=MARKER, model=MODEL, clock=self.clock,
        )

    # ---- 人與存檔 -----------------------------------------------------------------------

    def player(self, n: int = 1, **overrides: Any) -> CurrentUser:
        return CurrentUser.from_model(add_user(self.session, n, password=None, **overrides))

    def open_save(self, user: CurrentUser, *, content: dict[str, Any] | None = None, tier: str = "safe", **protagonist_changes: Any) -> RpgSession:
        """開一個存檔並把第 0 幕設為 `ready`（文字為「開場」）；`content` 預設為標準的測試劇本。"""
        snapshot = content if content is not None else validated().content
        save = self.sessions.create(
            user.id, source_version_id=None, snapshot=snapshot, snapshot_spec_version=1,
            protagonist=protagonist(**protagonist_changes), content_tier=tier, now=self.clock(),
        )
        self.finish_opening(user, save, "開場的劇情。")
        return save

    def finish_opening(self, user: CurrentUser, save: RpgSession, text: str, state: str = OPENING_READY) -> None:
        claimed = self.sessions.claim_opening(user.id, save.public_id, now=self.clock())
        assert claimed is not None and claimed[0] == "claimed"
        assert self.sessions.finish_opening(user.id, save.public_id, claimed_at=self.clock(), state=state, text=text)

    # ---- 設定 ---------------------------------------------------------------------------

    def set_limit(self, key: str, value: int) -> None:
        RuntimeLimitRepository(self.session).set_value(key, value, self.clock())
        self.session.flush()

    # ---- 觀察 ---------------------------------------------------------------------------

    def balances(self, user: CurrentUser) -> tuple[int, int]:
        state = self.wallets.state(user.id)
        return (0, 0) if state is None else (state.free_balance, state.paid_balance)

    def total(self, user: CurrentUser) -> int:
        free, paid = self.balances(user)
        return free + paid


# ---- 回合引擎（TurnRunner）的組裝 --------------------------------------------------------------------

FALLBACK_MODEL = "rpg-fallback-model"
STORY_SENTENCES = ("清晨的海風吹過山門。", "守門的弟子向你點了點頭。", "你踏進了霧氣之中。")
STORY = "".join(STORY_SENTENCES)
BAD_SENTENCE = "十歲的少女與他做愛。"                 # 規則層必擋（minor_sexual）


def story_script(*sentences: str, finish=True, seconds: float = 0.2):
    """一個串流腳本：每句一個片段，最後是正常結束事件。"""
    from tests.support.llm import Chunk, Finish

    items = [Chunk(sentence, seconds) for sentence in (sentences or STORY_SENTENCES)]
    if finish:
        items.append(Finish())
    return items


class RunnerKit(GameKit):
    """在 `GameKit` 之上加上 `TurnRunner` 需要的東西：假供應商與假單調時鐘、每個短交易各開新工作階段的工廠。"""

    def __init__(self, session: Session, **kwargs: Any) -> None:
        from app.config import LlmSettings
        from tests.support.llm import FakeLLMProvider, FakeMonotonic

        super().__init__(session, **kwargs)
        self.monotonic = FakeMonotonic()
        self.provider = FakeLLMProvider(self.monotonic)
        self.settings = LlmSettings(primary_model=MODEL, fallback_model=FALLBACK_MODEL)
        self.factory = sessionmaker(bind=session.get_bind())
        self.state_available = True

    def wallet_for(self, session: Session) -> WalletService:
        limits = RuntimeLimits(limits=RuntimeLimitRepository(session), clock=self.clock)
        return WalletService(wallets=WalletRepository(session), limits=limits, clock=self.clock)

    def llm_for(self, session: Session):
        from app.repositories.llm_call_log_repository import LlmCallLogRepository
        from app.services.llm_service import LlmService

        return LlmService(
            provider=self.provider, ledger=LlmCallLogRepository(session), settings=self.settings, clock=self.clock, release=session.commit,
            monotonic=self.monotonic,
        )

    def start(self, user: CurrentUser, save: RpgSession, *, msg: str = "m-1", action: str = "我走進山門。"):
        """開始階段（像請求一樣在最後提交），回傳 `TurnPlan`。"""
        begun = self.game.begin_turn(user, save.public_id, client_msg_id=msg, action=action)
        self.session.commit()
        assert begun.plan is not None
        return begun.plan

    def runner(self, plan, user: CurrentUser, **overrides: Any):
        from app.services.rpg_turn_runner import RunnerDeps, TurnRunner

        deps = RunnerDeps(
            session_factory=self.factory, wallet_factory=self.wallet_for, llm_factory=self.llm_for if self.state_available else None,
            provider=self.provider, settings=self.settings, clock=self.clock, monotonic=self.monotonic, **overrides,
        )
        return TurnRunner(plan=plan, current=user, deps=deps, marker=MARKER)

    def run(self, plan, user: CurrentUser, **overrides: Any):
        return list(self.runner(plan, user, **overrides).events())

    # ---- 觀察（每次都讀最新的資料）----------------------------------------------------------

    def turns(self, save: RpgSession):
        from sqlalchemy import select

        from app.models import MemoryLog

        self.session.expire_all()
        return list(self.session.scalars(select(MemoryLog).where(MemoryLog.session_id == save.id).order_by(MemoryLog.seq)))

    def ledger(self, kind: str | None = None):
        from sqlalchemy import select

        from app.models import LlmCallLog

        self.session.expire_all()
        statement = select(LlmCallLog).order_by(LlmCallLog.id)
        if kind is not None:
            statement = statement.where(LlmCallLog.kind == kind)
        return list(self.session.scalars(statement))

    def fresh(self, save: RpgSession) -> RpgSession:
        self.session.expire_all()
        return save

    # ---- 第 0 幕 ------------------------------------------------------------------------------

    def pending_save(self, user: CurrentUser, *, content: dict[str, Any] | None = None, tier: str = "safe", **protagonist_changes: Any) -> RpgSession:
        """開一個第 0 幕還在 `pending` 的存檔（新存檔的樣子）。"""
        snapshot = content if content is not None else validated().content
        save = self.sessions.create(
            user.id, source_version_id=None, snapshot=snapshot, snapshot_spec_version=1,
            protagonist=protagonist(**protagonist_changes), content_tier=tier, now=self.clock(),
        )
        self.session.commit()
        return save

    def begin_opening(self, user: CurrentUser, save: RpgSession):
        """開始階段（像請求一樣在最後提交）。"""
        begun = self.game.begin_opening(user, save.public_id)
        self.session.commit()
        return begun

    def opening_runner(self, plan, **overrides: Any):
        from app.services.rpg_opening_runner import OpeningRunner
        from app.services.rpg_runner_base import RunnerDeps

        deps = RunnerDeps(
            session_factory=self.factory, wallet_factory=self.wallet_for, llm_factory=None, provider=self.provider, settings=self.settings,
            clock=self.clock, monotonic=self.monotonic, **overrides,
        )
        return OpeningRunner(plan=plan, deps=deps, marker=MARKER)

    def run_opening(self, plan, **overrides: Any):
        return list(self.opening_runner(plan, **overrides).events())
