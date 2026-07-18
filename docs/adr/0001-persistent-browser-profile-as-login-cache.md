# ADR-0001：用持久化浏览器 profile 作全仓统一登录缓存

- 状态：已采纳
- 日期：2026-07-18

## 背景（Context）

仓库内多个脚本都需要登录态：`export_precise.py` 导出书籍内容、新增的书架抓取脚本提取书籍列表。若每个脚本各自启动一次性 context，用户每次都要扫码，体验差且无法无人值守续跑。

历史上 `export_precise.py` 内置了一份登录逻辑（goto shelf -> 检测 login -> 轮询等待扫码），与新抽出的 `weread_session.py` 重复。

## 决策（Decision）

1. 用 Playwright `launch_persistent_context` + 固定路径 `cache/browser_profile/` 持久化浏览器 profile，作为全仓统一的登录缓存。首次扫码后登录态落盘，后续脚本复用即免登录。
2. `weread_session.py` 是**全仓唯一登录入口组件**，提供 `launch_weread_context` / `ensure_logged_in` / `open_logged_in_page`。所有脚本通过它启动 context，不再各自内联登录逻辑。
3. `cache/browser_profile/` 已在 `.gitignore`，含敏感 cookie，不入库。

## 结果（Consequences）

- ✅ 登录一次，全仓脚本复用；支持无人值守续跑（导出卡住重开仍免扫码）。
- ✅ 登录逻辑单一来源，消除 `export_precise.py` 的重复。
- ⚠️ 所有脚本依赖 `weread_session.py` 的接口与 `cache/browser_profile/` 约定路径。
- ⚠️ 切换账号需清空 `cache/browser_profile/` 目录。
- ⚠️ profile 路径绑死 `cache/browser_profile/`，迁移需同步改组件默认值与所有调用方。

## 备注

作者字段提取策略（DOM + 网络拦截双保险）属战术选择、易回退，不单独成 ADR，记录在 `.scratch/<slug>/spec.md`。
