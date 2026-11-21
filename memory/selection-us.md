---
name: 选品剧本-美国
description: 给美国站选品时打开：英语调研步骤、筛选门槛、打分权重、认证和侵权风险、美国大促节奏和退货经验
type: selection
---
> 默认剧本：门槛和经验是初始值，请运营按自己店铺的数据核对后再用。

## 规则

```toml
regions = ["US"]
platforms = ["tiktok", "amazon"]
languages = ["en"]
research_max_age_days = 7
holiday_lead_weeks = 10
sources = ["google_suggest", "youtube_suggest", "holidays", "erp_sales", "owner_input", "csv_import"]
seeds = []                               # 默认品类词：临时选品没给关键词时用；每天盯什么写在「雷达规则」里
price_band = [15, 60]                    # 美元
max_weight_kg = 2
min_margin = 0.30
margin_good = 0.50
return_rate = 0.10                       # 美国退货多，按自己店铺数据改
return_loss_rate = 0.6                   # 退回来的货多半不能再卖，运费也损失
extra_rate = 0.0
commission_rates = { amazon = 0.15, tiktok = 0.08 }   # 初始值，以平台后台为准
shipping_base = 2.0                      # 运费估算：首重 + 每公斤（美元），初始值，按物流商报价改
shipping_per_kg = 9.0
weights = { demand = 30, margin = 30, competition = 25, logistics = 15 }
exclude_categories = ["仿牌", "刀具", "医疗器械", "成人"]
auto_adopt_sections = ["重点方向"]
```

## 调研步骤

1. 用类目词查英语的 Google 和 YouTube 下拉词：YouTube 上的开箱、测评词说明这个品适合做内容带货
2. 看节日日历：返校季、万圣节、黑五网一、圣诞前 8~10 周开始备选品
3. 查自己店铺近 30 天的销量和退货率

## 重点方向

（分身调研后按市场变化更新这一节，写清依据和时间）

## 市场经验

- 侵权风险高：外观专利、商标、版权图案都会被投诉下架，选品前先查品牌词和外观
- 认证：带电产品要 FCC，儿童产品要 CPC 和 CPSC 相关检测，没有认证的先排除
- 退货多、退回来的货常常不能二次销售，所以退货成本按较高比例算
- 价格带在 15~60 美元最好做，太便宜赚不回运费，太贵转化低
