# 01 — JSON 组装与章间 sleep 纯函数

Status: resolved
Blocked by:

## Goal

落地与 dedao/json 兼容的纯函数：markdown→纯文本、章节/书级 JSON 组装、文件名安全化、已存在判定、章间 sleep 秒数、批量清单解析。全部可单测，不依赖 Playwright。

## Acceptance

- content 为纯文本、无章节标题行；空正文 has_content=false
- chapter_id 为 ch_0001 形式
- word_count 为各章 content 字数和
- press/publication_date/isbn 默认空串
- book_json_exists 按 book_id 前缀匹配
- chapter_sleep_seconds 符合每千字 2s、min2、max15（参数化）
- 相关 unittest 通过

## Notes

Spec: `.scratch/dedao-compatible-json-export/spec.md`
Seam: JSON 导出组装 seam

## Answer

- 新增 `book_json.py` 纯函数模块与 `tests/test_book_json.py`（8 tests green）。
