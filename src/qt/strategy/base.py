"""策略协议。回测与将来的实盘共用，不得依赖回测引擎。"""

from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from datetime import date
from typing import Protocol

import pandas as pd

from qt.models import BuyCandidate, Position, SellSignal


class Strategy(Protocol):
    """策略：只读快照与持仓，产出买入候选与卖出信号。"""

    name: str

    def buy_candidates(
        self, day: date, snapshot: pd.DataFrame, held: AbstractSet[str]
    ) -> list[BuyCandidate]:
        """返回当日买入候选；已持有的不入选。"""
        ...

    def sell_signals(
        self, day: date, snapshot: pd.DataFrame, positions: Sequence[Position]
    ) -> list[SellSignal]:
        """返回当日卖出信号；不修改持仓。"""
        ...
