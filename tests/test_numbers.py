from ecom_twin.numbers import check_rationale_numbers, price_sku


def test_price_sku_matches_pipeline_formula():
    r = price_sku({"sku": "A", "source_price": 20, "shipping": 3.5, "other_cost": 0.5},
                  {"commission_rate": 0.08, "target_margin_rate": 0.30, "exchange_rate": 0.14, "rounding": "0.99"})
    # 成本 = 20*0.14 + 3.5 + 0.5 = 6.8；底价 = 6.8 / 0.62 = 10.97 -> 10.99
    assert r["cost"] == 6.8
    assert r["price"] == 10.99
    assert r["margin_rate"] > 0.30


def test_price_sku_rejects_impossible_params():
    r = price_sku({"sku": "A", "source_price": 1}, {"commission_rate": 0.5, "target_margin_rate": 0.5})
    assert "error" in r


def test_rationale_numbers_must_come_from_evidence():
    evidence = {"margin_rate": 0.3426, "price": 12.99}
    assert check_rationale_numbers("售价 12.99，毛利率 34.26%", evidence) == []
    assert check_rationale_numbers("预计日销 120 单", evidence) == ["120"]


def test_small_integers_are_exempt():
    assert check_rationale_numbers("两款里第 2 款更好，共 3 个 SKU", {}) == []
