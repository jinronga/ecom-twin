"""客服回复起草（DESIGN.md 8.7，客服-0：只起草，不接平台接口）。

运营把买家消息转进飞书，分身按店铺 × 语言的话术设置起草，过确定性检查后存成草稿，人复制到平台后台发送。
这里放不依赖模型的部分：话术设置怎么取、回复检查、风险分级、买家隐私打码。

- 风险分级由代码按关键词和金额判定，模型给的只作参考：花钱或改订单（退款、补发、补偿、取消、改地址）是三级，
  回复里有对外承诺（会在几天内、保证）是二级，其余是一级。拿不准就往高里算
- 回复里出现具体金额、或直接答应退款补偿的，一律拒收：花钱的动作要负责人定（客服-3 才接提案审批）
- 买家的邮箱、电话在存档前打码；回复里也不能带出来
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .compliance import ComplianceChecker

# 三级：花钱或改订单。买家提出的、回复里答应的，都算
MONEY_OR_ORDER = re.compile(
    r"refund|reimburs|compensat|money back|re-?ship|resend|send (you )?(a )?(new|another|replacement)|replacement|"
    r"cancel(l?ation)?|change (the |my |your )?address|discount code|coupon|"
    r"退款|退钱|补发|重发|补偿|赔偿|赔付|优惠券|取消订单|改地址|换货|退货", re.I)
AMOUNT = re.compile(r"[$¥€£]\s?\d|\d+(\.\d+)?\s?(usd|aed|sar|rmb|美元|元|迪拉姆|里亚尔)\b|\d+\s?%\s?(off|折扣)", re.I)
# 回复里答应了花钱的动作（不是「我们会核实」）
MONEY_PROMISE = re.compile(
    r"(we('| wi)ll|we have|i('| wi)ll|i have|已经?|会|马上|立即)[^.。!！?？\n]{0,20}"
    r"(refund|reimburs|compensat|re-?ship|resend|send (you )?(a )?(new|another|replacement)|退款|补发|补偿|赔偿|重发)", re.I)
# 二级：对外承诺
COMMITMENT = re.compile(
    r"within \d+|by (tomorrow|monday|tuesday|wednesday|thursday|friday|the end)|guarantee|promise|"
    r"\d+\s?(个)?(工作日|天|小时)内|保证|承诺|一定会|马上(给|为)您", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE = re.compile(r"(?<![\w-])\+?\d[\d\s()-]{7,}\d(?![\w-])")


def mask_pii(text: str) -> str:
    """买家邮箱、电话打码（存档、汇报、看板都用打过码的）。地址没法可靠识别，提示词要求不转述。"""
    return PHONE.sub("<电话>", EMAIL.sub("<邮箱>", text or ""))


@dataclass
class CsSettings:
    """[customer_service]：客服话术设置。profiles 按「店铺/语言」「店铺」「*/语言」「default」的顺序取第一个有的。"""
    max_chars: int = 600
    # 一行一组：前面是空格或逗号隔开的词，=> 后面是拦下时告诉 agent 怎么改。平台对卖家消息的通行限制
    banned_phrases: list[str] = field(default_factory=lambda: [
        "5-star, five star, 5 stars, positive review, leave a review, good review, 好评, 五星 => 不能诱导或交换好评",
        "remove your review, change your review, delete your review, update your review, 删评, 改评, 撤评 => 不能要求买家删改评价",
        "pay outside, paypal, bank transfer, western union, 私下转账, 线下付款 => 不能引导站外交易",
        "contact us at, email us, call us, 加我, 私聊我 => 不能把买家引到站外联系，回复都在平台内",
        "it's not our fault, not our problem, 不是我们的问题, 你自己 => 不推责给买家，先说能做什么",
    ])
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class CsProfile:
    key: str
    voice_samples: list[str]
    banned_phrases: list[str]
    max_chars: int
    notes: str


DEFAULT_VOICE = ["Hi there, thanks for reaching out! Let me check that for you.",
                 "Sorry for the wait. Your package is on the way and should arrive soon."]


def profile_for(settings: CsSettings, shop_key: str, language: str) -> CsProfile:
    language = (language or "en").lower()
    for key in (f"{shop_key}/{language}", shop_key, f"*/{language}", "default"):
        if key in settings.profiles:
            p = settings.profiles[key] or {}
            return CsProfile(key, list(p.get("voice_samples") or DEFAULT_VOICE),
                             list(settings.banned_phrases) + list(p.get("banned_phrases") or []),
                             int(p.get("max_chars") or settings.max_chars), str(p.get("notes") or ""))
    return CsProfile("内置默认", list(DEFAULT_VOICE), list(settings.banned_phrases), settings.max_chars, "")


PROFILE_KEY = re.compile(r"^(default|\*/[a-z]{2,5}|[a-z]+:\d+(/[a-z]{2,5})?)$")
PROFILE_LIMITS = {"voice_samples": 12, "voice_sample_chars": 200, "banned_phrases": 40, "notes_chars": 500,
                  "max_chars": (50, 2000)}


def normalize_profile(key: str, body: dict[str, Any]) -> dict[str, Any]:
    """控制台「客服话术」页提交的一套话术 → 存进配置的样子；不合法就抛 ValueError（报错直接给页面看）。"""
    if not PROFILE_KEY.match(key or ""):
        raise ValueError("名称要写成「平台:店铺ID/语言」「平台:店铺ID」「*/语言」或 default，例如 tiktok:7300/en")
    def lines(value: Any) -> list[str]:
        items = value if isinstance(value, list) else str(value or "").split("\n")
        return [" ".join(str(x).split()) for x in items if str(x).strip()]
    voice, banned = lines(body.get("voice_samples")), lines(body.get("banned_phrases"))
    if len(voice) > PROFILE_LIMITS["voice_samples"]:
        raise ValueError(f"口吻样例最多 {PROFILE_LIMITS['voice_samples']} 句")
    long = next((v for v in voice if len(v) > PROFILE_LIMITS["voice_sample_chars"]), None)
    if long:
        raise ValueError(f"口吻样例每句不超过 {PROFILE_LIMITS['voice_sample_chars']} 字：{long[:30]}…")
    if len(banned) > PROFILE_LIMITS["banned_phrases"]:
        raise ValueError(f"补充禁用说法最多 {PROFILE_LIMITS['banned_phrases']} 组")
    out: dict[str, Any] = {}
    if voice:
        out["voice_samples"] = voice
    if banned:
        out["banned_phrases"] = banned
    if body.get("max_chars") not in (None, ""):
        n = int(body["max_chars"])
        low, high = PROFILE_LIMITS["max_chars"]
        if not low <= n <= high:
            raise ValueError(f"字数上限在 {low} 到 {high} 之间")
        out["max_chars"] = n
    notes = " ".join(str(body.get("notes") or "").split())
    if len(notes) > PROFILE_LIMITS["notes_chars"]:
        raise ValueError(f"备注不超过 {PROFILE_LIMITS['notes_chars']} 字（售后政策细节放记忆库）")
    if notes:
        out["notes"] = notes
    if not out:
        raise ValueError("至少填一项；什么都不填就等于用默认，直接删掉这套即可")
    return out


def _phrase_rules(lines: list[str]) -> list[tuple[list[str], str]]:
    rules = []
    for raw in lines:
        words, _, hint = str(raw).partition("=>")
        items = [w.strip() for w in re.split(r"[,，、|]", words) if w.strip()]
        if items:
            rules.append((items, hint.strip() or "换个说法"))
    return rules


def risk_level(buyer_message: str, draft: str) -> tuple[int, list[str]]:
    """（等级, 依据）。等级 3：花钱或改订单；2：对外承诺；1：只读答复。"""
    reasons = []
    for text, who in ((buyer_message, "买家"), (draft, "回复")):
        m = MONEY_OR_ORDER.search(text or "") or AMOUNT.search(text or "")
        if m:
            reasons.append(f"{who}提到「{m.group(0)}」")
    if reasons:
        return 3, reasons
    m = COMMITMENT.search(draft or "")
    if m:
        return 2, [f"回复里有承诺「{m.group(0)}」"]
    return 1, []


def check_reply(draft: str, profile: CsProfile, platform: str, buyer_message: str = "",
                checker: ComplianceChecker | None = None) -> list[str]:
    """回复发出前的确定性检查，返回问题（空 = 过）。"""
    problems = []
    text = draft or ""
    lowered = text.lower()
    for v in ComplianceChecker.blocking((checker or ComplianceChecker()).check(text, platform, "描述")):
        problems.append(v.detail)
    for words, hint in _phrase_rules(profile.banned_phrases):
        hit = next((w for w in words if w.lower() in lowered), None)
        if hit:
            problems.append(f"用了「{hit}」：{hint}")
    if len(text) > profile.max_chars:
        problems.append(f"{len(text)} 字，超过上限 {profile.max_chars}：先回答买家问的那件事，删掉铺垫")
    if EMAIL.search(text) or PHONE.search(text):
        problems.append("回复里有邮箱或电话：不能带出买家或店铺的联系方式")
    m = MONEY_PROMISE.search(text) or AMOUNT.search(text)
    if m:
        problems.append(f"回复里答应了花钱的动作或写了金额「{m.group(0)}」：退款、补发、补偿要负责人定，"
                        "回复里只说会核实处理，具体方案和金额写进汇报给负责人")
    return problems
