# 记忆库

人工精炼的经验和判断（DESIGN.md 9）。每条记忆一个 Markdown 文件，头部写 name / description / type，正文用「## 主题」分节。

- description 每一轮都进 agent 的开局索引：写「做什么动作时打开它、打开能拿到什么」，列动作不列名词，不超过 120 字
- agent 按要做的动作比索引，比中了用 read_memory 读全文；拿不准时才用 search_memory 兜底检索。每次读取、检索都有记录
- agent 不能直接修改这里，只能提交记忆草稿（追加一节 / 改一段 / 删一段 / 新建）：`ecom-twin memory drafts` 查看，`approve` / `reject` 审核，或在控制台审核
- `ecom-twin memory check` 校验格式和索引预算（软预算：100 条、8KB）
- 客观事实（当前出价、库存、销量）不放这里，现场去 ERP 或平台查
- 固定步骤的 SOP 放 skill，不放这里
