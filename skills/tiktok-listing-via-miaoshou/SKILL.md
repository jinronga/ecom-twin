---
name: tiktok-listing-via-miaoshou
description: TikTok 上品闭环（经妙手）。负责人发来 1688 / AliExpress 等货源链接和上品规则，要求采集、优化标题、砍 SKU、定价、匹配类目并上架到 TikTok 店铺时使用。
---

# TikTok 上品（经妙手）

按下面的顺序做。采集、编辑、认领都是**采集箱暂存区**操作，买家看不到，可以直接做；
**上架（发布）必须提交提案，审批通过后才执行**，审批卡片会自动列出你在暂存区做过的全部改动。

## 0. 先确认规则

需要的信息：目标店铺（`tiktok:店铺ID`）、站点、价格带、每款最多保留几个 SKU、佣金率、目标毛利率、汇率、尾数规则、排除的关键词。
缺哪个就用 `ask_owner` 问，不要用默认值蒙混。规则可能在负责人消息的上下文里，先用 `feishu_context` 看看。

## 1. 店铺背景

`get_shop_context(shop_key)`：确认店铺在可操作白名单内，记下硬上限（尤其是最低毛利率、单个提案最多商品数）。
判断节奏前用 `read_memory` 读《大促日历》；这个类目的经验按索引里的触发动作找，拿不准再 `search_memory`。

## 2. 采集

`staging_collect(links)`：一次最多 50 个链接，返回 {货源ID: 公共采集箱ID}。
然后用 `staging_get_common` 逐个看标题、货源价（CNY）、SKU、图片数。图片太少、货源失效的直接在汇报里说明，不继续。

## 3. 优化标题

按 `load_sop("listing-optimization-expert")` 和 `load_sop("market-keyword-localization-expert")`（如有）写站点语言的标题。
写好后先 `check_compliance(text, "tiktok", "标题")`，有「禁止」项必须改到通过。

## 4. 砍 SKU、定价

- 按规则保留 SKU（价格带、允许的规格、每款上限、库存大于下限）
- 用 `price_skus(params, skus)` 算每个 SKU 的站点售价和毛利，**不要自己算**
- 毛利率低于店铺下限的 SKU 不上

## 5. 认领、写入店铺信息

1. `staging_claim_to_tiktok(common_ids)` → 得到 TikTok 采集箱 ID
2. `staging_claim_to_shop(detail_ids, shop_key)`
3. 类目：`miaoshou_tiktok_category_tree(site)` 找到最合适的叶子类目，`miaoshou_tiktok_category_metadata` 看必填属性；必填属性缺的写进汇报
4. `staging_edit_listing(detail_id, shop_key, title, cid, sku_prices)`：写入标题、类目、售价（`price_skus` 的结果）

## 6. 提交上架提案

`submit_proposal`：
- `action`：`miaoshou.tiktok.publish`
- `shop_key`：目标店铺
- `target`：`detail_ids`（TikTok 采集箱 ID）、`pricing_params`、`skus`（每个 SKU 的 detail_id、sku、source_price、shipping、other_cost）
- `rationale`：一两句话说明为什么上这几款；出现的数字必须来自工具结果

提案被拒绝时你会被唤醒，按拒绝原因调整后重新提交，或说明为什么结束。

## 7. 汇报

做了什么、上了哪几款（标题、售价区间、最低毛利率）、提案号、哪些商品没上以及原因、还缺什么（例如类目必填属性）。
