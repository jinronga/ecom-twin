"""生成 README 用的两张 SVG：功能模块图、上品数据链路泳道图。

改图就改这个脚本，再运行 python3 docs/images/gen_diagrams.py，不要手改 SVG。
"""
import sys
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
FONT = "PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, sans-serif"
INK, MUTED, LINE = "#1f2a24", "#5f6a64", "#7d8781"
BOX_FILL, BOX_STROKE = "#ffffff", "#c5cec8"
GROUP_FILL = "#f3f5f3"
ACC, ACC_FILL, ACC_SOFT = "#b4532a", "#fbeee7", "#fdf6f2"


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Svg:
    def __init__(self, w, h, label):
        self.w, self.h, self.label, self.parts = w, h, label, []

    def add(self, s):
        self.parts.append(s)

    def text(self, x, y, s, size=13, weight=400, color=INK, anchor="middle"):
        self.add(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{color}" '
                 f'text-anchor="{anchor}">{esc(s)}</text>')

    def box(self, x, y, w, h, title, sub="", accent=False, rx=8):
        fill, stroke = (ACC_FILL, ACC) if accent else (BOX_FILL, BOX_STROKE)
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" '
                 f'stroke-width="{1.5 if accent else 1}"/>')
        cx = x + w / 2
        if sub:
            self.text(cx, y + h / 2 - 3, title, 13, 600, ACC if accent else INK)
            self.text(cx, y + h / 2 + 14, sub, 11, 400, MUTED)
        else:
            self.text(cx, y + h / 2 + 5, title, 13, 600, ACC if accent else INK)

    def group(self, x, y, w, h, label, accent=False, dashed=False):
        fill = ACC_SOFT if accent else GROUP_FILL
        stroke = ACC if accent else "none"
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="{stroke}"{dash}/>')
        self.text(x + 12, y + 18, label, 11, 600, ACC if accent else MUTED, "start")

    def path(self, d, accent=False, dashed=False, both=False, label="", lx=0, ly=0, lanchor="middle"):
        color = ACC if accent else LINE
        marker = "a" if accent else "n"
        dash = ' stroke-dasharray="4 4"' if dashed else ""
        start = f' marker-start="url(#{marker}s)"' if both else ""
        self.add(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{1.6 if accent else 1.3}"{dash}'
                 f' marker-end="url(#{marker})"{start}/>')
        if label:
            # 标签底下垫一块白，压在线上也看得清
            wpx = sum(11 if ord(c) > 127 else 6.2 for c in label) + 8
            bx = lx - wpx / 2 if lanchor == "middle" else (lx - 4 if lanchor == "start" else lx - wpx + 4)
            self.add(f'<rect x="{bx:.1f}" y="{ly - 11}" width="{wpx:.1f}" height="15" fill="#ffffff" opacity="0.92"/>')
            self.text(lx, ly, label, 11, 500, ACC if accent else MUTED, lanchor)

    def render(self):
        defs = "".join(
            f'<marker id="{i}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="{o}">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>'
            for i, c, o in (("n", LINE, "auto"), ("a", ACC, "auto"),
                            ("ns", LINE, "auto-start-reverse"), ("as", ACC, "auto-start-reverse")))
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" '
                f'height="{self.h}" role="img" aria-label="{esc(self.label)}" font-family="{FONT}">'
                f'<defs>{defs}</defs><rect width="{self.w}" height="{self.h}" fill="#ffffff"/>'
                + "".join(self.parts) + "</svg>\n")


# ---------------- 图 1：功能模块 ----------------
def architecture():
    s = Svg(1200, 660, "功能模块图：消息经任务入口和调度器交给 Agent，Agent 只能通过 MCP 工具读数据、改暂存区或提交提案；"
                       "真正写店铺必须经过提案、人工审批和执行器")
    # 分组底色
    s.group(12, 60, 176, 300, "入口")
    s.group(212, 60, 220, 300, "调度")
    s.group(456, 200, 230, 420, "智能层（模型）")
    s.group(712, 60, 476, 260, "Agent 可以直接做的")
    s.group(712, 336, 476, 120, "管控通道：写店铺的唯一出口，规则在代码里，模型改不了", accent=True)
    s.group(12, 440, 420, 180, "数据与运营")

    # 入口
    s.box(24, 92, 152, 56, "飞书私聊 / 群", "@分身 · 引用回复")
    s.box(24, 180, 152, 56, "定时任务", "日报 · 巡检 · 复盘")
    s.box(24, 268, 152, 56, "命令行", "ask · demo")
    # 调度
    s.box(232, 92, 180, 56, "任务入口", "过滤 · 合并 · 续接")
    s.box(232, 228, 180, 56, "调度器", "重试 · 切换底座 · 汇报")
    s.path("M176,120 L232,120")
    s.path("M176,208 L204,208 L204,134 L232,134")
    s.path("M176,296 L204,296 L204,262 L232,262")
    s.path("M322,148 L322,228", label="建单 / 并单 · 补上下文", lx=330, ly=192, lanchor="start")
    # 智能层
    s.box(476, 228, 190, 56, "Agent 运行时", "Claude Code · Codex 等 4 种")
    s.path("M412,256 L476,256", label="派单", lx=444, ly=248)
    s.box(476, 372, 190, 56, "MCP 工具服务", "按任务隔离 · 30+ 工具")
    s.path("M571,284 L571,372", both=True, label="调用工具", lx=579, ly=332, lanchor="start")
    s.box(476, 520, 190, 56, "记忆库 · SOP", "人工采纳才入库")
    s.path("M571,428 L571,520", label="按需读 · 只能提草稿", lx=579, ly=478, lanchor="start")

    # MCP 往右分三条路：一根主干
    s.add(f'<path d="M666,400 L692,400 L692,120" fill="none" stroke="{LINE}" stroke-width="1.3"/>')
    s.box(732, 92, 170, 56, "只读查询", "店铺 · 订单 · 网页取证")
    s.box(732, 196, 170, 56, "暂存区编辑", "采集 · 改标题 · 砍 SKU")
    s.path("M692,120 L732,120", label="读", lx=712, ly=112)
    s.path("M692,224 L732,224", label="改草稿", lx=712, ly=216)
    s.box(984, 92, 184, 56, "妙手 ERP · 网页", "只读接口")
    s.box(984, 196, 184, 56, "妙手采集箱", "买家看不到 · 改动留痕")
    s.path("M902,120 L984,120")
    s.path("M902,224 L984,224")
    s.box(732, 264, 436, 40, "代码计算与检查：售价 · 毛利 · 违禁词 · 客服风险")
    s.path("M692,284 L732,284", label="算 · 查", lx=712, ly=276)

    # 管控通道
    s.path("M666,410 L732,410", accent=True, label="提交提案", lx=699, ly=400)
    s.box(732, 372, 120, 56, "提案", "数字须有出处", accent=True)
    s.box(880, 372, 120, 56, "审批", "飞书卡片 · 人批准", accent=True)
    s.box(1028, 372, 140, 56, "执行器", "硬上限 · 执行前复核", accent=True)
    s.path("M852,400 L880,400", accent=True)
    s.path("M1000,400 L1028,400", accent=True)
    s.box(1028, 520, 140, 56, "TikTok 店铺", "经妙手发布")
    s.path("M1098,428 L1098,520", accent=True, label="发布 · 回读确认", lx=1106, ly=478, lanchor="start")

    # 数据与运营
    s.box(232, 480, 180, 56, "SQLite", "任务 · 提案 · 审计 · 成本")
    s.box(24, 480, 152, 56, "后台控制台", "负责人 / 运营两种角色")
    s.box(24, 556, 152, 48, "飞书多维表格看板")
    s.path("M322,284 L322,480", label="状态 · 成本", lx=330, ly=392, lanchor="start")
    s.path("M232,508 L176,508")
    s.path("M260,536 L260,580 L176,580")
    s.path("M792,428 L792,600 L380,600 L380,536", dashed=True, label="每一步都留痕", lx=600, ly=596)
    return s.render()


# ---------------- 图 2：上品数据链路（竖向泳道） ----------------
def flow():
    lanes = ["负责人 · 飞书", "任务入口 · 调度", "Agent（经 MCP）", "代码管控", "妙手 · TikTok"]
    left, lw, head, rh, bw, bh = 52, 176, 56, 74, 152, 50
    steps = [  # (泳道, 标题, 说明)
        (0, "私聊交代", "3 个链接 · 按 30% 毛利"),
        (1, "建单 / 并单", "补近 24 小时上下文"),
        (2, "开工", "读记忆 · 加载上品 SOP"),
        (4, "采集箱暂存", "采集 · 改标题 · 砍 SKU"),
        (3, "算价 · 合规", "售价 · 毛利 · 违禁词"),
        (3, "提交提案", "理由里的数字须有出处"),
        (0, "审批卡片", "逐个 SKU 的毛利与后果"),
        (3, "执行器复核", "过期？数据变了？超限？"),
        (4, "发布 · 回读", "经妙手发布，确认已上架"),
        (0, "汇报 · 复盘", "证据截图 · 第 3/7 天提醒"),
    ]
    W = left + lw * len(lanes) + 16
    H = head + rh * len(steps) + 20
    s = Svg(W, H, "上品数据链路泳道图：负责人私聊交代，分身建单并补上下文，Agent 在妙手采集箱暂存区准备商品，"
                  "代码算价和检查后提交提案，负责人在飞书批准，执行器复核后经妙手发布并回读，最后汇报和复盘；"
                  "复核不通过就重新出提案")
    for l, name in enumerate(lanes):
        x = left + l * lw
        acc = name == "代码管控"
        s.add(f'<rect x="{x}" y="0" width="{lw}" height="{H}" fill="{ACC_SOFT if acc else (GROUP_FILL if l % 2 == 0 else "#ffffff")}"/>')
        s.add(f'<line x1="{x}" y1="0" x2="{x}" y2="{H}" stroke="#e1e6e2"/>')
        s.text(x + lw / 2, 32, name, 13, 700, ACC if acc else INK)
    s.add(f'<line x1="0" y1="{head - 8}" x2="{W}" y2="{head - 8}" stroke="#d5dcd7"/>')

    def cx(l):
        return left + l * lw + lw / 2

    pos = []
    for i, (l, title, sub) in enumerate(steps):
        y = head + i * rh + (rh - bh) / 2
        x = cx(l) - bw / 2
        acc = 4 <= i <= 7
        s.text(left / 2, y + bh / 2 + 4, str(i + 1), 12, 700, MUTED)
        s.box(x, y, bw, bh, title, sub, acc)
        pos.append((l, x, y))

    labels = {3: "经 MCP 调用", 6: "推送卡片", 7: "批准", 8: "通过"}
    for i in range(len(steps) - 1):
        (l1, x1, y1), (l2, x2, y2) = pos[i], pos[i + 1]
        acc = 4 <= i < 7
        lab = labels.get(i + 1, "")
        if l1 == l2:
            s.path(f"M{cx(l1)},{y1 + bh} L{cx(l2)},{y2}", accent=acc, label=lab, lx=cx(l1) + 8, ly=(y1 + bh + y2) / 2 + 4,
                   lanchor="start")
            continue
        cy = y1 + bh / 2
        sx = x1 + bw if l2 > l1 else x1
        s.path(f"M{sx},{cy} L{cx(l2)},{cy} L{cx(l2)},{y2}", accent=acc, label=lab, lx=(sx + cx(l2)) / 2, ly=cy - 6)

    # 复核不通过：回到提案（同一泳道，从执行器顶部直接往上，避开右侧的「通过」）
    _, px, py = pos[5]
    _, ex, ey = pos[7]
    lx = px + bw - 24
    s.path(f"M{lx},{ey} L{lx},{py + bh}", accent=True, dashed=True)
    s.text(lx + 8, (py + bh + ey) / 2 + 22, "不通过：停下，重新出提案", 11, 600, ACC, "start")
    # 拒绝
    _, ax, ay = pos[6]
    s.text(ax + bw / 2, ay + bh + 15, "拒绝要选原因 → 沉淀为经验", 11, 600, ACC)
    return s.render()


OUT.mkdir(parents=True, exist_ok=True)
(OUT / "architecture.svg").write_text(architecture(), encoding="utf-8")
(OUT / "data-flow.svg").write_text(flow(), encoding="utf-8")
print("ok")
