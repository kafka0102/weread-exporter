# 02 — 导出成功后写 JSON + CLI 单本参数

Status: resolved
Blocked by: 01

## Goal

扩展 export_precise：全书抓取成功后写出 `data/books/id_书名.json`；支持 `--force`、`--download-images`、`--out-dir`；默认不下载图片；单本已存在则跳过。

## Acceptance

- 单本 CLI 接受 book_id 或 URL
- 默认不调用图片下载；`--download-images` 才下载
- 成功后 JSON 落在 out-dir，中间产物仍在 output/<book_id>/
- 已存在且无 force → 跳过且不覆盖
- force → 允许重导并覆盖目标 json
- env sleep 新常量接入 env_config/.env.example/AGENTS
- 测试覆盖跳过/force/写文件行为（可 mock 会话）

## Notes

接线时尽量把“会话抓取”与“落盘 JSON”分开，便于测。

## Answer

- 扩展 export_precise：JSON 落盘、--force/--download-images/--out-dir、默认不下载图片、已存在跳过。
