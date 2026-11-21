"""选品剧本：按站点配置的选品流程，和记忆放在一起维护。

剧本就是一条 type = selection 的记忆。正文分两部分：
- 「## 规则」里的 toml 代码块：给代码执行的门槛、权重、数据源、调研时效（本模块解析和校验）
- 其余各节：给人和 agent 看的市场特点、选品经验

分工：流程和门槛写在剧本里，运营在控制台就能改、分身能提草稿更新；
算数、全局底线（剧本只能比 settings 更严）、数据来源校验留在代码里，剧本改不坏。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from typing import Any

from .numbers import margin_at_price

SELECTION_TYPE = "selection"
RULES_SECTION = "规则"
DEFAULT_REGION = "*"

# 已登记的数据源：剧本里只能用这些名字。实际取数的适配器按这个清单逐个接入
SOURCES = {
    "google_suggest": "Google 下拉词（按国家、语言）",
    "bing_suggest": "Bing 下拉词",
    "youtube_suggest": "YouTube 下拉词",
    "holidays": "节日热词",
    "erp_sales": "ERP 自有店铺销量、退货",
    "miaoshou_1688": "经妙手采集 1688 货源",
    "third_party_api": "第三方选品或榜单接口",
    "csv_import": "表格导入",
    "owner_input": "负责人提供的数据",
}
SCORE_FACTORS = ("demand", "margin", "competition", "logistics")
_RULES_BLOCK = re.compile(r"```toml\s*\n(.*?)```", re.S)
_REGION = re.compile(r"^[A-Z]{2}$")


class PlaybookError(ValueError):
    """剧本规则写错了：message 直接给人看，写清哪一项、该怎么改。"""


@dataclass
class Rules:
    regions: list[str]
    platforms: list[str] = field(default_factory=list)
    research_max_age_days: int = 7
    sources: list[str] = field(default_factory=list)
    seeds: list[str] = field(default_factory=list)          # 默认品类词：临时选品没给关键词时兜底；定时盯什么写在雷达规则里
    price_band: tuple[float, float] | None = None
    max_weight_kg: float | None = None
    min_margin: float = 0.0
    margin_good: float | None = None
    weights: dict[str, float] = field(default_factory=lambda: {k: 25.0 for k in SCORE_FACTORS})
    exclude_categories: list[str] = field(default_factory=list)
    auto_adopt_sections: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)   # 调研用的语言，空就按国家默认语言
    holiday_lead_weeks: int = 10                          # 节日往后看几周
    shops: list[str] = field(default_factory=list)        # ERP 销量只看这些店铺，空就看全部
    return_rate: float = 0.0                              # 退货和拒收占比（货到付款地区要填）
    return_loss_rate: float = 1.0                         # 一单退回来，落地成本损失多少（1 = 全损）
    extra_rate: float = 0.0                               # 按售价比例的其他费用：货到付款手续费、平台服务费等
    commission_rates: dict[str, float] = field(default_factory=dict)  # 各平台佣金率，试算和打分缺佣金时用
    categories: list[str] = field(default_factory=list)       # 找方向时在内置品类库之外再扫的品类词（可写当地语言）
    skip_categories: list[str] = field(default_factory=list)  # 找方向时不考虑的品类（中文名或英文词都行）
    shipping_base: float | None = None                    # 运费估算：首重（美元）
    shipping_per_kg: float | None = None                  # 运费估算：每公斤（美元）


def _str_list(raw: dict, key: str, upper: bool = False) -> list[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise PlaybookError(f"{key} 要写成字符串列表，例如 {key} = [\"a\", \"b\"]")
    return [v.strip().upper() if upper else v.strip() for v in value]


def _number(raw: dict, key: str, low: float, high: float, default=None):
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlaybookError(f"{key} 要写数字，现在是 {value!r}")
    if not low <= value <= high:
        raise PlaybookError(f"{key} = {value} 超出范围，应在 {low} ~ {high} 之间")
    return float(value)


def parse_rules(body: str) -> Rules:
    """从剧本正文里取出「## 规则」那一节的 toml 代码块，校验后返回。写错就抛 PlaybookError。"""
    section = _section(body, RULES_SECTION)
    if section is None:
        raise PlaybookError(f"选品剧本要有「## {RULES_SECTION}」一节，里面放一个 ```toml 代码块")
    m = _RULES_BLOCK.search(section)
    if not m:
        raise PlaybookError(f"「## {RULES_SECTION}」里没有 ```toml 代码块")
    try:
        raw = tomllib.loads(m.group(1))
    except tomllib.TOMLDecodeError as e:
        raise PlaybookError(f"规则不是合法的 toml：{e}") from e
    known = {f for f in Rules.__dataclass_fields__}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise PlaybookError(f"不认识的规则项：{'、'.join(unknown)}。能用的有：{'、'.join(sorted(known))}")

    regions = _str_list(raw, "regions", upper=True)
    if not regions:
        raise PlaybookError("regions 必填：写适用的国家代码（如 [\"SA\", \"AE\"]），兜底剧本写 [\"*\"]")
    bad = [r for r in regions if r != DEFAULT_REGION and not _REGION.match(r)]
    if bad:
        raise PlaybookError(f"regions 里的 {'、'.join(bad)} 不是两位国家代码（如 US、SA、ID），兜底用 \"*\"")
    sources = _str_list(raw, "sources")
    unknown_sources = [s for s in sources if s not in SOURCES]
    if unknown_sources:
        raise PlaybookError(f"数据源 {'、'.join(unknown_sources)} 没有登记。能用的有：{'、'.join(SOURCES)}")

    band = raw.get("price_band")
    if band is not None:
        if (not isinstance(band, list) or len(band) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in band)
                or not 0 <= band[0] < band[1]):
            raise PlaybookError("price_band 要写成 [最低价, 最高价]，最低价小于最高价，例如 [15, 60]")
        band = (float(band[0]), float(band[1]))

    weights = raw.get("weights")
    if weights is None:
        weights = {k: 25.0 for k in SCORE_FACTORS}
    else:
        if not isinstance(weights, dict):
            raise PlaybookError("weights 要写成 { demand = 30, margin = 30, competition = 20, logistics = 20 }")
        extra = sorted(set(weights) - set(SCORE_FACTORS))
        if extra:
            raise PlaybookError(f"weights 里的 {'、'.join(extra)} 不认识，只能是 {'、'.join(SCORE_FACTORS)}")
        for k, v in weights.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise PlaybookError(f"weights.{k} 要写不小于 0 的数字")
        if sum(weights.values()) <= 0:
            raise PlaybookError("weights 不能全是 0")
        weights = {k: float(weights.get(k, 0)) for k in SCORE_FACTORS}

    auto = _str_list(raw, "auto_adopt_sections")
    if RULES_SECTION in auto:
        raise PlaybookError(f"「{RULES_SECTION}」不能自动采纳：门槛和权重改了要人批")
    min_margin = _number(raw, "min_margin", 0, 0.95, 0.0)
    margin_good = _number(raw, "margin_good", 0, 0.99)
    if margin_good is not None and margin_good <= min_margin:
        raise PlaybookError(f"margin_good（{margin_good}）要大于 min_margin（{min_margin}）")
    days = _int(raw, "research_max_age_days", 1, 90, 7, "调研数据多少天内有效")
    lead = _int(raw, "holiday_lead_weeks", 1, 52, 10, "节日往后看几周")
    rates = raw.get("commission_rates", {})
    if not isinstance(rates, dict) or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 0.6
                                              for v in rates.values()):
        raise PlaybookError("commission_rates 要写成 { amazon = 0.15, tiktok = 0.08 }，每个在 0~0.6 之间")
    langs = [x.lower() for x in _str_list(raw, "languages")]
    bad_lang = [x for x in langs if not re.match(r"^[a-z]{2}$", x)]
    if bad_lang:
        raise PlaybookError(f"languages 里的 {'、'.join(bad_lang)} 不是两位语言代码（如 en、ar、id）")
    return Rules(
        regions=regions, platforms=[p.lower() for p in _str_list(raw, "platforms")], research_max_age_days=days,
        sources=sources, seeds=_str_list(raw, "seeds"), price_band=band,
        max_weight_kg=_number(raw, "max_weight_kg", 0.01, 1000), min_margin=min_margin, margin_good=margin_good,
        weights=weights, exclude_categories=_str_list(raw, "exclude_categories"), auto_adopt_sections=auto,
        languages=langs, holiday_lead_weeks=lead, shops=_str_list(raw, "shops"),
        return_rate=_number(raw, "return_rate", 0, 0.9, 0.0), return_loss_rate=_number(raw, "return_loss_rate", 0, 1, 1.0),
        extra_rate=_number(raw, "extra_rate", 0, 0.5, 0.0),
        commission_rates={str(k).lower(): float(v) for k, v in rates.items()},
        categories=_str_list(raw, "categories"), skip_categories=_str_list(raw, "skip_categories"),
        shipping_base=_number(raw, "shipping_base", 0, 100), shipping_per_kg=_number(raw, "shipping_per_kg", 0, 200),
    )


def _int(raw: dict, key: str, low: int, high: int, default: int, meaning: str) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PlaybookError(f"{key} 要写 {low}~{high} 的整数（{meaning}）")
    return value


def _section(body: str, title: str) -> str | None:
    lines, inside, found = [], False, False
    for line in body.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            inside = heading.group(1) == title
            found = found or inside
            continue
        if inside:
            lines.append(line)
    return "\n".join(lines) if found else None


@dataclass
class Playbook:
    name: str
    rules: Rules
    body: str
    version: str  # 文件内容的 sha256 前 12 位：清单记下用的是哪一版剧本


def load_playbooks(memories) -> tuple[list[Playbook], list[str]]:
    """从记忆里挑出选品剧本。规则写错的不参与匹配，错误单独返回（控制台和 check 会显示）。"""
    from .memory import file_sha256

    playbooks, errors = [], []
    for m in memories:
        if m.type != SELECTION_TYPE:
            continue
        try:
            playbooks.append(Playbook(m.name, parse_rules(m.body), m.body, file_sha256(m.path)[:12]))
        except PlaybookError as e:
            errors.append(f"《{m.name}》：{e}")
    return playbooks, errors


def match_playbook(playbooks: list[Playbook], region: str, platform: str = "") -> Playbook:
    """按「国家 + 平台 → 国家 → 兜底 * 」的顺序找最具体的那份剧本。同样具体的有两份就报错，让人去改。"""
    region, platform = (region or "").strip().upper(), (platform or "").strip().lower()

    def specificity(p: Playbook) -> int:
        if p.rules.platforms and platform and platform not in p.rules.platforms:
            return -1  # 写了平台却不包含这个平台：不适用
        on_platform = 1 if platform and platform in p.rules.platforms else 0
        if region in p.rules.regions:
            return 2 + on_platform
        if DEFAULT_REGION in p.rules.regions:
            return on_platform
        return -1

    ranked = sorted(((specificity(p), p) for p in playbooks), key=lambda x: -x[0])
    ranked = [(s, p) for s, p in ranked if s >= 0]
    if not ranked:
        raise PlaybookError(f"没有适用于 {region or '?'}{'/' + platform if platform else ''} 的选品剧本，"
                            "先在记忆库建一份（或建一份 regions = [\"*\"] 的兜底剧本）")
    top = [p for s, p in ranked if s == ranked[0][0]]
    if len(top) > 1:
        raise PlaybookError(f"《{'》《'.join(p.name for p in top)}》都适用于 {region}，分不出用哪份，"
                            "改一下它们的 regions 或 platforms")
    return top[0]


def effective_min_margin(rules: Rules, global_min: float) -> float:
    """剧本只能比全局底线更严：取两者里更高的那个。"""
    return max(rules.min_margin, float(global_min or 0))


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def score_candidate(candidate: dict[str, Any], rules: Rules, global_min_margin: float = 0.0) -> dict[str, Any]:
    """一个候选的硬门槛判断和打分，数字全由代码算。

    candidate 字段：name、category、price（当地售价，美元）、source_price + exchange_rate（货源价和汇率）、
    shipping、other_cost、commission_rate、extra_rate、weight_kg、demand（0~100 需求热度）、
    competition（0~100，越高竞争越激烈）、evidence（数据出处，调研快照或链接）。
    """
    name = str(candidate.get("name") or "未命名")
    reasons: list[str] = []
    negative = [k for k in NUMERIC_FIELDS if _is_number(candidate.get(k)) and float(candidate[k]) < 0]
    if negative:
        return {"name": name, "passed": False, "score": 0.0,
                "reasons": [f"{'、'.join(NUMERIC_FIELDS[k] for k in negative)}不能是负数"]}

    if _is_number(candidate.get("source_price")) and float(candidate["source_price"]) > 0 \
            and not _is_number(candidate.get("exchange_rate")):
        return {"name": name, "passed": False, "score": 0.0,
                "reasons": ["缺汇率：货源价是人民币，要给 exchange_rate（实时汇率取不到时请手动填）"]}

    def pick(key: str, default: float) -> float:
        value = candidate.get(key)
        return default if value is None or value == "" else float(value)

    calc = margin_at_price(
        pick("price", 0), pick("source_price", 0), pick("exchange_rate", 1), pick("shipping", 0),
        pick("other_cost", 0), pick("commission_rate", 0), pick("extra_rate", rules.extra_rate),
        return_rate=pick("return_rate", rules.return_rate), return_loss_rate=pick("return_loss_rate", rules.return_loss_rate))
    if "error" in calc:
        return {"name": name, "passed": False, "reasons": [calc["error"]], "score": 0.0}
    floor = effective_min_margin(rules, global_min_margin)

    if rules.price_band and not rules.price_band[0] <= calc["price"] <= rules.price_band[1]:
        reasons.append(f"售价 {calc['price']} 不在价格带 {rules.price_band[0]:g}~{rules.price_band[1]:g} 内")
    weight = candidate.get("weight_kg")
    if rules.max_weight_kg is not None:
        if weight is None:
            reasons.append("缺重量：剧本限制了重量，必须给 weight_kg")
        elif float(weight) > rules.max_weight_kg:
            reasons.append(f"重量 {float(weight):g}kg 超过上限 {rules.max_weight_kg:g}kg")
    category = str(candidate.get("category") or "")
    hit = next((c for c in rules.exclude_categories if c and c in category), None)
    if hit:
        reasons.append(f"类目「{category}」命中排除项「{hit}」")
    if calc["margin_rate"] < floor:
        reasons.append(f"毛利率 {calc['margin_rate']:.2%} 低于底线 {floor:.2%}")

    good = rules.margin_good if rules.margin_good is not None else floor + 0.2
    scores = {
        "margin": _clamp((calc["margin_rate"] - floor) / (good - floor) * 100) if good > floor else 0.0,
        "logistics": (_clamp((1 - float(weight) / rules.max_weight_kg) * 100)
                      if weight is not None and rules.max_weight_kg else 50.0),
    }
    missing = []
    for key in ("demand", "competition"):
        value = candidate.get(key)
        if value is None:
            missing.append(key)
            scores[key] = 0.0
        else:
            v = _clamp(float(value))
            scores[key] = v if key == "demand" else 100 - v
    total_weight = sum(rules.weights.values())
    total = sum(rules.weights[k] * scores[k] for k in SCORE_FACTORS) / total_weight
    return {
        "name": name, "passed": not reasons, "reasons": reasons, "score": round(total, 1),
        "scores": {k: round(v, 1) for k, v in scores.items()}, "missing": missing,
        "margin": calc, "min_margin": floor, "evidence": candidate.get("evidence") or "",
    }


NUMERIC_FIELDS = {"price": "售价", "source_price": "货源价", "exchange_rate": "汇率", "shipping": "运费",
                  "other_cost": "其他成本", "commission_rate": "佣金率", "weight_kg": "重量",
                  "demand": "需求热度", "competition": "竞争度"}


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return value is not None and value != "" and not isinstance(value, bool)
    except (TypeError, ValueError):
        return False


def estimate_shipping(rules: Rules, weight_kg: float | None) -> float | None:
    """按剧本的首重 + 每公斤估运费；剧本没配或没给重量就返回 None。"""
    if weight_kg is None or rules.shipping_per_kg is None:
        return None
    return round((rules.shipping_base or 0) + rules.shipping_per_kg * float(weight_kg), 2)


def apply_defaults(candidate: dict[str, Any], rules: Rules, platform: str = "",
                   exchange_rate: float | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """候选里没填的佣金率、运费、汇率，按剧本和实时汇率补上。返回（补完的候选, 哪些是补的及出处）。

    控制台试算和 agent 打分用同一套补法，两边算出来才一致。填了的一律不动。
    """
    out, filled = dict(candidate), {}
    if not _is_number(out.get("commission_rate")):
        rate = rules.commission_rates.get((platform or "").lower())
        if rate is not None:
            out["commission_rate"], filled["commission_rate"] = rate, f"剧本里 {platform} 的佣金率"
    if not _is_number(out.get("shipping")):
        weight = float(out["weight_kg"]) if _is_number(out.get("weight_kg")) else None
        ship = estimate_shipping(rules, weight)
        if ship is not None:
            out["shipping"] = ship
            filled["shipping"] = f"剧本估算：首重 {rules.shipping_base or 0:g} + 每公斤 {rules.shipping_per_kg:g} 美元"
    if not _is_number(out.get("exchange_rate")) and exchange_rate:
        out["exchange_rate"], filled["exchange_rate"] = exchange_rate, "实时汇率（人民币兑美元）"
    return out, filled


def score_candidates(candidates: list[dict], rules: Rules, global_min_margin: float = 0.0) -> list[dict]:
    """全部候选打分，通过门槛的在前、分高的在前。"""
    results = [score_candidate(c, rules, global_min_margin) for c in candidates]
    return sorted(results, key=lambda r: (not r["passed"], -r["score"]))


def auto_adoptable(memory, draft: dict) -> bool:
    """草稿改的是剧本里允许自动采纳的那一节吗。规则节、新建、整条改名都不算。"""
    if memory is None or memory.type != SELECTION_TYPE:
        return False
    mode = draft.get("mode")
    if mode not in ("append", "revise", "remove"):
        return False
    if mode == "append":
        first = ((draft.get("body") or "").strip().splitlines() or [""])[0]
        heading = re.match(r"^##\s+(.+?)\s*$", first)
        section = heading.group(1) if heading else ""
    else:
        section = draft.get("target_section") or ""
    # 草稿正文里只要还带了别的「## 」标题，就可能顺手改到别的节（甚至规则）：一律等人审
    headings = re.findall(r"^##\s+(.+?)\s*$", draft.get("body") or "", re.M)
    if any(h != section for h in headings):
        return False
    try:
        rules = parse_rules(memory.body)
    except PlaybookError:
        return False
    return bool(section) and section != RULES_SECTION and section in rules.auto_adopt_sections
