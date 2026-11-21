---
name: 选品剧本-中东
description: 给沙特、阿联酋选品时打开：阿拉伯语和英语调研步骤、筛选门槛、打分权重、宗教文化禁忌和中东市场经验
type: selection
---
> 示例剧本：门槛和经验是初始值，请运营按实际情况核对后再用。

## 规则

```toml
regions = ["SA", "AE"]
platforms = ["noon", "tiktok"]
languages = ["ar", "en"]
research_max_age_days = 7
holiday_lead_weeks = 12
sources = ["google_suggest", "holidays", "erp_sales", "owner_input", "csv_import"]
seeds = ["ramadan decor", "abaya", "oud", "prayer mat", "زينة رمضان", "سجادة صلاة"]   # 默认品类词：临时选品没给关键词时用；每天盯什么写在「雷达规则」里
price_band = [15, 60]
max_weight_kg = 2
min_margin = 0.30
margin_good = 0.50
return_rate = 0.20                       # 货到付款拒收和退货，初始值，按自己店铺数据改
return_loss_rate = 0.5                   # 退回来的货大约损失一半（运费、包装、二次销售折价）
extra_rate = 0.02                        # 货到付款手续费等按售价比例的费用
commission_rates = { noon = 0.12, tiktok = 0.08 }   # 初始值，以平台后台为准
shipping_base = 2.0                      # 运费估算：首重 + 每公斤（美元），初始值，按物流商报价改
shipping_per_kg = 9.0
weights = { demand = 30, margin = 30, competition = 20, logistics = 20 }
exclude_categories = ["酒", "猪", "成人", "赌博"]
auto_adopt_sections = ["重点方向"]
```

## 调研步骤

1. 种子词同时查阿拉伯语和英语的下拉词（沙特以阿拉伯语为主，阿联酋英语也很常见）；英文种子词查出来的多是英文词，阿语需求要用阿语种子词查
2. 看节日日历：斋月、开斋节、宰牲节、白色星期五前 6~8 周开始备选品
3. 查自己店铺近 30 天的销量和退货率

## 重点方向

（分身调研后按市场变化更新这一节，写清依据和时间）

## 市场经验

- 货到付款占比高，拒收和退货已经按 return_rate 算进毛利；拿到自己店铺的真实退货率后改掉初始值
- 图片和文案注意宗教文化禁忌，模特穿着要保守
- 斋月期间作息和购物高峰会变，上新和投放时间跟着调
