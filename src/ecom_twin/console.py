"""后台控制台（DESIGN.md 12）：先做记忆维护——记忆库增删改、记忆草稿审核、召回记录、变更记录。

- 只用标准库 http.server，默认只监听 127.0.0.1
- 登录：启动时打印带令牌的链接，打开后写入 HttpOnly + SameSite=Strict 的 cookie；API 也接受 X-Console-Token 头
- 所有修改都写审计日志（DESIGN.md 12.3）
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import tomllib
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .config import load_raw, settings_from_raw, validate_settings
from .paths import SETTINGS_FILE
from .config_overrides import FIELDS, REPLACE, coerce, deep_merge, dump_toml, field_for, load_overrides
from .memory import MemoryError, MemoryStore, file_sha256
from .runtime import RUNTIMES, check_all
from .store import Store, utcnow

PAGE = Path(__file__).parent / "web" / "console.html"
COOKIE = "ecom_twin_console"
MEMORY_TYPES = ["experience", "calendar", "fact", "preference", "selection", "radar"]


def task_json(task) -> dict[str, Any]:
    """Only expose task overview fields, not session IDs, transcripts or chat identifiers."""
    fields = ("id", "title", "scene", "shop_key", "runtime", "current_step", "conclusion",
              "blocker", "waiting", "created_at", "updated_at")
    return {**{key: getattr(task, key) for key in fields},
            "status": task.status.value, "source": task.source.value}


def memory_json(m, graph: dict | None = None) -> dict[str, Any]:
    links = (graph or {}).get(m.name, {})
    # sha256：保存时带回来，期间文件被别处改过就拒绝保存（防覆盖）
    return {"slug": m.slug, "name": m.name, "description": m.description, "type": m.type, "body": m.body,
            "updated_at": m.path.stat().st_mtime, "sha256": file_sha256(m.path),
            "links": links.get("links", []), "linked_by": links.get("linked_by", []), "orphan": links.get("orphan", False)}


class ConsoleApp:
    """与 HTTP 无关的业务层，方便测试。"""

    def __init__(self, store: Store, memory: MemoryStore, settings_path: Path | None = None):
        self.store = store
        self.memory = memory
        self.settings_path = Path(settings_path) if settings_path else SETTINGS_FILE

    # ---- 规则配置 ----
    def _overrides_path(self) -> Path:
        return self.settings_path.with_name("settings.console.toml")

    @staticmethod
    def _effective(settings: Any, f: Any) -> Any:
        """当前生效的值（含默认值），而不是只看文件里写了什么。"""
        if f.section == "approvers":
            return {k: vars(v) for k, v in settings.approvers.items()}
        if f.section == "send_permissions":
            return {k: vars(v) for k, v in settings.send_permissions.items()}
        if f.section == "shadow":
            return settings.shadow_actions
        holder = {"limits": settings.limits, "feishu": settings.feishu, "intake": settings.intake,
                  "browser": settings.browser, "computer_use": settings.computer_use,
                  "runtime": settings.runtime, "outbound": settings.outbound,
                  "customer_service": settings.customer_service, "approval": settings}.get(f.section)
        return getattr(holder, f.key, None)

    def config_view(self) -> dict[str, Any]:
        settings = settings_from_raw(load_raw(self.settings_path))
        fields = []
        for f in FIELDS:
            fields.append({"section": f.section, "key": f.key, "label": f.label, "type": f.type,
                           "high_risk": f.high_risk, "value": self._effective(settings, f)})
        return {"fields": fields, "note": "保存后重启 ecom-twin serve 生效；改动写在 config/settings.console.toml，"
                                          "不会改动你手写的 settings.toml"}

    def _claude_binary(self) -> str:
        """本机 Claude Code 的路径：以配置 [runtime.cli] claude_code 为准，留空自动查找。"""
        try:
            return settings_from_raw(load_raw(self.settings_path)).runtime.cli.get("claude_code", "") or ""
        except (OSError, ValueError, TypeError, AttributeError):
            return ""

    def _doubtful(self) -> list[dict]:
        """判了「不相关」却写不出属于哪一类的：照样没处理，列出来给人看是不是漏了活。"""
        out = []
        for row in self.store.audits_by_event("triage_doubtful"):
            task = self.store.get_task(row["entity_id"])
            detail = json.loads(row["detail"] or "{}")
            out.append({"created_at": row["created_at"], "task_id": row["entity_id"],
                        "title": task.title if task else "", "reason": detail.get("reason", "")})
        return out

    # ---- 客服话术（DESIGN.md 8.7）：按「店铺/语言」一套套编辑，存进 [customer_service] profiles ----
    def cs_profiles_view(self) -> dict[str, Any]:
        from .cs import DEFAULT_VOICE
        cs = settings_from_raw(load_raw(self.settings_path)).customer_service
        return {"profiles": cs.profiles, "defaults": {"voice_samples": DEFAULT_VOICE, "banned_phrases": cs.banned_phrases,
                                                       "max_chars": cs.max_chars},
                "order": "按「店铺/语言」→「店铺」→「*/语言」→ default 的顺序取第一个有的，都没有用内置默认"}

    def cs_profile_save(self, key: str, body: dict[str, Any]) -> tuple[int, Any]:
        from .cs import normalize_profile
        profiles = dict(settings_from_raw(load_raw(self.settings_path)).customer_service.profiles)
        profiles[key] = normalize_profile(key, body)
        return self.config_update({"section": "customer_service", "key": "profiles", "value": profiles})

    def cs_profile_delete(self, key: str) -> tuple[int, Any]:
        profiles = dict(settings_from_raw(load_raw(self.settings_path)).customer_service.profiles)
        if key not in profiles:
            return 404, {"error": f"没有「{key}」这套话术"}
        profiles.pop(key)
        return self.config_update({"section": "customer_service", "key": "profiles", "value": profiles})

    def cs_preview(self, body: dict[str, Any]) -> dict[str, Any]:
        """拿一条买家消息和一段回复，看匹配哪套话术、过不过检查、判几级。不改任何东西。"""
        from .cs import check_reply, profile_for, risk_level
        cs = settings_from_raw(load_raw(self.settings_path)).customer_service
        shop = body.get("shop_key") or "default"
        profile = profile_for(cs, shop, body.get("language") or "en")
        platform = (shop.split(":", 1)[0] if ":" in shop else "tiktok").lower()
        problems = check_reply(body.get("draft", ""), profile, platform, body.get("buyer_message", ""))
        level, reasons = risk_level(body.get("buyer_message", ""), body.get("draft", ""))
        return {"profile": profile.key, "max_chars": profile.max_chars, "voice_samples": profile.voice_samples,
                "notes": profile.notes, "passed": not problems, "problems": problems, "risk_level": level, "risk_reasons": reasons}

    def _selection_ctx(self, region: str, platform: str):
        from .selection import load_playbooks, match_playbook
        playbooks, errors = load_playbooks(self.memory.all())
        return match_playbook(playbooks, region, platform), errors

    def _fx(self) -> dict:
        from .research import fx_rate
        try:
            return {"value": (fx := fx_rate(self.store))["rate"], "source": f"{fx['source']} {fx['date']}"}
        except Exception as e:  # noqa: BLE001 —— 取不到汇率就让人手填，不编一个数
            return {"value": None, "source": f"实时汇率取不到（{str(e)[:80]}），请手动填写"}

    def selection_defaults(self, query: dict[str, str]) -> tuple[int, Any]:
        """试算的自动填充：剧本的国家和平台、实时汇率、平台佣金率、按重量估的运费、按调研快照估的需求热度。"""
        from datetime import datetime, timezone
        from .research import estimate_demand
        from .selection import SELECTION_TYPE, PlaybookError, estimate_shipping, parse_rules
        current = self.memory.get(query.get("slug", "")) if query.get("slug") else None
        options = None
        if current is not None and current.type == SELECTION_TYPE:
            try:
                rules = parse_rules(current.body)
                options = {"regions": rules.regions, "platforms": rules.platforms}
            except PlaybookError:
                options = None
        region, platform = (query.get("region") or "").upper(), (query.get("platform") or "").lower()
        if not region and options:
            region = next((r for r in options["regions"] if r != "*"), "")
        if not region:
            return 200, {"options": options, "note": "先选国家"}
        try:
            playbook, _ = self._selection_ctx(region, platform)
        except PlaybookError as e:
            return 400, {"error": str(e), "options": options}
        rules = playbook.rules
        weight = query.get("weight")
        weight = float(weight) if weight not in (None, "") else None
        rate = rules.commission_rates.get(platform)
        ship = estimate_shipping(rules, weight)
        snapshot = self.store.latest_research_snapshot(region, playbook.name)
        demand = estimate_demand(snapshot, query.get("keyword", ""))
        age = (datetime.now(timezone.utc) - snapshot["created_at"]).days if snapshot else None
        return 200, {
            "playbook": playbook.name, "region": region, "platform": platform, "options": options,
            "exchange_rate": self._fx(),
            "commission_rate": {"value": rate, "source": f"剧本里 {platform} 的佣金率（初始值，以平台后台为准）"}
            if rate is not None else {"value": None, "source": "剧本没配这个平台的佣金率，请手动填写"},
            "shipping": {"value": ship, "source": f"剧本估算：首重 {rules.shipping_base or 0:g} + 每公斤 {rules.shipping_per_kg:g} 美元"}
            if ship is not None else {"value": None, "source": "填了重量才能估；剧本没配运费就请手动填写"},
            "demand": {"value": demand["score"], "source": demand["note"], "matches": demand["matches"],
                       "snapshot": snapshot["id"] if snapshot else None, "age_days": age,
                       "stale": snapshot is None or age > rules.research_max_age_days},
        }

    def selection_discover(self, body: dict[str, Any]) -> tuple[int, Any]:
        """控制台「找方向」：扫一遍品类库，代码打分排序（会访问公开接口，几十秒）。"""
        from .discovery import discover
        from .selection import PlaybookError
        region, platform = (body.get("region") or "").upper(), (body.get("platform") or "").lower()
        try:
            playbook, _ = self._selection_ctx(region, platform)
        except PlaybookError as e:
            return 400, {"error": str(e)}
        split = lambda v: [x.strip() for x in (v if isinstance(v, list) else re.split(r"[,，、\n]", v or "")) if x.strip()]  # noqa: E731
        result = discover(self.store, playbook, region, platform, exclude=split(body.get("exclude")),
                          focus=split(body.get("focus")), top=int(body.get("top") or 8))
        self.store.audit("research", result["快照"], "discover", "console", {"region": region})
        return 200, result

    def selection_research(self, body: dict[str, Any]) -> tuple[int, Any]:
        """在控制台直接做一次调研（会访问公开接口），把搜索词也当种子词查，查完就能估需求热度。"""
        from .research import run_research
        from .selection import PlaybookError
        region, platform = (body.get("region") or "").upper(), (body.get("platform") or "").lower()
        try:
            playbook, _ = self._selection_ctx(region, platform)
        except PlaybookError as e:
            return 400, {"error": str(e)}
        settings = settings_from_raw(load_raw(self.settings_path))
        keyword = (body.get("keyword") or "").strip()
        snap = run_research(self.store, playbook, region, platform, "", extra_seeds=[keyword] if keyword else [],
                            third_party={"url": settings.research.third_party_url,
                                         "key_env": settings.research.third_party_key_env})
        self.store.audit("research", snap["id"], "created", "console", {"region": region, "keyword": keyword})
        return 200, {"id": snap["id"], "sources": {k: v.get("status") for k, v in snap["data"].items()},
                     "new_terms": snap["new_terms"][:20]}

    # ---- 选品雷达 ----
    def _task_app(self):
        """控制台进程里建任务、排队用：任务服务每秒领一次队列，看板同步也由它负责。"""
        from types import SimpleNamespace
        from .board import BoardSync
        from .feishu import FakeFeishu
        from .tasks import TaskService
        settings = settings_from_raw(load_raw(self.settings_path))
        return SimpleNamespace(store=self.store, settings=settings,
                               tasks=TaskService(self.store, BoardSync(self.store, FakeFeishu(), settings.feishu)))

    def serve_alive(self) -> bool:
        from .radar import serve_alive
        return serve_alive(self.store)

    def radar_view(self) -> dict[str, Any]:
        from .radar import describe_schedule, load_rules, next_run
        rules, errors = load_rules(self.memory.all())
        slugs = {m.name: m.slug for m in self.memory.all()}
        out = []
        for r in rules:
            last = self.store.last_radar_run(r.name)
            nxt = next_run(r, self.store) if r.enabled else None
            out.append({"name": r.name, "slug": slugs.get(r.name, ""), "schedule": r.schedule,
                        "describe": describe_schedule(r.schedule), "timezone": r.timezone, "regions": r.regions,
                        "platform": r.platform, "watch": r.watch, "modifiers": r.modifiers, "enabled": r.enabled,
                        "expires_at": r.expires_at.isoformat() if r.expires_at else "", "max_leads": r.max_leads,
                        "next_run": nxt.isoformat() if nxt else None,
                        "next_run_local": f"{_WEEK[nxt.astimezone(r.tz).weekday()]} {nxt.astimezone(r.tz):%m-%d %H:%M}" if nxt else "",
                        "pending": bool(self.store.kv_get(f"radar:trigger:{r.name}")),
                        "last_run": {**last, "started_at": last["started_at"].isoformat(),
                                     "finished_at": last["finished_at"].isoformat() if last["finished_at"] else None}
                        if last else None})
        runs = [{**x, "started_at": x["started_at"].isoformat(),
                 "finished_at": x["finished_at"].isoformat() if x["finished_at"] else None}
                for x in self.store.list_radar_runs(limit=30)]
        return {"rules": out, "errors": errors, "runs": runs, "serve_alive": self.serve_alive()}

    def radar_create(self, body: dict[str, Any]) -> tuple[int, Any]:
        """控制台「新建雷达」：表单里的预设已经在页面上拼成了 cron，这里生成规则正文存成记忆。"""
        from .radar import describe_schedule, render_rule
        name = (body.get("name") or "").strip()
        if not name:
            return 400, {"error": "给规则起个名字，比如「雷达-沙特斋月」"}
        regions = [r.strip().upper() for r in (body.get("regions") or []) if str(r).strip()]
        watch = [w.strip() for w in (body.get("watch") or []) if str(w).strip()]
        schedule = (body.get("schedule") or "").strip()
        text = render_rule(name, schedule, body.get("timezone") or "Asia/Shanghai", regions, watch,
                           (body.get("platform") or "").strip().lower(),
                           [m.strip() for m in (body.get("modifiers") or []) if str(m).strip()],
                           int(body.get("max_leads") or 3), (body.get("expires_at") or "").strip(), body.get("note") or "")
        desc = (f"选品雷达规则：{describe_schedule(schedule)}盯{'、'.join(regions)}的{'、'.join(watch[:3])}"
                "，有新方向、节日窗口、痛点词就出线索")[:120]
        try:
            m = self.memory.save(name, desc, "radar", text, actor="console")
        except MemoryError as e:
            return 400, {"error": str(e)}
        return 200, {"slug": m.slug}

    def radar_trigger(self, slug: str) -> tuple[int, Any]:
        from .radar import request_trigger
        m = self.memory.get(slug)
        if m is None or m.type != "radar":
            return 404, {"error": "没有这条雷达规则"}
        request_trigger(self.store, m.name)
        self.store.audit("radar", m.name, "triggered", "console")
        alive = self.serve_alive()
        return 200, {"ok": True, "serve_alive": alive,
                     "note": "已排上，任务服务一分钟内会执行" if alive else "已排上，但任务服务没在运行，启动服务后才会执行"}

    def radar_toggle(self, slug: str, body: dict[str, Any]) -> tuple[int, Any]:
        m = self.memory.get(slug)
        if m is None or m.type != "radar":
            return 404, {"error": "没有这条雷达规则"}
        enabled = bool(body.get("enabled"))
        new_body, n = re.subn(r"(?m)^enabled\s*=\s*(true|false)\b.*$", f"enabled = {'true' if enabled else 'false'}", m.body)
        if not n:
            new_body = re.sub(r"(```toml\s*\n)", rf"\1enabled = {'true' if enabled else 'false'}\n", m.body, count=1)
        try:
            self.memory.save(m.name, m.description, m.type, new_body, slug=m.slug, actor="console")
        except MemoryError as e:
            return 400, {"error": str(e)}
        return 200, {"enabled": enabled}

    def lead_update(self, lid: str, body: dict[str, Any]) -> tuple[int, Any]:
        try:
            lead = self.store.update_lead(lid, body.get("status", ""), body.get("reason", ""))
        except KeyError as e:
            return 404, {"error": str(e)}
        except ValueError as e:
            return 400, {"error": str(e)}
        self.store.audit("lead", lid, f"status:{lead['status']}", "console", {"reason": lead["reason"]})
        return 200, {"lead": _jsonable(lead)}

    def lead_convert(self, lid: str) -> tuple[int, Any]:
        """线索转选品：建一个选品任务排进队列，任务服务接手，按剧本调研打分。"""
        from .paths import WORKSPACES_DIR
        from .radar import convert_lead
        try:
            r = convert_lead(self._task_app(), lid, WORKSPACES_DIR)
        except KeyError as e:
            return 404, {"error": str(e).strip("'")}
        except ValueError as e:
            return 400, {"error": str(e)}
        return 200, r

    # ---- 问分身（控制台对话） ----
    CHAT_PREFIX = ("负责人在控制台「问分身」对话框里说：\n{msg}\n\n"
                   "（这是对话，回复会直接显示在对话框里：先回答，再说做了什么，口语、简短。"
                   "要查情况先用 workspace_overview、lead_list、radar_list；要建雷达规则用 radar_rule_propose，"
                   "它只生成草稿，负责人在对话框里点「采纳」才生效。改线索、转选品、立即运行雷达要负责人明确说了才做。"
                   "问「做什么品」「有什么值得做」而手上没有候选时，按 product-discovery 先找方向（selection_discover），"
                   "不要因为缺货源价、没配妙手就停下来反问；最多问一个真正卡住的问题，其余按剧本默认值先做，"
                   "在回复里说明用了哪些默认值。网页打不开就跳过、写一句，不要反复重试。）")

    def chat_list(self) -> dict[str, Any]:
        from .runtime import RUNTIMES
        settings = settings_from_raw(load_raw(self.settings_path))
        convs = [{"id": t.id, "title": t.title, "runtime": t.runtime, "status": t.status.value,
                  "updated_at": t.updated_at.isoformat() if t.updated_at else None}
                 for t in self.store.list_conversations()]
        from .runtime.cli_check import CLI_SPECS
        runtimes = [{"key": k, "label": CLI_SPECS[k].label if k in CLI_SPECS else k, "default": k == settings.runtime.default}
                    for k in RUNTIMES]
        return {"conversations": convs, "runtimes": runtimes, "serve_alive": self.serve_alive()}

    def chat_send(self, body: dict[str, Any]) -> tuple[int, Any]:
        from .dispatcher import enqueue_prompt
        from .models import TaskSource
        from .paths import WORKSPACES_DIR
        from .runtime import RUNTIMES
        from .tasks import title_from_text
        msg = (body.get("message") or "").strip()
        if not msg:
            return 400, {"error": "说点什么吧"}
        app = self._task_app()
        cid = (body.get("conversation_id") or "").strip()
        if cid:
            task = self.store.get_task(cid)
            if task is None or task.source != TaskSource.CHAT:
                return 404, {"error": "没有这个对话"}
            resume = True
        else:
            runtime = body.get("runtime") or app.settings.runtime.default
            if runtime not in RUNTIMES:
                return 400, {"error": f"不认识的底座：{runtime}"}
            task = app.tasks.create(title_from_text(msg), TaskSource.CHAT, "待定", owner=app.settings.feishu.owner_open_id)
            task.runtime = runtime
            self.store.save_task(task)
            resume = False
        self.store.add_chat_message(task.id, "user", msg)
        enqueue_prompt(app, WORKSPACES_DIR, task.id, self.CHAT_PREFIX.format(msg=msg), resume=resume)
        return 200, {"conversation_id": task.id, "serve_alive": self.serve_alive()}

    def chat_get(self, cid: str) -> tuple[int, Any]:
        from .models import TaskSource
        task = self.store.get_task(cid)
        if task is None or task.source != TaskSource.CHAT:
            return 404, {"error": "没有这个对话"}
        q = self.store.get_task_queue(cid) or {}
        busy = q.get("state") in ("queued", "running") or task.status.value in ("排队中", "进行中", "执行中")
        drafts = [{"id": d["id"], "name": d["name"], "type": d["type"], "status": d["status"], "body": d["body"]}
                  for d in self.store.drafts_for_task(cid)]
        return 200, {"id": cid, "title": task.title, "runtime": task.runtime, "status": task.status.value,
                     "busy": busy, "queue_state": q.get("state", ""), "drafts": drafts,
                     "messages": [{"role": m["role"], "text": m["text"], "created_at": m["created_at"].isoformat()}
                                  for m in self.store.list_chat_messages(cid)]}

    # ---- 看板拖拽（照 Multica：任意卡片拖到任意一列改状态，同一列里拖动排序） ----
    RUNNING = ("进行中", "执行中")

    def task_reply(self, task_id: str, body: dict[str, Any]) -> tuple[int, Any]:
        """任务详情里继续输入：作为补充交给分身，在同一个会话里接着跑（续跑）。
        控制台对话的回复写回对话记录；飞书来的任务，分身的汇报照常回飞书原来的 thread。"""
        from .dispatcher import enqueue_prompt
        from .models import TaskSource
        from .paths import WORKSPACES_DIR
        task = self.store.get_task(task_id)
        if task is None:
            return 404, {"error": "任务不存在"}
        text = (body.get("text") or "").strip()
        if not text:
            return 400, {"error": "说点什么吧"}
        self.store.add_chat_message(task_id, "user", text)
        prompt = (self.CHAT_PREFIX.format(msg=text) if task.source == TaskSource.CHAT
                  else f"负责人在控制台的任务详情里补充：\n{text}\n\n按这条接着处理，做完照常汇报。")
        enqueue_prompt(self._task_app(), WORKSPACES_DIR, task_id, prompt, resume=True)
        self.store.audit("task", task_id, "console_reply", "console")
        alive = self.serve_alive()
        where = "回复会显示在这里" if task.source == TaskSource.CHAT else "分身的汇报会回到飞书原来的 thread，这里也能看到交付结果"
        return 200, {"ok": True, "note": ("已交给分身，" + where) if alive else "已排上，但任务服务没在运行，启动后才会执行"}

    def task_move(self, task_id: str, body: dict[str, Any]) -> tuple[int, Any]:
        from .dispatcher import enqueue_prompt
        from .models import TaskStatus
        from .paths import WORKSPACES_DIR
        task = self.store.get_task(task_id)
        if task is None:
            return 404, {"error": "任务不存在"}
        to = body.get("to", "")
        order = [str(x) for x in body.get("order") or []]
        same_column = to == body.get("from")
        if task.status.value in self.RUNNING and not same_column:
            return 400, {"error": f"{task_id} 分身正在跑，这一轮结束会覆盖你改的状态，等它跑完再拖"}
        note = "已调整顺序"
        if not same_column:
            app = self._task_app()
            dequeued = self.store.cancel_task_queue(task_id) if to != "running" else False
            if to == "pending":
                app.tasks.set_status(task_id, TaskStatus.PENDING)
                note = "已放回待处理，不会自动跑"
            elif to == "running":
                enqueue_prompt(app, WORKSPACES_DIR, task_id, "负责人在控制台看板上把任务拖到了「进行中」，请接着处理。", resume=True)
                note = "已让分身接着处理" + ("" if self.serve_alive() else "，但任务服务没在运行，启动后才会执行")
            elif to == "waiting":
                app.tasks.set_status(task_id, TaskStatus.WAITING, blocker=task.blocker or "负责人在看板上标为待确认")
                note = "已标为待确认"
            elif to == "closed":
                app.tasks.set_status(task_id, TaskStatus.CLOSED)
                note = "已关闭"
            elif to == "failed":
                app.tasks.set_status(task_id, TaskStatus.FAILED, blocker="负责人在看板上标为需关注")
                note = "已标为需关注"
            else:
                return 400, {"error": f"不认识的列：{to}"}
            if dequeued:
                note += "，这次排队已取消"
            self.store.audit("task", task_id, f"moved:{to}", "console", {"from": task.status.value})
        if order:
            self.store.set_board_order(order)
        return 200, {"ok": True, "note": note}

    def selection_preview(self, body: dict[str, Any]) -> tuple[int, Any]:
        """选品剧本试算：给一个样例商品，看匹配哪份剧本、卡在哪条门槛、打几分。只算不存。"""
        from .selection import PlaybookError, load_playbooks, match_playbook, score_candidate
        playbooks, errors = load_playbooks(self.memory.all())
        try:
            playbook = match_playbook(playbooks, body.get("region", ""), body.get("platform", ""))
        except PlaybookError as e:
            return 400, {"error": str(e) + (f"（另有剧本规则写错：{'；'.join(errors)}）" if errors else "")}
        from .selection import apply_defaults
        candidate = body.get("candidate") if isinstance(body.get("candidate"), dict) else {}
        candidate, filled = apply_defaults(candidate, playbook.rules, body.get("platform", ""))
        global_min = settings_from_raw(load_raw(self.settings_path)).limits.min_margin_rate
        try:
            result = score_candidate(candidate, playbook.rules, global_min)
            result["filled"] = filled
        except (TypeError, ValueError) as e:
            return 400, {"error": f"样例商品的数字填得不对：{e}"}
        recent = [{"id": s["id"], "region": s["region"], "created_at": s["created_at"].isoformat(),
                   "new_terms": s["new_terms"][:10],
                   "sources": {k: v.get("status") for k, v in s["data"].items()}}
                  for s in self.store.list_research_snapshots(50) if s["playbook"] == playbook.name][:5]
        return 200, {"playbook": playbook.name, "version": playbook.version, "result": result, "snapshots": recent}

    def config_update(self, body: dict[str, Any]) -> tuple[int, Any]:
        f = field_for(body.get("section", ""), body.get("key", ""))
        if f is None:
            return 400, {"error": "这个配置项不能在控制台修改"}
        try:
            value = coerce(f, body.get("value"))
        except (ValueError, TypeError, re.error) as e:
            return 400, {"error": f"值不合法：{e}"}
        overrides = load_overrides(self._overrides_path())
        before = self._effective(settings_from_raw(load_raw(self.settings_path)), f)
        if f.type == "json" and not f.key:  # 整节配置（审批人、代发权限）：整节替换
            overrides[f.section] = {REPLACE: True, **value}
        elif f.type == "json":  # 某一节里的一个字段（客服话术 profiles）：只替换这个字段，不动同节其它项
            overrides.setdefault(f.section, {})[f.key] = {REPLACE: True, **value}
        else:
            overrides.setdefault(f.section, {})[f.key] = value
        base = tomllib.loads(self.settings_path.read_text(encoding="utf-8")) if self.settings_path.exists() else {}
        try:
            settings = settings_from_raw(deep_merge(base, overrides))
            errors = [t for level, t in validate_settings(settings, list(RUNTIMES)) if level == "错误"]
        except (TypeError, ValueError) as e:
            return 400, {"error": f"配置无法加载：{e}"}
        if errors:
            hint = ("。负责人 Open ID、店铺白名单、飞书凭证请先在配置页填写：终端运行 ecom-twin setup"
                    if any("feishu." in e or "allowed_shops" in e for e in errors) else "")
            return 400, {"error": "保存后配置会出错：" + "；".join(errors) + hint}
        if f.high_risk and not body.get("confirm"):
            return 200, {"need_confirm": True, "label": f.label, "before": before, "after": value}
        self._overrides_path().write_text(
            "# 由控制台写入，叠加在 settings.toml 之上。可以手工删除某项来恢复 settings.toml 的值\n\n"
            + dump_toml(overrides), encoding="utf-8")
        self.store.audit("config", f"{f.section}.{f.key}" if f.key else f.section, "updated", "console",
                         {"before": before, "after": value, "high_risk": f.high_risk})
        return 200, {"ok": True}

    def route(self, method: str, path: str, query: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        parts = [unquote(p) for p in path.split("/") if p][1:]  # 去掉 "api"；中文标识在 URL 里是编码过的
        try:
            if parts == ["tasks"] and method == "GET":
                ranks = self.store.board_ranks()
                return 200, {"tasks": [{**task_json(task), "rank": ranks.get(task.id)} for task in self.store.list_tasks()]}
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "reply" and method == "POST":
                return self.task_reply(parts[1], body)
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "move" and method == "POST":
                return self.task_move(parts[1], body)
            if len(parts) == 2 and parts[0] == "tasks" and method == "GET":
                task = self.store.get_task(parts[1])
                if task is None:
                    return 404, {"error": "任务不存在"}
                steps = [{"name": s.name, "status": s.status.value, "summary": s.summary,
                          "error": s.error, "started_at": s.started_at, "ended_at": s.ended_at}
                         for s in self.store.list_steps(task.id)]
                q = self.store.get_task_queue(task.id) or {}
                messages = [{"role": m["role"], "text": m["text"], "created_at": m["created_at"].isoformat()}
                            for m in self.store.list_chat_messages(task.id)]
                return 200, {"task": task_json(task), "steps": steps, "messages": messages,
                             "busy": q.get("state") in ("queued", "running"), "queue_state": q.get("state", ""),
                             "serve_alive": self.serve_alive()}
            if parts == ["memories"] and method == "GET":
                graph = self.memory.graph()
                return 200, {"memories": [memory_json(m, graph) for m in self.memory.all()], "types": MEMORY_TYPES,
                             "index": self.memory.index_text(), "budget": self.memory.budget(),
                             "problems": [{"level": lv, "text": t} for lv, t in self.memory.check()]}
            if parts == ["memories"] and method == "POST":
                m = self.memory.save(body.get("name", ""), body.get("description", ""), body.get("type", ""),
                                     body.get("body", ""), slug=None)
                return 201, memory_json(m)
            if parts == ["memory-bundle"] and method == "GET":
                names = [n for n in (query.get("names") or "").split(",") if n.strip()]
                return 200, {"text": self.memory.export_bundle(names or None)}
            if parts == ["memory-bundle"] and method == "POST":
                return 200, self.memory.import_bundle(body.get("text", ""))
            if len(parts) == 3 and parts[0] == "memories" and parts[2] == "history" and method == "GET":
                return 200, {"history": self.memory.history(parts[1])}
            if len(parts) == 4 and parts[0] == "memories" and parts[2] == "history" and method == "GET":
                return 200, {"text": self.memory.read_snapshot(parts[1], parts[3])}
            if len(parts) == 3 and parts[0] == "memories" and parts[2] == "suggest-description" and method == "POST":
                return 200, self.memory.suggest_description(parts[1], binary=self._claude_binary())
            if len(parts) == 3 and parts[0] == "memories" and parts[2] == "rollback" and method == "POST":
                return 200, memory_json(self.memory.rollback(parts[1], body.get("file", "")))
            if len(parts) == 2 and parts[0] == "memories":
                if method == "PUT":
                    m = self.memory.save(body.get("name", ""), body.get("description", ""), body.get("type", ""),
                                         body.get("body", ""), slug=parts[1], expected_sha=body.get("sha256") or None)
                    return 200, memory_json(m)
                if method == "DELETE":
                    self.memory.delete(parts[1])
                    return 200, {"ok": True}
            if parts == ["drafts"] and method == "GET":
                drafts = self.store.list_memory_drafts(query.get("status", "待审核"))
                for d in drafts:  # 审核时要对着目标记忆的当前内容看
                    target = self.memory.by_name(d.get("target_name") or "") if d.get("mode") != "create" else None
                    d["target_body"] = target.body if target else None
                return 200, {"drafts": drafts, "stats": self.store.draft_stats()}
            if len(parts) == 3 and parts[0] == "drafts" and method == "POST":
                if parts[2] == "approve":
                    edits = {k: body[k] for k in ("name", "description", "type", "body", "target_section", "old_text")
                             if k in body}
                    path = self.memory.approve(parts[1], edits or None)
                    return 200, {"ok": True, "memory": path.stem}
                if parts[2] == "reject":
                    self.memory.reject(parts[1], note=body.get("note", ""))
                    return 200, {"ok": True}
            if parts == ["recalls"] and method == "GET":
                recalls = self.store.list_recalls(query.get("task") or None)
                return 200, {"recalls": list(reversed(recalls))[:500], "summary": self.store.recall_summary()}
            if parts == ["audit"] and method == "GET":
                return 200, {"events": self.store.audit_log(("memory", "memory_draft", "config"))}
            if parts == ["config"] and method == "GET":
                return 200, self.config_view()
            if parts == ["config"] and method == "POST":
                return self.config_update(body)
            if parts == ["intake"] and method == "GET":
                return 200, {"buffer": [dict(r) for g in self.store.buffer_groups() for r in g],
                             "decisions": self.store.merge_decisions(100),
                             "outbound": self.store.outbound_messages()[:100],
                             "doubtful": self._doubtful()}
            if parts == ["runtimes"] and method == "GET":
                settings = settings_from_raw(load_raw(self.settings_path))
                return 200, {"default": settings.runtime.default, "fallback": settings.runtime.fallback,
                             "runtimes": [vars(st) | {"usable": st.usable} for st in check_all(settings.runtime.cli)]}
            if parts == ["shadow"] and method == "GET":
                from .shadow import ShadowService
                settings = settings_from_raw(load_raw(self.settings_path))
                return 200, ShadowService(self.store, settings, {}).report()
            if parts == ["cs-profiles"] and method == "GET":
                return 200, self.cs_profiles_view()
            if len(parts) == 2 and parts[0] == "cs-profiles" and method == "PUT":
                return self.cs_profile_save(parts[1], body)
            if len(parts) == 2 and parts[0] == "cs-profiles" and method == "DELETE":
                return self.cs_profile_delete(parts[1])
            if parts == ["cs-preview"] and method == "POST":
                return 200, self.cs_preview(body)
            if parts == ["selection-preview"] and method == "POST":
                return self.selection_preview(body)
            if parts == ["chat"] and method == "GET":
                return 200, self.chat_list()
            if parts == ["chat"] and method == "POST":
                return self.chat_send(body)
            if len(parts) == 2 and parts[0] == "chat" and method == "GET":
                return self.chat_get(parts[1])
            if parts == ["radar"] and method == "GET":
                return 200, self.radar_view()
            if parts == ["radar"] and method == "POST":
                return self.radar_create(body)
            if len(parts) == 3 and parts[0] == "radar" and parts[2] == "trigger" and method == "POST":
                return self.radar_trigger(parts[1])
            if len(parts) == 3 and parts[0] == "radar" and parts[2] == "toggle" and method == "POST":
                return self.radar_toggle(parts[1], body)
            if parts == ["leads"] and method == "GET":
                return 200, {"leads": [_jsonable(x) for x in self.store.list_leads(query.get("status") or None)]}
            if len(parts) == 2 and parts[0] == "leads" and method == "POST":
                return self.lead_update(parts[1], body)
            if len(parts) == 3 and parts[0] == "leads" and parts[2] == "convert" and method == "POST":
                return self.lead_convert(parts[1])
            if parts == ["selection-defaults"] and method == "GET":
                return self.selection_defaults(query)
            if parts == ["selection-discover"] and method == "POST":
                return self.selection_discover(body)
            if parts == ["selection-research"] and method == "POST":
                return self.selection_research(body)
            if parts == ["cs-drafts"] and method == "GET":
                return 200, {"drafts": self.store.list_cs_drafts(query.get("status") or None),
                             "stats": self.store.cs_stats()}
            if len(parts) == 3 and parts[0] == "cs-drafts" and parts[2] == "decide" and method == "POST":
                self.store.decide_cs_draft(parts[1], body.get("status", ""), body.get("final_text", ""), body.get("note", ""))
                self.store.audit("cs_draft", parts[1], "decided", "console", {"status": body.get("status")})
                return 200, {"ok": True}
            if parts == ["listings"] and method == "GET":
                return 200, {"listings": self.store.listing_diffs(query.get("task") or None)}
            if parts == ["costs"] and method == "GET":
                month = query.get("month") or utcnow().strftime("%Y-%m")
                return 200, {"month": month, **{by: self.store.usage_summary(month, by)
                                                for by in ("runtime", "scene", "shop_key")}}
        except MemoryError as e:
            return 400, {"error": str(e)}
        return 404, {"error": "not found"}


# 角色（DESIGN.md 12：运营只能看和审阅，负责人才能改硬上限和代发权限）
_WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _jsonable(row: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


ROLE_OWNER, ROLE_REVIEWER = "负责人", "运营"


def allowed(role: str, method: str, path: str) -> bool:
    """运营：所有页面都能看，能采纳 / 驳回记忆草稿；改配置、改记忆库、导入、回滚都要负责人。"""
    if role == ROLE_OWNER or method == "GET":
        return True
    parts = [p for p in path.split("/") if p][1:]
    if method == "POST" and parts in (["cs-preview"], ["selection-preview"]):  # 预览、试算只算不改，运营也能用
        return True
    if method == "POST" and len(parts) == 2 and parts[0] == "leads":  # 线索由运营跟进
        return True
    if method != "POST" or len(parts) != 3:
        return False
    # 审阅记忆草稿；客服回复是运营复制发送的，发送结果也由运营记；线索转选品也由运营做
    return ((parts[0] == "drafts" and parts[2] in ("approve", "reject")) or (parts[0] == "cs-drafts" and parts[2] == "decide")
            or (parts[0] == "leads" and parts[2] == "convert"))


def make_handler(app: ConsoleApp, token: str, review_token: str = "") -> type[BaseHTTPRequestHandler]:
    tokens = {token: ROLE_OWNER, **({review_token: ROLE_REVIEWER} if review_token else {})}

    def role_of(supplied: str) -> str | None:
        return next((r for t, r in tokens.items() if supplied and hmac.compare_digest(supplied, t)), None)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ecom-twin-console"

        def log_message(self, fmt: str, *args: Any) -> None:  # 不打印每个请求
            pass

        def _role(self) -> str | None:
            supplied = self.headers.get("X-Console-Token", "")
            if not supplied:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                supplied = cookie[COOKIE].value if COOKIE in cookie else ""
            return role_of(supplied)

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8",
                  headers: dict[str, str] | None = None) -> None:
            data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False,
                                                                         default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str) -> None:
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/" and method == "GET":
                supplied = query.get("token", "")
                if role_of(supplied):  # 用带令牌的链接登录，然后去掉令牌
                    self._send(HTTPStatus.FOUND, b"", "text/plain", {
                        "Location": "/",
                        "Set-Cookie": f"{COOKIE}={supplied}; HttpOnly; SameSite=Strict; Path=/"})
                    return
                if not self._role():
                    self._send(401, "未登录：请使用启动时打印的带令牌链接打开控制台".encode(), "text/plain; charset=utf-8")
                    return
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                return
            if not url.path.startswith("/api/"):
                self._send(404, {"error": "not found"})
                return
            role = self._role()
            if not role:
                self._send(401, {"error": "未登录"})
                return
            if url.path == "/api/whoami":
                self._send(200, {"role": role})
                return
            if not allowed(role, method, url.path):
                app.store.audit("console", url.path, "forbidden", role, {"method": method})
                self._send(403, {"error": "运营账号只能查看、审阅记忆草稿、记客服回复的发送结果；改规则配置、记忆库、导入、回滚要用负责人的链接"})
                return
            body: dict[str, Any] = {}
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                try:
                    body = json.loads(self.rfile.read(length))
                except json.JSONDecodeError:
                    self._send(400, {"error": "请求体不是合法 JSON"})
                    return
            try:
                status, payload = app.route(method, url.path, query, body)
            except (ValueError, TypeError) as e:  # 输入格式不对（数字填成文字等）：给页面一个能看懂的 400
                status, payload = 400, {"error": f"输入不对：{e}"}
            except Exception as e:  # noqa: BLE001 —— 外网调研等出错也要回一个 JSON，不能让连接直接断
                app.store.audit("console", url.path, "error", role, {"error": f"{type(e).__name__}: {e}"[:300]})
                status, payload = 500, {"error": f"处理失败：{type(e).__name__}: {str(e)[:200]}"}
            self._send(status, payload)

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._handle("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle("DELETE")

    return Handler


def make_server(store: Store, memory: MemoryStore, host: str = "127.0.0.1", port: int = 8765,
                token: str | None = None, settings_path: Path | None = None,
                review_token: str | None = None) -> tuple[ThreadingHTTPServer, str]:
    """返回 (server, 负责人令牌)；运营令牌在 server.review_token。"""
    token = token or secrets.token_urlsafe(24)
    review_token = review_token or secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((host, port), make_handler(ConsoleApp(store, memory, settings_path), token, review_token))
    server.review_token = review_token  # type: ignore[attr-defined]
    return server, token
