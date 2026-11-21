---
name: 选品剧本-默认
description: 给还没有专门剧本的国家选品时打开：通用的调研步骤、筛选门槛、打分权重和选品经验（兜底用，按站点细化请另建剧本）
type: selection
---
> 兜底剧本：没有专门剧本的国家都用这份。门槛偏保守，按站点细化时复制一份改 regions。

## 规则

```toml
regions = ["*"]
research_max_age_days = 7
sources = ["google_suggest", "holidays", "erp_sales", "owner_input", "csv_import"]
price_band = [10, 50]
max_weight_kg = 2
min_margin = 0.25
return_rate = 0.05                       # 退货率初始值，按自己店铺数据改
weights = { demand = 30, margin = 30, competition = 20, logistics = 20 }
exclude_categories = ["电池", "液体", "刀具", "仿牌"]
auto_adopt_sections = ["重点方向"]
```

## 调研步骤

1. 用种子词查目标国家、当地语言的下拉词，看当地人围绕这个品在搜什么
2. 看节日日历：未来 6~8 周有没有大促或节日，有就把节日词加进种子词
3. 查自己店铺近 30 天同类目的销量和退货率，判断是不是已经卖过、卖得怎样

## 重点方向

（分身调研后按市场变化更新这一节，写清依据和时间）

## 选品经验

- 下拉词只能证明有人在搜，不能证明卖得动；要和销量或榜单对照
- 体积大、易碎、带电的品，物流和售后成本常常吃掉毛利，先算落地成本再谈
