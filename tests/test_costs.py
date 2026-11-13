import pytest

from qt.costs import LOT_SIZE, CostModel
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


def test_max_lots_correction_loop_with_commission_above_minimum() -> None:
    m = CostModel(default_costs())
    # 100 手：成交金额 100100，佣金 25.025（高于最低 5 元）超出现金，须退到 99 手
    assert m.max_lots(10.0, 100_100.0) == 99
    assert m.buy_cost(10.0, 9900) <= 100_100.0 < m.buy_cost(10.0, 10000)


def test_max_lots_never_negative_for_non_positive_cash_or_price() -> None:
    m = CostModel(default_costs())
    for price, cash in ((10.0, -9015.0), (10.0, 0.0), (0.0, 15000.0), (-1.0, 15000.0)):
        assert m.max_lots(price, cash) == 0, (price, cash)
    assert m.max_lots(float("nan"), 15000.0) == 0
    assert m.max_lots(10.0, float("nan")) == 0


def test_lot_size_is_one_board_lot() -> None:
    assert LOT_SIZE == 100
