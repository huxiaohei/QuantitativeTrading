import pytest

from qt.costs import CostModel
from tests.fakes import default_costs


def test_buy_cost_with_min_commission() -> None:
    m = CostModel(default_costs())
    # 成交价 10 * 1.001 = 10.01，金额 10010；佣金 max(5, 2.5025) = 5；过户费 0.1001
    assert m.buy_cost(10.0, 1000) == pytest.approx(10015.1001)


def test_sell_proceeds_includes_stamp_tax() -> None:
    m = CostModel(default_costs())
    # 成交价 9.99，金额 9990；佣金 5；印花税 4.995；过户费 0.0999
    assert m.sell_proceeds(10.0, 1000) == pytest.approx(9979.9051)


def test_max_lots_rounds_down_to_board_lot() -> None:
    m = CostModel(default_costs())
    assert m.max_lots(10.0, 15000.0) == 14  # 15 手含费用超出 15000
    assert m.max_lots(200.0, 15000.0) == 0  # 一手 20000 买不起
