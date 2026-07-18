#!/usr/bin/env python3
"""微信读书 — 通用浏览器登录会话。

登录一次后会话保存在 cache/browser_profile/，后续脚本复用，无需重复扫码。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

USER_DATA_DIR = os.path.join("cache", "browser_profile")
SHELF_URL = "https://weread.qq.com/web/shelf"
DEFAULT_VIEWPORT = {"width": 1200, "height": 900}
DEFAULT_ARGS = ["--disable-blink-features=AutomationControlled"]


def ensure_profile_dir(user_data_dir: str = USER_DATA_DIR) -> str:
    """确保持久化 profile 目录存在，返回绝对或相对路径。"""
    os.makedirs(user_data_dir, exist_ok=True)
    return user_data_dir


async def launch_weread_context(
    playwright: Any,
    *,
    user_data_dir: str = USER_DATA_DIR,
    headless: bool = False,
    viewport: Optional[dict] = None,
    **kwargs: Any,
):
    """启动带持久化登录态的 Chromium context。

    调用方负责关闭 context，并管理 async_playwright 生命周期。
    """
    ensure_profile_dir(user_data_dir)
    launch_kwargs = {
        "headless": headless,
        "viewport": viewport or DEFAULT_VIEWPORT,
        "args": list(kwargs.pop("args", DEFAULT_ARGS)),
    }
    launch_kwargs.update(kwargs)
    return await playwright.chromium.launch_persistent_context(
        user_data_dir, **launch_kwargs
    )


def is_login_url(url: str) -> bool:
    return "login" in (url or "").lower()


async def ensure_logged_in(
    context,
    *,
    shelf_url: str = SHELF_URL,
    timeout_ms: int = 30000,
    poll_seconds: float = 5.0,
    max_wait_seconds: float = 600.0,
    close_check_page: bool = True,
) -> bool:
    """确认当前 context 已登录微信读书。

    - 已登录：打印提示并返回 True
    - 需扫码：等待用户完成登录，成功返回 True，超时返回 False
    """
    page = await context.new_page()
    try:
        await page.goto(shelf_url, timeout=timeout_ms)
        await asyncio.sleep(3)
        if not is_login_url(page.url):
            print("  ✅ 已登录（复用缓存会话）")
            return True

        print("\n  ⚠️  请扫码登录微信读书（会话将缓存，后续无需重复登录）")
        waited = 0.0
        while waited < max_wait_seconds:
            await asyncio.sleep(poll_seconds)
            waited += poll_seconds
            if not is_login_url(page.url):
                print("  ✅ 登录成功")
                return True
        print("  ❌ 登录超时")
        return False
    finally:
        if close_check_page:
            await page.close()


async def open_logged_in_page(
    playwright: Any,
    *,
    url: Optional[str] = None,
    user_data_dir: str = USER_DATA_DIR,
    headless: bool = False,
    **context_kwargs: Any,
):
    """启动 context 并确保登录，返回 (context, page)。

    登录失败时关闭 context 并返回 (None, None)。
    """
    context = await launch_weread_context(
        playwright,
        user_data_dir=user_data_dir,
        headless=headless,
        **context_kwargs,
    )
    ok = await ensure_logged_in(context)
    if not ok:
        await context.close()
        return None, None
    page = await context.new_page()
    if url:
        await page.goto(url, wait_until="networkidle", timeout=30000)
    return context, page
