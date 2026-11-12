"""交易成本模型：佣金、印花税、过户费与滑点。"""

from qt.config import CostsConfig

_LOT = 100


class CostModel:
    """买入加价、卖出减价；佣金双向收取且有最低值，印花税仅卖出。"""

    def __init__(self, cfg: CostsConfig) -> None:
        self._cfg = cfg

    def _commission(self, amount: float) -> float:
        return max(self._cfg.min_commission, amount * self._cfg.commission_rate)

    def buy_cost(self, price: float, shares: float) -> float:
        """买入的现金流出总额（含滑点、佣金、过户费）。"""
        amount = price * (1 + self._cfg.slippage) * shares
        return amount + self._commission(amount) + amount * self._cfg.transfer_fee_rate

    def sell_proceeds(self, price: float, shares: float) -> float:
        """卖出的现金流入净额（扣滑点、佣金、印花税、过户费）。"""
        amount = price * (1 - self._cfg.slippage) * shares
        fees = (
            self._commission(amount)
            + amount * self._cfg.stamp_tax_rate
            + amount * self._cfg.transfer_fee_rate
        )
        return amount - fees

    def max_lots(self, price: float, cash: float) -> int:
        """现金够买的最大整手数（含费用），买不起一手返回 0。"""
        lots = int(cash // (price * (1 + self._cfg.slippage) * _LOT))
        while lots > 0 and self.buy_cost(price, lots * _LOT) > cash:
            lots -= 1
        return lots
