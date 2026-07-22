#!/usr/bin/env python3
"""微信读书 — 通用浏览器登录会话。

登录一次后会话保存在 cache/browser_profile/，后续脚本复用，无需重复扫码。
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
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


# Chromium 崩溃后可能残留；有头模式 + 损坏的 Sync Data 会直接 SIGTRAP。
_PROFILE_SINGLETON_NAMES = (
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "RunningChromeVersion",
)


def prepare_browser_profile(user_data_dir: str = USER_DATA_DIR) -> str:
    """启动前清理 profile 中会导致 Chromium 立刻崩溃/锁死的残留。

    - 清除 Singleton* / RunningChromeVersion（上次异常退出残留）
    - 移除 Default/Sync Data（本仓库自动化 profile 不需要 Chrome Sync；
      损坏时会在 headful launch_persistent_context 时 SIGTRAP）
    """
    root = Path(ensure_profile_dir(user_data_dir))
    for name in _PROFILE_SINGLETON_NAMES:
        path = root / name
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass

    sync_data = root / "Default" / "Sync Data"
    try:
        if sync_data.is_dir():
            shutil.rmtree(sync_data, ignore_errors=True)
        elif sync_data.exists() or sync_data.is_symlink():
            sync_data.unlink()
    except OSError:
        pass
    return str(root)


def normalized_viewport(viewport: Optional[dict] = None) -> dict:
    """返回可传给 Chromium 的整数 viewport/window 尺寸。"""
    raw = viewport or DEFAULT_VIEWPORT
    return {
        "width": max(360, int(raw.get("width") or DEFAULT_VIEWPORT["width"])),
        "height": max(480, int(raw.get("height") or DEFAULT_VIEWPORT["height"])),
    }


def with_window_size_arg(args: list[str], viewport: dict) -> list[str]:
    """把真实浏览器窗口尺寸同步到 Chromium 启动参数。"""
    out = [arg for arg in args if not str(arg).startswith("--window-size=")]
    out.append(f"--window-size={viewport['width']},{viewport['height']}")
    return out


def build_launch_kwargs(
    *,
    headless: bool = False,
    viewport: Optional[dict] = None,
    **kwargs: Any,
) -> dict:
    """构建 launch_persistent_context 参数。

    有头模式用真实窗口承载页面，避免窗口很大但网页仍被固定 viewport
    约束在一小块区域内；无头模式保留固定 viewport，保证导出稳定。
    """
    vp = normalized_viewport(viewport)
    args = list(kwargs.pop("args", DEFAULT_ARGS))
    explicit_no_viewport = kwargs.pop("no_viewport", None)
    use_real_window = (
        (not headless)
        if explicit_no_viewport is None
        else bool(explicit_no_viewport)
    )

    launch_kwargs = {
        "headless": headless,
        "args": with_window_size_arg(args, vp) if use_real_window else args,
    }
    if use_real_window:
        launch_kwargs["no_viewport"] = True
    else:
        launch_kwargs["viewport"] = vp
    launch_kwargs.update(kwargs)
    return launch_kwargs


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
    prepare_browser_profile(user_data_dir)
    launch_kwargs = build_launch_kwargs(
        headless=headless,
        viewport=viewport,
        **kwargs,
    )
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


def has_cached_login_profile(user_data_dir: str = USER_DATA_DIR) -> bool:
    """判断 browser profile 是否像已有登录痕迹。

    依据 Cookies / Local Storage 等落盘文件是否非空，不保证会话仍有效；
    仅用于决定「请求 --headless 时是否允许真正无头启动」。
    """
    default_dir = os.path.join(user_data_dir, "Default")
    cookie_candidates = (
        os.path.join(default_dir, "Cookies"),
        os.path.join(default_dir, "Network", "Cookies"),
    )
    for path in cookie_candidates:
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return True
        except OSError:
            continue

    local_storage = os.path.join(default_dir, "Local Storage", "leveldb")
    try:
        if os.path.isdir(local_storage):
            for entry in os.scandir(local_storage):
                if entry.is_file() and entry.name not in (".", "..") and entry.stat().st_size > 0:
                    return True
    except OSError:
        pass
    return False


def resolve_headless(
    requested: bool,
    *,
    user_data_dir: str = USER_DATA_DIR,
    announce: bool = True,
) -> bool:
    """解析最终是否使用无头：仅当请求无头且缓存像有登录态时生效。"""
    if not requested:
        return False
    if has_cached_login_profile(user_data_dir):
        return True
    if announce:
        print(
            "  ⚠️  已指定 --headless，但未检测到 cache 登录信息，"
            "改为有头模式以便扫码登录"
        )
    return False


async def ensure_logged_in(
    context,
    *,
    shelf_url: str = SHELF_URL,
    timeout_ms: int = 30000,
    poll_seconds: float | None = None,
    max_wait_seconds: float = 600.0,
    close_check_page: bool = True,
    allow_interactive_login: bool = True,
) -> bool:
    """确认当前 context 已登录微信读书。

    - 已登录：打印提示并返回 True
    - 需扫码且 allow_interactive_login=True：自动点开登录弹层，等待用户扫码
    - 需扫码且 allow_interactive_login=False（典型无头模式）：打印错误并立即返回 False
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

        if not allow_interactive_login:
            print(
                "  ❌ 无头模式下检测到未登录/登录页，无法扫码。"
                "请去掉 --headless 重新登录，或确认 cache/browser_profile 登录态有效后重试。"
            )
            return False

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
    ok = await ensure_logged_in(
        context, allow_interactive_login=not headless)
    if not ok:
        await context.close()
        return None, None
    page = await context.new_page()
    if url:
        await page.goto(url, wait_until="networkidle", timeout=30000)
    return context, page
