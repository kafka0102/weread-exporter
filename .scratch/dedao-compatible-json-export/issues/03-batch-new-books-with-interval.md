# 03 — 批量 new_books 导出与书间间隔

Status: resolved
Blocked by: 02

## Goal

无 book_id 时从 data/new_books.txt 批量导出；跳过 books 已存在；书间 SLEEP_BOOK_INTERVAL（默认 60）；任一本失败则停止；章切换后动态 sleep 接入真实导出循环。

## Acceptance

- 无参运行读取 new_books.txt
- 跳过已存在 book_id
- 书间等待可配置
- 章完成后按字数 sleep
- 失败停止并非零退出
- README 说明单本/批量用法
- 测试覆盖批量选择与失败停止（mock）

## Notes

批量来源固定 new_books.txt，不在本次改 dedupe。

## Answer

- 无参批量 new_books、书间间隔、章间动态 sleep、失败停止、README 已更新。
