#!/usr/bin/env python3
"""微信读书 — 通用浏览器登录会话。

登录一次后会话保存在 cache/browser_profile/，后续脚本复用，无需重复扫码。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import env_config  # noqa: F401  # 导入即加载 .env
from env_config import (
    SLEEP_LOGIN_AFTER_GOTO,
    SLEEP_LOGIN_POLL,
    SLEEP_LOGIN_SWITCH_PC,
)

USER_DATA_DIR = os.path.join("cache", "browser_profile")
SHELF_URL = "https://weread.qq.com/web/shelf"
DEFAULT_VIEWPORT = {"width": 1200, "height": 900}
DEFAULT_ARGS = ["--disable-blink-features=AutomationControlled"]

# 未登录时书架页导航栏上的「登录」按钮；点击后打开扫码弹层，URL 变为 ...#login
LOGIN_BUTTON_SELECTOR = "button.navBar_link_Login"
LOGIN_DIALOG_SELECTOR = ".login_dialog_container, .login_dialog_qrcode_img_main"


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


async def page_needs_login(page) -> bool:
    """判断当前页面是否仍需登录。

    微信读书未登录时打开 /web/shelf 不会跳转到登录 URL，只会显示导航栏
    「登录」按钮；仅靠 URL 会误判为已登录。
    """
    if is_login_url(page.url):
        return True
    try:
        return await page.locator(LOGIN_BUTTON_SELECTOR).count() > 0
    except Exception:
        return False


async def open_login_dialog(page) -> bool:
    """打开扫码登录弹层。已打开或成功点击返回 True，找不到按钮返回 False。"""
    try:
        if await page.locator(LOGIN_DIALOG_SELECTOR).count() > 0:
            return True
        btn = page.locator(LOGIN_BUTTON_SELECTOR).first
        if await btn.count() == 0:
            return False
        await btn.click(timeout=5000)
        await asyncio.sleep(SLEEP_LOGIN_SWITCH_PC)
        return True
    except Exception:
        return False


async def ensure_logged_in(
    context,
    *,
    shelf_url: str = SHELF_URL,
    timeout_ms: int = 30000,
    poll_seconds: float | None = None,
    max_wait_seconds: float = 600.0,
    close_check_page: bool = True,
) -> bool:
    """确认当前 context 已登录微信读书。

    - 已登录：打印提示并返回 True
    - 需扫码：自动点开登录弹层，等待用户扫码；成功返回 True，超时返回 False
    """
    if poll_seconds is None:
        poll_seconds = SLEEP_LOGIN_POLL
    page = await context.new_page()
    try:
        await page.goto(shelf_url, timeout=timeout_ms)
        try:
            await page.wait_for_selector(
                f"{LOGIN_BUTTON_SELECTOR}, a[href*='reader'], [data-book-id]",
                timeout=10000,
            )
        except Exception:
            await asyncio.sleep(SLEEP_LOGIN_AFTER_GOTO)

        if not await page_needs_login(page):
            print("  ✅ 已登录（复用缓存会话）")
            return True

        opened = await open_login_dialog(page)
        if opened:
            print("\n  ⚠️  已打开登录弹层，请用微信扫码登录（会话将缓存，后续无需重复登录）")
        else:
            print("\n  ⚠️  未检测到登录弹层，请在浏览器中手动点击「登录」并扫码")

        waited = 0.0
        while waited < max_wait_seconds:
            await asyncio.sleep(poll_seconds)
            waited += poll_seconds
            if not await page_needs_login(page):
                print("  ✅ 登录成功")
                return True
            remaining = max(0, int(max_wait_seconds - waited))
            print(f"  … 等待扫码中（剩余约 {remaining}s）")
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
