---
name: dedupe-shelf-books
description: 把 data/shelf_books.txt 与 data/forbid_books.txt、data/ebook-info.json、本地已导出目录（默认 ~/data/weixin/books）比对，按「归一化主书名」判断重复，分别落到 data/dup_books.txt / data/new_books.txt。优先运行 python dedupe_shelf_books.py（导出后也要再跑，才能把已下载 ID 从 new 迁到 dup）。触发：书架去重、找重复书/新书、生成 dup_books/new_books、剔除已下载。
---

# Skill: 书架书去重

判断微信读书**书架书**（`data/shelf_books.txt`）是否应归入：

- `data/dup_books.txt`：禁止下载 / 本地已导出 / 电子书库已有 / **书架（或已处理列表）同主书名重复**
- `data/new_books.txt`：待导出新书（**同主书名只保留第一本**）

**优先直接跑脚本**，不要手写匹配：

```bash
python dedupe_shelf_books.py
# 只看结果不写文件：
python dedupe_shelf_books.py --dry-run
```

## 文件

| 文件 / 目录 | 角色 | 格式 |
|------|------|------|
| `data/shelf_books.txt` | 输入：书架 | 每行 `ID,书名,作者名` |
| `data/forbid_books.txt` | 输入：禁止下载 | 每行 weread bookId（可 `ID,书名,作者`，只取 ID） |
| `data/ebook-info.json` | 输入：电子书库 | JSON 数组；只用 `bookName`（**不比作者**） |
| `~/data/weixin/books/`（或 `BOOKS_DIR`） | 输入：本地已导出 | 文件名 `{weread_id}_书名.json`，按 `_` 前 ID 匹配 |
| `data/dup_books.txt` | 输出：重复/禁止/已下载 | `ID,书名,作者名`，**追加** |
| `data/new_books.txt` | 输出：新书 | 同上，**追加**（迁移清理时会重写） |

> ebook-info 的 `id` 与 weread bookId **不可互通**。  
> 本地 books 文件名前缀 **就是** weread bookId，按 ID 精确匹配。

## 匹配规则（脚本已实现）

对每本未处理过的书架书，按优先级：

1. **forbid**：shelf `ID` ∈ `forbid_ids` → **dup**
2. **downloaded**：shelf `ID` ∈ 本地 `*.json` 文件名前缀 → **dup**
3. **ebook-info**：**归一化主书名**与库中任一 `bookName` 归一化结果相同 → **dup**（**作者不同也算重复**）
4. **shelf-title-dup**：**归一化主书名**已出现在 `dup_books` / `new_books` 或本轮更早处理的书架书中 → **dup**（**书架重名只留第一本**，不比作者）
5. 否则 → **new**，并把该主书名记为已占用

### 书名归一化（`normalize_title`）

只比主标题，不比作者：

1. **去掉括号及括号内全部内容**（半角 `()` / 全角 `（）`），可循环多次  
   - 例：`词品（中华经典名著全本全注全译丛书）` → `词品`  
   - 例：`杜甫诗歌鉴赏辞典（珍藏本）` → `杜甫诗歌鉴赏辞典`  
   - 例：`纳兰词(插图注释版 全二册)` → `纳兰词`
2. **去掉副标题**：第一个横线/破折号/冒号及其后内容  
   - 分隔符：`——` `—` `－` `–` `-` `：` `:`  
   - 例：`长安诗酒汴京花：全二册` → `长安诗酒汴京花`  
   - 例：`香尘灭：宋词与宋人` → `香尘灭`  
   - 例：`风止意难平——藏在古诗词里的遗憾` → `风止意难平`
3. **去掉空白与 `·`/`・`/`.`** 后做**全等**比较

因此：

| shelf | 对照 | 结果 |
|-------|------|------|
| `长安诗酒汴京花：全二册` | ebook `长安诗酒汴京花（全二册）` | **dup**（主标题相同） |
| `古今词话` / 杨湜 | ebook `古今词话` / 沈雄 | **dup**（作者不同也算） |
| `续词品` | 仅有 `词品` | **new**（主标题不同） |
| 书架先后 `古今词话` 杨湜 / `古今词话` 沈雄 | （库中无） | 第一本 **new**，第二本 **dup**（`shelf-title-dup`） |
| `词品` 与 `词品（珍藏本）` | 书架内两本 | 归一化同为 `词品` → 只留先出现的一本 |

## 处理流程（与脚本一致）

1. 读 `shelf_books.txt`、`forbid_books.txt`、本地 books 目录、`ebook-info.json`
2. **回扫 new_books**：按当前规则（forbid / 本地已下载 / 归一化书名命中 ebook-info / 与 dup 或 new 内先前条目重名）再判一次；已属 dup 的迁入 `dup_books` 并从 new 删除
3. 已出现在 `dup_books` 或 `new_books` 的 ID → 跳过（可重复运行）
4. 已占用书名集合 = `dup_books` ∪ `new_books` 中所有归一化主书名
5. 对剩余 todo 按上面规则分类；每处理一本（无论 dup/new）都把其主书名加入已占用集合，**追加**写入（不覆盖已有内容）
6. 汇报：forbid 数、本地已下载数、new→dup 迁移数、本轮 dup/new 及原因拆分（含 `shelf-title-dup`）


## 重要：导出后必须重跑

`data/new_books.txt` **不会**在 `export_precise.py` 成功写出 JSON 后自动删行。

批量导出运行时会跳过 `BOOKS_DIR` 里已有同 ID 的书，但**清单文件本身保持原样**。
因此：

1. 生成清单：`python dedupe_shelf_books.py`
2. 批量导出：`python export_precise.py --list data/new_books.txt ...`
3. **导出一批后立刻再跑** `python dedupe_shelf_books.py`  
   → 脚本会把 new 里已下载的 ID **迁移**到 `dup_books.txt`（原因 `downloaded`），并顺带清掉 new 内按当前规则已属 dup 的条目

若只看「上次生成的 new_books」而不重跑，会误以为 skill 没剔除已下载书——那是**清单过期**，不是匹配逻辑失效。可用 `--dry-run` 先看将迁移多少本。

## 边界

- 缺 `shelf_books.txt` 或 `ebook-info.json` → 中止并提示，不要空写结果
- 缺 `forbid_books.txt` 或本地 books 目录 → 不中止，当空集
- **只追加** dup/new（迁移清理 new 时例外会重写）
- 空书名不参与书名占用（无法归一化出 key）
- 不要手改匹配逻辑绕过脚本；若规则要变，改 `dedupe_shelf_books.py` 与本 skill

## 相关代码

- 脚本：`dedupe_shelf_books.py`
- 单测：`tests/test_dedupe_shelf_books.py`
- 书名归一化入口：`normalize_title()`
