---
name: 选品剧本-欧洲
description: 给英国、德国、法国、西班牙、意大利等欧洲站选品时打开：多语言调研步骤、门槛权重、增值税和合规要求、退货经验
type: selection
---
> 默认剧本：门槛和经验是初始值，请运营按自己店铺的数据核对后再用。各国差异大，某个国家做大了再单独拆一份剧本。

## 规则

```toml
regions = ["GB", "DE", "FR", "ES", "IT", "NL", "PL"]
platforms = ["tiktok", "amazon"]
research_max_age_days = 7
holiday_lead_weeks = 10
sources = ["google_suggest", "holidays", "erp_sales", "owner_input", "csv_import"]
seeds = []                               # 默认品类词：临时选品没给关键词时用；每天盯什么写在「雷达规则」里
price_band = [15, 60]                    # 按美元换算
max_weight_kg = 2
min_margin = 0.30
margin_good = 0.50
return_rate = 0.12                       # 欧盟网购 14 天无理由退货，按自己店铺数据改
return_loss_rate = 0.5
extra_rate = 0.17                        # 售价含增值税：按 20% 税率，税约占含税售价的 17%（各国 19%~23%）
commission_rates = { amazon = 0.15, tiktok = 0.09 }   # 初始值，以平台后台为准
shipping_base = 2.0                      # 运费估算：首重 + 每公斤（美元），初始值，按物流商报价改
shipping_per_kg = 10.0
weights = { demand = 30, margin = 30, competition = 20, logistics = 20 }
exclude_categories = ["仿牌", "刀具", "医疗器械", "成人"]
auto_adopt_sections = ["重点方向"]
```

## 调研步骤

1. 类目词按每个国家的语言查下拉词：英文词在德国、法国查出来的多是英文结果，当地需求要用当地语言的词查
2. 看节日日历：复活节、返校季、黑五、圣诞前 8~10 周开始备选品
3. 查自己店铺近 30 天的销量和退货率

## 重点方向

（分身调研后按市场变化更新这一节，写清依据和时间）

## 市场经验

- 售价含增值税，增值税已按 extra_rate 算进毛利；各国税率不同，单国剧本按当地税率改
- 合规：电子产品要 CE（英国是 UKCA）；欧盟要有当地负责人；德国、法国等对包装和电子废弃物有注册要求，没办好的先别选
- 消费者有 14 天无理由退货权，退货成本要算进去
- 一个品要做多国，标题和详情要分语言写，别直接用英文
