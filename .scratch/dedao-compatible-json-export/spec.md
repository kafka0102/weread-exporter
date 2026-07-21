# Spec: 兼容 dedao/json 的微信读书导出

Status: ready-for-agent

## Problem Statement

用户需要把微信读书中的电子书导出为与 `/Users/yujianjia/data/dedao/json` 一致的 JSON 结构，便于后续用 super-poet 的电子书导入链路接入。当前 `export_precise.py` 只产出 Markdown 与图片中间产物，没有书级 JSON，也没有按 `new_books.txt` 批量导出、书间/章间风控节奏。

## Solution

扩展现有导出入口：抓取过程仍使用 `output/<book_id>/` 的 chapters/raw 做续传；全书成功后写出 `data/books/<book_id>_<书名>.json`，字段与章节形态对齐 dedao/json 样例（纯文本 content、`has_content`、书级元数据键齐全）。支持单本指定与批量（`data/new_books.txt` + 跳过已存在）。默认不下载图片。章完成后按字数动态 sleep；书与书间隔可配置，默认 60 秒。单本若 books 中已存在同 id 则跳过，除非 `--force`。批量遇失败停止。

## User Stories

1. As a 本地图书整理者, I want 指定 book_id 导出一本书的 JSON, so that 我可以单独验证格式是否兼容导入。
2. As a 本地图书整理者, I want 导出文件名为 `id_书名.json`, so that 文件名稳定且可读。
3. As a 本地图书整理者, I want JSON 顶层包含 id/title/author/press/publication_date/isbn/word_count/body, so that 与 dedao 样例字段一致。
4. As a 本地图书整理者, I want 拿不到的出版社/出版日/ISBN 填空串, so that 结构完整且导入端不必特判缺键。
5. As a 本地图书整理者, I want word_count 为各章纯文本字数之和, so that 字数与正文一致。
6. As a 本地图书整理者, I want 每章有 chapter_name/chapter_id/content/has_content, so that 章节结构可被下游解析。
7. As a 本地图书整理者, I want chapter_id 为 `ch_0001` 形式, so that 即使无源站锚点也有稳定编号。
8. As a 本地图书整理者, I want content 为纯文本且不含章节标题行, so that 与 dedao/json 正文形态一致。
9. As a 本地图书整理者, I want 非空正文 has_content=true、否则 false, so that 空壳章语义正确。
10. As a 本地图书整理者, I want 默认不下载图片, so that 导出更快且避免无用 IO。
11. As a 本地图书整理者, I want 可选开启图片下载, so that 需要插图时仍可取回。
12. As a 本地图书整理者, I want 中间产物仍写在 output/<book_id>/, so that 中断后可续传。
13. As a 本地图书整理者, I want 全书成功后才写入 data/books JSON, so that 不会留下半成品 JSON 被当成已完成。
14. As a 本地图书整理者, I want 单本遇到 books 中已存在同 book_id 前缀文件时默认跳过, so that 不会误覆盖。
15. As a 本地图书整理者, I want `--force` 可强制重导单本, so that 我能覆盖坏文件。
16. As a 本地图书整理者, I want 不传 book_id 时批量处理 data/new_books.txt, so that 只导出去重后的新书。
17. As a 本地图书整理者, I want 批量时跳过 books 中已存在的 book_id, so that 重复运行安全。
18. As a 本地图书整理者, I want 批量书与书之间默认等待 60 秒, so that 降低风控风险。
19. As a 本地图书整理者, I want 章切换后按该章字数动态等待, so that 长章等待更久、短章不至于过快。
20. As a 本地图书整理者, I want 动态等待公式为每千字 2 秒且夹在 2–15 秒, so that 节奏可预期。
21. As a 本地图书整理者, I want 书间与章间 sleep 都进入 .env, so that 可按机器与账号调整。
22. As a 本地图书整理者, I want 输出目录默认为 data/books 且可用 --out-dir 覆盖, so that 试验时不污染正式目录。
23. As a 本地图书整理者, I want 单本接受 URL 或 book_id, so that 用法与现有脚本一致。
24. As a 本地图书整理者, I want 批量失败时停止后续书, so that 登录失效等问题能立刻暴露。
25. As a 开发者, I want 纯函数可单测（JSON 组装、字数 sleep、已存在判定、文件名安全化）, so that 不依赖浏览器也能验证兼容性。
26. As a 本地图书整理者, I want README/.env.example/AGENTS 同步新 sleep 与用法, so that 后来者能直接运行。

## Implementation Decisions

- 扩展现有 `export_precise` 入口，而不是新建平行导出器：有位置参数则单本，无则批量。
- 继续用 browser_profile 缓存登录；默认不下载图片，`--download-images` 才下载。
- 中间产物路径保持 `output/<book_id>/chapters|raw|images`；成功后组装并写出 JSON。
- JSON 契约：
  - 书级键固定：id(str book_id), title, author, press="", publication_date="", isbn="", word_count(int), body(list)
  - 章级键固定：chapter_name, chapter_id=`ch_{idx:04d}`, content(纯文本、双换行分段、无标题行), has_content=bool(content.strip())
- 从现有 chapter markdown 转纯文本：去掉首行标题与 markdown 图片语法，保留段落文本；不引入 HTML。
- “已存在”判定：在 out-dir 下匹配 `{book_id}_*.json` 前缀，不依赖书名是否变化。
- 批量来源固定读 `data/new_books.txt`（ID,书名,作者）；不在本次改 dedupe skill。
- 章间 sleep：保存完一章且进入下一章后，按刚完成章的纯文本字数 `ceil(chars/1000)*per_1k`，再 clamp 到 [min,max]。
- 书间 sleep：`SLEEP_BOOK_INTERVAL` 默认 60；最后一本后不需要多余等待。
- 失败策略：单本失败以非零退出；批量任一本失败则停止并非零退出。
- 新增/调整的配置常量经 env_config + .env.example + AGENTS 表同步。

## Testing Decisions

- 只测外部行为：给定章节 md/raw 与元数据，产出的 JSON 结构、字段、文件名、跳过逻辑、sleep 秒数。
- 优先测纯函数 seam，不启 Playwright。
- 现有 prior art：`tests/test_env_config.py`、`tests/test_fetch_shelf.py` 的 unittest 风格。
- Seam（最高点、尽量单一）：
  1. **JSON 导出组装 seam**（推荐最高 seam）：`build_book_json` / `write_book_json` / `md_to_plain_content` / `safe_book_filename` / `book_json_exists` / `chapter_sleep_seconds` / `iter_batch_book_ids` 等纯函数集合。浏览器抓取仍走现有 `run_session`，测试不穿透 Canvas。
  2. CLI 编排可用轻量集成测（mock 导出函数）覆盖单本跳过、force、批量停止；若成本高则以纯函数 + 少量编排测为准。
- 假设：用户若要改 seam，可在 review 时调整；默认不新增 Playwright e2e。

## Out of Scope

- 实现 super-poet 侧微信读书 JSON 导入器（本次只保证格式兼容）。
- 从微信读书补全 press/publication_date/isbn。
- 修改 dedupe skill 或 ebook-info 比对逻辑。
- 改变 Canvas 翻页抓取算法本身（除非为写入 JSON/ sleep 所需的最小接线）。
- 图片默认下载或图片 CDN 本地化进 JSON content。

## Further Notes

- 参考样例：`/Users/yujianjia/data/dedao/json/104611_词学十讲_龙榆生.json` 等。
- super-poet 当前生产导入路径读 HTML 再 parse；本 JSON 是为后续导入调整预留的中间格式，故字段兼容优先于复用现有 HTML parser。
- 文件名仅 `id_书名.json`（不含作者），与部分 dedao 样例文件名不同，属本轮明确决策。
