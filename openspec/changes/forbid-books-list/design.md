## Context

批量导出链路：`export_precise.export_batch` → `book_json.iter_batch_book_ids` + `filter_pending_books`（已导出 JSON 则跳过）。

书架去重链路：skill 文档驱动，将 `shelf_books.txt` 与本地 books ID、ebook-info 语义比对，写入 `dup_books.txt` / `new_books.txt`。

用户希望再加一层「人工禁止下载」名单，两边共用同一文件。

## Goals / Non-Goals

**Goals:**

- 单一文件 `data/forbid_books.txt` 作为禁止 ID 源。
- 批量导出跳过禁止 ID（不打开阅读器、不计为失败）。
- 去重时禁止 ID 直接进 dup，永不进 new。
- 文件缺失或为空时行为与现网一致（不拦截）。
- 格式与解析可测、可文档化。

**Non-Goals:**

- 不拦截「显式传入单个 book_id / reader URL」的手动导出。
- 不做 UI / CLI 增删禁止列表的管理命令。
- 不按书名/作者模糊匹配禁止列表，只按 weread book ID。
- 不把禁止书从 `new_books.txt` 自动物理删除（导出侧跳过即可；去重侧负责不写入）。

## Decisions

1. **文件格式：每行一个 book_id**
   - 与用户描述一致；比 `ID,书名,作者` 更轻，人工维护成本低。
   - 支持空行与 `#` 行首注释；行内若误写成 `id,title,...`，只取逗号前第一段 ID（与 shelf 行兼容，降低误用成本）。
   - 备选：完整 shelf 三列 → 拒绝，禁止列表语义是 ID 集合，不必绑书名。

2. **解析放在 `book_json.py`**
   - 已有 `parse_shelf_line` / `iter_batch_book_ids` / `filter_pending_books`，新增：
     - `DEFAULT_FORBID_BOOKS = Path("data") / "forbid_books.txt"`
     - `load_forbid_book_ids(path) -> set[str]`
     - `filter_forbidden_books(books, forbid_ids) -> list`（或合并进 pending 过滤）
   - `export_batch` 在 `filter_pending_books` 之后（或之前）剔除 forbid 集合，并打印跳过数量/ID。

3. **导出侧只影响批量清单路径**
   - `export_batch` 与默认 `data/new_books.txt` 场景；单本 `export_one_book` 不读 forbid。
   - 理由：禁止列表是「全量待办」治理手段；手动指定 ID 仍允许强制导出（若未来要拦，另开 change）。

4. **去重 skill：新增优先级 0 / 独立关卡**
   - 在既有 A（本地已下载）/ B（ebook-info）之前或并列：命中 forbid → **dup**。
   - 输出格式仍写 `ID,书名,作者` 到 `dup_books.txt`，与现有一致。
   - 双份 skill（`.agents` / `.claude`）内容同步。

5. **入库策略**
   - `data/forbid_books.txt` 入库，初始可仅含注释说明如何填写。
   - 不将用户私有禁止 ID 硬编码进仓库；空清单即可。

6. **缺文件行为**
   - 路径不存在 → 视为空集合，不报错中断。

## Risks / Trade-offs

- [Risk] `new_books.txt` 里已有禁止书，导出侧跳过但文件仍残留 → 可接受；文档说明可用去重重跑或手删；不自动改写 new 以免与「只追加」习惯冲突。
- [Risk] 用户把 shelf 整行粘进 forbid 文件 → 取逗号前 ID 兼容，降低踩坑。
- [Risk] skill 是文档驱动而非代码 → 必须更新 skill 与 CONTEXT，否则 Agent 会漏判；后续若有脚本化 dedupe 再复用同一 loader。

## Migration Plan

1. 合入代码 + 空 `data/forbid_books.txt` + 文档。
2. 用户按需把不想下载的 weread ID 写入该文件。
3. 重跑 dedupe skill；再跑 `python export_precise.py` 批量导出。
4. 回滚：删除或清空 forbid 文件即可恢复原行为。

## Open Questions

- 无阻塞问题。若后续需要「单本 CLI 也拦截」，另开需求。
