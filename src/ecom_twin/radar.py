"""选品雷达：按用户配的时间盯市场，代码先比对信号，有信号才叫醒 agent 写成线索。

设计参考 Multica 的唤醒（wakeup）：
- 每条雷达规则自己定时（cron + 时区），可以设到期时间、暂停、立即触发，每次运行都有记录
- 没有信号就安静结束（相当于 wakeup 的 checkin），只在运行记录里留一行，不叫醒 agent、不发消息
- 有信号才建任务叫醒 agent：一次运行最多一个任务，任务的汇报就是这一次的雷达简报

规则是一条 type = radar 的记忆：「## 规则」里放 toml（什么时候跑、盯哪些词、什么算信号），
其余各节是给 agent 的关注说明。和选品剧本分工：剧本管「怎么判断」，雷达规则管「什么时候、盯什么」。
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

RADAR_TYPE = "radar"
RULES_SECTION = "规则"
_RULES_BLOCK = re.compile(r"```toml\s*\n(.*?)```", re.S)
_REGION = re.compile(r"^[A-Z]{2}$")
MAX_WATCH = 10
MAX_MODIFIERS = 15
MAX_RADAR_SEEDS = 30
# 英语里表示「用户在找解决办法、在比较、在抱怨」的说法：这类下拉词就是痛点
PAIN_PATTERN = re.compile(r"\b(for|best|vs|alternative|alternatives|not working|problem|problems|fix|without|safe|"
                          r"cheap|that doesn'?t|won'?t|too)\b", re.I)


class RadarError(ValueError):
    """规则写错了：message 直接给人看。"""


# ---------------- cron ----------------
_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def _field(spec: str, low: int, high: int, names: dict[str, int] | None = None) -> set[int]:
    values: set[int] = set()
    for part in spec.lower().split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) <= 0:
                raise RadarError(f"cron 里的步长「{step_s}」不对")
            step = int(step_s)
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _num(a, names), _num(b, names)
        else:
            start = end = _num(part, names)
        if not (low <= start <= high and low <= end <= high and start <= end):
            raise RadarError(f"cron 里的「{part}」超出范围 {low}~{high}")
        values.update(range(start, end + 1, step))
    return values


def _num(s: str, names: dict[str, int] | None) -> int:
    if names and s in names:
        return names[s]
    if not s.isdigit():
        raise RadarError(f"cron 里的「{s}」不是数字")
    return int(s)


@dataclass
class Cron:
    expr: str
    minutes: set[int]
    hours: set[int]
    days: set[int]
    months: set[int]
    weekdays: set[int]  # 0 = 周日
    any_day: bool
    any_weekday: bool

    @classmethod
    def parse(cls, expr: str) -> "Cron":
        parts = (expr or "").split()
        if len(parts) != 5:
            raise RadarError(f"schedule 要写 5 段的 cron（分 时 日 月 周），比如 \"0 9 * * *\" 是每天 9 点，现在是「{expr}」")
        weekdays = _field(parts[4], 0, 7, _DOW_NAMES)
        if 7 in weekdays:
            weekdays = (weekdays - {7}) | {0}
        return cls(expr, _field(parts[0], 0, 59), _field(parts[1], 0, 23), _field(parts[2], 1, 31),
                   _field(parts[3], 1, 12), weekdays, parts[2] == "*", parts[4] == "*")

    def _day_ok(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom, dow = d.day in self.days, (d.weekday() + 1) % 7 in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return dow
        if self.any_weekday:
            return dom
        return dom or dow  # 标准 cron：日和周都写了，满足其一就行

    def next_after(self, after: datetime, tz: ZoneInfo) -> datetime | None:
        """after 之后（不含）下一次触发的时间，UTC。两年内找不到就返回 None。"""
        local = after.astimezone(tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = local.date()
        for _ in range(366 * 2):
            if self._day_ok(day):
                for h in sorted(self.hours):
                    for m in sorted(self.minutes):
                        cand = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
                        if cand >= local:
                            return cand.astimezone(timezone.utc)
            day += timedelta(days=1)
            local = datetime(day.year, day.month, day.day, tzinfo=tz)
        return None


_WEEKDAY_CN = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]


def describe_schedule(expr: str) -> str:
    """把常见的 cron 翻译成人话：每天 09:00、每周一 09:00、每月 1 日 09:00、每 6 小时。认不出的原样返回。"""
    try:
        c = Cron.parse(expr)
    except RadarError:
        return expr
    m, h, dom, mon, dow = expr.split()
    every_h = re.fullmatch(r"\*/(\d+)", h)
    if m.isdigit() and every_h and dom == mon == dow == "*":
        return f"每 {every_h.group(1)} 小时（第 {int(m)} 分）"
    if not (m.isdigit() and len(c.hours) >= 1 and mon == "*"):
        return expr
    times = "、".join(f"{hh:02d}:{int(m):02d}" for hh in sorted(c.hours))
    if dom == "*" and dow == "*":
        return f"每天 {times}"
    if dom == "*":
        return f"每{'、'.join(_WEEKDAY_CN[d] for d in sorted(c.weekdays))} {times}"
    if dow == "*":
        return f"每月 {'、'.join(str(d) for d in sorted(c.days))} 日 {times}"
    return expr


# ---------------- 规则 ----------------
@dataclass
class RadarRule:
    name: str
    schedule: str
    timezone: str
    regions: list[str]
    watch: list[str]
    platform: str = ""
    modifiers: list[str] = field(default_factory=list)
    new_terms_min: int = 5
    rising_runs: int = 2
    holiday_weeks: int = 8
    pain_terms: bool = True
    max_leads: int = 3
    expires_at: date | None = None
    enabled: bool = True
    instruction: str = ""
    version: str = ""

    @property
    def cron(self) -> Cron:
        return Cron.parse(self.schedule)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


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


def _strs(raw: dict, key: str, limit: int) -> list[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise RadarError(f"{key} 要写成字符串列表，例如 {key} = [\"a\", \"b\"]")
    if len(value) > limit:
        raise RadarError(f"{key} 最多 {limit} 个，现在 {len(value)} 个：盯太多会被搜索接口限流")
    return [v.strip() for v in value]


def _int(raw: dict, key: str, low: int, high: int, default: int) -> int:
    v = raw.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
        raise RadarError(f"{key} 要写 {low}~{high} 的整数")
    return v


def parse_radar(name: str, body: str, version: str = "") -> RadarRule:
    section = _section(body, RULES_SECTION)
    if section is None or not (m := _RULES_BLOCK.search(section)):
        raise RadarError(f"雷达规则要有「## {RULES_SECTION}」一节，里面放一个 ```toml 代码块")
    try:
        raw = tomllib.loads(m.group(1))
    except tomllib.TOMLDecodeError as e:
        raise RadarError(f"规则不是合法的 toml：{e}") from e
    known = {"schedule", "timezone", "regions", "platform", "watch", "modifiers", "signals", "max_leads",
             "expires_at", "enabled"}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise RadarError(f"不认识的规则项：{'、'.join(unknown)}。能用的有：{'、'.join(sorted(known))}")
    schedule = raw.get("schedule")
    if not isinstance(schedule, str):
        raise RadarError("schedule 必填：什么时候跑，比如 \"0 9 * * *\" 是每天 9 点（控制台里可以用预设生成）")
    Cron.parse(schedule)
    tz = raw.get("timezone", "Asia/Shanghai")
    try:
        ZoneInfo(str(tz))
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise RadarError(f"timezone「{tz}」不认识，写 IANA 时区名，比如 Asia/Shanghai、America/Los_Angeles") from e
    regions = [r.upper() for r in _strs(raw, "regions", 10)]
    if not regions or any(not _REGION.match(r) for r in regions):
        raise RadarError("regions 必填，写两位国家代码，比如 [\"US\"]、[\"SA\", \"AE\"]")
    watch = _strs(raw, "watch", MAX_WATCH)
    if not watch:
        raise RadarError("watch 必填：盯哪些品类词，比如 [\"heated eye mask\"]")
    signals = raw.get("signals", {})
    if not isinstance(signals, dict) or set(signals) - {"new_terms_min", "rising_runs", "holiday_weeks", "pain_terms"}:
        raise RadarError("signals 只能写 new_terms_min、rising_runs、holiday_weeks、pain_terms")
    if "pain_terms" in signals and not isinstance(signals["pain_terms"], bool):
        raise RadarError("signals.pain_terms 写 true 或 false")
    expires = raw.get("expires_at")
    if expires is not None:
        if isinstance(expires, str):
            try:
                expires = date.fromisoformat(expires)
            except ValueError as e:
                raise RadarError("expires_at 写成 \"2027-03-31\" 这样的日期") from e
        elif isinstance(expires, datetime):
            expires = expires.date()
        elif not isinstance(expires, date):
            raise RadarError("expires_at 写成 \"2027-03-31\" 这样的日期")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RadarError("enabled 写 true 或 false")
    instruction = "\n\n".join(f"## {t}\n{s}" for t, s in _sections(body) if t and t != RULES_SECTION).strip()
    return RadarRule(
        name=name, schedule=schedule, timezone=str(tz), regions=regions, watch=watch,
        platform=str(raw.get("platform", "")).lower(), modifiers=_strs(raw, "modifiers", MAX_MODIFIERS),
        new_terms_min=_int(signals, "new_terms_min", 1, 200, 5), rising_runs=_int(signals, "rising_runs", 2, 30, 2),
        holiday_weeks=_int(signals, "holiday_weeks", 0, 26, 8), pain_terms=signals.get("pain_terms", True),
        max_leads=_int(raw, "max_leads", 1, 10, 3), expires_at=expires, enabled=enabled,
        instruction=instruction, version=version)


def _sections(body: str) -> list[tuple[str, str]]:
    out, title, lines = [], "", []
    for line in body.splitlines():
        h = re.match(r"^##\s+(.+?)\s*$", line)
        if h:
            if title or "".join(lines).strip():
                out.append((title, "\n".join(lines).strip()))
            title, lines = h.group(1), []
        else:
            lines.append(line)
    if title or "".join(lines).strip():
        out.append((title, "\n".join(lines).strip()))
    return out


def load_rules(memories) -> tuple[list[RadarRule], list[str]]:
    from .memory import file_sha256
    rules, errors = [], []
    for m in memories:
        if m.type != RADAR_TYPE:
            continue
        try:
            rules.append(parse_radar(m.name, m.body, file_sha256(m.path)[:12]))
        except RadarError as e:
            errors.append(f"《{m.name}》：{e}")
    return rules, errors


def render_rule(name: str, schedule: str, tz: str, regions: list[str], watch: list[str], platform: str = "",
                modifiers: list[str] | None = None, max_leads: int = 3, expires_at: str = "", note: str = "") -> str:
    """控制台「新建雷达」和对话里建规则用：生成规则正文（toml 块 + 关注说明）。"""
    def arr(xs):
        return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in xs) + "]"
    lines = [f'schedule = "{schedule}"            # {describe_schedule(schedule)}', f'timezone = "{tz}"',
             f"regions = {arr(regions)}"]
    if platform:
        lines.append(f'platform = "{platform}"')
    lines += [f"watch = {arr(watch)}", f"modifiers = {arr(modifiers or [])}",
              "signals = { new_terms_min = 5, rising_runs = 2, holiday_weeks = 8, pain_terms = true }",
              f"max_leads = {max_leads}"]
    if expires_at:
        lines.append(f'expires_at = "{expires_at}"')
    lines.append("enabled = true")
    return ("## 规则\n\n```toml\n" + "\n".join(lines) + "\n```\n\n## 关注说明\n\n"
            + (note.strip() or "（写给分身看：重点关注什么、什么样的方向不用报）") + "\n")


# ---------------- 到期判断 ----------------
def next_run(rule: RadarRule, store, now: datetime | None = None) -> datetime | None:
    """下一次运行时间。从上次运行（没运行过就从第一次看到这条规则的时间）往后找。"""
    now = now or datetime.now(timezone.utc)
    last = store.last_radar_run(rule.name)
    base = last["started_at"] if last else _first_seen(store, rule.name, now)
    nxt = rule.cron.next_after(base, rule.tz)
    if nxt and rule.expires_at and nxt.astimezone(rule.tz).date() > rule.expires_at:
        return None
    return nxt


def _first_seen(store, name: str, now: datetime) -> datetime:
    key = f"radar:seen:{name}"
    seen = store.kv_get(key)
    if seen:
        return datetime.fromisoformat(seen)
    store.kv_set(key, now.isoformat())
    return now


def is_due(rule: RadarRule, store, now: datetime) -> bool:
    if not rule.enabled:
        return False
    if rule.expires_at and now.astimezone(rule.tz).date() > rule.expires_at:
        return False  # 过期了就不再补跑错过的那一次
    nxt = next_run(rule, store, now)
    return nxt is not None and nxt <= now


def request_trigger(store, name: str) -> None:
    """控制台「立即运行」：记一条请求，任务服务下一次检查时执行。"""
    store.kv_set(f"radar:trigger:{name}", datetime.now(timezone.utc).isoformat())


# ---------------- 信号 ----------------
def _seen_signal(store, rule: str, key: str) -> bool:
    k = f"radar:signal:{rule}:{key}"
    if store.kv_get(k):
        return True
    store.kv_set(k, datetime.now(timezone.utc).isoformat())
    return False


def detect_signals(store, rule: RadarRule, region: str, snap: dict, today: date) -> list[dict]:
    """代码比对出的信号。同一个信号只报一次（节日窗口按节日、痛点按词、连续冒新词按种子词和次数）。"""
    out: list[dict] = []
    sid = snap["id"]
    new_terms = snap.get("new_terms") or []
    if len(new_terms) >= rule.new_terms_min:
        out.append({"type": "新词", "region": region, "snapshot": sid, "count": len(new_terms), "words": new_terms[:20]})
    groups = snap.get("new_by_group") or {}
    watched = [g for g in _all_groups(snap)]
    for group in watched:
        key = f"radar:streak:{rule.name}:{region}:{group}"
        streak = int(store.kv_get(key) or 0)
        streak = streak + 1 if groups.get(group) else 0
        store.kv_set(key, str(streak))
        if streak == rule.rising_runs:
            out.append({"type": "连续冒新词", "region": region, "snapshot": sid, "group": group, "runs": streak,
                        "words": groups[group][:10]})
    if rule.holiday_weeks:
        for h in (snap["data"].get("holidays") or {}).get("items") or []:
            if h["days_left"] <= rule.holiday_weeks * 7 and not _seen_signal(store, rule.name, f"holiday:{region}:{h['id']}:{h['date']}"):
                out.append({"type": "节日备货窗口", "region": region, "snapshot": sid, "holiday": h["name"],
                            "date": h["date"], "days_left": h["days_left"]})
    if rule.pain_terms:
        pains = [w for w in new_terms if PAIN_PATTERN.search(w) and not _seen_signal(store, rule.name, f"pain:{region}:{w}")]
        if pains:
            out.append({"type": "痛点词", "region": region, "snapshot": sid, "words": pains[:15]})
    return out


def _all_groups(snap: dict) -> list[str]:
    groups = []
    for source in ("google_suggest", "bing_suggest", "youtube_suggest"):
        groups += list(((snap["data"].get(source) or {}).get("items") or {}).keys())
    return sorted(set(groups))


def radar_seeds(rule: RadarRule) -> list[str]:
    """盯的词，加上「品类词 + 修饰词」的组合（挖细分方向）。"""
    combos = [f"{w} {m}" for w in rule.watch for m in rule.modifiers]
    return list(dict.fromkeys(rule.watch + combos))[:MAX_RADAR_SEEDS]


# ---------------- 跑一次 ----------------
def run_rule(app, memory, rule: RadarRule, submit: Callable[[str, str], None] | None, trigger: str = "定时",
             now: datetime | None = None, research: Callable[..., dict] | None = None) -> dict:
    """跑一次雷达规则：逐个国家调研、比对信号。没信号安静结束；有信号建一个任务叫醒 agent。"""
    from .models import TaskSource
    from .research import run_research
    from .selection import PlaybookError, load_playbooks, match_playbook

    now = now or datetime.now(timezone.utc)
    research = research or run_research
    playbooks, _ = load_playbooks(memory.all())
    signals, snapshots, notes = [], [], []
    for region in rule.regions:
        try:
            pb = match_playbook(playbooks, region, rule.platform)
        except PlaybookError as e:
            notes.append(f"{region}：{e}")
            continue
        snap = research(app.store, pb, region, rule.platform, "", extra_seeds=radar_seeds(rule),
                        max_seeds=MAX_RADAR_SEEDS)
        snapshots.append(snap["id"])
        failed = [k for k, v in snap["data"].items() if v.get("status") == "failed"]
        if failed:
            notes.append(f"{region} 取数失败：{'、'.join(failed)}")
        found = detect_signals(app.store, rule, region, snap, now.astimezone(rule.tz).date())
        signals += found
        notes.append(f"{region}：新词 {len(snap.get('new_terms') or [])} 个" + (f"，信号 {len(found)} 个" if found else ""))
    if not signals:
        note = "检查过，没有新信号（" + "；".join(notes) + "）"
        app.store.add_radar_run(rule.name, trigger, "安静", note, [], snapshots, started_at=now)
        return {"status": "安静", "note": note, "snapshots": snapshots}
    task_id = ""
    if submit is not None:
        owner = app.settings.feishu.owner_open_id
        task = app.tasks.create(f"选品雷达 · {rule.name} · {now.astimezone(rule.tz):%m-%d}", TaskSource.SCHEDULE, "选品",
                                owner=owner)
        task_id = task.id
        submit(task_id, radar_prompt(rule, signals))
    note = f"发现 {len(signals)} 个信号（" + "；".join(notes) + "）" + (f"，已交给分身分析 {task_id}" if task_id else "")
    app.store.add_radar_run(rule.name, trigger, "有信号", note, signals, snapshots, task_id, started_at=now)
    return {"status": "有信号", "note": note, "signals": signals, "snapshots": snapshots, "task_id": task_id}


def radar_prompt(rule: RadarRule, signals: list[dict]) -> str:
    lines = [f"选品雷达《{rule.name}》到点检查，代码比对出下面这些信号（数字和词都是代码从调研快照里取的，不要改写）：", ""]
    for s in signals:
        detail = {k: v for k, v in s.items() if k not in ("type", "region", "snapshot")}
        lines.append(f"- 【{s['type']}】{s['region']} · 快照 {s['snapshot']} · {json.dumps(detail, ensure_ascii=False)}")
    lines += ["", "负责人写的关注说明：", rule.instruction or "（没写）", "",
              f"请判断哪些值得跟进，最多出 {rule.max_leads} 条线索，每条用 radar_lead_submit 提交："
              "写清国家、方向、依据（快照编号和里面的原词）、建议下一步（比如拿哪几个品去选品打分）。"
              "需要门槛参考就用 selection_playbook 看对应国家的剧本。只是新闻、人名、和电商无关的词不用报；"
              "都不值得跟进就不提交线索，直接说一句为什么。最后用三五句话汇报：出了哪几条线索、为什么。"]
    return "\n".join(lines)


def run_due(app, memory, submit: Callable[[str, str], None] | None, now: datetime | None = None,
            research: Callable[..., dict] | None = None) -> list[dict]:
    """调度器每分钟调一次：跑到期的规则和控制台点了「立即运行」的规则。"""
    now = now or datetime.now(timezone.utc)
    rules, _ = load_rules(memory.all())
    done = []
    for rule in rules:
        flag = app.store.kv_get(f"radar:trigger:{rule.name}")
        if flag:
            app.store.kv_set(f"radar:trigger:{rule.name}", "")
            done.append(run_rule(app, memory, rule, submit, "手动", now, research))
        elif is_due(rule, app.store, now):
            done.append(run_rule(app, memory, rule, submit, "定时", now, research))
    return done


# ---------------- 控制台和 agent 共用 ----------------
def serve_alive(store) -> bool:
    """任务服务在不在：serve 每 20 秒写一次心跳。"""
    beat = store.kv_get("serve:heartbeat")
    if not beat:
        return False
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(beat) < timedelta(seconds=90)
    except ValueError:
        return False


def convert_lead(app, lead_id: str, workroot) -> dict:
    """线索转选品：原子地占住线索，建选品任务排进队列。控制台按钮和分身的 lead_convert 都走这里。"""
    from .dispatcher import enqueue_prompt
    from .models import TaskSource
    from .tasks import title_from_text
    lead = app.store.claim_lead_conversion(lead_id)
    if lead is None:
        raise KeyError(f"没有线索 {lead_id}")
    if not lead.get("_claimed"):
        if lead["status"] == "已转选品":
            raise ValueError(f"已经转过了：{lead['selection_task_id'] or '正在转'}")
        raise ValueError(f"线索已经是「{lead['status']}」，要先改回「在看」再转")
    try:
        task = app.tasks.create(title_from_text(f"选品 · {lead['region']} · {lead['title']}"), TaskSource.SELF_DM, "选品",
                                owner=app.settings.feishu.owner_open_id)
        enqueue_prompt(app, workroot, task.id, (
            f"负责人把雷达线索 {lead_id} 转成了选品任务，按 product-selection 的步骤做：\n"
            f"国家：{lead['region']}　平台：{lead['platform'] or '未指定'}\n方向：{lead['title']}\n{lead['direction']}\n"
            f"依据：{lead['evidence']}\n雷达给的建议：{lead['suggestion']}\n\n"
            "先调研（把这个方向的词加进 owner_data 或作为关键词），再找候选并打分，交清单给负责人。"), resume=False)
    except Exception:
        app.store.set_lead_selection_task(lead_id, "", revert_to=lead["status"])
        raise
    app.store.set_lead_selection_task(lead_id, task.id)
    return {"task_id": task.id,
            "note": "已建选品任务" + ("，任务服务会接手" if serve_alive(app.store) else "，任务服务没在运行，启动后才会执行")}
