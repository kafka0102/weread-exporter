## Why

批量导出以 `data/new_books.txt` 为输入，但其中可能混入用户明确不想下载的书；书架去重 skill 目前只会按「已下载 / ebook-info 同作」判 dup，无法提前把黑名单书挡在 `new_books.txt` 之外。需要一份可维护的微信读书 ID 禁止列表，在去重与全量导出两处统一生效。

## What Changes

- 新增 `data/forbid_books.txt`：每行一个微信读书 `book_id`（可含空行与 `#` 注释），作为禁止下载清单。
- 批量导出（读取 `new_books.txt` 全量下载）时：若当前书 ID 在禁止列表中，则跳过，不发起导出。
- 书架去重 skill（`.agents/skills/dedupe-shelf-books` 与 `.claude/skills/dedupe-shelf-books`）：书架书 ID 命中禁止列表时视为 dup，写入 `data/dup_books.txt`，**不**写入 `data/new_books.txt`。
- 补充相关文档（README / CONTEXT / skill 说明）与单测，保证可重复运行与缺文件时的安全默认行为。

## Capabilities

### New Capabilities

- `forbid-books`: 禁止列表的读取约定，以及在批量导出、书架去重两条链路中的忽略/判 dup 行为。

### Modified Capabilities

- （无既有 main specs；本仓库首次引入 OpenSpec，仅建立本 change 的 delta specs。）

## Impact

- 代码：`book_json.py`（清单解析/过滤辅助）、`export_precise.py`（`export_batch` 跳过逻辑）；可能抽公共 `load_forbid_book_ids`。
- 数据：新增入库文件 `data/forbid_books.txt`（默认可为空或仅注释）。
- Skill/文档：`dedupe-shelf-books` 双份 skill、`CONTEXT.md`、`README.md`、`AGENTS.md` 中与去重/批量导出相关的说明。
- 测试：`tests/test_book_json.py`、`tests/test_export_precise_cli.py` 等。
- 单本 CLI 显式指定 `book_id` 导出：不在本 change 强制拦截范围（仅批量/去重链路）。
