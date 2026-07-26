## 1. 禁止列表数据与解析

- [x] 1.1 新增入库文件 `data/forbid_books.txt`（注释说明格式，默认可无 ID）
- [x] 1.2 在 `book_json.py` 增加 `DEFAULT_FORBID_BOOKS` 与 `load_forbid_book_ids`（忽略空行/`#` 注释；逗号行取首段）
- [x] 1.3 增加 `filter_forbidden_books`（或等价过滤），用单元测试覆盖解析与过滤

## 2. 批量导出接入

- [x] 2.1 在 `export_precise.export_batch` 中加载 forbid 集合并跳过命中书，打印跳过信息且不视为失败
- [x] 2.2 补充/更新 `tests/test_export_precise_cli.py`（或 book_json 测试）覆盖「清单含禁止书时跳过」

## 3. 去重 skill 与文档

- [x] 3.1 更新 `.agents/skills/dedupe-shelf-books/SKILL.md`：forbid 命中 → dup，不进 new
- [x] 3.2 同步更新 `.claude/skills/dedupe-shelf-books/SKILL.md`
- [x] 3.3 更新 `CONTEXT.md`、`README.md`、必要时 `AGENTS.md` 中关于 forbid / 批量导出 / 去重的说明

## 4. 验证

- [x] 4.1 运行相关单元测试并通过
- [x] 4.2 按仓库规范提交本次可交付改动
