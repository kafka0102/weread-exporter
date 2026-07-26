# forbid-books Specification

## Purpose

维护 weread book ID 禁止列表，并在批量导出与书架去重中统一生效。

## Requirements

### Requirement: Forbid list file format
系统 MUST 支持读取 `data/forbid_books.txt` 作为禁止下载的微信读书 book ID 集合。文件每行最多一个 ID；空行与以 `#` 开头的注释行 MUST 被忽略。若某行包含逗号，系统 MUST 仅取逗号前第一段作为 ID。文件不存在时 MUST 视为空集合且不得中断流程。

#### Scenario: Load IDs with comments and blanks
- **WHEN** `data/forbid_books.txt` 内容为若干 ID、空行与 `#` 注释
- **THEN** 加载结果仅为有效 ID 集合，不含空串与注释文本

#### Scenario: Missing forbid file
- **WHEN** `data/forbid_books.txt` 不存在
- **THEN** 禁止集合为空，批量导出与去重按未配置禁止列表继续执行

#### Scenario: Shelf-style line is accepted as ID only
- **WHEN** 禁止列表某行为 `bookid,书名,作者`
- **THEN** 仅 `bookid` 进入禁止集合

### Requirement: Batch export skips forbidden books
当以清单文件（默认 `data/new_books.txt`）批量导出时，系统 MUST 跳过禁止集合中的 book ID，不得为其打开阅读器或写入新的导出结果；跳过 MUST 不视为失败，批量流程 MUST 继续处理其余书。显式传入单个 book_id / reader URL 的导出路径不在本要求强制拦截范围内。

#### Scenario: Forbidden book in new_books list
- **WHEN** `new_books.txt` 含书 A 与书 B，且 A 的 ID 在 `forbid_books.txt` 中
- **THEN** 批量导出跳过 A、处理 B（若 B 仍待导出），且不因跳过 A 而中止

#### Scenario: All books forbidden
- **WHEN** 清单内全部 ID 均在禁止集合中
- **THEN** 系统报告无待处理或全部被禁止跳过，正常结束且退出成功语义与「无待导出」一致

### Requirement: Dedupe treats forbidden books as dup
书架去重流程 MUST 将命中 `data/forbid_books.txt` 的书架书判定为 dup：追加到 `data/dup_books.txt`，且 MUST NOT 写入 `data/new_books.txt`。该判定 MUST 在「待写入 new」之前生效，且不依赖 ebook-info 书名作者匹配。

#### Scenario: Shelf book ID is forbidden
- **WHEN** `shelf_books.txt` 中某书 ID 存在于 `forbid_books.txt`，且该 ID 尚未出现在 dup/new 输出中
- **THEN** 该行写入 `dup_books.txt`，不写入 `new_books.txt`

#### Scenario: Forbidden overrides would-be new
- **WHEN** 某书架书未本地下载、ebook-info 也不匹配，但 ID 在禁止列表中
- **THEN** 仍判定为 dup，不得因「语义上是新书」而写入 `new_books.txt`
