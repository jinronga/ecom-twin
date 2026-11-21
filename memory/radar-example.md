---
name: 雷达-示例
description: 选品雷达规则示例：每天早上盯美国站的加热眼罩类目，有新细分方向、节日备货窗口、痛点词就出线索；默认关闭
type: radar
---
> 示例规则，默认关闭。照着改 watch（盯的品类词）和 regions，把 enabled 改成 true 就会按时运行。
> 也可以在控制台「选品雷达」页用预设新建，或者在「问分身」里直接说「每天早上 9 点帮我盯美国的加热眼罩」。

## 规则

```toml
schedule = "0 9 * * *"            # 每天 09:00
timezone = "Asia/Shanghai"
regions = ["US"]
watch = ["heated eye mask"]       # 盯哪些品类词，最多 10 个
modifiers = ["for kids", "cordless", "gift", "for travel"]   # 品类词 × 修饰词，挖细分方向
signals = { new_terms_min = 5, rising_runs = 2, holiday_weeks = 8, pain_terms = true }
max_leads = 3                     # 一次最多出几条线索
enabled = false
```

## 关注说明

重点看新出现的使用场景和人群（比如干眼症、旅行、送礼），只是品牌词、型号词的不用报。
