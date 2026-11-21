"""数字由代码算（DESIGN.md 1.3 原则 5、7.3 硬性要求）。

- price_sku：站点化定价，逻辑与 commerce-market-skills/miaoshou 流水线的 pricing.py 一致
- check_rationale_numbers：agent 理由里出现的数字必须能在代码算出的证据里找到
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable


def _round_price(value: float, mode: str) -> float:
    if value <= 0:
        return value
    mode = str(mode or "none").lower()
    if mode == "int":
        return float(math.ceil(value))
    if mode in ("0.99", "0.95"):
        tail = float(mode)
        candidate = math.floor(value) + tail
        if candidate < value:  # 不低于计算出的底价
            candidate = math.floor(value) + 1 + tail
        return round(candidate, 2)
    return round(value, 2)


def price_sku(item: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """售价 = (货源价×汇率 + 运费 + 其他成本) / (1 - 佣金率 - 目标毛利率 - 其他比例成本)，再按尾数取整。

    返回售价、成本拆解、按最终售价反算的实际毛利和毛利率。参数非法时返回 error。
    """

    def p(key: str, default: float = 0.0) -> float:
        return float(item.get(key, params.get(key, default)))

    commission = p("commission_rate")
    margin = p("target_margin_rate")
    extra = p("extra_rate")
    fx = p("exchange_rate", 1.0)
    rounding = item.get("rounding", params.get("rounding", "none"))

    source = float(item.get("source_price", 0.0)) * fx
    fixed_cost = source + float(item.get("shipping", 0.0)) + float(item.get("other_cost", 0.0))
    denom = 1.0 - commission - margin - extra
    if denom <= 0:
        return {"sku": item.get("sku"), "error": f"佣金率+毛利率+其他比例成本 >= 1（{1 - denom:.4f}），无法定价"}

    price = _round_price(fixed_cost / denom, rounding)
    profit = price - fixed_cost - price * commission - price * extra
    return {
        "sku": item.get("sku"),
        "currency": params.get("currency", "USD"),
        "price": price,
        "cost": round(fixed_cost, 4),
        "commission": round(price * commission, 4),
        "profit": round(profit, 4),
        "margin_rate": round(profit / price, 4) if price else 0.0,
    }


_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?%?")


def margin_at_price(price: float, source_price: float, exchange_rate: float = 1.0, shipping: float = 0.0,
                    other_cost: float = 0.0, commission_rate: float = 0.0, extra_rate: float = 0.0,
                    return_rate: float = 0.0, return_loss_rate: float = 1.0) -> dict[str, Any]:
    """按市场售价反算每发出一单的预期毛利（选品用；price_sku 是反方向：按目标毛利倒推售价）。

    落地成本 = 货源价×汇率 + 运费 + 其他成本；费用 = 售价×(佣金率 + 其他比例成本，如货到付款手续费)。
    退货和拒收按 return_rate 计：退回的单拿不到货款、也不收费用，落地成本损失 return_loss_rate 那一部分。
    预期利润 = (1 − 退货率)×(售价 − 费用) − 落地成本 + 退货率×(1 − 损失比例)×落地成本。
    """
    if price <= 0:
        return {"error": "售价必须大于 0"}
    landed = float(source_price) * float(exchange_rate) + float(shipping) + float(other_cost)
    fees = float(price) * (float(commission_rate) + float(extra_rate))
    r, loss = float(return_rate), float(return_loss_rate)
    no_return = float(price) - landed - fees
    profit = (1 - r) * (float(price) - fees) - landed + r * (1 - loss) * landed
    return {"price": round(float(price), 4), "landed_cost": round(landed, 4), "fees": round(fees, 4),
            "return_rate": r, "return_cost": round(no_return - profit, 4),
            "profit": round(profit, 4), "margin_rate": round(profit / float(price), 4)}


def _flatten_numbers(value: Any) -> Iterable[float]:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        yield float(value)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _flatten_numbers(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _flatten_numbers(v)
    elif isinstance(value, str):
        for m in _NUMBER.findall(value):
            yield from _parse(m)


def _parse(token: str) -> Iterable[float]:
    if token.endswith("%"):
        v = float(token[:-1])
        yield v
        yield v / 100
    else:
        yield float(token)


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.011, abs(b) * 0.005)


def check_rationale_numbers(rationale: str, evidence: dict[str, Any], small_int_exempt: int = 10) -> list[str]:
    """返回理由里找不到出处的数字。

    规则：理由中的每个数字（百分数同时按 x 和 x/100 比对）都必须与证据中的某个数字相近
    （绝对误差 0.011 或相对误差 0.5%）。0~small_int_exempt 的整数视为序号或计数，豁免。
    """
    known = list(_flatten_numbers(evidence))
    unknown: list[str] = []
    for token in _NUMBER.findall(rationale):
        raw = float(token.rstrip("%"))
        if not token.endswith("%") and raw.is_integer() and 0 <= raw <= small_int_exempt:
            continue
        if not any(_close(c, k) for c in _parse(token) for k in known):
            unknown.append(token)
    return unknown
