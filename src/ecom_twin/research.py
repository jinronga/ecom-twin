"""选品市场调研：按剧本里登记的数据源取数，存成带时间和出处的快照。

数据源的取法借鉴「出海工具台」（chuhai-tools）：下拉词用 Google / Bing / YouTube 的公开接口，
节日按内置日历算（农历、伊斯兰历按年份查表）。这里直接取，不经过那个平台。
每日热搜、新闻、维基浏览量试过后去掉了：热搜多是新闻和人名，种子词很少正好是维基词条，对选品帮助不大。

几条约束：
- 只访问下面写死的公开地址（第三方接口的地址来自负责人配置），限超时、限响应大小，不能被拿去访问内网
- 每个数据源单独记成功、失败、跳过，失败不当成「没有需求」
- 标了示例数据的结果不算有效数据
- 快照里的词和以前所有快照对比，第一次是基准，之后没见过的标成新词
"""

from __future__ import annotations

import csv
import io
import json
import os
import ssl
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .selection import Playbook, Rules

USER_AGENT = "Mozilla/5.0 (compatible; ecom-twin-research/0.1)"
MAX_BYTES = 1_000_000
TIMEOUT = 12
REQUEST_GAP = 0.4          # 同一个数据源连续请求之间歇一下，公开接口容易限流
MAX_SEEDS = 12
ALLOWED_HOSTS = {"suggestqueries.google.com", "api.bing.com", "api.frankfurter.dev"}

# 国家的默认语言（剧本里写了 languages 就用剧本的）。没列的按英语
REGION_LANGS = {
    "US": ["en"], "GB": ["en"], "CA": ["en"], "AU": ["en"], "DE": ["de"], "FR": ["fr"], "ES": ["es"], "IT": ["it"],
    "NL": ["nl"], "PL": ["pl"], "MX": ["es"], "BR": ["pt"], "JP": ["ja"], "KR": ["ko"],
    "SA": ["ar", "en"], "AE": ["ar", "en"], "KW": ["ar"], "QA": ["ar"], "BH": ["ar"], "OM": ["ar"], "EG": ["ar"],
    "ID": ["id"], "MY": ["ms", "en"], "TH": ["th"], "VN": ["vi"], "PH": ["en"], "SG": ["en"],
}
EU_US = ["US", "GB", "CA", "AU", "DE", "FR", "ES", "IT", "NL", "PL"]
GULF = ["SA", "AE", "KW", "QA", "BH", "OM", "EG"]
SEA = ["ID", "MY", "TH", "VN", "PH", "SG"]
MUSLIM = GULF + ["ID", "MY"]

# 节日日历：借鉴 chuhai-tools 的 src/lib/holidays.ts（农历、伊斯兰历、印度历按年份查表）。
# 开斋节、宰牲节按斋月起始日偏移，白色星期五、12.12 等按规则推，都是近似日期，以官方公布为准。
HOLIDAYS: list[dict[str, Any]] = [
    {"id": "newyear", "name": "新年", "regions": ["*"], "seeds": ["new year"], "rule": ("fixed", 1, 1)},
    {"id": "lunar", "name": "春节", "regions": SEA + ["SG"], "seeds": ["chinese new year", "lunar new year"],
     "rule": ("table", ["2025-01-29", "2026-02-17", "2027-02-06", "2028-01-26", "2029-02-13", "2030-02-03"])},
    {"id": "valentine", "name": "情人节", "regions": ["*"], "seeds": ["valentines day gift"], "rule": ("fixed", 2, 14)},
    {"id": "ramadan", "name": "斋月", "regions": MUSLIM, "seeds": ["ramadan", "ramadan decor"],
     "rule": ("table", ["2025-03-01", "2026-02-18", "2027-02-08", "2028-01-28", "2029-01-16", "2030-01-06"])},
    {"id": "eid_fitr", "name": "开斋节（近似）", "regions": MUSLIM, "seeds": ["eid mubarak", "eid gift"],
     "rule": ("offset", "ramadan", 30)},
    {"id": "eid_adha", "name": "宰牲节（近似）", "regions": MUSLIM, "seeds": ["eid al adha"], "rule": ("offset", "ramadan", 99)},
    {"id": "easter", "name": "复活节", "regions": EU_US, "seeds": ["easter basket"], "rule": ("easter",)},
    {"id": "mothers", "name": "母亲节（美国等）", "regions": ["US", "CA", "AU", "JP"], "seeds": ["mothers day gift"],
     "rule": ("nth", 5, 6, 2)},
    {"id": "fathers", "name": "父亲节", "regions": ["US", "GB", "CA"], "seeds": ["fathers day gift"], "rule": ("nth", 6, 6, 3)},
    {"id": "school", "name": "返校季", "regions": EU_US, "seeds": ["back to school"], "rule": ("fixed", 8, 1)},
    {"id": "mid_99", "name": "9.9 大促", "regions": SEA, "seeds": ["9.9 sale"], "rule": ("fixed", 9, 9)},
    {"id": "halloween", "name": "万圣节", "regions": EU_US + ["JP"], "seeds": ["halloween decorations"],
     "rule": ("fixed", 10, 31)},
    {"id": "diwali", "name": "排灯节", "regions": ["IN"], "seeds": ["diwali"],
     "rule": ("table", ["2025-10-20", "2026-11-08", "2027-10-29", "2028-10-17", "2029-11-05", "2030-10-26"])},
    {"id": "singles", "name": "双十一", "regions": SEA, "seeds": ["11.11 sale"], "rule": ("fixed", 11, 11)},
    {"id": "thanksgiving", "name": "感恩节", "regions": ["US"], "seeds": ["thanksgiving"], "rule": ("nth", 11, 3, 4)},
    {"id": "blackfriday", "name": "黑五", "regions": EU_US, "seeds": ["black friday deals"],
     "rule": ("offset", "thanksgiving", 1)},
    {"id": "whitefriday", "name": "白色星期五（中东黑五，近似）", "regions": GULF, "seeds": ["white friday"],
     "rule": ("offset", "thanksgiving", 1)},
    {"id": "double12", "name": "12.12 大促", "regions": SEA, "seeds": ["12.12 sale"], "rule": ("fixed", 12, 12)},
    {"id": "christmas", "name": "圣诞节", "regions": EU_US + ["PH"], "seeds": ["christmas gift"], "rule": ("fixed", 12, 25)},
]


# 节日的当地语言种子词：英文词在德国、法国查出来的多是英文结果，当地需求要用当地语言查
LOCAL_HOLIDAY_SEEDS: dict[str, dict[str, list[str]]] = {
    "christmas": {"de": ["weihnachtsgeschenke"], "fr": ["cadeau noel"], "es": ["regalos de navidad"],
                  "it": ["regali di natale"], "nl": ["kerstcadeau"], "pl": ["prezenty na święta"]},
    "easter": {"de": ["ostern deko"], "fr": ["décoration pâques"], "es": ["decoración pascua"],
               "it": ["decorazioni pasqua"]},
    "halloween": {"de": ["halloween deko"], "fr": ["déguisement halloween"], "es": ["disfraz halloween"],
                  "it": ["costumi halloween"]},
    "valentine": {"de": ["valentinstag geschenk"], "fr": ["cadeau saint valentin"], "es": ["regalo san valentin"],
                  "it": ["regalo san valentino"]},
    "school": {"de": ["schulanfang"], "fr": ["rentrée scolaire"], "es": ["vuelta al cole"], "it": ["ritorno a scuola"]},
    "ramadan": {"ar": ["رمضان"]},
}


class ResearchError(RuntimeError):
    pass


# ---------------- 取数 ----------------
_SSL: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    """照常校验证书。python.org 装的 Python 在 macOS 上默认没有根证书，装了 certifi 就用它的。"""
    global _SSL
    if _SSL is None:
        try:
            import certifi
            _SSL = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            _SSL = ssl.create_default_context()
    return _SSL


def http_get(url: str, headers: dict[str, str] | None = None, allowed: set[str] | None = None) -> str:
    """只取写死的公开地址：限域名、限 https、限超时和大小。"""
    parsed = urllib.parse.urlsplit(url)
    hosts = allowed if allowed is not None else ALLOWED_HOSTS
    if parsed.scheme != "https" or parsed.hostname not in hosts:
        raise ResearchError(f"不允许访问 {parsed.scheme}://{parsed.hostname}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_context()) as resp:  # noqa: S310 —— 已限定 https 和域名
        if urllib.parse.urlsplit(resp.geturl()).hostname not in hosts:
            raise ResearchError("被重定向到了不允许的地址")
        body = resp.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ResearchError("响应太大")
    return body.decode("utf-8", errors="replace")


def _q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def suggest(engine: str, seed: str, gl: str, hl: str, get: Callable[[str], str] = http_get) -> list[str]:
    if engine == "google_suggest":
        url = f"https://suggestqueries.google.com/complete/search?client=firefox&ie=utf-8&oe=utf-8&gl={gl}&hl={hl}&q={_q(seed)}"
    elif engine == "youtube_suggest":
        url = (f"https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&ie=utf-8&oe=utf-8"
               f"&gl={gl}&hl={hl}&q={_q(seed)}")
    elif engine == "bing_suggest":
        url = f"https://api.bing.com/osjson.aspx?query={_q(seed)}&mkt={hl}-{gl}"
    else:
        raise ResearchError(f"不是下拉词数据源：{engine}")
    data = json.loads(get(url))
    return [s for s in (data[1] if isinstance(data, list) and len(data) > 1 else []) if isinstance(s, str)]


def fx_rate(store, base: str = "CNY", quote: str = "USD", get: Callable[..., str] | None = None,
            today: date | None = None) -> dict:
    """实时汇率（Frankfurter，欧洲央行每个工作日公布的参考汇率），同一天只取一次。取不到就抛错，不编一个数。"""
    get = get or http_get
    today = today or date.today()
    key = f"fx:{base}:{quote}:{today.isoformat()}"
    cached = store.kv_get(key)
    if cached:
        return json.loads(cached)
    data = json.loads(get(f"https://api.frankfurter.dev/v1/latest?base={_q(base)}&symbols={_q(quote)}"))
    rate = (data.get("rates") or {}).get(quote)
    if not isinstance(rate, (int, float)) or rate <= 0:
        raise ResearchError(f"汇率接口没返回 {base}→{quote}")
    result = {"rate": float(rate), "date": data.get("date") or "", "source": "Frankfurter（欧洲央行参考汇率）"}
    store.kv_set(key, json.dumps(result))
    return result


SUGGEST_SOURCES = ("google_suggest", "bing_suggest", "youtube_suggest")


def estimate_demand(snapshot: dict | None, keyword: str) -> dict:
    """按搜索词估需求热度：数快照里有多少个不同的下拉词同时包含搜索词的每个词。

    分数 = 匹配到的词数 × 5，封顶 100（20 个以上就是 100）。只是「有多少人在围绕它搜」的粗估，
    匹配到的词原样列出来当依据，人可以改。
    """
    words = [w for w in (keyword or "").lower().split() if w]
    if not snapshot:
        return {"score": None, "matches": [], "note": "还没有调研快照"}
    if not words:
        return {"score": None, "matches": [], "note": f"填上搜索词，就按快照 {snapshot['id']} 估算"}
    found = set()
    for source in SUGGEST_SOURCES:
        items = (snapshot["data"].get(source) or {}).get("items") or {}
        for suggestions in items.values():
            for s in suggestions:
                low = s.lower()
                if all(w in low for w in words):
                    found.add(low)
    matches = sorted(found)
    return {"score": min(100, len(matches) * 5), "matches": matches[:30], "count": len(matches),
            "note": f"快照 {snapshot['id']} 里有 {len(matches)} 个下拉词包含「{keyword}」，每个 5 分，封顶 100"}


# ---------------- 节日 ----------------
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + (n - 1) * 7)


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * m_) // 451
    month = (h + m_ - 7 * m + 114) // 31
    day = (h + m_ - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def holiday_date(h: dict, year: int) -> date | None:
    rule = h["rule"]
    kind = rule[0]
    if kind == "fixed":
        return date(year, rule[1], rule[2])
    if kind == "nth":  # (月, 星期几 0=周一, 第几个)
        return _nth_weekday(year, rule[1], rule[2], rule[3])
    if kind == "easter":
        return _easter(year)
    if kind == "table":
        return next((date.fromisoformat(d) for d in rule[1] if d.startswith(f"{year}-")), None)
    if kind == "offset":
        base = next((x for x in HOLIDAYS if x["id"] == rule[1]), None)
        d = holiday_date(base, year) if base else None
        return d + timedelta(days=rule[2]) if d else None
    return None


def upcoming_holidays(region: str, today: date, weeks: int = 10) -> list[dict]:
    """这个国家未来几周内的节日，带日期和还剩几天（剧本 holiday_lead_weeks 控制看多远）。"""
    out = []
    for h in HOLIDAYS:
        if "*" not in h["regions"] and region not in h["regions"]:
            continue
        for year in (today.year, today.year + 1):
            d = holiday_date(h, year)
            if d and 0 <= (d - today).days <= weeks * 7:
                out.append({"id": h["id"], "name": h["name"], "date": d.isoformat(), "days_left": (d - today).days,
                            "seeds": h["seeds"]})
    return sorted(out, key=lambda x: x["date"])


# ---------------- 跑一次调研 ----------------
def _languages(rules: Rules, region: str) -> list[str]:
    return rules.languages or REGION_LANGS.get(region, ["en"])


def _ok(items: Any, **extra) -> dict:
    return {"status": "ok", "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": items, **extra}


def run_research(store, playbook: Playbook, region: str, platform: str = "", task_id: str = "", *,
                 owner_input: str = "", csv_path: Path | None = None, third_party: dict | None = None,
                 extra_seeds: list[str] | None = None, max_seeds: int = MAX_SEEDS,
                 get: Callable[..., str] | None = None, today: date | None = None,
                 sleep: Callable[[float], None] | None = None) -> dict:
    """按剧本的 sources 调研一遍，存快照并返回。每个数据源独立成败，最后和以前的快照比出新词。"""
    get, sleep = get or http_get, sleep or time.sleep
    rules, region = playbook.rules, region.upper()
    today = today or date.today()
    langs = _languages(rules, region)
    holidays = upcoming_holidays(region, today, rules.holiday_lead_weeks)
    extra = [s.strip() for s in (extra_seeds or []) if s and s.strip()]
    seeds = list(dict.fromkeys(extra + rules.seeds + [s for h in holidays for s in h["seeds"]]))[:max_seeds]
    data: dict[str, dict] = {}
    terms: list[str] = []

    for source in rules.sources:
        try:
            if source in ("google_suggest", "bing_suggest", "youtube_suggest") and not seeds:
                data[source] = {"status": "skipped",
                                "note": "没有种子词：剧本 seeds 是空的，近期也没有节日词。在剧本里填上自己做的类目词"}
            elif source in ("google_suggest", "bing_suggest", "youtube_suggest"):
                results = {}
                for hl in langs:
                    local = [s for h in holidays for s in LOCAL_HOLIDAY_SEEDS.get(h["id"], {}).get(hl, [])]
                    for seed in list(dict.fromkeys(extra + rules.seeds + local + seeds))[:max_seeds]:
                        words = suggest(source, seed, region, hl, get)
                        results[f"{hl}:{seed}"] = words
                        # 带上「语言:种子词」：新加的种子词第一次查到的词是它的基准，不算市场变化
                        terms += [f"{source}|{hl}:{seed}\t{w.lower()}" for w in words]
                        sleep(REQUEST_GAP)
                data[source] = _ok(results, seeds=seeds, languages=langs)
            elif source == "holidays":
                data[source] = _ok(holidays, note="开斋节、宰牲节、白色星期五是近似日期，以官方公布为准")
            elif source == "erp_sales":
                since = (today - timedelta(days=30)).isoformat()
                rows = store.sales_summary(since, rules.shops or None)
                data[source] = _ok(rows, since=since) if rows else {
                    "status": "empty", "note": "近 30 天没有导入 ERP 数据（ecom-twin report import 表格.csv）"}
            elif source == "csv_import":
                data[source] = _ok(_read_csv(csv_path)) if csv_path else {
                    "status": "skipped", "note": "没给表格：把选品工具或 ERP 导出的 CSV 放进任务目录，用 csv_path 传进来"}
            elif source == "owner_input":
                data[source] = _ok(owner_input) if owner_input.strip() else {
                    "status": "skipped", "note": "负责人没提供数据"}
            elif source == "third_party_api":
                data[source] = _third_party(third_party or {}, region, langs[0], seeds, get)
            elif source == "miaoshou_1688":
                data[source] = {"status": "skipped", "note": "货源在找候选那一步用妙手采集（staging_collect），不在调研里取"}
        except Exception as e:  # noqa: BLE001 —— 一个数据源挂了不影响别的，原因写进快照
            data[source] = {"status": "failed", "error": f"{type(e).__name__}: {str(e)[:200]}"}

    terms = sorted(set(terms))
    seen = store.seen_research_terms(region, playbook.name)
    seen_groups = {t.split("\t", 1)[0] for t in seen}
    # 只有以前查过的「数据源|语言:种子词」下面冒出来的词才算新词；第一次调研、新加的种子词都是基准
    fresh = [t for t in set(terms) - seen if t.split("\t", 1)[0] in seen_groups]
    new_terms = sorted({t.split("\t", 1)[1] for t in fresh})
    new_by_group: dict[str, list[str]] = {}
    for t_ in fresh:
        group, word = t_.split("\t", 1)
        new_by_group.setdefault(group.split("|", 1)[1], []).append(word)  # 键是「语言:种子词」
    sid = store.add_research_snapshot(region, platform, playbook.name, playbook.version, task_id, data, terms, new_terms)
    snap = store.get_research_snapshot(sid)
    snap["new_by_group"] = {k: sorted(v) for k, v in new_by_group.items()}  # 只在这次返回里用（雷达判断连续冒新词）
    return snap


def _read_csv(path: Path, limit: int = 200) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8-sig")
    return [dict(row) for _, row in zip(range(limit), csv.DictReader(io.StringIO(text)))]


def _third_party(cfg: dict, region: str, lang: str, seeds: list[str], get: Callable[..., str]) -> dict:
    """负责人配置的第三方选品接口：[research.third_party] url / key_env。返回体里标了示例数据的不算数。"""
    url = (cfg.get("url") or "").strip()
    if not url:
        return {"status": "skipped", "note": "没配置第三方接口：settings.toml 的 [research.third_party] url"}
    host = urllib.parse.urlsplit(url).hostname or ""
    headers = {}
    key_env = cfg.get("key_env") or ""
    if key_env:
        if not os.environ.get(key_env):
            return {"status": "failed", "error": f"环境变量 {key_env} 没有配置接口密钥"}
        headers["Authorization"] = f"Bearer {os.environ[key_env]}"
    query = urllib.parse.urlencode({"region": region, "lang": lang, "seeds": ",".join(seeds)})
    payload = json.loads(get(f"{url}{'&' if '?' in url else '?'}{query}", headers, {host}))
    if isinstance(payload, dict) and (payload.get("isSample") or payload.get("is_sample")):
        return {"status": "sample", "note": "接口返回的是示例数据，不能当依据"}
    return _ok(payload)


# ---------------- 快照是否可用 ----------------
def _has_data(source: dict) -> bool:
    """状态是 ok 而且真的有内容：空列表、全空的下拉词结果都不算。"""
    if source.get("status") != "ok":
        return False
    items = source.get("items")
    if isinstance(items, dict):
        return any(v for v in items.values())
    return bool(items)


def snapshot_problems(snapshot: dict | None, rules: Rules, region: str, now: datetime | None = None) -> list[str]:
    """出清单前的闸：快照要存在、是这个国家的、没过期、至少有一个数据源真的取到了数据。"""
    if snapshot is None:
        return ["没有调研快照：先用 selection_research 调研"]
    problems = []
    if snapshot["region"] != region.upper():
        problems.append(f"快照 {snapshot['id']} 是 {snapshot['region']} 的，不是 {region.upper()} 的")
    now = now or datetime.now(timezone.utc)
    age = now - snapshot["created_at"]
    if age > timedelta(days=rules.research_max_age_days):
        problems.append(f"快照 {snapshot['id']} 已经 {age.days} 天了，超过剧本要求的 {rules.research_max_age_days} 天，重新调研")
    if not any(_has_data(v) for v in snapshot["data"].values()):
        problems.append(f"快照 {snapshot['id']} 里没有一个数据源取到数据，不能当依据")
    return problems
