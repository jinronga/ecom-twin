"""找方向：还不知道做什么品时，扫一遍常见品类的搜索需求，由代码打分排出值得看的方向。

和选品打分（selection_score）的区别：这一步不需要货源价、不需要妙手，只回答「哪些方向有需求、能细分、赶得上节日」。
用户按方向找到货源后，再走调研打分。

数据只用下拉词和节日日历（和调研同一套取数）。分数全由代码算：
- 需求宽度：一个品类词下有多少不同的下拉词（长尾越多，需求越宽）
- 能细分：带人群、场景、送礼的词有多少（for women、for travel、gift……说明有切入点）
- 痛点：best、vs、waterproof 这类比较、找解决办法的词
- 节日：适合送礼的品类，离送礼节日的时间是否正好在备货窗口里
"""

from __future__ import annotations

import re
import time
import urllib.parse
from datetime import date
from typing import Any, Callable

from .research import REGION_LANGS, http_get, suggest, upcoming_holidays

# 常见的、不带电的品类（带电要认证、压货多，默认不放进来）。kids 标记的要注意儿童产品认证。
CATEGORIES: list[dict[str, Any]] = [
    {"q": "home organization", "cn": "家居收纳", "gift": False},
    {"q": "kitchen gadgets", "cn": "厨房小工具", "gift": True},
    {"q": "bathroom accessories", "cn": "浴室用品", "gift": False},
    {"q": "home decor", "cn": "家居装饰", "gift": True},
    {"q": "wall art", "cn": "墙面装饰画", "gift": True},
    {"q": "scented candles", "cn": "香薰蜡烛", "gift": True},
    {"q": "throw pillow covers", "cn": "抱枕套", "gift": True},
    {"q": "bedding", "cn": "床品", "gift": False},
    {"q": "pet toys", "cn": "宠物玩具", "gift": True},
    {"q": "pet grooming", "cn": "宠物美容用品", "gift": False},
    {"q": "dog accessories", "cn": "狗狗用品", "gift": True},
    {"q": "cat accessories", "cn": "猫咪用品", "gift": True},
    {"q": "jewelry", "cn": "首饰", "gift": True},
    {"q": "hair accessories", "cn": "发饰", "gift": True},
    {"q": "makeup bag", "cn": "化妆包", "gift": True},
    {"q": "tote bag", "cn": "托特包", "gift": True},
    {"q": "phone case", "cn": "手机壳", "gift": True},
    {"q": "stationery", "cn": "文具", "gift": True},
    {"q": "planner", "cn": "计划本、手账", "gift": True},
    {"q": "craft supplies", "cn": "手工 DIY 材料", "gift": True},
    {"q": "party supplies", "cn": "派对用品", "gift": False},
    {"q": "gift wrapping", "cn": "礼品包装", "gift": False},
    {"q": "yoga accessories", "cn": "瑜伽用品", "gift": True},
    {"q": "resistance bands", "cn": "弹力带等健身小件", "gift": False},
    {"q": "water bottle", "cn": "水杯", "gift": True},
    {"q": "camping gear", "cn": "露营用品", "gift": True},
    {"q": "gardening tools", "cn": "园艺工具", "gift": True},
    {"q": "car accessories", "cn": "车载用品（不带电）", "gift": True},
    {"q": "travel accessories", "cn": "旅行用品", "gift": True},
    {"q": "baby accessories", "cn": "母婴用品", "gift": True, "kids": True},
    {"q": "kids toys", "cn": "儿童玩具", "gift": True, "kids": True},
    {"q": "socks", "cn": "袜子", "gift": True},
    {"q": "hats", "cn": "帽子", "gift": True},
    {"q": "sunglasses", "cn": "太阳镜", "gift": True},
    {"q": "nail art", "cn": "美甲用品", "gift": True},
    {"q": "skincare tools", "cn": "护肤工具（刮痧板、滚轮）", "gift": True},
    {"q": "desk accessories", "cn": "桌面摆件", "gift": True},
    {"q": "keychain", "cn": "钥匙扣", "gift": True},
    {"q": "fishing gear", "cn": "钓鱼用品", "gift": True},
]
# 适合送礼的节日：送礼类品类离这些节日在备货窗口里，节日分高
GIFT_HOLIDAYS = {"christmas", "valentine", "mothers", "fathers", "blackfriday", "whitefriday", "eid_fitr", "lunar",
                 "singles", "double12", "mid_99"}
SEGMENT = re.compile(r"\b(for (?:women|men|her|him|kids|girls|boys|mom|dad|teens|seniors|dogs?|cats?|puppies|"
                     r"travel|home|office|car|kitchen|bathroom|bedroom|outdoor|camping|beginners|small spaces?)|gift|gifts|"
                     r"personalized|custom|aesthetic|cute|set|kit|bulk)\b", re.I)
PAIN = re.compile(r"\b(best|vs|alternative|not working|problem|fix|without|waterproof|non[- ]toxic|heavy duty|durable|"
                  r"easy to clean|leak ?proof|odor|smell|cheap|affordable|portable|foldable|compact)\b", re.I)
DISCOVER_GAP = 0.3
AMAZON = {"US": "amazon.com", "GB": "amazon.co.uk", "DE": "amazon.de", "FR": "amazon.fr", "ES": "amazon.es",
          "IT": "amazon.it", "NL": "amazon.nl", "PL": "amazon.pl", "CA": "amazon.ca", "AU": "amazon.com.au",
          "JP": "amazon.co.jp", "SA": "amazon.sa", "AE": "amazon.ae", "SG": "amazon.sg"}
SHOPEE = {"ID": "shopee.co.id", "MY": "shopee.com.my", "TH": "shopee.co.th", "VN": "shopee.vn", "PH": "shopee.ph",
          "SG": "shopee.sg"}


def check_links(region: str, query: str) -> dict[str, str]:
    """给人点开核实的链接（只是带好关键词的搜索页，不抓数据）。"""
    q = urllib.parse.quote_plus(query)
    links = {"Google Trends 近一年走势": f"https://trends.google.com/trends/explore?date=today%2012-m&geo={region}&q={q}"}
    if region in AMAZON:
        links["Amazon 搜索结果（看价格带、评价数、卖家多不多）"] = f"https://www.{AMAZON[region]}/s?k={q}"
    if region in SHOPEE:
        links["Shopee 搜索结果"] = f"https://{SHOPEE[region]}/search?keyword={q}"
    return links


def _excluded(cat: dict, words: list[str]) -> str:
    for w in words:
        w = w.strip().lower()
        if w and (w in cat["cn"].lower() or w in cat["q"].lower() or (w in ("儿童", "母婴", "kids", "baby") and cat.get("kids"))):
            return w
    return ""


def discover(store, playbook, region: str, platform: str = "", *, exclude: list[str] | None = None,
             focus: list[str] | None = None, top: int = 8, today: date | None = None,
             get: Callable[..., str] | None = None, sleep: Callable[[float], None] | None = None) -> dict:
    """扫一遍品类库（加上剧本 categories 和本次 focus 的词），代码打分排序，存成调研快照。"""
    get, sleep = get or http_get, sleep or time.sleep
    today = today or date.today()
    region = region.upper()
    rules = playbook.rules
    lang = (rules.languages or REGION_LANGS.get(region, ["en"]))[0]
    skip = list(rules.exclude_categories) + list(rules.skip_categories) + list(exclude or [])
    cats = [dict(c) for c in CATEGORIES]
    for word in list(rules.categories) + list(focus or []):
        if word and not any(c["q"].lower() == word.lower() for c in cats):
            cats.append({"q": word, "cn": word, "gift": True, "custom": True})
    holidays = upcoming_holidays(region, today, 26)
    gift_holidays = [h for h in holidays if h["id"] in GIFT_HOLIDAYS]
    lead_days = rules.holiday_lead_weeks * 7

    items: dict[str, list[str]] = {}
    ranked, skipped, failed = [], [], []
    for cat in cats:
        hit = _excluded(cat, skip)
        if hit:
            skipped.append({"品类": cat["cn"], "原因": f"命中排除「{hit}」"})
            continue
        words: list[str] = []
        try:
            for q in ([cat["q"], f"{cat['q']} for"] if lang == "en" else [cat["q"]]):
                got = suggest("google_suggest", q, region, lang, get)
                items[f"{lang}:{q}"] = got
                words += got
                sleep(DISCOVER_GAP)
        except Exception as e:  # noqa: BLE001 —— 单个品类取不到不影响别的，记下来
            failed.append(f"{cat['cn']}：{type(e).__name__}")
            continue
        uniq = sorted({w.lower() for w in words})
        segs = [w for w in uniq if SEGMENT.search(w)]
        pains = [w for w in uniq if PAIN.search(w)]
        breadth = min(100.0, len(uniq) * 5)
        seg_score = min(100.0, len(segs) * 12.5)
        pain_score = min(100.0, len(pains) * 20)
        holiday, holiday_score, timing = None, 0.0, ""
        if cat.get("gift"):
            # 剧本要提前 holiday_lead_weeks 备货：剩的天数不到备货期的六成就来不及从头找货源了
            for h in gift_holidays:
                if lead_days * 0.6 <= h["days_left"] <= lead_days + 28:
                    holiday, holiday_score, timing = h, 100.0, "正好赶上备货期"
                    break
            if holiday is None:  # 赶不上的节日之后如果还有能提前布局的，优先报那个（更有用）
                later = next((h for h in gift_holidays if lead_days + 28 < h["days_left"] <= 180), None)
                early = next((h for h in gift_holidays if h["days_left"] < lead_days * 0.6), None)
                if later:
                    holiday, holiday_score, timing = later, 50.0, "还早，可以提前布局"
                elif early:
                    holiday, holiday_score, timing = early, 30.0, "偏晚，只适合现货快发"
        score = round(0.4 * breadth + 0.3 * seg_score + 0.1 * pain_score + 0.2 * holiday_score, 1)
        ranked.append({
            "方向": cat["cn"], "品类词": cat["q"], "总分": score,
            "分项": {"需求宽度": breadth, "能细分": seg_score, "痛点": pain_score, "节日": holiday_score},
            "下拉词数": len(uniq), "细分切入点": segs[:8], "痛点词": pains[:6], "示例下拉词": uniq[:8],
            "节日": f"{holiday['name']} {holiday['date']}（还有 {holiday['days_left']} 天，{timing}）" if holiday else "",
            "提醒": "儿童或母婴产品，美国要 CPC 等儿童产品认证，欧盟要 CE" if cat.get("kids") else "",
            "核实链接": check_links(region, cat["q"]),
            **({"来源": "剧本或本次指定"} if cat.get("custom") else {}),
        })
    ranked.sort(key=lambda x: -x["总分"])
    notes = []
    if lang != "en":
        notes.append(f"{region} 的主要语言是 {lang}，品类库是英文词，查到的多是英文结果；剧本里用 categories 补当地语言的品类词会更准")
    if failed:
        notes.append("这些品类没取到：" + "、".join(failed[:10]))
    data = {"google_suggest": {"status": "ok" if items else "failed", "items": items},
            "holidays": {"status": "ok", "items": holidays},
            "discovery": {"status": "ok" if ranked else "failed", "items": ranked}}
    terms = sorted({f"google_suggest|{k}\t{w.lower()}" for k, ws in items.items() for w in ws})
    sid = store.add_research_snapshot(region, platform, playbook.name, playbook.version, "", data, terms, [])
    return {"快照": sid, "剧本": playbook.name, "国家": region, "方向": ranked[:max(1, min(top, 20))],
            "排除的": skipped, "说明": notes,
            "怎么读": "总分 = 需求宽度 40% + 能细分 30% + 痛点 10% + 节日 20%，都由代码从下拉词和节日日历算出。"
                    "只说明有人在搜、能细分，不说明卖得动：价格带、竞品多不多要点「核实链接」看，或者找到货源后再打分"}
