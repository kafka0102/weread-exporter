# 书架「有作者 3289/3294」与 5 条脏行排查（2026-10-23）

> 现象：`python fetch_shelf.py` 报「需打开详情补全作者: 5/3294 本」，逐本打开
> 《编》《著》《主编》《吴夏平 张巍 张之为》《副主编》，全部「未取到作者」，
> 最后「共 3294 本（有作者 3289/3294）」，看上去像书架数据没抓出来。
> 结论：这 5 条不是书，是早期「一行一书名」格式残留的**编者片段**；迁移成
> `ID,书名,作者` 后变成 `主编,,` —— 首字段被当成 id、书名/作者为空。
> `load_shelf_books` 只要求首字段非空，于是它们每轮被读回、写回，还被当成
> reader URL 去打开。修复见 commit `49549bb`。

## 一、现场

`data/shelf_books.txt` 里的 5 行（无 id、无书名、无作者）：

```text
编,,
著,,
主编,,
吴夏平 张巍 张之为,,
副主编,,
```

相邻的真实条目说明它们从哪来：

```text
68432900813abacd8g01084b,刘禹锡研究（第四辑）,戴伟华
主编,, / 吴夏平 张巍 张之为,, / 副主编,,      ← 同一张卡片的编者信息
77e32220813abacc2g014c88,飞花令（含地、天、道、自然）,王立宏   ← 「…编」类卡片
```

| 证据 | 内容 |
|---|---|
| 修 red loop | `load_shelf_books('data/shelf_books.txt')` → 3294 本，其中非法 id 5 条 |
| 补全目标 | 5 条全部进入作者补全，URL 形如 `https://weread.qq.com/web/reader/主编` |
| git 历史 | 首见于 `b22dd6b`（2026-08-10）、`0ee13bc`（2026-09-11）；`567f82e`（2026-10-17）把旧格式的裸行改写成 `X,,` |

## 二、根因：读取端不校验 + 每轮原样写回

1. 现行 DOM 抓取只认 `a[href*="reader"]` 里的 `/reader/([A-Za-z0-9]+)`，`merge_books`
   也按 `is_reader_book_id` 过滤 —— 抓取侧**不会**再产生这种行（抓到的都带合法 id、
   书名非空）。
2. `load_shelf_books` 只判断 `book["id"]` 非空 → `主编,,` 被读成 id=`主编`。
3. 提前停页时 `merge_partial_shelf` 把「没翻到的旧书」原样接回，`write_shelf_books`
   再写回 → 脏行**自我复现**，不会随时间消失。
4. `books_missing_author` 只看 author 是否为空 → 5 条进入补全流程，去打开必然 404
   的 reader URL，每轮白跑 5 次（间隔 5s）。

## 三、顺带核实：`首屏 API 0 本` 是正常时序

一次性探针（只读，跑完即删）复跑书架首屏：12 秒内 `syncBook` **不触发**，滚动一屏
后才出现 `API +100`，且返回的 bookId **全是纯数字**（合法 reader id = 0）。
这与 7 月的 `.scratch/diag_shelf_full.log`（`apiValid=0`、`authorFilledByTitle 0`）一致。
即：`title`/`author` 一直只来自 DOM + 历史文件回填，`API 0 本` 不代表抓取失败；
提前停页（首屏已连续 10 本在本地）时根本不会滚动，所以 API 一直显示 0。

## 四、修复与验证

- `drop_invalid_books`：按 `is_reader_book_id` 分离脏行（保持原顺序）
- `read_shelf_rows` / `load_shelf_books`：前者读全部可解析行，后者只保留合法
  reader book_id —— 脏行不再进入合并与补全，文件下次抓取写回即自愈
- `fetch_shelf`：读取本地文件时提示 `🧹 …有 N 条脏行（…）：已忽略，写回时清理`，
  落盘前再过滤一次兜底
- `books_needing_author_lookup`：补全作者前再过滤，绝不打开非 reader URL
- 回归测试：`tests/test_fetch_shelf.py` 新增 6 个用例（脏行分离、读写自愈、
  补全过滤）

验证：

```text
修复前：loaded 3294，invalid 5，待补全作者 5
修复后：loaded 3289，invalid 0，待补全作者 0
```

全套 `python -m unittest discover -s tests -t .`：412 通过、2 跳过
（`tests/test_repair_empty_chapters.py` 因环境缺 `pytest` 收集失败，与本次无关）。
