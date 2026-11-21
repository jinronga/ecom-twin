---
name: 选品剧本-东南亚
description: 给印尼、马来西亚、泰国、越南、菲律宾、新加坡选品时打开：本地语言调研步骤、低价小件门槛、大促节奏和货到付款经验
type: selection
---
> 默认剧本：门槛和经验是初始值，请运营按自己店铺的数据核对后再用。各国差异大，某个国家做大了再单独拆一份剧本。

## 规则

```toml
regions = ["ID", "MY", "TH", "VN", "PH", "SG"]
platforms = ["tiktok", "shopee", "lazada"]
research_max_age_days = 7
holiday_lead_weeks = 8
sources = ["google_suggest", "holidays", "erp_sales", "owner_input", "csv_import"]
seeds = []                               # 默认品类词：临时选品没给关键词时用；每天盯什么写在「雷达规则」里
price_band = [3, 25]                     # 美元，东南亚以低价走量为主
max_weight_kg = 1
min_margin = 0.20
margin_good = 0.40
return_rate = 0.08                       # 货到付款有拒收，按自己店铺数据改
return_loss_rate = 0.5
extra_rate = 0.03                        # 货到付款手续费、平台服务费等
commission_rates = { tiktok = 0.08, shopee = 0.10, lazada = 0.10 }   # 初始值，以平台后台为准
shipping_base = 1.0                      # 运费估算：首重 + 每公斤（美元），初始值，按物流商报价改
shipping_per_kg = 5.0
weights = { demand = 30, margin = 25, competition = 20, logistics = 25 }
exclude_categories = ["仿牌", "刀具", "成人", "赌博"]
auto_adopt_sections = ["重点方向"]
```

## 调研步骤

1. 类目词用当地语言查下拉词（印尼语、泰语、越南语等），菲律宾、新加坡用英语
2. 看节日日历：9.9、11.11、12.12 大促，春节，印尼和马来西亚还有斋月、开斋节，大促前 6~8 周备选品
3. 查自己店铺近 30 天的销量和退货率

## 重点方向

（分身调研后按市场变化更新这一节，写清依据和时间）

## 市场经验

- 价格敏感，低价小件好卖；重的、大的品运费占比高，物流权重因此调高
- 印尼、菲律宾、越南货到付款多，拒收要算进成本
- 印尼、马来西亚穆斯林人口多，斋月和开斋节是全年最重要的销售节点之一，选品注意宗教禁忌
- 大促集中在 9.9、11.11、12.12，平台活动报名要提前
