#!/usr/bin/env python3
"""
微信读书导出 — 精确图文版 v3

逐页捕获：每翻一页，抓当前视口内的 canvas 文字 + 视口内图片，
按屏幕 y 坐标把文字行和图片交错排序，图片精确落在对应段落之间。
双页拆分(左页→右页)，按章节切分，自动续传，卡住重开。
"""
import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

import env_config  # noqa: F401  # 导入即加载 .env
from book_json import (
    book_json_exists,
    build_book_json_from_chapter_mds,
    chapter_sleep_seconds,
    filter_forbidden_books,
    filter_pending_books,
    iter_batch_book_ids,
    load_chapters_from_export_dir,
    write_book_json,
)
from env_config import (
    BOOKS_DIR,
    READER_FORCE_SINGLE_PAGE,
    READER_VIEWPORT_HEIGHT,
    READER_VIEWPORT_WIDTH,
    resolve_reader_viewport,
    SLEEP_BOOK_INTERVAL,
    SLEEP_CHAPTER_MAX,
    SLEEP_CHAPTER_MIN,
    SLEEP_CHAPTER_PER_2K_CHARS,
    SLEEP_READER_AFTER_HOOK,
    SLEEP_READER_AFTER_LOAD,
    SLEEP_READER_CATALOG_CLICK,
    SLEEP_READER_CATALOG_CLOSE,
    SLEEP_READER_CATALOG_OPEN,
    SLEEP_READER_CATALOG_SCROLL,
    SLEEP_READER_PAGE_RENDER,
    SLEEP_READER_PAGE_TURN,
    SLEEP_READER_REOPEN,
    SLEEP_READER_STABLE_POLL,
)
from weread_session import (
    USER_DATA_DIR,
    ensure_logged_in,
    launch_weread_context,
    page_needs_login,
    resolve_headless,
    ensure_browser_window_size,
)

DEFAULT_BOOKS_DIR = BOOKS_DIR


def format_elapsed(seconds):
    """把耗时格式化为秒或分钟（>=60 秒用分钟）。"""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        if abs(seconds - round(seconds)) < 0.05:
            return f"{int(round(seconds))} 秒"
        return f"{seconds:.1f} 秒"
    minutes = seconds / 60.0
    if abs(minutes - round(minutes)) < 0.05:
        return f"{int(round(minutes))} 分钟"
    return f"{minutes:.1f} 分钟"

DEFAULT_NEW_BOOKS = Path("data") / "new_books.txt"


def reader_viewport(width=None, height=None, *, prefer_largest=None):
    """导出用阅读器视口。宽/高 0 或未配置时自动匹配本机屏幕。"""
    return resolve_reader_viewport(width, height, prefer_largest=prefer_largest)


def viewport_focus_point(viewport=None):
    """点击聚焦阅读器内容区的坐标（视口中心略偏上）。"""
    vp = viewport or reader_viewport()
    return int(vp["width"] * 0.5), int(vp["height"] * 0.45)


async def page_viewport(page, fallback=None):
    """读取当前页面真实 viewport；失败时回退到配置值。"""
    fb = dict(fallback or reader_viewport())
    try:
        size = await page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight})"
        )
        w = int(size.get("width") or 0)
        h = int(size.get("height") or 0)
        if w > 0 and h > 0:
            return {"width": w, "height": h}
    except Exception:
        pass
    return fb


async def ensure_configured_viewport(page, viewport):
    """对齐阅读器视口与真实窗口。

    有头模式（no_viewport）下窗口常小于「本机屏幕配置值」。
    若仍 set_viewport_size 到更大 CSS 尺寸，会出现：
    - 正文 canvas 不渲染（canvas≈0）
    - 黑屏、目录侧栏关不掉、跳转校验永远失败

    规则：
    - 实际视口已接近配置：接受实际
    - 实际可用（宽≥800 且 高≥480）但小于配置：接受实际，绝不强行放大
    - 实际过小（异常）时才尝试 set_viewport_size 到配置
    """
    desired = {
        "width": int(viewport["width"]),
        "height": int(viewport["height"]),
    }
    actual = await page_viewport(page, desired)
    if actual == desired:
        return desired

    width_ok = actual["width"] >= desired["width"] - 8
    height_ok = actual["height"] >= max(480, int(desired["height"] * 0.7))
    if width_ok and height_ok:
        print(
            f"  🪟 页面视口: {actual['width']}x{actual['height']} "
            f"（配置 {desired['width']}x{desired['height']}），"
            f"接受实际窗口尺寸"
        )
        return actual

    # 窗口比配置小但已可用：有头模式常见，接受窗口，禁止放大 CSS 视口
    usable = actual["width"] >= 800 and actual["height"] >= 480
    smaller_than_desired = (
        actual["width"] < desired["width"] - 8
        or actual["height"] < desired["height"] - 8
    )
    if usable and smaller_than_desired:
        print(
            f"  🪟 页面视口: {actual['width']}x{actual['height']} "
            f"（配置 {desired['width']}x{desired['height']}），"
            f"接受实际窗口（不强行放大 CSS 视口）"
        )
        return actual

    print(
        f"  🪟 页面视口: {actual['width']}x{actual['height']} "
        f"（配置 {desired['width']}x{desired['height']}），"
        f"实际过小，尝试强制配置值"
    )
    try:
        await page.set_viewport_size(desired)
        await asyncio.sleep(SLEEP_READER_PAGE_RENDER)
        forced = await page_viewport(page, desired)
        if forced["width"] >= 800 and forced["height"] >= 480:
            print(
                f"  ✅ 已强制视口 {forced['width']}x{forced['height']}"
            )
            return forced
        print(
            f"  ⚠️  强制后视口仍为 {forced['width']}x{forced['height']}，"
            f"改用实际 {actual['width']}x{actual['height']}"
        )
        return actual
    except Exception as e:
        print(f"  ⚠️  强制视口失败: {e}，改用实际 {actual['width']}x{actual['height']}")
        return actual


async def focus_reader_for_keyboard(page):
    """把键盘焦点交给页面主体，不点击正文或图片。"""
    return await page.evaluate(
        """() => {
            const active = document.activeElement;
            if (active && active !== document.body && typeof active.blur === 'function') {
                active.blur();
            }
            if (!document.body) return '';
            const hadTabIndex = document.body.hasAttribute('tabindex');
            if (!hadTabIndex) document.body.setAttribute('tabindex', '-1');
            document.body.focus({preventScroll: true});
            if (!hadTabIndex) document.body.removeAttribute('tabindex');
            return document.activeElement?.tagName || '';
        }"""
    )


async def blur_reader_inputs(page) -> None:
    """失焦任何 input/textarea，避免按键打进搜索框。"""
    try:
        await page.evaluate(
            """() => {
                const blurOne = (el) => {
                    try { if (el && typeof el.blur === 'function') el.blur(); } catch (e) {}
                };
                blurOne(document.activeElement);
                document.querySelectorAll('input, textarea, [contenteditable="true"]').forEach(blurOne);
            }"""
        )
    except Exception:
        pass


async def dismiss_reader_search(page) -> bool:
    """关闭目录/顶栏搜索态，绝不去点搜索图标本身。

    仅当「可见搜索输入 + 顶栏取消」同时出现时，才认定进入搜索态并点取消。
    目录面板里常驻但尺寸为 0 的搜索 input、以及 pointer-events:none 的
    float search 包装层，都不当作搜索态，避免误按 Esc/误点搜索。
    返回是否实际处理过搜索 UI。
    """
    handled = False
    try:
        clicked_cancel = await page.evaluate(
            """() => {
                const visible = (el, minW=12, minH=12) => {
                    if (!el) return false;
                    const st = window.getComputedStyle(el);
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    if (parseFloat(st.opacity || '1') < 0.05) return false;
                    if (st.pointerEvents === 'none') return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < minW || r.height < minH) return false;
                    const vw = window.innerWidth, vh = window.innerHeight;
                    if (r.right <= 0 || r.left >= vw || r.bottom <= 0 || r.top >= vh) return false;
                    return true;
                };
                // 真·搜索态：可见、可点、有尺寸的搜索输入
                const hasSearch = Array.from(document.querySelectorAll(
                    'input[placeholder*="搜索"], input[type="search"]'
                )).some(el => visible(el, 40, 12));
                if (!hasSearch) return false;
                // 只点顶栏「取消」，绝不点 title/aria 含搜索的按钮
                for (const el of Array.from(document.querySelectorAll('button, a, span, div'))) {
                    const label = (
                        (el.getAttribute('title') || '') + ' ' +
                        (el.getAttribute('aria-label') || '') + ' ' +
                        (el.textContent || '')
                    ).trim();
                    if (/搜索|search|查找/i.test(label) && (el.textContent || '').trim() !== '取消') {
                        continue;
                    }
                    if ((el.textContent || '').trim() !== '取消') continue;
                    if (!visible(el, 12, 12)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.top > 140) continue;
                    el.click();
                    return true;
                }
                return false;
            }"""
        )
        if clicked_cancel:
            handled = True
            await asyncio.sleep(0.12)
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.08)
        # 无论是否搜索态都 blur，防止方向键打进输入框
        await blur_reader_inputs(page)
    except Exception:
        try:
            await blur_reader_inputs(page)
        except Exception:
            pass
    return handled


async def turn_reader_page(page, *, method: str = "arrow"):
    """聚焦阅读器并翻到下一页。

    method:
      - arrow: ArrowRight（默认）
      - arrow2: 连按两次 ArrowRight
      - click_midright: 点阅读区中右（避开右侧工具条与搜索按钮）
      - pagedown/space: 仅显式指定时使用
    """
    await dismiss_reader_search(page)
    await focus_reader_for_keyboard(page)
    await blur_reader_inputs(page)
    m = (method or "arrow").strip().lower()
    if m == "arrow2":
        await page.keyboard.press("ArrowRight")
        await asyncio.sleep(0.05)
        await page.keyboard.press("ArrowRight")
        return "arrow2"
    if m in ("click_midright", "click_right"):
        # 右侧工具条约在 x≈1028（1200 宽）；点中右内容区翻下一页，避开工具条
        try:
            box = await page.evaluate(
                """() => {
                    const vw = window.innerWidth || 1200;
                    const vh = window.innerHeight || 900;
                    const x = Math.min(vw * 0.72, vw - 220);
                    const y = vh * 0.48;
                    return {x, y};
                }"""
            )
            await page.mouse.click(float(box["x"]), float(box["y"]))
        except Exception:
            await page.keyboard.press("ArrowRight")
        await blur_reader_inputs(page)
        return "click_midright"
    key = {
        "space": "Space",
        "pagedown": "PageDown",
        "arrow": "ArrowRight",
    }.get(m, "ArrowRight")
    await page.keyboard.press(key)
    return m

async def force_reader_repaint(page) -> None:
    """翻页后 canvas 未再次 fillText 时，触发重绘。

    有头模式常用 no_viewport，set_viewport_size 会失败；改为 resize 事件 +
    canvas 微样式扰动 + 鼠标微移，避免静默抓空页。
    """
    try:
        await page.evaluate(
            """() => {
                try { window.dispatchEvent(new Event('resize')); } catch (e) {}
                document.querySelectorAll('canvas').forEach(c => {
                    try {
                        const st = c.style;
                        const old = st.transform;
                        st.transform = 'translateZ(0) scale(1.001)';
                        void c.offsetWidth;
                        st.transform = old || '';
                    } catch (e) {}
                });
            }"""
        )
        # 微移鼠标：不 click，避免拉起顶栏/误触搜索
        try:
            await page.mouse.move(380, 420)
            await asyncio.sleep(0.03)
            await page.mouse.move(400, 430)
        except Exception:
            pass
        # 无头/固定 viewport 时再尝试 1px 尺寸抖动（失败忽略）
        try:
            vp = await page.evaluate(
                "() => ({width: window.innerWidth, height: window.innerHeight})"
            )
            w = int(vp.get("width") or 0)
            h = int(vp.get("height") or 0)
            if w > 20 and h > 20:
                await page.set_viewport_size({"width": w - 1, "height": h})
                await asyncio.sleep(0.04)
                await page.set_viewport_size({"width": w, "height": h})
        except Exception:
            pass
        await asyncio.sleep(0.05)
    except Exception:
        pass


async def recover_reader_text_after_nav(page, *, allow_nudge: bool = True) -> int:
    """目录跳转/重定位后尽量保住 canvas 文字，禁止先 reset 再空等。

    历史 bug：跳转等待期间 fillText 已写入 __wr_chars，随后 __wr_reset
    一把清掉，再靠微扰动很难重绘 → 页面明明有字、日志一直 stale。
    返回稳定后的字符计数（可能为 0）。

    allow_nudge=False 时只做短等+微重绘，供普通翻页空抓使用，避免每轮 10s+。
    """
    # 1) 先等已有绘制落稳（不 reset）——目录跳后通常已有字
    count = await wait_stable(page, 0, timeout=1.2 if allow_nudge else 0.6)
    if count and count > 0:
        return count
    # 2) 扰动重绘，仍不 reset
    await force_reader_repaint(page)
    count = await wait_stable(page, 0, timeout=1.0 if allow_nudge else 0.5)
    if count and count > 0:
        return count
    if not allow_nudge:
        return int(count or 0)
    # 3) 左右键轻推一页再回到当前，迫使重新 fillText（仅导航后）
    try:
        await dismiss_reader_search(page)
        await focus_reader_for_keyboard(page)
        await blur_reader_inputs(page)
        await page.keyboard.press("ArrowLeft")
        await asyncio.sleep(0.12)
        await page.evaluate("() => window.__wr_reset && window.__wr_reset()")
        await page.keyboard.press("ArrowRight")
        await asyncio.sleep(max(0.15, float(SLEEP_READER_PAGE_TURN) * 0.4))
        await force_reader_repaint(page)
        count = await wait_stable(page, 0, timeout=1.5)
        if count and count > 0:
            return count
    except Exception:
        pass
    return int(count or 0)


def classify_reader_paging_mode(geo: dict) -> str:
    """根据滚动高度与 canvas 几何判断翻页模式。

    - horizontal: 左右翻页/双页（文档几乎不纵向滚动，或 canvas 并排）
    - vertical_scroll: 上下滚动长文（scrollHeight 明显大于视口，或 canvas 纵向堆叠）
    - unknown: 信息不足
    """
    if not isinstance(geo, dict):
        return "unknown"
    try:
        scroll_h = float(geo.get("scrollHeight") or 0)
        inner_h = float(geo.get("innerHeight") or 0)
    except (TypeError, ValueError):
        scroll_h, inner_h = 0.0, 0.0
    cans = list(geo.get("canvases") or [])
    if inner_h > 0 and scroll_h > inner_h * 2.2:
        return "vertical_scroll"
    if len(cans) >= 2:
        try:
            lefts = sorted(float(c.get("l") or 0) for c in cans)
            tops = sorted(float(c.get("t") or 0) for c in cans)
        except (TypeError, ValueError):
            lefts, tops = [], []
        if lefts and tops:
            if lefts[-1] - lefts[0] > 120 and tops[-1] - tops[0] < 120:
                return "horizontal"
            if lefts[-1] - lefts[0] < 80 and tops[-1] - tops[0] > 200:
                return "vertical_scroll"
    if inner_h > 0 and scroll_h <= inner_h * 1.5:
        return "horizontal"
    return "unknown"


async def inspect_reader_geometry(page) -> dict:
    """读取滚动容器与正文 canvas 屏幕几何，供翻页模式判断。"""
    try:
        geo = await page.evaluate(
            """() => {
                const se = document.scrollingElement || document.documentElement;
                const cans = Array.from(document.querySelectorAll('canvas'))
                    .map(c => {
                        const r = c.getBoundingClientRect();
                        return {
                            t: Math.round(r.top),
                            l: Math.round(r.left),
                            w: Math.round(r.width),
                            h: Math.round(r.height),
                        };
                    })
                    .filter(c => c.h > 200 && c.w > 80);
                return {
                    scrollHeight: se ? se.scrollHeight : 0,
                    innerHeight: window.innerHeight || 0,
                    scrollY: Math.round(window.scrollY || 0),
                    canvases: cans,
                };
            }"""
        )
        return geo if isinstance(geo, dict) else {}
    except Exception:
        return {}


async def ensure_horizontal_paging_mode(page) -> str:
    """确保阅读器处于左右翻页（非上下长文滚动）。

    上下滚动模式下：
    - 目录跳转常滚到错误锚点，正文 canvas 落在视口外；
    - ArrowRight 会在两页之间空转；
    - fillText 不随 scroll 重绘，抓取大量空页。

    检测到滚动模式时，点击右侧「双栏/普通阅读」切换按钮
    （button.readerControls_item.isNormalReader）。已是左右模式时绝不点击，
    避免误切回滚动。
    返回最终模式：horizontal / vertical_scroll / unknown。
    """
    geo = await inspect_reader_geometry(page)
    mode = classify_reader_paging_mode(geo)
    n0 = len(geo.get("canvases") or [])
    if n0 == 0:
        # 视口错位或尚未绘制：短等 + 重绘后再测，避免 canvas≈0 时误判
        try:
            await force_reader_repaint(page)
            await asyncio.sleep(max(0.3, float(SLEEP_READER_PAGE_RENDER)))
            geo = await inspect_reader_geometry(page)
            mode = classify_reader_paging_mode(geo)
            n0 = len(geo.get("canvases") or [])
        except Exception:
            pass
        if n0 == 0:
            print(
                "  ⚠️  未检测到正文 canvas（可能视口/加载异常），"
                "稍后目录跳转可能失败"
            )
    if mode == "horizontal":
        n = len(geo.get("canvases") or [])
        print(f"  📖 翻页模式: 左右/双页（canvas≈{n}，非长文滚动）")
        return mode
    if mode != "vertical_scroll":
        return mode

    print(
        "  ⚠️  检测到上下滚动阅读"
        f"（scrollH={geo.get('scrollHeight')}/{geo.get('innerHeight')}），"
        "尝试切换到左右翻页…"
    )
    try:
        box = await page.evaluate(
            """() => {
                const btn = document.querySelector(
                    'button.readerControls_item.isNormalReader'
                );
                if (!btn) return null;
                const r = btn.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) {
                    try { btn.click(); } catch (e) {}
                    return {js: true};
                }
                return {
                    x: r.x + r.width / 2,
                    y: r.y + r.height / 2,
                };
            }"""
        )
    except Exception:
        box = None
    if not box:
        print("  ⚠️  未找到阅读模式切换按钮，继续在滚动模式下抓取（易卡住）")
        return mode
    try:
        if not box.get("js"):
            await page.mouse.click(float(box["x"]), float(box["y"]))
    except Exception as e:
        print(f"  ⚠️  切换翻页模式点击失败: {e}")
        return mode
    await asyncio.sleep(max(1.0, float(SLEEP_READER_AFTER_LOAD) * 0.4))
    try:
        await force_reader_repaint(page)
    except Exception:
        pass
    geo2 = await inspect_reader_geometry(page)
    mode2 = classify_reader_paging_mode(geo2)
    if mode2 == "horizontal":
        print("  ✅ 已切换为左右翻页模式")
    else:
        print(
            f"  ⚠️  切换后仍为 {mode2}"
            f"（scrollH={geo2.get('scrollHeight')}/{geo2.get('innerHeight')}）"
        )
    return mode2


async def count_reader_canvases(page):
    """可见正文 canvas 数量（高度足够的才算阅读页）。"""
    return await page.evaluate(
        """() => Array.from(document.querySelectorAll('canvas'))
            .map(c => c.getBoundingClientRect())
            .filter(r => r.height > 300 && r.width > 100).length"""
    )


async def ensure_reader_layout(page, viewport, *, force_single_page=False):
    """记录阅读器布局；必要时逐步收窄视口，尽量落到单页。

    微信读书 web 在宽视口下会并排渲染左右两页（两个 canvas）。
    返回实际采用的 viewport。

    重要：强制单页若全部失败，必须恢复原始桌面视口。
    否则会停留在最后一次尝试的窄 CSS 视口（如 480），
    而浏览器窗口仍很宽，页面呈现「左侧一条内容、右侧大片空白」。
    """
    original = {
        "width": int(viewport["width"]),
        "height": int(viewport["height"]),
    }
    vp = dict(original)
    n = await count_reader_canvases(page)
    if n <= 1:
        if n == 1:
            print("  📄 阅读布局: 单页")
        return vp

    if not force_single_page:
        print(f"  📖 阅读布局: 双页（canvas={n}），保持桌面排版并按 canvas 拆页")
        return vp

    print(f"  ⚠️  检测到双页布局（canvas={n}），尝试收窄视口强制单页…")
    narrowed = False
    for w in (720, 640, 560, 480):
        if w >= original["width"]:
            continue
        vp = {"width": w, "height": original["height"]}
        narrowed = True
        try:
            await page.set_viewport_size(vp)
        except Exception as e:
            print(f"    set_viewport_size({w}) 失败: {e}")
            continue
        try:
            await page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"    reload 异常: {e}")
        await asyncio.sleep(SLEEP_READER_AFTER_LOAD)
        n = await count_reader_canvases(page)
        print(f"    视口 {w}x{vp['height']} → canvas={n}")
        if n <= 1:
            print("  📄 阅读布局: 单页")
            return vp

    print(f"  ⚠️  仍为双页（canvas={n}），将依赖按 canvas 拆页兜底")
    if narrowed and vp != original:
        print(
            f"  🪟 恢复桌面视口 {original['width']}x{original['height']}，"
            f"避免窄 CSS 视口留在宽窗口中"
        )
        try:
            await page.set_viewport_size(original)
        except Exception as e:
            print(f"    恢复 set_viewport_size 失败: {e}")
        try:
            await page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"    恢复 reload 异常: {e}")
        await asyncio.sleep(SLEEP_READER_AFTER_LOAD)
        return original
    return original


CANVAS_HOOK = r"""
(function() {
    window.__wr_chars = [];
    window.__wr_canvas_seq = window.__wr_canvas_seq || 0;
    function ensureCid(canvas) {
        if (!canvas) return 0;
        if (canvas.__wr_cid == null) {
            window.__wr_canvas_seq += 1;
            canvas.__wr_cid = window.__wr_canvas_seq;
        }
        return canvas.__wr_cid || 0;
    }
    function dropCid(cid) {
        if (!cid || !window.__wr_chars || !window.__wr_chars.length) return;
        window.__wr_chars = window.__wr_chars.filter(function(c) {
            return (c && c.cid) !== cid;
        });
    }
    var origFill = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y) {
        if (text && String(text).trim()) {
            var cl = 0, ct = 0, s = null, cid = 0;
            try {
                var canvas = this.canvas;
                if (canvas) {
                    cid = ensureCid(canvas);
                    if (canvas.getBoundingClientRect) {
                        var r = canvas.getBoundingClientRect();
                        cl = Math.round(r.left);
                        ct = Math.round(r.top);
                    }
                }
            } catch (e) {}
            try {
                var m = /(\d+(?:\.\d+)?)px/.exec(this.font || '');
                if (m) s = parseFloat(m[1]);
            } catch (e) {}
            window.__wr_chars.push({
                t: String(text),
                x: Math.round(x * 10) / 10,
                y: Math.round(y * 10) / 10,
                cl: cl,
                ct: ct,
                cid: cid,
                s: s
            });
        }
        return origFill.apply(this, arguments);
    };
    // 整页重绘前常 clearRect 全画布；丢掉该 canvas 旧字，避免 force_repaint
    // 叠两次绘制把左右栏/新旧页字符交错拼成乱码。
    var origClear = CanvasRenderingContext2D.prototype.clearRect;
    CanvasRenderingContext2D.prototype.clearRect = function(x, y, w, h) {
        try {
            var canvas = this.canvas;
            if (canvas) {
                var cid = ensureCid(canvas);
                var cw = canvas.width || 0, ch = canvas.height || 0;
                var area = Math.abs(Number(w) * Number(h));
                var full = cw > 0 && ch > 0 && area >= cw * ch * 0.45;
                // 也覆盖 clearRect(0,0,huge,huge) 而未用 canvas.width 的情况
                if (full || (Number(x) <= 0 && Number(y) <= 0 && Number(w) >= cw * 0.9 && Number(h) >= ch * 0.9)) {
                    dropCid(cid);
                }
            }
        } catch (e) {}
        return origClear.apply(this, arguments);
    };
    window.__wr_reset = function() { window.__wr_chars = []; };
    window.__wr_count = function() { return window.__wr_chars.length; };
})();
"""

# 当前视口内可见的书籍插图，带屏幕坐标
VIEWPORT_IMGS_JS = """
() => {
    const H = window.innerHeight, W = window.innerWidth, out = [];
    document.querySelectorAll('img[class*="wr_readerImage"]').forEach(i => {
        const src = i.src || i.getAttribute('data-src') || '';
        if (!src.includes('res.weread.qq.com/wrepub')) return;
        const r = i.getBoundingClientRect();
        if (r.width > 40 && r.height > 40 && r.bottom > 0 && r.top < H &&
            r.right > 0 && r.left < W &&
            getComputedStyle(i).visibility !== 'hidden' &&
            getComputedStyle(i).display !== 'none') {
            out.push({src, top: Math.round(r.top), left: Math.round(r.left),
                      w: i.naturalWidth||i.width, h: i.naturalHeight||i.height});
        }
    });
    return out;
}
"""

# reader 的两个 canvas 的屏幕位置
CANVAS_RECTS_JS = """
() => Array.from(document.querySelectorAll('canvas')).map(c => {
    const r = c.getBoundingClientRect();
    return {top: r.top, left: r.left, w: Math.round(r.width), h: Math.round(r.height)};
}).filter(r => r.h > 300)
"""

MEASURE_RE = re.compile(r'^[a-zA-Z0-9`~!@#$%^&*()\-_=+\[\]{}|;:\',<.>/?\\"\s]+$')
SENTENCE_END = set("。！？；：」）】》…—")
# canvas 软折行合并阈值：短于此长度的行（词牌/作者名等）不与邻行粘连
SOFT_WRAP_MIN_LEN = 16
# 软阈值：触发更积极的正文越章/切章检查（长章正常翻页也会超过此值）
RUNAWAY_CHAPTER_LINES = 2500
RUNAWAY_CHAPTER_PAGES = 120
# 硬阈值：仅停滞时用于判定「脏缓冲不落盘」；有新内容时绝不因行数重开
HARD_RUNAWAY_CHAPTER_LINES = 12000
HARD_RUNAWAY_CHAPTER_PAGES = 400
# 顶栏跨过「下一章」仍持续灌入新正文时，连续确认后按正文切章（不点目录）
HEADER_MULTI_AHEAD_CONFIRM = 2
# 标题前缀后若接这些成分，视为正文提及而非新章起始
_NOT_CHAPTER_START_REST = re.compile(
    r"^(的|与|和|在|是|了|也|都|就|还|曾|并|便|则|却|又|已|将|会|能|要|"
    r"把|被|让|从|向|对|比|因|而|但|曾经|这首|早在|不过|与他|便是)"
)


def chapter_text_line_count(blocks) -> int:
    """当前章缓冲中的正文行数。"""
    return sum(1 for b in (blocks or []) if b.get("type") == "text")


def is_soft_runaway_chapter(n_lines: int, page_num: int) -> bool:
    """缓冲偏大：应更积极做正文越章切分，但仍视为可能的正常长章。"""
    return (
        int(n_lines or 0) >= RUNAWAY_CHAPTER_LINES
        or int(page_num or 0) >= RUNAWAY_CHAPTER_PAGES
    )


def is_hard_runaway_chapter(n_lines: int, page_num: int) -> bool:
    """缓冲极大：仅用于停滞时丢弃脏数据，避免污染续传锚点。"""
    return (
        int(n_lines or 0) >= HARD_RUNAWAY_CHAPTER_LINES
        or int(page_num or 0) >= HARD_RUNAWAY_CHAPTER_PAGES
    )


# 本章内长行去重阈值：短行（标点/诗题）允许重复，长行重复多半是翻页空转
CHAPTER_LINE_DEDUPE_MIN_LEN = 12
# 同一章内重复命中已抓页指纹的次数，达到后视为翻页空转
MAX_PAGE_CYCLE_HITS = 6
# 普通章翻页无新内容达到此次数 → 重开会话；末章更短，见 LAST_CHAPTER_STALE_LIMIT
STALE_PAGE_LIMIT = 8
# 目录最后一章：更早收尾，避免书末黑屏空翻页（用户感知「停不下来」）
LAST_CHAPTER_STALE_LIMIT = 3
# 连续抓到 0 字（黑屏/空白页/纯图页）次数：末章达此即结束
LAST_CHAPTER_EMPTY_STREAK = 2
# 近书末纯图页：canvas 无字且无新内容时，少次重试即收尾（避免双页图来回翻）
IMAGE_ONLY_NEAR_END_EMPTY_STREAK = 2
IMAGE_ONLY_NEAR_END_STALE_LIMIT = 3


def chapter_text_line_set(blocks) -> set[str]:
    """从章节缓冲重建已见正文行集合。"""
    out: set[str] = set()
    for b in blocks or []:
        if b.get("type") != "text":
            continue
        t = (b.get("text") or "").strip()
        if t:
            out.add(t)
    return out


def should_skip_chapter_line(text: str, chapter_seen_lines) -> bool:
    """是否因本章已出现过而跳过该行。

    短行可重复（诗词叠句、单字标点等）；达到阈值的长行再去重，
    用来打断「只与上一页去重」导致的跨页循环重灌。
    """
    t = (text or "").strip()
    if not t:
        return True
    if len(t) < CHAPTER_LINE_DEDUPE_MIN_LEN:
        return False
    return t in (chapter_seen_lines or set())


def is_title_like_line(line: str) -> bool:
    """短标题行（词牌、作者小标题、【赏析】等）不应参与软折行粘连。"""
    s = (line or "").strip()
    if not s:
        return False
    if s.startswith("【") and s.endswith("】"):
        return True
    if len(s) <= 10 and not any(ch in SENTENCE_END or ch in "，、,." for ch in s):
        return True
    return False


def should_merge_soft_wrap(prev: str, cur: str) -> bool:
    """是否把 cur 接到 prev 末尾（canvas 软折行）。"""
    if not prev or not cur:
        return False
    if is_title_like_line(prev) or is_title_like_line(cur):
        return False
    if prev[-1] in SENTENCE_END:
        return False
    # 仅当上一行足够长，才更像折行而非独立短行/标题
    return len(prev) >= SOFT_WRAP_MIN_LEN


def is_chapter_start_text(text: str, chapter_title: str) -> bool:
    """判断一行文字是否为下一章起始（目录标题行或标题+词牌粘连）。

    canvas 文本常去掉空格（「沈佺期三首」），目录却带空格（「沈佺期 三首」），
    必须按压缩键匹配，否则会漏切章、日志停在旧章而页面已前进。

    canvas/DOM 偶发插入零宽字符（U+200B 等），例如
    「士与商：\u200b“贱商之子”…」；比较前必须剔除，否则长标题永远无法切章。

    极短标题（≤2 字压缩键，如「云」「雪」「雁」）只允许整行精确/压缩全等，
    禁止前缀命中正文「云破月来花弄影」等，否则会窜到目录后部短章名。
    """
    t = strip_format_chars((text or "").strip())
    title = normalize_catalog_title(chapter_title)
    if not t or not title:
        return False
    if t == title:
        return True
    # 压缩空白后全等
    tk, titlek = compact_title_key(t), compact_title_key(title)
    if tk and titlek and tk == titlek:
        return True
    # 极短标题（云/雪/雁/序/春日 等 ≤2 字）禁止前缀粘连正文
    if not titlek or len(titlek) <= 2:
        return False
    # 前缀：原串或压缩串（标题+词牌/作者粘连）
    if t.startswith(title):
        rest = t[len(title):]
    elif tk.startswith(titlek):
        rest_k = tk[len(titlek):]
        if not rest_k:
            return True
        rest = rest_k
    else:
        return False
    if not rest:
        return True
    if rest[0] in "，、,;；。！？":
        return False
    if _NOT_CHAPTER_START_REST.match(rest):
        return False
    return True


def split_blocks_at_chapter_start(blocks, chapter_title: str):
    """在 blocks 中按 chapter_title 章首切分为 (before, after)。

    after 为空表示未找到章首；before 可能为空（整页已属新章）。

    长标题在 canvas 上常被拆成多行，例如：
      「士与商：“贱商之子”」+「李白与唐代政经制度…」
    单行匹配会漏切；因此还会拼接后续若干文本块再判定章首。
    """
    title = normalize_catalog_title(chapter_title)
    if not title or not blocks:
        return list(blocks or []), []
    n = len(blocks)
    titlek = compact_title_key(title)
    max_join = 8 if titlek and len(titlek) >= 8 else 4
    for i in range(n):
        if blocks[i].get("type") != "text":
            continue
        joined = ""
        for j in range(i, min(n, i + max_join)):
            b = blocks[j]
            if b.get("type") != "text":
                if joined:
                    break
                continue
            piece = strip_format_chars((b.get("text") or "").strip())
            if not piece:
                continue
            joined = piece if not joined else (joined + piece)
            if is_chapter_start_text(joined, title):
                return blocks[:i], blocks[i:]
            jk = compact_title_key(joined)
            # 已比标题长仍不是章首前缀，停止向后拼
            if jk and titlek and len(jk) > len(titlek) + 24:
                if not jk.startswith(titlek):
                    break
    return list(blocks), []


# 目录项 textContent 偶发粘上阅读进度，如「王国维当前读到 99%」
_CATALOG_PROGRESS_RE = re.compile(
    r"(当前读到|已读到|读到)\s*\d+\s*%?\s*$"
)
_CATALOG_PERCENT_RE = re.compile(r"\s*\d+\s*%\s*$")
# canvas/DOM 偶发插入的格式字符；不剔除会导致长章名切章/校验失败
_FORMAT_CHARS_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u2060]")


def strip_format_chars(text: str) -> str:
    """去掉零宽空格等格式字符。"""
    if not text:
        return ""
    return _FORMAT_CHARS_RE.sub("", text)


def normalize_catalog_title(text: str) -> str:
    """清洗目录/顶栏章名：去进度文案、零宽字符与首尾空白。"""
    s = strip_format_chars((text or "").strip())
    if not s:
        return ""
    s = _CATALOG_PROGRESS_RE.sub("", s).strip()
    s = _CATALOG_PERCENT_RE.sub("", s).strip()
    s = strip_format_chars(s).strip()
    # 仅剩 # 之类无意义标记时视为空
    if s in {"#", "·", "-", "—"}:
        return ""
    return s


def compact_title_key(text: str) -> str:
    """用于标题比对的压缩键：去空白/零宽字符，降低「沈佺期 三首」vs「沈佺期三首」漏切。"""
    s = normalize_catalog_title(text)
    if not s:
        return ""
    return re.sub(r"\s+", "", s)


def clean_catalog_titles(titles) -> list[str]:
    """清洗目录列表：去进度污染、去空、保序去重。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in titles or []:
        title = normalize_catalog_title(str(raw or ""))
        if not title or title in seen:
            continue
        seen.add(title)
        out.append(title)
    return out


def catalog_index(catalog_titles, title: str):
    """在目录中定位章名；支持清洗后的模糊相等。找不到返回 None。"""
    if not catalog_titles:
        return None
    raw = (title or "").strip()
    norm = normalize_catalog_title(raw)
    if not raw and not norm:
        return None
    # 精确
    try:
        return list(catalog_titles).index(raw)
    except ValueError:
        pass
    if norm:
        try:
            return list(catalog_titles).index(norm)
        except ValueError:
            pass
        nk = compact_title_key(norm)
        # 先只做压缩全等，避免短名「春日」误命中「春日京中有怀」
        for i, c in enumerate(catalog_titles):
            cn = normalize_catalog_title(c)
            if not cn:
                continue
            if cn == norm:
                return i
            if nk and compact_title_key(cn) == nk:
                return i
        # 顶栏偶发更短/更长：仅当较短方≥4 且是较长方前缀/包含时放宽
        if nk and len(nk) >= 4:
            for i, c in enumerate(catalog_titles):
                cn = normalize_catalog_title(c)
                if not cn:
                    continue
                ck = compact_title_key(cn)
                if not ck or len(ck) < 4:
                    continue
                shorter, longer = (nk, ck) if len(nk) <= len(ck) else (ck, nk)
                if longer.startswith(shorter) and abs(len(nk) - len(ck)) <= 8:
                    return i
                if shorter in longer and abs(len(nk) - len(ck)) <= 4 and len(shorter) >= 6:
                    return i
        # 顶栏常见「书名 + 空格 + 章名」整串：取最长命中的目录项
        if nk and len(nk) >= 4:
            best_i, best_len = None, 0
            for i, c in enumerate(catalog_titles):
                cn = normalize_catalog_title(c)
                if not cn:
                    continue
                ck = compact_title_key(cn)
                if not ck or len(ck) < 4:
                    continue
                if nk == ck or nk.endswith(ck) or (len(ck) >= 6 and ck in nk):
                    if len(ck) > best_len:
                        best_i, best_len = i, len(ck)
            if best_i is not None:
                return best_i
    return None


def next_catalog_title(catalog_titles, current_title: str):
    """返回目录中 current_title 的下一章标题；找不到则 None。"""
    if not catalog_titles:
        return None
    idx = catalog_index(catalog_titles, current_title)
    if idx is None:
        return None
    if idx + 1 >= len(catalog_titles):
        return None
    return catalog_titles[idx + 1]


def is_last_catalog_chapter(current_title: str, catalog_titles) -> bool:
    """当前章是否为目录最后一项。"""
    if not catalog_titles:
        return False
    idx = catalog_index(catalog_titles, current_title)
    return idx is not None and idx == len(catalog_titles) - 1


# 文末性质标题：书末附录/后记等；卡住时按全书完成收尾，避免无限重开
_END_MATTER_TITLE_RE = re.compile(
    r"^(附录|后记|补记|跋|编后记|再版后记|译后记|修订后记|结语|尾声|"
    r"致谢|鸣谢|参考文献|参考书目|参考资料|索引|出版后记)"
)

# 目录里常见的无正文装饰项：通常不可点跳/无抓取价值，仅剩它们时视为全书完成
# 「文前/文后/文前1/文后2」等为微信读书常见书衣装饰项
_NON_CONTENT_CATALOG_TITLE_RE = re.compile(
    r"^(封底|封面|扉页|版权页|版权信息|书名页|出版信息|版本说明|"
    r"空白页|勒口|腰封|插图|彩插|图版|图录|广告页|"
    r"前折页|后折页|前环衬|后环衬|环衬|衬页|"
    r"文前\d*|文后\d*)"
    r"([：:\s].*)?$"
)


def is_end_matter_title(title: str) -> bool:
    """是否为文末性质章名（附录/后记/跋/致谢等）。"""
    t = normalize_catalog_title(title)
    if not t:
        return False
    if _END_MATTER_TITLE_RE.match(t):
        return True
    # 「附录一」「附录 A」「附录：xxx」等
    return t.startswith("附录")


def is_non_content_catalog_title(title: str) -> bool:
    """是否为封面/封底/版权页等无正文目录项。"""
    t = normalize_catalog_title(title)
    if not t:
        return False
    return bool(_NON_CONTENT_CATALOG_TITLE_RE.match(t))


# 近书末：阅读进度很高且连续失败时按完成收尾，避免目录/空章死循环
NEAR_END_PROGRESS_PERCENT = 99
NEAR_END_FAIL_LIMIT = 3

_PROGRESS_PERCENT_RE = re.compile(
    r"(?:当前读到|已读到|读到)\s*(\d{1,3})\s*%"
)
_BARE_PERCENT_RE = re.compile(r"(?<!\d)(\d{1,3})\s*%")


def parse_reader_progress_percent(text: str):
    """从文本解析全书阅读进度百分比；无效返回 None。"""
    s = str(text or "")
    if not s:
        return None
    m = _PROGRESS_PERCENT_RE.search(s)
    if not m:
        m = _BARE_PERCENT_RE.search(s)
    if not m:
        return None
    try:
        pct = int(m.group(1))
    except (TypeError, ValueError):
        return None
    if 0 <= pct <= 100:
        return pct
    return None


def catalog_titles_after(catalog_titles, current_title) -> list[str]:
    """返回 current_title 之后的目录项；找不到当前章时返回 []。"""
    if not catalog_titles:
        return []
    idx = catalog_index(catalog_titles, current_title)
    if idx is None:
        return []
    return list(catalog_titles[idx + 1 :])


def is_export_complete_after(last_title: str, catalog_titles) -> bool:
    """已导出 last_title 后，是否没有更多需要抓取的目录章。

    目录末项算完成；其后仅剩封底/封面/版权页等无正文项也算完成。
    """
    if not last_title or not catalog_titles:
        return False
    if is_last_catalog_chapter(last_title, catalog_titles):
        return True
    rest = catalog_titles_after(catalog_titles, last_title)
    if not rest:
        return False
    return all(is_non_content_catalog_title(t) for t in rest)


def is_export_terminal_chapter(current_title: str, catalog_titles) -> bool:
    """卡住时应按书末收尾的章：目录末项，或仅剩文末附录/后记链/无正文装饰项。

    例：目录 … → 附录… → 后记；在附录上翻页停滞时，不应反复重开丢弃已抓正文。
    例：正文末章后仅剩「封底」；续传/停滞时不要再强跳封底。
    """
    if is_last_catalog_chapter(current_title, catalog_titles):
        return True
    if not catalog_titles:
        return False
    idx = catalog_index(catalog_titles, current_title)
    if idx is None:
        return False
    rest = catalog_titles[idx + 1 :]
    # 仅剩封底/封面等：当前正文末章按书末收尾
    if rest and all(is_non_content_catalog_title(t) for t in rest):
        return True
    if not is_end_matter_title(current_title):
        return False
    return all(is_end_matter_title(t) or is_non_content_catalog_title(t) for t in rest)


def resolve_stale_advance_target(current_title: str, catalog_titles):
    """翻页停滞时若应前进到下一章，返回目录中的下一章标题。

    文末章返回 None（调用方按全书结束处理）。找不到当前章或没有下一章时也返回 None。
    """
    if not catalog_titles or not normalize_catalog_title(current_title or ""):
        return None
    if is_export_terminal_chapter(current_title, catalog_titles):
        return None
    return next_catalog_title(catalog_titles, current_title)


def resolve_chapter_title(raw_title: str, catalog_titles) -> str:
    """把顶栏/目录原始文本规范到目录章名；无法对齐则返回清洗后的原文。"""
    cleaned = normalize_catalog_title(raw_title)
    if not cleaned:
        return ""
    if not catalog_titles:
        return cleaned
    idx = catalog_index(catalog_titles, cleaned)
    if idx is not None:
        return catalog_titles[idx]
    return cleaned


def should_follow_header_title(catalog_titles, current_title, header_title) -> bool:
    """是否应根据顶栏章名切换 current_chapter。

    内容切章可能已超前于顶栏：诗词选集里顶栏常停在「王维 二十七首」这类
    作者/卷小节名，而正文已按目录切到「渭川田家」等子篇。若此时盲从顶栏，
    会把目录进度回退并反复落盘同一批章节。

    反过来，顶栏也可能一次跳过多章（翻页空抓时阅读器其实已连翻多页）。
    若盲从跨章顶栏，会把中间章整段丢掉（临洞庭湖 → 直接王维）。

    规则：
    - 顶栏空/与当前相同：不切换
    - 无目录：允许切换（退化行为）
    - 顶栏无法对齐目录：不切换
    - 当前无法对齐目录：允许切换
    - 有目录时：仅当顶栏是「目录中的下一章」才切换（只前进一格）
    """
    header = resolve_chapter_title(header_title, catalog_titles)
    current = resolve_chapter_title(current_title, catalog_titles)
    if not header or header == current:
        return False
    if not catalog_titles:
        return True
    h_idx = catalog_index(catalog_titles, header)
    if h_idx is None:
        return False
    c_idx = catalog_index(catalog_titles, current)
    if c_idx is None:
        return True
    # 只跟随紧邻下一章，避免顶栏跨章把中间目录项整段跳过
    return h_idx == c_idx + 1


def reader_needs_chapter_sync(catalog_titles, target_title, reader_title) -> bool:
    """内容切章后，阅读器是否仍未落到目标章。"""
    target = resolve_chapter_title(target_title, catalog_titles)
    if not target:
        return False
    reader = resolve_chapter_title(reader_title, catalog_titles)
    return compact_title_key(reader) != compact_title_key(target)


def chapter_blocks_fingerprint(title: str, blocks) -> str:
    """章节正文指纹：用于检测死循环重复落盘。"""
    body, _imgs = render_chapter_md(title, blocks or [], 0)
    payload = (normalize_catalog_title(title) + "\n" + body).encode("utf-8")
    return hashlib.md5(payload).hexdigest()


def page_blocks_fingerprint(blocks) -> str:
    """单页正文指纹（忽略图片 URL），用于判断翻页是否真的产生新内容。"""
    parts = []
    for b in blocks or []:
        if b.get("type") == "text":
            t = (b.get("text") or "").strip()
            if t:
                parts.append(t)
        elif b.get("type") == "img":
            parts.append("[img]")
    return hashlib.md5("\n".join(parts).encode("utf-8")).hexdigest() if parts else ""


def display_chapter_title(title: str, ch_idx: int) -> str:
    """落盘用章名；空标题回退为编号，避免 md 首行变成「# 」。"""
    t = normalize_catalog_title(title)
    if t:
        return t
    return f"{int(ch_idx):04d}"


def catalog_index_delta(catalog_titles, current_title, other_title):
    """other 相对 current 的目录序号差（other_idx - current_idx）。

    任一无法在目录定位时返回 None。
    """
    if not catalog_titles:
        return None
    c_idx = catalog_index(catalog_titles, current_title)
    o_idx = catalog_index(catalog_titles, other_title)
    if c_idx is None or o_idx is None:
        return None
    return o_idx - c_idx


def find_future_catalog_hit(
    blocks,
    catalog_titles,
    current_title: str,
    *,
    min_ahead: int = 1,
    max_ahead: int = 40,
):
    """在正文中查找 current 之后第 min_ahead..max_ahead 个目录章首。

    返回 (title, before, after)；找不到则 None。
    多个命中时取正文中最早出现者；同位置取目录更靠前的。
    """
    if not blocks or not catalog_titles:
        return None
    c_idx = catalog_index(catalog_titles, current_title)
    if c_idx is None:
        return None
    start = c_idx + max(1, int(min_ahead))
    end = min(len(catalog_titles), c_idx + 1 + max(0, int(max_ahead)))
    if start >= end:
        return None
    best = None
    best_pos = None
    for title in catalog_titles[start:end]:
        before, after = split_blocks_at_chapter_start(blocks, title)
        if not after:
            continue
        pos = len(blocks) - len(after)
        if best is None or pos < best_pos:
            best = (title, before, after)
            best_pos = pos
    return best


def trim_blocks_before_future_catalog(
    blocks,
    catalog_titles,
    current_title: str,
    *,
    min_ahead: int = 1,
    max_ahead: int = 40,
):
    """强制跳转前，丢掉已窜入的后续章正文；返回 (trimmed, hit_title)。"""
    hit = find_future_catalog_hit(
        blocks,
        catalog_titles,
        current_title,
        min_ahead=min_ahead,
        max_ahead=max_ahead,
    )
    if not hit:
        return list(blocks or []), ""
    title, before, _after = hit
    return list(before), title


def skipped_next_chapter_evidence(
    blocks, catalog_titles, current_title: str, *, max_ahead: int = 40
) -> str:
    """紧邻下一章章首未出现，但更后面的目录章首已出现在正文中时，返回后者。

    典型场景：短诗「春晓」被 canvas 粘连/翻页越过，正文已到「王维 二十七首」
    而逻辑仍停在「舟中晓望」。内容切章只认下一章会永远漏切。
    """
    if not blocks or not catalog_titles:
        return ""
    nxt = next_catalog_title(catalog_titles, current_title)
    if not nxt:
        return ""
    _before, after_next = split_blocks_at_chapter_start(blocks, nxt)
    if after_next:
        return ""
    hit = find_future_catalog_hit(
        blocks,
        catalog_titles,
        current_title,
        min_ahead=2,
        max_ahead=max_ahead,
    )
    if not hit:
        return ""
    return hit[0] or ""


def content_overrun_split(blocks, catalog_titles, current_title: str, *, max_ahead: int = 40):
    """线性翻页策略下的正文越章恢复。

    当紧邻下一章标题未出现，但更后面的目录章首已出现在正文中时，
    在最早出现的后续章首处切开。返回 (hit_title, before, after)；否则 None。

    注意：这会跳过中间未出现在正文中的目录项（通常是极短诗被 canvas 粘连/翻页越过）。
    中途绝不为此去点目录；漏章靠重开续传或接受短缺失，避免目录乱跳污染整卷。
    """
    if not blocks or not catalog_titles:
        return None
    nxt = next_catalog_title(catalog_titles, current_title)
    if nxt:
        _b, after_next = split_blocks_at_chapter_start(blocks, nxt)
        if after_next:
            # 下一章已在正文：走常规 find_chapter_split
            return None
    return find_future_catalog_hit(
        blocks,
        catalog_titles,
        current_title,
        min_ahead=2 if nxt else 1,
        max_ahead=max_ahead,
    )


def find_chapter_split(blocks, catalog_titles, current_title: str):
    """在正文块中查找应切到的下一章。

    返回 (next_title, before, after)；找不到则 None。

    - 当前章能在目录定位时：只匹配「下一章」标题（避免跳章吞掉中间目录项）。
      章名比对已忽略空格差异（canvas「沈佺期三首」vs 目录「沈佺期 三首」）。
    - 当前章未知（顶栏为空）时：按目录顺序找第一个作为章首出现的标题。
    """
    if not blocks or not catalog_titles:
        return None
    cur = normalize_catalog_title(current_title)
    if cur:
        nxt = next_catalog_title(catalog_titles, cur)
        if not nxt:
            return None
        before, after = split_blocks_at_chapter_start(blocks, nxt)
        if not after:
            return None
        return nxt, before, after

    # 顶栏空：按目录顺序找第一个章首
    for title in catalog_titles:
        before, after = split_blocks_at_chapter_start(blocks, title)
        if after:
            return title, before, after
    return None


def infer_title_for_blocks_before(next_title: str, catalog_titles, current_title: str) -> str:
    """内容切章时，before 段应归属的章名。"""
    cur = resolve_chapter_title(current_title, catalog_titles)
    if cur:
        return cur
    idx = catalog_index(catalog_titles, next_title)
    if idx is not None and idx > 0:
        return catalog_titles[idx - 1]
    if catalog_titles:
        return catalog_titles[0]
    return ""


def dedupe_chars_by_position(chars):
    """同一 canvas 坐标只保留最后一次 fillText。

    force_repaint / 双缓冲重绘时，新旧两帧会叠进 __wr_chars；
    若 clearRect 钩子未触发，按 (page_key,x,y) 去重可去掉交错叠字。

    page_key 优先用 canvas 稳定 id(cid)；无 cid 时回退屏幕 left(cl)。
    双页左右 canvas 的局部 x/y 常重叠，若只用 (0,x,y) 会把左页字误删。
    """
    if not chars:
        return []
    last = {}
    order = []
    for i, c in enumerate(chars):
        try:
            cid = int(c.get("cid") or 0)
        except (TypeError, ValueError):
            cid = 0
        try:
            cl = round(float(c.get("cl") or 0))
        except (TypeError, ValueError):
            cl = 0
        try:
            x = round(float(c.get("x") or 0), 1)
            y = round(float(c.get("y") or 0), 1)
        except (TypeError, ValueError):
            x, y = 0.0, 0.0
        # cid>0 已能区分页；否则用 cl 区分双页，避免左右同局部坐标互删
        page_key = ("cid", cid) if cid > 0 else ("cl", cl)
        t = c.get("t") or ""
        # 多字测量串与单字分开键，避免误伤
        if len(t) == 1:
            key = (page_key, x, y, 1, "")
        else:
            key = (page_key, x, y, 0, t)
        if key not in last:
            order.append(key)
        last[key] = c
    return [last[k] for k in order]


def group_chars_by_canvas(chars):
    """按 canvas 把字符分到各页，从左到右返回。

    fillText 的 x/y 是 canvas 局部坐标；双页时左右页 y 区间重叠，
    若不按 canvas 拆开再分行，会把左右页同一 y 的字交错拼成乱码。

    优先用 hook 写入的 canvas 稳定 id(cid)；否则回退屏幕 left(cl)。
    cid 能避免两页 cl 都读成 0/相近时被合成一桶。
    """
    if not chars:
        return []

    # --- 优先 cid ---
    by_cid: dict[int, list] = {}
    no_cid = []
    for c in chars:
        cid = c.get("cid")
        try:
            cid_v = int(cid) if cid is not None else 0
        except (TypeError, ValueError):
            cid_v = 0
        if cid_v > 0:
            by_cid.setdefault(cid_v, []).append(c)
        else:
            no_cid.append(c)

    if len(by_cid) >= 2:
        def cid_left(cid_chars):
            vals = []
            for ch in cid_chars:
                try:
                    vals.append(float(ch.get("cl") or 0))
                except (TypeError, ValueError):
                    pass
            return min(vals) if vals else 0.0

        ordered = sorted(by_cid.items(), key=lambda kv: (cid_left(kv[1]), kv[0]))
        pages = [lst for _, lst in ordered]
        if no_cid:
            pages[0].extend(no_cid)
        return pages

    # --- 回退 cl 聚类 ---
    buckets: dict[int | None, list] = {}
    for c in chars:
        cl = c.get("cl")
        if cl is None:
            buckets.setdefault(None, []).append(c)
            continue
        try:
            cl_v = float(cl)
        except (TypeError, ValueError):
            buckets.setdefault(None, []).append(c)
            continue
        key = None
        for k in buckets:
            if k is not None and abs(k - cl_v) <= 40:
                key = k
                break
        if key is None:
            key = int(round(cl_v))
        buckets.setdefault(key, []).append(c)

    ordered_keys = sorted(k for k in buckets if k is not None)
    pages = [buckets[k] for k in ordered_keys]
    if None in buckets:
        if pages:
            pages[0].extend(buckets[None])
        else:
            pages = [buckets[None]]
    return pages


def split_spread(chars):
    """双页拆分：优先按 canvas 屏幕位置；否则回退 y 回跳启发式。

    返回 [左页chars, 右页chars, ...] 或 [单页chars]
    """
    if not chars:
        return [chars]

    pages = group_chars_by_canvas(chars)
    if len(pages) >= 2:
        return pages

    # 兼容未带 cl 的旧捕获：按绘制顺序的 y 回跳切分
    if len(chars) < 20:
        return [chars]
    singles = [(i, c) for i, c in enumerate(chars) if len(c["t"]) == 1]
    if len(singles) < 10:
        return [chars]
    for j in range(1, len(singles)):
        if singles[j - 1][1]["y"] > 400 and singles[j][1]["y"] < 200:
            return [chars[:singles[j][0]], chars[singles[j][0]:]]
    return [chars]


def chars_to_lines(chars):
    """把单页字符按 y 分行，返回 [{y, text}]（未合并段落）"""
    real = [c for c in chars if len(c["t"]) == 1 or not MEASURE_RE.match(c["t"])]
    if not real:
        return []
    rows = {}
    for c in real:
        y_key = round(c["y"] / 3) * 3
        rows.setdefault(y_key, []).append(c)
    lines = []
    for yk in sorted(rows):
        line = "".join(c["t"] for c in sorted(rows[yk], key=lambda c: c["x"]))
        if line.strip():
            lines.append({"y": yk, "text": line.strip()})
    return lines


def build_page_blocks(chars, images, canvas_rects, seen_imgs):
    """把一次渲染(可能双页)拆成有序块列表: [{type:'text'/'img', ...}]
       文字行和图片按屏幕 y 交错；左页整页在前，右页在后。"""
    blocks = []
    pages = split_spread(dedupe_chars_by_position(chars))

    # 判定左右 canvas
    rects = sorted(canvas_rects, key=lambda r: r["left"])
    left_rect = rects[0] if rects else {"top": 0, "left": 0}
    right_rect = rects[1] if len(rects) > 1 else left_rect
    mid_x = (left_rect["left"] + right_rect["left"]) / 2 + 180 if len(rects) > 1 else 99999

    # 图片按左右分组
    left_imgs = [im for im in images if im["left"] < mid_x]
    right_imgs = [im for im in images if im["left"] >= mid_x]

    def emit_page(page_chars, page_rect, page_imgs):
        lines = chars_to_lines(page_chars)
        items = []
        for ln in lines:
            items.append(("text", page_rect["top"] + ln["y"], ln["text"]))
        for im in page_imgs:
            if im["src"] in seen_imgs:
                continue
            items.append(("img", im["top"], im))
        items.sort(key=lambda t: t[1])
        for typ, _y, payload in items:
            if typ == "text":
                blocks.append({"type": "text", "text": payload})
            else:
                seen_imgs.add(payload["src"])
                blocks.append({"type": "img", "src": payload["src"],
                                "w": payload["w"], "h": payload["h"]})

    if len(pages) >= 2:
        # 多 canvas：左→右逐页输出；首屏图归左，其余可见图归右/末页
        page_rects = list(rects) if len(rects) >= len(pages) else [left_rect, right_rect]
        while len(page_rects) < len(pages):
            page_rects.append(page_rects[-1])
        for i, page_chars in enumerate(pages):
            if i == 0:
                pimgs = left_imgs
            elif i == len(pages) - 1:
                pimgs = right_imgs
            else:
                pimgs = []
            emit_page(page_chars, page_rects[i], pimgs)
    else:
        # 单页：图片全归这页，仍按 y 排
        emit_page(pages[0] if pages else [], left_rect, left_imgs + right_imgs)
    return blocks


def img_filename(url, ch_idx, seq):
    ext = "jpg"
    m = re.search(r'\.(jpg|jpeg|png|gif|webp)', url.lower())
    if m:
        ext = m.group(1).replace("jpeg", "jpg")
    return f"ch{ch_idx:04d}_img{seq:02d}.{ext}"


def render_chapter_md(ch_title, blocks, ch_idx):
    """把有序块渲染成 Markdown：文字行合并成段落，图片就地插入"""
    out = [f"# {display_chapter_title(ch_title, ch_idx)}\n"]
    para = []
    img_records = []
    img_seq = 0

    def flush_para():
        nonlocal para
        if not para:
            return
        # 合并 canvas 软折行；词牌/短标题/【赏析】等保持独立段落。
        # 注意：不得因 line==ch_title 丢弃正文行——词下作者署名常与章名相同（如「李白」）。
        merged = []
        for line in para:
            if merged and should_merge_soft_wrap(merged[-1], line):
                merged[-1] += line
            else:
                merged.append(line)
        for m in merged:
            if m.strip():
                out.append(m.strip())
        para = []

    for b in blocks:
        if b["type"] == "text":
            para.append(b["text"])
        else:
            flush_para()
            img_seq += 1
            fname = img_filename(b["src"], ch_idx, img_seq)
            out.append(f"![图](images/{fname})")
            img_records.append({"url": b["src"], "file": fname})
    flush_para()

    body = "\n\n".join(out) + "\n"
    return body, img_records


async def wait_stable(page, prev_count=0, timeout=8):
    """等页面渲染稳定，返回稳定后的字符数。

    注意：__wr_reset 后计数从 0 开始。若把「连续两次 0」也当成稳定，
    会在 canvas 尚未 fillText 时过早返回，导致抓到空页、翻页看起来像没动。
    因此仅在 count>0 且连续两次相同后才视为稳定。
    """
    last = -1
    poll = SLEEP_READER_STABLE_POLL
    steps = max(1, int(timeout / poll)) if poll > 0 else 1
    for _ in range(steps):
        c = await page.evaluate("() => window.__wr_count()")
        if c > 0 and c == last:
            return c
        last = c
        await asyncio.sleep(poll)
    return last if last is not None and last >= 0 else 0


def get_last_chapter_title(md_dir):
    if not os.path.exists(md_dir):
        return None, 0
    files = sorted(f for f in os.listdir(md_dir) if f.endswith(".md"))
    if not files:
        return None, 0
    idx = int(files[-1].replace(".md", ""))
    # 优先 raw json 的 title（避免空标题 md 首行变成「#」）
    raw_path = os.path.join(os.path.dirname(md_dir), "raw", files[-1].replace(".md", ".json"))
    title = ""
    if os.path.isfile(raw_path):
        try:
            with open(raw_path, encoding="utf-8") as rf:
                meta = json.load(rf)
            title = normalize_catalog_title(str(meta.get("title") or ""))
        except Exception:
            title = ""
    if not title:
        with open(os.path.join(md_dir, files[-1]), encoding="utf-8") as f:
            line = f.readline().strip()
        if line.startswith("#"):
            title = normalize_catalog_title(line.lstrip("#").strip())
        else:
            title = normalize_catalog_title(line)
    return title or None, idx


def load_catalog_titles(catalog_path):
    """读取目录标题列表；失败返回 []。读取时清洗进度污染。"""
    try:
        with open(catalog_path, encoding="utf-8") as f:
            titles = json.load(f)
        return clean_catalog_titles(titles if isinstance(titles, list) else [])
    except Exception:
        return []


def load_last_catalog_title(catalog_path):
    titles = load_catalog_titles(catalog_path)
    return titles[-1] if titles else ""


async def _title(page):
    """读取阅读器当前章名。

    优先顶栏/页信息；最近有头模式改用真实窗口后，单一 class 偶发读空，
    因此多选择器兜底。仍可能为空——调用方需再用目录项兜底。

    注意：关闭态 DOM 里常残留「选中」目录项 class。若盲信该节点，
    目录点击后会把目标章名当成当前章，造成「跳转成功、正文仍在旧章」。
    仅当选中项自身可见且可点时才作为兜底。
    """
    return await page.evaluate(
        """() => {
            const visible = (el, minW=8, minH=8) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                if (parseFloat(st.opacity || '1') < 0.05) return false;
                if (st.pointerEvents === 'none') return false;
                const r = el.getBoundingClientRect();
                return r.width >= minW && r.height >= minH
                    && r.bottom > 0 && r.top < (window.innerHeight || 0)
                    && r.right > 0 && r.left < (window.innerWidth || 0);
            };
            const sels = [
                '.renderTargetPageInfo_header_chapterTitle',
                '.readerTopBar_title_chapter',
                '.readerTopBar_title',
                '[class*="header_chapterTitle"]',
                '[class*="chapterTitle"]',
            ];
            for (const s of sels) {
                const el = document.querySelector(s);
                const t = (el?.textContent || '').trim();
                if (t && visible(el, 4, 4)) return t;
                // 顶栏节点偶发宽高为 0 但仍有文本，仍可采用
                if (t && el) return t;
            }
            // 仅当目录选中项真正可见时才采用（避免关闭态残留 selected）
            const active = document.querySelector(
                '.readerCatalog_list_item_selected, .readerCatalog_list_item.selected, .readerCatalog_list_item.isActive, [class*="readerCatalog_list_item"][class*="selected"], [class*="readerCatalog_list_item"][class*="active"]'
            );
            if (active && visible(active, 40, 12)) {
                const at = (active.textContent || '').trim();
                if (at) return at;
            }
            return '';
        }"""
    )


async def read_chapter_title(page, catalog_titles=None, *, fallback: str = "") -> str:
    """读取并规范当前章名；顶栏为空时用 fallback / 目录推断。"""
    raw = await _title(page)
    resolved = resolve_chapter_title(raw, catalog_titles or [])
    if resolved:
        return resolved
    fb = resolve_chapter_title(fallback, catalog_titles or [])
    if fb:
        return fb
    return ""


async def read_reader_progress_percent(page):
    """读取阅读器当前全书进度百分比；读不到返回 None。"""
    try:
        raw = await page.evaluate(
            r"""() => {
                const texts = [];
                const push = (s) => {
                    const t = String(s || '').replace(/\s+/g, ' ').trim();
                    if (t) texts.push(t);
                };
                document.querySelectorAll(
                    '.readerCatalog_list_item_selected, .readerCatalog_list_item.selected, '
                    + '.readerCatalog_list_item.isActive, [class*="readerCatalog_list_item"][class*="selected"], '
                    + '[class*="readerCatalog_list_item"][class*="active"]'
                ).forEach(el => push(el.textContent));
                document.querySelectorAll(
                    '[class*="progress"], [class*="Progress"], '
                    + '[class*="readerFooter"], [class*="readerBottom"], '
                    + '[class*="readerControl"], [class*="percent"]'
                ).forEach(el => push(el.textContent));
                for (const t of texts) {
                    const m = t.match(/(?:当前读到|已读到|读到)\s*(\d{1,3})\s*%/);
                    if (m) return parseInt(m[1], 10);
                }
                for (const t of texts) {
                    const m = t.match(/(?<!\d)(\d{1,3})\s*%/);
                    if (m) {
                        const n = parseInt(m[1], 10);
                        if (n >= 0 && n <= 100) return n;
                    }
                }
                const body = (document.body && document.body.innerText) || '';
                const bm = body.match(/(?:当前读到|已读到|读到)\s*(\d{1,3})\s*%/);
                if (bm) return parseInt(bm[1], 10);
                return null;
            }"""
        )
    except Exception:
        return None
    if raw is None:
        return None
    try:
        pct = int(raw)
    except (TypeError, ValueError):
        return None
    if 0 <= pct <= 100:
        return pct
    return None


def reader_chapter_matches(header_title: str, target_title: str, catalog_titles=None) -> bool:
    """顶栏章名是否已落到目标章（允许双页顶栏偶发显示紧邻下一章）。"""
    if not target_title:
        return False
    target = resolve_chapter_title(target_title, catalog_titles or []) or normalize_catalog_title(target_title)
    header = resolve_chapter_title(header_title, catalog_titles or []) or normalize_catalog_title(header_title)
    if not target:
        return False
    if not header:
        return False
    if compact_title_key(header) == compact_title_key(target):
        return True
    # 双页时顶栏可能已是下一章
    delta = catalog_index_delta(catalog_titles or [], target, header)
    return delta is not None and delta == 1


async def verify_reader_on_chapter(page, target_title: str, catalog_titles=None, *, retries: int = 4) -> bool:
    """目录点击后确认阅读器是否落到目标章附近。

    成功条件（满足其一）：
    - 正文出现目标章首/包含目标章名压缩键；
    - 顶栏对齐目标章（或双页下一章）且已抓到非空正文；
    - 目标为封底/文后等无正文装饰项且顶栏已对齐（允许正文为空）。

    禁止「仅顶栏对齐、正文为 0」即成功：滚动模式下目录点击会改顶栏/选中项，
    但 canvas 仍停在旧章或视口外，随后 ArrowRight 会在旧区空转。
    """
    target = resolve_chapter_title(target_title, catalog_titles or []) or normalize_catalog_title(target_title)
    if not target:
        return False
    tkey = compact_title_key(target)
    for i in range(max(1, int(retries))):
        await asyncio.sleep(max(0.2, float(SLEEP_READER_PAGE_RENDER)))
        try:
            chars = await page.evaluate("() => (window.__wr_chars || []).slice(0, 800)")
        except Exception:
            chars = []
        content_hit = False
        has_body = bool(chars)
        if chars:
            sample = "".join(str(c.get("t") or "") for c in chars[:500])
            if tkey and tkey in compact_title_key(sample):
                content_hit = True
            if not content_hit:
                try:
                    rects = await page.evaluate(CANVAS_RECTS_JS)
                except Exception:
                    rects = []
                blocks = build_page_blocks(chars, [], rects, set())
                for b in blocks[:20]:
                    if b.get("type") == "text" and is_chapter_start_text(
                            b.get("text") or "", target):
                        content_hit = True
                        break
        if content_hit:
            return True
        header = await read_chapter_title(page, catalog_titles)
        if reader_chapter_matches(header, target, catalog_titles):
            # 无正文装饰项：顶栏对齐即可（文后/封底常无 canvas 字）
            if is_non_content_catalog_title(target):
                return True
            if has_body:
                return True
        if i + 1 < retries:
            try:
                await force_reader_repaint(page)
                await wait_stable(page, 0, timeout=1.5)
            except Exception:
                pass
    return False


async def fetch_book_title(page):
    info = await page.evaluate("""() => {
        const title = document.querySelector('.readerCatalog_bookInfo_title_txt, .bookInfo_right_header_title')
            ?.textContent?.trim() || document.title.replace(/-.*$/, '').trim();
        const author = document.querySelector('.readerCatalog_bookInfo_author, .bookInfo_author a')
            ?.textContent?.trim() || '';
        return {title, author};
    }""")
    return info.get("title", "未知"), info.get("author", "")


async def dismiss_reader_overlays(page) -> bool:
    """关掉阅读器遮罩/蒙层，避免 wr_mask 拦截目录按钮点击。"""
    removed = await page.evaluate(
        """() => {
            let n = 0;
            document.querySelectorAll(
                '.wr_mask, .wr_mask_Show, [class*="wr_mask"]'
            ).forEach(el => {
                try {
                    el.style.pointerEvents = 'none';
                    el.style.display = 'none';
                    el.remove();
                    n += 1;
                } catch (e) {}
            });
            return n;
        }"""
    )
    return bool(removed)


async def is_reader_catalog_open(page) -> bool:
    """目录/目录搜索层是否真正展开并遮挡阅读区。

    关闭态下微信读书仍会在 DOM 里保留：
    - placeholder=搜索 的 input（宽高常为 0）
    - `.readerCatalog_list_item`（宽高常为 0）
    - `reader_float_search_panel_wrapper`（全屏但 pointer-events:none）
    因此**必须**以「可见且可交互的目录项 / 真搜索态」为准，不能仅凭节点存在判断。
    """
    try:
        return bool(
            await page.evaluate(
                """() => {
                    const visibleBox = (el, minW=20, minH=20) => {
                        if (!el) return false;
                        const st = window.getComputedStyle(el);
                        if (st.display === 'none' || st.visibility === 'hidden') return false;
                        if (parseFloat(st.opacity || '1') < 0.05) return false;
                        if (st.pointerEvents === 'none') return false;
                        const r = el.getBoundingClientRect();
                        if (r.width < minW || r.height < minH) return false;
                        const vw = window.innerWidth, vh = window.innerHeight;
                        if (r.right <= 8 || r.left >= vw - 8) return false;
                        if (r.bottom <= 8 || r.top >= vh - 8) return false;
                        return true;
                    };

                    const visibleCatalogItems = () => Array.from(
                        document.querySelectorAll('.readerCatalog_list_item')
                    ).filter(el => visibleBox(el, 40, 16));

                    // A) 真搜索态：可见「取消」+ 可见可点的搜索输入
                    const cancelBtns = Array.from(document.querySelectorAll('button, a, span, div'))
                        .filter(el => {
                            const t = (el.textContent || '').trim();
                            return t === '取消' && visibleBox(el, 20, 12);
                        });
                    const searchInputs = Array.from(document.querySelectorAll(
                        'input[placeholder*="搜索"], input[type="search"]'
                    )).filter(el => visibleBox(el, 40, 12));
                    if (cancelBtns.length && searchInputs.length) {
                        return true;
                    }

                    // B) 有可见目录项 = 目录真正展开（最可靠）
                    if (visibleCatalogItems().length > 0) {
                        return true;
                    }

                    // C) 蒙层 + 可见目录列表（不要用「DOM 里有搜索 input」当证据）
                    const mask = document.querySelector(
                        '.wr_mask_Show, .wr_mask.wr_mask_Show, .wr_mask[class*="Show"]'
                    );
                    if (mask) {
                        const st = window.getComputedStyle(mask);
                        const r = mask.getBoundingClientRect();
                        if (st.display !== 'none' && parseFloat(st.opacity || '1') > 0.05
                            && st.pointerEvents !== 'none'
                            && r.width > 50 && r.height > 50
                            && r.left > -100) {
                            if (document.querySelector('.readerCatalog_list, .readerCatalog_list_scroll_area')
                                && visibleCatalogItems().length > 0) {
                                return true;
                            }
                        }
                    }

                    // D) 右侧大面板且内部有可见目录项
                    const panels = document.querySelectorAll(
                        '.readerCatalog, .readerCatalog_list_scroll_area'
                    );
                    for (const el of panels) {
                        if (el.closest && el.closest('button')) continue;
                        if (!visibleBox(el, 160, 160)) continue;
                        const r = el.getBoundingClientRect();
                        const hasVisItem = el.querySelector && Array.from(
                            el.querySelectorAll('.readerCatalog_list_item')
                        ).some(it => visibleBox(it, 40, 16));
                        if (!hasVisItem) continue;
                        if (r.width >= 200 && r.height >= 200) {
                            return true;
                        }
                    }
                    return false;
                }"""
            )
        )
    except Exception:
        return False


async def restore_reader_catalog_styles(page) -> None:
    """清除可能被错误写入的目录样式，避免目录按钮永久失灵。"""
    try:
        await page.evaluate(
            """() => {
                document.querySelectorAll(
                    '.readerCatalog, [class*="readerCatalog"]'
                ).forEach(el => {
                    if (!el.style) return;
                    el.style.removeProperty('display');
                    el.style.removeProperty('visibility');
                    el.style.removeProperty('pointer-events');
                });
            }"""
        )
    except Exception:
        pass


async def close_reader_catalog(page):
    """关闭目录侧栏与遮罩。

    目录开着时 ArrowRight 会在目录列表里移动，不会翻阅读页。

    策略：退搜索态 → Esc → 再点目录按钮收起 → 点遮罩/点左侧正文区 → 再 Esc。
    **绝不** display:none 永久隐藏目录组件，也**绝不**点击搜索按钮。
    """
    closed = False
    try:
        if await dismiss_reader_search(page):
            closed = True
        if not await is_reader_catalog_open(page):
            await dismiss_reader_overlays(page)
            await blur_reader_inputs(page)
            return closed

        closed = True

        # 0) 再清一次搜索态
        await dismiss_reader_search(page)
        await asyncio.sleep(0.1)

        # 1) Esc 退出搜索/收起一层
        for _ in range(3):
            if not await is_reader_catalog_open(page):
                break
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.12)

        # 2) 仅点明确的关闭/取消，绝不点 title=搜索 的按钮
        if await is_reader_catalog_open(page):
            await dismiss_reader_search(page)
            await page.evaluate(
                """() => {
                    for (const el of Array.from(document.querySelectorAll('button, a, span, div'))) {
                        if ((el.textContent || '').trim() === '取消') {
                            const r = el.getBoundingClientRect();
                            if (r.width >= 12 && r.height >= 12 && r.top <= 120) {
                                el.click();
                                break;
                            }
                        }
                    }
                    const closeBtns = Array.from(document.querySelectorAll(
                        '.readerCatalog button, [class*="readerCatalog"] button'
                    ));
                    for (const b of closeBtns) {
                        const t = (b.getAttribute('title') || b.getAttribute('aria-label') || b.textContent || '').trim();
                        if (!t) continue;
                        if (/搜索|search|查找/i.test(t)) continue;
                        if (t === '关闭' || t === '取消' || t === '×' || t === 'x' || t === 'X') {
                            b.click();
                            return;
                        }
                    }
                }"""
            )
            await asyncio.sleep(0.15)

        # 3) 多次 toggle 目录按钮（真实鼠标），避免点了章名后面板仍开着
        for _ in range(3):
            if not await is_reader_catalog_open(page):
                break
            clicked = await page.evaluate(
                """() => {
                    const btn = document.querySelector(
                        'button.readerControls_item.catalog, button[title="目录"]'
                    );
                    if (!btn) return null;
                    const t = (btn.getAttribute('title') || btn.getAttribute('aria-label') || '').trim();
                    if (/搜索|search|查找/i.test(t)) return null;
                    const r = btn.getBoundingClientRect();
                    if (r.width < 8 || r.height < 8) {
                        btn.click();
                        return {js: true};
                    }
                    return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                }"""
            )
            if clicked and not clicked.get("js"):
                try:
                    await page.mouse.click(float(clicked["x"]), float(clicked["y"]))
                except Exception:
                    await page.evaluate(
                        """() => {
                            const btn = document.querySelector(
                                'button.readerControls_item.catalog, button[title="目录"]'
                            );
                            if (btn) btn.click();
                        }"""
                    )
            elif not clicked:
                await page.evaluate(
                    """() => {
                        const btn = document.querySelector(
                            'button.readerControls_item.catalog, button[title="目录"]'
                        );
                        if (btn) btn.click();
                    }"""
                )
            await asyncio.sleep(0.22)

        # 4) 点遮罩 / 点阅读区空白收起（多点位，避开右侧工具条）
        if await is_reader_catalog_open(page):
            await page.evaluate(
                """() => {
                    const masks = Array.from(document.querySelectorAll(
                        '.wr_mask, .wr_mask_Show, [class*="wr_mask"]'
                    ));
                    for (const mask of masks) {
                        const st = window.getComputedStyle(mask);
                        const r = mask.getBoundingClientRect();
                        if (st.display === 'none' || parseFloat(st.opacity || '1') < 0.05) continue;
                        if (st.pointerEvents === 'none') continue;
                        if (r.width < 50 || r.height < 50 || r.left < -100) continue;
                        mask.click();
                        return;
                    }
                }"""
            )
            await asyncio.sleep(0.08)
            for x, y in ((160, 320), (220, 480), (360, 240), (500, 400)):
                if not await is_reader_catalog_open(page):
                    break
                try:
                    await page.mouse.click(float(x), float(y))
                except Exception:
                    pass
                await asyncio.sleep(0.1)

        await dismiss_reader_overlays(page)
        await blur_reader_inputs(page)

        # 5) 最后再 Esc 两下
        for _ in range(2):
            if not await is_reader_catalog_open(page):
                break
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.12)
            await dismiss_reader_overlays(page)
            await blur_reader_inputs(page)

        if await is_reader_catalog_open(page):
            print("  ⚠️  目录侧栏仍未关闭（后续翻页可能失效）")
    except Exception:
        try:
            await dismiss_reader_overlays(page)
        except Exception:
            pass
    return closed



async def open_reader_catalog(page) -> bool:
    """打开目录；若已打开则直接成功。处理 wr_mask 拦截。

    成功标准：出现可见的 `.readerCatalog_list_item`（关闭态 DOM 里虽有节点但宽高为 0）。
    """
    await restore_reader_catalog_styles(page)
    if await is_reader_catalog_open(page):
        await blur_reader_inputs(page)
        return True
    await dismiss_reader_overlays(page)
    await dismiss_reader_search(page)

    async def _click_catalog_btn() -> bool:
        box = await page.evaluate(
            """() => {
                const btn = document.querySelector(
                    'button.readerControls_item.catalog, button[title="目录"]'
                );
                if (!btn) return null;
                // 排除 class 里夹带 search 的误匹配
                const t = (btn.getAttribute('title') || btn.getAttribute('aria-label') || '').trim();
                if (/搜索|search|查找/i.test(t)) return null;
                if (!/catalog/i.test(btn.className || '') && t !== '目录') return null;
                const r = btn.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) {
                    // 仍尝试 JS click（有时布局测量为 0 但仍可点）
                    btn.click();
                    return {js: true};
                }
                return {x: r.x + r.width / 2, y: r.y + r.height / 2};
            }"""
        )
        if not box:
            return False
        if box.get("js"):
            return True
        try:
            await page.mouse.click(float(box["x"]), float(box["y"]))
            return True
        except Exception:
            try:
                await page.click(
                    'button.readerControls_item.catalog, button[title="目录"]',
                    timeout=5000,
                    force=True,
                )
                return True
            except Exception:
                return False

    if not await _click_catalog_btn():
        try:
            await page.click(
                'button.readerControls_item.catalog, button[title="目录"]',
                timeout=5000,
                force=True,
            )
        except Exception:
            await dismiss_reader_overlays(page)
            try:
                await page.click(
                    'button.readerControls_item.catalog, button[title="目录"]',
                    timeout=5000,
                    force=True,
                )
            except Exception:
                return False
    await asyncio.sleep(SLEEP_READER_CATALOG_OPEN)
    if await is_reader_catalog_open(page):
        await blur_reader_inputs(page)
        return True

    # 再试一次 Esc 清场后点击
    await page.keyboard.press("Escape")
    await dismiss_reader_overlays(page)
    await restore_reader_catalog_styles(page)
    await asyncio.sleep(0.2)
    if not await _click_catalog_btn():
        try:
            await page.click(
                'button.readerControls_item.catalog, button[title="目录"]',
                timeout=5000,
                force=True,
            )
        except Exception:
            return False
    await asyncio.sleep(SLEEP_READER_CATALOG_OPEN)
    opened = await is_reader_catalog_open(page)
    if opened:
        # 打开目录后微信读书常把焦点放进搜索框——立刻失焦
        await blur_reader_inputs(page)
    return opened



async def scrape_catalog_titles(page) -> list[str]:
    """打开目录后尽量滚完整表，抓取并清洗全部章名。"""
    raw_titles: list[str] = []
    seen: set[str] = set()

    async def harvest():
        batch = await page.evaluate(
            """() => Array.from(document.querySelectorAll('.readerCatalog_list_item')).map(el => {
                const titleEl = el.querySelector(
                    '[class*="title"], .readerCatalog_list_item_title, .chapterItem_title'
                );
                return ((titleEl && titleEl.textContent) || el.textContent || '').trim();
            })"""
        )
        for t in batch or []:
            nt = normalize_catalog_title(t)
            if nt and nt not in seen:
                seen.add(nt)
                raw_titles.append(nt)

    await harvest()
    # 虚拟列表：向下滚几次尽量收全
    for _ in range(40):
        moved = await page.evaluate(
            """() => {
                const sc = document.querySelector(
                    '.readerCatalog_list_scroll_area, [class*="readerCatalog_list_scroll"]'
                );
                if (!sc) return false;
                const before = sc.scrollTop;
                const max = Math.max(0, sc.scrollHeight - sc.clientHeight);
                if (before >= max - 1) return false;
                sc.scrollTop = Math.min(max, before + Math.max(sc.clientHeight * 0.9, 120));
                return sc.scrollTop > before;
            }"""
        )
        if not moved:
            break
        await asyncio.sleep(max(0.05, float(SLEEP_READER_CATALOG_SCROLL) * 0.15))
        await harvest()

    # 滚回顶部，便于点首项
    await page.evaluate(
        """() => {
            const sc = document.querySelector(
                '.readerCatalog_list_scroll_area, [class*="readerCatalog_list_scroll"]'
            );
            if (sc) sc.scrollTop = 0;
        }"""
    )
    await asyncio.sleep(SLEEP_READER_CATALOG_SCROLL)
    return clean_catalog_titles(raw_titles)


async def goto_first_chapter(page, catalog_path=None):
    """打开目录、保存章名列表、点击第一项；返回清洗后的首章标题。"""
    first_title = ""
    try:
        if not await open_reader_catalog(page):
            raise RuntimeError("无法打开目录")
        titles = await scrape_catalog_titles(page)
        if titles and catalog_path:
            with open(catalog_path, "w", encoding="utf-8") as f:
                json.dump(titles, f, ensure_ascii=False)
        # 目录滚回顶部后，用真实鼠标点首项（Vue 对 element.click() 常不跳转）
        await page.evaluate(
            """() => {
                const sc = document.querySelector(
                    '.readerCatalog_list_scroll_area, [class*="readerCatalog_list_scroll"]'
                );
                if (sc) sc.scrollTop = 0;
            }"""
        )
        await asyncio.sleep(max(0.1, float(SLEEP_READER_CATALOG_SCROLL) * 0.3))
        want = titles[0] if titles else ""
        clicked = await click_catalog_list_item(page, want)
        first_title = normalize_catalog_title(clicked) or want
        await asyncio.sleep(SLEEP_READER_CATALOG_CLICK)
        await dismiss_reader_search(page)
        await close_reader_catalog(page)
        await asyncio.sleep(SLEEP_READER_CATALOG_CLOSE)
        if await is_reader_catalog_open(page):
            await close_reader_catalog(page)
    except Exception as e:
        print(f"  ⚠️  目录跳转异常: {e}")
    # 顶栏可能滞后/读空：多读几次，仍空则用目录首项
    header = ""
    for _ in range(5):
        header = await read_chapter_title(page, load_catalog_titles(catalog_path) if catalog_path else None,
                                          fallback=first_title)
        if header:
            break
        await asyncio.sleep(0.3)
    if not header:
        header = first_title
    print(f"  ✅ 已跳到全书开头，当前:「{header}」(点击首项「{first_title}」)")
    return header


def save_chapter(ch_title, blocks, ch_idx, md_dir, raw_dir, *, allow_empty=False):
    title = display_chapter_title(ch_title, ch_idx)
    body, img_records = render_chapter_md(title, blocks, ch_idx)
    text_len = sum(len(b["text"]) for b in blocks if b["type"] == "text")
    if text_len == 0 and not img_records:
        if not allow_empty:
            return 0, []
        # 故意空章（短诗被顶栏越过等）：写仅标题占位，保证续传编号前进
        body = f"# {title}\n"
    with open(os.path.join(md_dir, f"{ch_idx:04d}.md"), "w", encoding="utf-8") as f:
        f.write(body)
    with open(os.path.join(raw_dir, f"{ch_idx:04d}.json"), "w", encoding="utf-8") as f:
        json.dump({"title": title, "images": img_records, "text_len": text_len},
                  f, ensure_ascii=False)
    return text_len, img_records


def chapter_saved(text_len, images):
    """章节只有实际写入文字或图片时才算本次新增。"""
    return bool(text_len or images)



async def click_catalog_list_item(page, target_title: str = "") -> str:
    """用真实鼠标点击目录项（Vue 对纯 JS click 常无响应）。

    - target_title 为空：点第一个可见项（用于跳到全书开头）
    - target_title 非空：只点匹配项；当前屏找不到则返回 ""（调用方滚目录再试）
      **绝不**在指定目标时退回点击首项，否则会误跳到「版权信息」。
    """
    target = normalize_catalog_title(target_title)
    info = await page.evaluate(
        r"""(target) => {
            const strip = (s) => String(s || '')
                .replace(/[\u200b\u200c\u200d\ufeff\u2060]/g, '')
                .replace(/(当前读到|已读到|读到)\s*\d+\s*%?\s*$/g, '')
                .replace(/\s*\d+\s*%\s*$/g, '')
                .trim();
            const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                if (parseFloat(st.opacity || '1') < 0.05) return false;
                const r = el.getBoundingClientRect();
                return r.width >= 40 && r.height >= 12
                    && r.right > 0 && r.left < window.innerWidth
                    && r.bottom > 0 && r.top < window.innerHeight;
            };
            const matchTitle = (t, tgt) => {
                if (!t) return false;
                if (!tgt) return true;
                if (t === tgt) return true;
                const compact = (s) => String(s || '').replace(/[\u200b\u200c\u200d\ufeff\u2060]/g, '').replace(/\s+/g, '');
                const tc = compact(t), gc = compact(tgt);
                if (tc && gc && tc === gc) return true;
                // 短标题只允许精确/压缩全等，避免「春日」点到「春日京中有怀」
                if (gc.length <= 4 || tc.length <= 4) return false;
                if (tc.startsWith(gc) || gc.startsWith(tc)) {
                    const b = Math.abs(tc.length - gc.length);
                    return Math.min(tc.length, gc.length) >= 4 && b <= 8;
                }
                return false;
            };
            const items = Array.from(document.querySelectorAll('.readerCatalog_list_item'));
            const scored = [];
            for (const el of items) {
                if (el.closest && el.closest('form, [class*="searchInput"], [class*="SearchInput"]')) {
                    continue;
                }
                if (!visible(el)) continue;
                const titleEl = el.querySelector(
                    '[class*="title"], .readerCatalog_list_item_title, .chapterItem_title'
                );
                const t = strip((titleEl && titleEl.textContent) || el.textContent || '');
                if (!t) continue;
                if (!matchTitle(t, target)) continue;
                let score = 0;
                if (t === target) score = 100;
                else if (t.startsWith(target) || target.startsWith(t)) score = 80;
                else score = 50;
                scored.push({el, t, score});
            }
            scored.sort((a, b) => b.score - a.score);
            let pick = scored.length ? scored[0].el : null;
            let raw = scored.length ? scored[0].t : '';
            if (!pick && !target) {
                pick = items.find(el => visible(el)) || null;
                if (pick) {
                    const titleEl = pick.querySelector(
                        '[class*="title"], .readerCatalog_list_item_title, .chapterItem_title'
                    );
                    raw = strip((titleEl && titleEl.textContent) || pick.textContent || '');
                }
            }
            if (!pick) return null;
            pick.scrollIntoView({block: 'center'});
            const r = pick.getBoundingClientRect();
            if (r.width < 20 || r.height < 10) return null;
            return {
                raw,
                x: r.x + Math.min(Math.max(r.width / 2, 20), 120),
                y: r.y + r.height / 2,
                w: r.width,
                h: r.height,
            };
        }""",
        target,
    )
    if not info:
        return ""
    raw = normalize_catalog_title(str(info.get("raw") or ""))
    if target:
        if not raw:
            return ""
        rk, tk = compact_title_key(raw), compact_title_key(target)
        exact = raw == target or (rk and tk and rk == tk)
        if not exact:
            # 非精确时允许较长标题的前缀包含，但短标题必须精确
            if not rk or not tk or min(len(rk), len(tk)) <= 4:
                return ""
            if not (rk.startswith(tk) or tk.startswith(rk)):
                return ""
            if abs(len(rk) - len(tk)) > 8:
                return ""
    try:
        x, y = float(info["x"]), float(info["y"])
        w, h = float(info.get("w") or 0), float(info.get("h") or 0)
        if w >= 20 and h >= 10:
            await page.mouse.click(x, y)
        else:
            return ""
    except Exception:
        return ""
    return raw


async def goto_catalog_chapter(page, target_title: str, catalog_titles=None) -> str:
    """打开目录并点击目标章名；成功返回实际点到的清洗标题，失败返回空串。

    点击后必须校验顶栏/正文已落到目标章，避免“点了但还在上一章”。
    失败时务必关闭目录，避免侧栏常开挡住翻页。
    """
    target = normalize_catalog_title(target_title)
    if not target:
        return ""
    try:
        if not await open_reader_catalog(page):
            print(f"  ⚠️  无法打开目录以跳转「{target}」")
            await close_reader_catalog(page)
            return ""

        land_fails = 0
        # 长目录（数百章）按索引先粗定位，再细滚找，避免末章滚不到
        cat_n = len(catalog_titles or [])
        cat_i = catalog_index(catalog_titles, target) if catalog_titles else None
        if cat_i is not None and cat_n > 1:
            ratio = max(0.0, min(1.0, float(cat_i) / float(cat_n - 1)))
            try:
                await page.evaluate(
                    """(ratio) => {
                        const sc = document.querySelector(
                            '.readerCatalog_list_scroll_area, [class*="readerCatalog_list_scroll"]'
                        );
                        if (!sc) return;
                        const max = Math.max(0, sc.scrollHeight - sc.clientHeight);
                        sc.scrollTop = Math.round(max * ratio);
                    }""",
                    ratio,
                )
                await asyncio.sleep(max(0.05, float(SLEEP_READER_CATALOG_SCROLL) * 0.2))
            except Exception:
                pass
        for _ in range(160):
            # 目录内若误入搜索态，先退回列表
            await dismiss_reader_search(page)
            await blur_reader_inputs(page)
            clicked = await click_catalog_list_item(page, target)
            if clicked:
                hit = normalize_catalog_title(clicked)
                ok = (
                    hit == target
                    or (
                        hit and target
                        and (hit in target or target in hit)
                        and min(len(hit), len(target)) >= 2
                        and abs(len(hit) - len(target)) <= 12
                    )
                )
                if not ok:
                    print(
                        f"    … 目录点到「{(hit or '')[:24]}」≠目标「{target[:24]}」，继续滚找"
                    )
                else:
                    await asyncio.sleep(SLEEP_READER_CATALOG_CLICK)
                    await dismiss_reader_search(page)
                    await close_reader_catalog(page)
                    await asyncio.sleep(SLEEP_READER_CATALOG_CLOSE)
                    for _ in range(4):
                        if not await is_reader_catalog_open(page):
                            break
                        await close_reader_catalog(page)
                        await asyncio.sleep(0.12)
                    # 点击成功 ≠ 真正跳转成功：必须校验顶栏/正文
                    try:
                        await page.evaluate("() => window.__wr_reset && window.__wr_reset()")
                    except Exception:
                        pass
                    await force_reader_repaint(page)
                    await recover_reader_text_after_nav(page, allow_nudge=False)
                    landed = await verify_reader_on_chapter(
                        page, target, catalog_titles, retries=3
                    )
                    if landed:
                        return hit or target
                    header = await read_chapter_title(page, catalog_titles)
                    land_fails += 1
                    print(
                        f"    … 目录点击后未落到「{target[:20]}」"
                        f"（顶栏「{(header or '空')[:20]}」），"
                        f"关闭目录后重试（{land_fails}/3）"
                    )
                    await close_reader_catalog(page)
                    await asyncio.sleep(0.2)
                    if land_fails >= 3:
                        print(
                            f"  ⚠️  目录跳转「{target[:20]}」连续未落地，放弃并关闭目录"
                        )
                        break
                    if not await open_reader_catalog(page):
                        continue
                    continue

            moved = await page.evaluate(
                """() => {
                    const sc = document.querySelector(
                        '.readerCatalog_list_scroll_area, [class*="readerCatalog_list_scroll"]'
                    );
                    if (!sc) return false;
                    const before = sc.scrollTop;
                    const max = Math.max(0, sc.scrollHeight - sc.clientHeight);
                    if (before >= max - 1) return false;
                    sc.scrollTop = Math.min(
                        max, before + Math.max(sc.clientHeight * 0.85, 100)
                    );
                    return sc.scrollTop > before;
                }"""
            )
            if not moved:
                break
            await asyncio.sleep(max(0.05, float(SLEEP_READER_CATALOG_SCROLL) * 0.15))

        print(f"  ⚠️  目录中未找到「{target}」")
    except Exception as e:
        print(f"  ⚠️  目录跳转「{target}」异常: {e}")
    finally:
        try:
            await close_reader_catalog(page)
        except Exception:
            pass
    return ""


async def run_session(book_id, md_dir, raw_dir, start_idx, seen_imgs,
                      goto_first=False, catalog_path=None, headless=False,
                      reader_width=None, reader_height=None,
                      force_single_page=None, prefer_largest=None):
    reached_end = False
    catalog_titles = load_catalog_titles(catalog_path) if catalog_path else []
    last_cat_title = catalog_titles[-1] if catalog_titles else ""
    async with async_playwright() as p:
        viewport = reader_viewport(
            reader_width, reader_height, prefer_largest=prefer_largest)
        ctx = await launch_weread_context(
            p, headless=headless, viewport=viewport)
        if not await ensure_logged_in(
                ctx, allow_interactive_login=not headless):
            await ctx.close()
            if headless:
                raise RuntimeError(
                    "无头模式下检测到未登录/登录页，已终止。"
                    "请去掉 --headless 扫码登录，或确认 cache/browser_profile 登录态有效后重试。"
                )
            return "", "", 0, 0, start_idx, False, False

        page = await ctx.new_page()
        await page.add_init_script(CANVAS_HOOK)
        print("\n  打开阅读器...")
        # 先按目标尺寸拉窗口，避免 profile 恢复成矮窗导致每页只有几行
        try:
            sized = await ensure_browser_window_size(
                page, viewport, prefer_largest=prefer_largest)
            if sized.get("width") and sized.get("height"):
                print(
                    f"  🪟 浏览器窗口: {sized['width']}x{sized['height']}"
                    f"（目标 {viewport['width']}x{viewport['height']}）"
                )
        except Exception as e:
            print(f"  ⚠️  调整浏览器窗口失败: {e}")
        # 用 domcontentloaded + 固定等待，避免阅读器页长连接/轮询导致 networkidle 永不触发而超时。
        await page.goto(
            f"https://weread.qq.com/web/reader/{book_id}",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await asyncio.sleep(SLEEP_READER_AFTER_LOAD)
        # 导航后 profile 可能再次改尺寸，再拉一次
        try:
            await ensure_browser_window_size(
                page, viewport, prefer_largest=prefer_largest)
        except Exception:
            pass
        viewport = await ensure_configured_viewport(page, viewport)
        if headless and await page_needs_login(page):
            await ctx.close()
            raise RuntimeError(
                "无头模式下打开阅读器后出现登录页，已终止。"
                "请去掉 --headless 扫码登录后再试。"
            )

        if force_single_page is None:
            force_single_page = READER_FORCE_SINGLE_PAGE
        viewport = await ensure_reader_layout(
            page, viewport, force_single_page=force_single_page)
        # 上下滚动会长文空转；优先切到左右翻页再抓取/续传
        await ensure_horizontal_paging_mode(page)

        book_title, book_author = await fetch_book_title(page)
        bootstrap_title = ""
        if goto_first:
            bootstrap_title = await goto_first_chapter(page, catalog_path) or ""
            catalog_titles = load_catalog_titles(catalog_path) if catalog_path else catalog_titles
            last_cat_title = catalog_titles[-1] if catalog_titles else ""
        elif catalog_path and not catalog_titles:
            catalog_titles = load_catalog_titles(catalog_path)
            last_cat_title = catalog_titles[-1] if catalog_titles else ""

        await focus_reader_for_keyboard(page)
        await asyncio.sleep(SLEEP_READER_AFTER_HOOK)

        current_chapter = await read_chapter_title(
            page, catalog_titles, fallback=bootstrap_title)
        # 顶栏仍空且是全书开头：用目录首项，保证后续能按目录顺序切章
        if not current_chapter and catalog_titles and (goto_first or start_idx <= 1):
            current_chapter = catalog_titles[0]
        # 续传：阅读器常停在错误位置，目录跳到「已导出最后一章」的下一章
        if (not goto_first) and start_idx > 1 and catalog_titles:
            from_md_title, _ = get_last_chapter_title(md_dir)
            anchor = normalize_catalog_title(from_md_title or "")
            # 已导出正文末章后仅剩封底等无正文项：直接视为全书完成
            if anchor and is_export_complete_after(anchor, catalog_titles):
                rest = catalog_titles_after(catalog_titles, anchor)
                rest_hint = "、".join((t[:12] for t in rest[:3])) if rest else "目录末"
                print(
                    f"  … 上次「{anchor[:24]}」后仅剩非正文目录项"
                    f"（{rest_hint}），视为全书抓取完成"
                )
                await page.close(); await ctx.close()
                return book_title, book_author, 0, 0, start_idx, True, False
            nxt = next_catalog_title(catalog_titles, anchor) if anchor else None
            if nxt and is_non_content_catalog_title(nxt):
                print(
                    f"  … 下一章「{nxt[:24]}」为无正文目录项，视为全书抓取完成"
                )
                await page.close(); await ctx.close()
                return book_title, book_author, 0, 0, start_idx, True, False
            if nxt:
                cur_resolved = resolve_chapter_title(
                    current_chapter or "", catalog_titles)
                need_jump = compact_title_key(cur_resolved) != compact_title_key(nxt)
                if need_jump:
                    print(
                        f"  … 续传定位：目录跳到「{nxt[:32]}」"
                        f"（接在「{(anchor or '')[:24]}」后；"
                        f"顶栏曾是「{(current_chapter or '')[:20]}」）"
                    )
                    jumped = await goto_catalog_chapter(page, nxt, catalog_titles)
                    for _ in range(3):
                        await close_reader_catalog(page)
                        if not await is_reader_catalog_open(page):
                            break
                    await blur_reader_inputs(page)
                    await focus_reader_for_keyboard(page)
                    await recover_reader_text_after_nav(page)
                    if jumped:
                        # 以目标章为准；顶栏双页常显示下一章名，不能盲信
                        current_chapter = resolve_chapter_title(
                            jumped, catalog_titles) or nxt
                    else:
                        # 目标为封底等无正文项，或已导出章后仅剩无正文项：未落地也算完成
                        if is_non_content_catalog_title(nxt) or (
                            anchor and is_export_complete_after(anchor, catalog_titles)
                        ):
                            print(
                                f"  ⚠️  续传跳转「{nxt[:24]}」未落地，"
                                f"该目标为无正文/书末项，视为全书抓取完成"
                            )
                            for _ in range(5):
                                await close_reader_catalog(page)
                                if not await is_reader_catalog_open(page):
                                    break
                                await page.keyboard.press("Escape")
                                await asyncio.sleep(0.15)
                            await page.close(); await ctx.close()
                            return book_title, book_author, 0, 0, start_idx, True, False
                        # 未落地：不要假装已在目标章（会把附录正文灌进末章）。
                        # 保留阅读器当前章，后续靠正文切章/翻页前进；末章则重开或收尾。
                        print(
                            f"  ⚠️  续传目录未确认落到「{nxt[:24]}」，"
                            f"保持当前位置「{(current_chapter or '空')[:20]}」并关闭目录"
                        )
                        for _ in range(5):
                            await close_reader_catalog(page)
                            if not await is_reader_catalog_open(page):
                                break
                            await page.keyboard.press("Escape")
                            await asyncio.sleep(0.15)
                        # 近书末：续传跳转失败且进度≥99%，直接收尾
                        pct = await read_reader_progress_percent(page)
                        if pct is not None and pct >= NEAR_END_PROGRESS_PERCENT:
                            print(
                                f"  … 阅读进度 {pct}% 且续传跳转失败，"
                                f"视为全书抓取完成"
                            )
                            await page.close(); await ctx.close()
                            return book_title, book_author, 0, 0, start_idx, True, False
                        # 若当前已在目标前一章，逻辑仍对准 nxt，靠线性翻页进入
                        cur_now = resolve_chapter_title(
                            await read_chapter_title(page, catalog_titles) or current_chapter,
                            catalog_titles,
                        )
                        if cur_now and compact_title_key(cur_now) == compact_title_key(nxt):
                            current_chapter = nxt
                        elif cur_now:
                            current_chapter = cur_now
                        else:
                            current_chapter = nxt
                else:
                    current_chapter = nxt
        print(f"  📖 {book_title} — {book_author}")
        print(f"  会话开始:「{current_chapter}」")
        ch_idx = start_idx
        ch_blocks = []
        total_chars = total_imgs = 0
        chapters_this_session = 0
        stale = 0
        empty_page_streak = 0
        near_end_fail_streak = 0
        page_num = 0
        # 会话内已落盘章节指纹；重复则说明切章回退/停滞
        saved_chapter_fps: set[str] = set()
        dup_chapter_hits = [0]
        MAX_DUP_CHAPTER_HITS = 5
        # 跨会话也识别「同一章同一正文」：续传重开后避免附录重复落盘
        try:
            for name in sorted(
                f for f in os.listdir(md_dir) if f.endswith(".md")
            )[-12:]:
                md_path = os.path.join(md_dir, name)
                with open(md_path, encoding="utf-8") as mf:
                    body = mf.read()
                title_line = ""
                for line in body.splitlines():
                    if line.startswith("#"):
                        title_line = normalize_catalog_title(line.lstrip("#").strip())
                        break
                payload = ((title_line or "") + "\n" + body).encode("utf-8")
                saved_chapter_fps.add(hashlib.md5(payload).hexdigest())
        except Exception:
            pass
        # 去重策略：
        # - 上一页行集合：挡住双页半页重叠
        # - 本章已抓页指纹：挡住翻页空转把同一 spread 反复灌入
        # - 本章长行集合：挡住折行抖动导致指纹变了但仍是旧正文
        # 注意：章内集合在切章后必须按新缓冲重建，不能做成会话级永久集合。
        last_page_fp = ""
        last_page_lines: set[str] = set()
        seen_page_fps: set[str] = set()
        chapter_seen_lines: set[str] = set()
        page_cycle_hits = 0
        # 优先方向键；停滞时再点阅读区中右（避开右侧工具条/搜索）
        turn_methods = ("arrow", "arrow2", "click_midright")
        turn_method_idx = 0
        catalog_jump_count = 0
        catalog_jump_failures = 0
        MAX_CATALOG_JUMPS = 8
        MAX_CATALOG_JUMP_FAILURES = 3
        # 顶栏已前进但当前章仍无正文时，优先目录回跳重抓，避免空跟章丢篇
        empty_header_resync = 0
        MAX_EMPTY_HEADER_RESYNC = 1
        # 顶栏跨过多章却仍持续抓到「新正文」时的确认计数（防止双页顶栏闪烁误跳）
        header_multi_ahead_hits = 0
        # 顶栏长期停在上一章时，允许一次目录重定位到逻辑章
        header_lag_resync_used = 0
        # 中途默认不点目录；仅翻页空转/顶栏错位时有限次目录跳转
        request_reopen = False
        # 首页已抓过：首轮主循环不要立刻翻走正确页
        skip_first_turn = True

        def reset_page_dedupe(*, keep_chapter_progress: bool = False):
            """换章或目录跳转后清空页级去重状态。

            keep_chapter_progress=True：保留本章已抓行/页指纹（仅清「上一页」重叠态）。
            """
            nonlocal last_page_fp, last_page_lines, seen_page_fps, chapter_seen_lines, page_cycle_hits
            last_page_fp = ""
            last_page_lines = set()
            page_cycle_hits = 0
            if keep_chapter_progress:
                return
            seen_page_fps = set()
            chapter_seen_lines = set()

        def adopt_chapter_blocks(blocks):
            """切章后采用新缓冲，并重建本章去重集合。"""
            nonlocal ch_blocks, chapter_seen_lines, seen_page_fps, page_cycle_hits
            ch_blocks = list(blocks or [])
            chapter_seen_lines = chapter_text_line_set(ch_blocks)
            seen_page_fps = set()
            page_cycle_hits = 0
            reset_page_dedupe(keep_chapter_progress=True)

        async def capture_current_page():
            """抓当前页的有序块，累加到 ch_blocks；返回是否有新内容。

            去重：
            - 整页指纹与上一页相同 → 丢弃
            - 整页指纹在本章已出现过 → 翻页空转，丢弃并记 cycle
            - 文本行若出现在「上一页」→ 跳过（双页重叠半页）
            - 本章已出现的长行 → 跳过（折行抖动下的循环重灌）
            """
            nonlocal ch_blocks, last_page_fp, last_page_lines
            nonlocal seen_page_fps, chapter_seen_lines, page_cycle_hits
            await asyncio.sleep(SLEEP_READER_PAGE_RENDER)
            chars = await page.evaluate("() => window.__wr_chars")
            rects = await page.evaluate(CANVAS_RECTS_JS)
            imgs = await page.evaluate(VIEWPORT_IMGS_JS)
            new_blocks = build_page_blocks(chars, imgs, rects, seen_imgs)
            if not new_blocks:
                return False
            page_fp = page_blocks_fingerprint(new_blocks)
            if page_fp and page_fp == last_page_fp:
                return False
            if page_fp and page_fp in seen_page_fps:
                page_cycle_hits += 1
                last_page_fp = page_fp
                return False

            page_line_set: set[str] = set()
            added = 0
            for b in new_blocks:
                if b.get("type") == "text":
                    t = (b.get("text") or "").strip()
                    if not t:
                        continue
                    page_line_set.add(t)
                    if t in last_page_lines:
                        continue
                    if should_skip_chapter_line(t, chapter_seen_lines):
                        continue
                    if (
                        ch_blocks
                        and ch_blocks[-1].get("type") == "text"
                        and (ch_blocks[-1].get("text") or "").strip() == t
                    ):
                        continue
                    ch_blocks.append({"type": "text", "text": t})
                    chapter_seen_lines.add(t)
                    added += 1
                else:
                    ch_blocks.append(b)
                    added += 1

            if page_fp:
                last_page_fp = page_fp
                last_page_lines = page_line_set
                if added > 0:
                    seen_page_fps.add(page_fp)
                    # 有真实新正文时清零空转计数
                    page_cycle_hits = 0
                    return True
                # 本页有字但全被去重挡下：
                # - 仅当「同一页指纹再次出现」才算空转
                # - 新指纹但无新长行：常见于双页半页重叠/折行，不算 cycle
                if page_line_set and page_fp in seen_page_fps:
                    page_cycle_hits += 1
                elif page_line_set:
                    seen_page_fps.add(page_fp)
                return False
            return added > 0


        async def jump_catalog_and_reanchor(
            target_title: str, *, reason: str, clear_buffer: bool = True
        ) -> str:
            """有限次目录跳转并重建抓取锚点；成功返回规范章名。"""
            nonlocal catalog_jump_count, catalog_jump_failures, page_num, stale
            nonlocal turn_method_idx, skip_first_turn
            target = resolve_chapter_title(target_title, catalog_titles) or normalize_catalog_title(target_title)
            if not target:
                return ""
            if catalog_jump_count >= MAX_CATALOG_JUMPS:
                print(f"    … 目录跳转次数已满，放弃「{target[:20]}」（{reason}）")
                return ""
            catalog_jump_count += 1
            print(
                f"    … 目录重定位「{target[:24]}」"
                f"（{reason}；{catalog_jump_count}/{MAX_CATALOG_JUMPS}）"
            )
            jumped = await goto_catalog_chapter(page, target, catalog_titles)
            for _ in range(3):
                await close_reader_catalog(page)
                if not await is_reader_catalog_open(page):
                    break
            await blur_reader_inputs(page)
            await focus_reader_for_keyboard(page)
            await recover_reader_text_after_nav(page)
            if not jumped:
                catalog_jump_failures += 1
                return ""
            # goto 已校验落地；再关一次目录，防止侧栏残留
            if await is_reader_catalog_open(page):
                await close_reader_catalog(page)
            page_num = 0
            stale = 0
            turn_method_idx = 0
            skip_first_turn = True
            if clear_buffer:
                adopt_chapter_blocks([])
            else:
                reset_page_dedupe(keep_chapter_progress=True)
            return resolve_chapter_title(jumped, catalog_titles) or target

        async def break_page_cycle_or_lag() -> bool:
            """翻页空转/顶栏严重错位时的有限纠偏。

            原则：
            - 双页顶栏滞后很常见，不能仅凭顶栏落后就重定位/重开
            - 同一页指纹反复出现才算空转；半页重叠去重不算
            - 文末章空转：保留缓冲并收尾，避免「抓到了又丢掉」
            - 中间章空转：不要丢弃缓冲后重开。无新内容时再重开只会卡在同一章
              （续传锚点未前进 → 连续重开失败）。应交外层「停滞前进」
              落盘并跳下一章。
            - 仅当当前章无法在目录定位时，才丢弃缓冲并重开会话

            返回 True 表示已处理并应 continue/break 主循环。
            """
            nonlocal current_chapter, ch_idx, page_num, stale, reached_end
            nonlocal page_cycle_hits, header_lag_resync_used, request_reopen
            nonlocal ch_blocks, turn_method_idx, header_multi_ahead_hits

            header_now = await read_chapter_title(page, catalog_titles)
            delta = catalog_index_delta(catalog_titles, current_chapter, header_now)
            # 需要连续多次「整页指纹重复」才认定空转
            cyc = page_cycle_hits >= MAX_PAGE_CYCLE_HITS and page_num >= 4
            # 顶栏落后 ≥2 章且已有若干页仍无前进时才重定位（双页滞后 1 章很常见）
            lag = (
                delta is not None and delta <= -2
                and page_num >= 6
                and page_cycle_hits >= 2
                and header_lag_resync_used < 1
            )

            if lag and current_chapter:
                header_lag_resync_used += 1
                print(
                    f"    … 顶栏「{(header_now or '')[:16]}」落后逻辑"
                    f"「{(current_chapter or '')[:16]}」Δ={delta}，尝试目录重定位"
                )
                landed = await jump_catalog_and_reanchor(
                    current_chapter,
                    reason="顶栏严重落后重定位",
                    clear_buffer=True,
                )
                if landed:
                    current_chapter = landed
                    page_cycle_hits = 0
                    await capture_current_page()
                    while await split_if_next_chapter_started():
                        if reached_end:
                            break
                    return True
                print("    … 重定位失败，丢弃未完成缓冲并重开（不落盘半章）")
                adopt_chapter_blocks([])
                request_reopen = True
                return True

            if not cyc:
                return False

            n_lines_now = chapter_text_line_count(ch_blocks)
            # 文末附录/末章：空转时保留缓冲并收尾，避免「抓到了又丢掉」反复重开
            if is_export_terminal_chapter(current_chapter, catalog_titles):
                print(
                    f"    … 文末章翻页空转 cycle={page_cycle_hits} "
                    f"p={page_num} lines={n_lines_now} "
                    f"「{(current_chapter or '')[:20]}」"
                    f"/顶栏「{(header_now or '')[:16]}」"
                    f"→ 按全书末尾收尾（不重开）"
                )
                reached_end = True
                return True

            # 顶栏已跨过多章：空转前再试一次扩大窗口的正文越章，尽量切开脏缓冲
            if (
                catalog_titles
                and current_chapter
                and delta is not None
                and delta > 1
                and ch_blocks
            ):
                max_ahead = max(40, min(int(delta) + 5, 200))
                recovered = False
                while True:
                    hit = content_overrun_split(
                        ch_blocks,
                        catalog_titles,
                        current_chapter,
                        max_ahead=max_ahead,
                    )
                    if not hit:
                        break
                    hit_title, before, after = hit
                    print(
                        f"    … 空转正文越章：逻辑「{(current_chapter or '')[:16]}」"
                        f"→「{hit_title[:16]}」（max_ahead={max_ahead}）"
                    )
                    if before:
                        n, _imgs, saved = await commit_chapter(
                            current_chapter,
                            before,
                            note_suffix=" [空转正文越章]",
                        )
                        if saved:
                            ch_idx += 1
                            await sleep_between_chapters(n)
                    adopt_chapter_blocks(after)
                    current_chapter = resolve_chapter_title(
                        hit_title, catalog_titles
                    ) or hit_title
                    page_num = 0
                    stale = 0
                    page_cycle_hits = 0
                    header_multi_ahead_hits = 0
                    recovered = True
                    if is_export_terminal_chapter(
                            current_chapter, catalog_titles):
                        reached_end = True
                        return True
                    if is_last_catalog_chapter(
                            current_chapter, catalog_titles):
                        break
                while await split_if_next_chapter_started():
                    recovered = True
                    if reached_end:
                        return True
                if recovered:
                    return True

            # 中间章空转且目录可定位：交给外层停滞前进（落盘并跳下一章）。
            # 旧逻辑「丢弃缓冲 + 重开」会让续传锚点停在上一完整章，反复卡死同一章。
            if (
                catalog_titles
                and catalog_index(catalog_titles, current_chapter) is not None
            ):
                print(
                    f"    … 检测到翻页空转 cycle={page_cycle_hits} "
                    f"p={page_num} lines={n_lines_now} "
                    f"「{(current_chapter or '')[:20]}」"
                    f"/顶栏「{(header_now or '')[:16]}」"
                    f"→ 交由停滞前进落盘并跳下一章（不丢弃重开）"
                )
                # 抬高 stale，确保外层立刻走「停滞前进」而不是继续空翻页
                stale = max(stale, STALE_PAGE_LIMIT)
                return False

            print(
                f"    … 检测到翻页空转 cycle={page_cycle_hits} "
                f"p={page_num} lines={n_lines_now} "
                f"「{(current_chapter or '')[:20]}」"
                f"/顶栏「{(header_now or '')[:16]}」"
                f"→ 丢弃未完成缓冲并重开（目录无法定位）"
            )
            adopt_chapter_blocks([])
            request_reopen = True
            return True

        async def commit_chapter(title, blocks, *, note_suffix="", allow_empty=False):
            """落盘一章并累计统计；返回 (text_len, imgs, saved)。

            若同一章同一正文指纹反复出现，视为切章死循环并中止。
            allow_empty=True 时写入仅标题占位章，保证续传能越过空章。
            """
            nonlocal total_chars, total_imgs, chapters_this_session, reached_end
            fp = chapter_blocks_fingerprint(title, blocks)
            # 停滞前进也用 allow_empty=True 落盘；若仍放行重复指纹，会把同一附录
            # 写成 0039/0040/... 无限编号。有正文时无论是否 allow_empty 都去重。
            has_text = any(
                b.get("type") == "text" and (b.get("text") or "").strip()
                for b in (blocks or [])
            )
            if fp in saved_chapter_fps and (has_text or not allow_empty):
                dup_chapter_hits[0] += 1
                print(
                    f"  ⚠️  重复章节内容「{(title or '')[:32]}」"
                    f"（{dup_chapter_hits[0]}/{MAX_DUP_CHAPTER_HITS}），跳过落盘"
                )
                if dup_chapter_hits[0] >= MAX_DUP_CHAPTER_HITS:
                    raise RuntimeError(
                        "章节进度疑似死循环：相同标题与正文反复出现。"
                        "常见原因是顶栏停在卷/作者名而内容切章已前进，"
                        "随后又被顶栏回退；或长标题含零宽字符导致无法切到下一章。"
                        "请更新导出逻辑后清理对应 output 目录重试。"
                    )
                return 0, [], False
            n, imgs = save_chapter(
                title, blocks, ch_idx, md_dir, raw_dir, allow_empty=allow_empty)
            saved = bool(chapter_saved(n, imgs) or allow_empty)
            if allow_empty and not chapter_saved(n, imgs):
                # 占位章：save_chapter 应已写 md；若没写则失败
                if not os.path.exists(os.path.join(md_dir, f"{ch_idx:04d}.md")):
                    return n, imgs, False
            elif not saved:
                return n, imgs, False
            saved_chapter_fps.add(fp)
            total_chars += n
            total_imgs += len(imgs)
            note = f" +{len(imgs)}图" if imgs else ""
            note += note_suffix
            print(f"  [{ch_idx:4d}] {title[:32]:32s} {n:6d}字 ({page_num}页){note}")
            chapters_this_session += 1
            return n, imgs, True

        async def sleep_between_chapters(n_chars):
            # 0 字（空切章/重复跳过）不等待，避免刷屏空等
            if not n_chars or int(n_chars) <= 0:
                return
            wait_s = chapter_sleep_seconds(
                n_chars,
                per_2k=SLEEP_CHAPTER_PER_2K_CHARS,
                min_seconds=SLEEP_CHAPTER_MIN,
                max_seconds=SLEEP_CHAPTER_MAX,
            )
            print(f"    … 章间等待 {wait_s:.1f}s（按 {n_chars} 字）")
            await asyncio.sleep(wait_s)

        async def sync_reader_after_content_split(target_title: str) -> bool:
            """内容已切章时，线性策略不再点目录追赶阅读器。

            继续 ArrowRight/点中右翻页即可；若顶栏长期落后，由正文越章切分
            或会话重开续传处理，避免中途频繁点菜单把位置点乱。
            """
            reader_title = await read_chapter_title(page, catalog_titles)
            if reader_needs_chapter_sync(
                    catalog_titles, target_title, reader_title):
                print(
                    f"    … 内容已切到「{target_title[:20]}」，"
                    f"阅读器仍在「{(reader_title or '未知')[:20]}」，"
                    f"不点目录，继续线性翻页"
                )
            return True

        async def split_if_next_chapter_started():
            """目录下一章标题已出现在正文块中时，提前切章（修复标题栏滞后/读空导致的窜章）。

            返回 True 表示发生了切章。
            """
            nonlocal ch_blocks, current_chapter, ch_idx, page_num, stale, reached_end
            found = find_chapter_split(ch_blocks, catalog_titles, current_chapter)
            if not found:
                return False
            nxt, before, after = found
            title_to_save = infer_title_for_blocks_before(
                nxt, catalog_titles, current_chapter)
            is_last = is_last_catalog_chapter(title_to_save, catalog_titles)
            if before:
                n, _imgs, saved = await commit_chapter(
                    title_to_save, before, note_suffix=" [内容切章]")
                # 仅实际落盘成功才推进编号，避免 0 字空切制造空洞
                if saved:
                    ch_idx += 1
                    if is_last:
                        adopt_chapter_blocks(after)
                        current_chapter = nxt
                        page_num = 0
                        stale = 0
                        reached_end = True
                        return True
                    await sleep_between_chapters(n)
                elif is_last:
                    adopt_chapter_blocks(after)
                    current_chapter = nxt
                    page_num = 0
                    stale = 0
                    reached_end = True
                    return True
            # before 为空：上一章已落盘或本页已属新章，仅把块归属切到下一章
            adopt_chapter_blocks(after)
            current_chapter = nxt
            page_num = 0
            stale = 0
            await sync_reader_after_content_split(current_chapter)
            return True

        # 首页：目录/续传跳转后 fillText 往往已在 __wr_chars，
        # 绝不能先 __wr_reset，否则「页面有字、抓取为空」→ 空跟章死循环重开。
        bootstrap_got = await capture_current_page()
        if not bootstrap_got:
            n = await recover_reader_text_after_nav(page, allow_nudge=True)
            if n and n > 0:
                bootstrap_got = await capture_current_page()
            if not bootstrap_got:
                await force_reader_repaint(page)
                await wait_stable(page, 0, timeout=2.0)
                bootstrap_got = await capture_current_page()
        if bootstrap_got:
            print(
                f"    … 首页已抓到正文「{(current_chapter or '')[:24]}」"
                f"（{sum(1 for b in ch_blocks if b.get('type')=='text')} 行）"
            )
        else:
            print(
                f"    … 首页暂无正文「{(current_chapter or '')[:24]}」，"
                f"先不翻页再试一次"
            )
            await force_reader_repaint(page)
            await recover_reader_text_after_nav(page, allow_nudge=True)
            await capture_current_page()
        while await split_if_next_chapter_started():
            if reached_end:
                break

        # 若首页缓冲仍空但顶栏已是紧邻下一章：短章被跳过/双页顶栏超前，
        # 允许空章前进，避免重开死循环。
        if not any(
            b.get("type") == "text" and (b.get("text") or "").strip()
            for b in (ch_blocks or [])
        ):
            header0 = await read_chapter_title(page, catalog_titles)
            if header0 and should_follow_header_title(
                    catalog_titles, current_chapter, header0):
                print(
                    f"    … 首页空且顶栏已是下一章「{header0[:20]}」，"
                    f"空章前进（不重开）"
                )
                n, _imgs, saved = await commit_chapter(
                    current_chapter, ch_blocks,
                    note_suffix=" [空章跳过]", allow_empty=True)
                if saved:
                    ch_idx += 1
                adopt_chapter_blocks([])
                current_chapter = resolve_chapter_title(
                    header0, catalog_titles) or header0
                page_num = 0
                await capture_current_page()
                while await split_if_next_chapter_started():
                    if reached_end:
                        break

        while not reached_end:
            # 搜索框/目录开着时方向键不会翻页，且会把字打进搜索
            await dismiss_reader_search(page)
            catalog_blocking = False
            if await is_reader_catalog_open(page):
                await close_reader_catalog(page)
                await dismiss_reader_search(page)
                if await is_reader_catalog_open(page):
                    # 再硬关一次：目录开着时按方向键只会在目录列表移动，导致「看似翻页」
                    await close_reader_catalog(page)
                    await blur_reader_inputs(page)
                if await is_reader_catalog_open(page):
                    catalog_blocking = True
                    stale += 1
                    near_end_fail_streak += 1
                    if stale in (3, 5, 8):
                        print(
                            f"    … 目录未关闭，跳过翻页键 stale={stale}/8 "
                            f"当前「{(current_chapter or '')[:24]}」"
                        )
                    await dismiss_reader_overlays(page)
                    await page.keyboard.press("Escape")
                    await blur_reader_inputs(page)
                    await asyncio.sleep(0.15)
                    # 近书末连续失败：进度≥99% 则视为结束，避免目录关不掉死循环
                    if near_end_fail_streak >= NEAR_END_FAIL_LIMIT:
                        pct = await read_reader_progress_percent(page)
                        if pct is not None and pct >= NEAR_END_PROGRESS_PERCENT:
                            print(
                                f"    … 阅读进度 {pct}% 且连续失败"
                                f"{near_end_fail_streak} 次（目录未关），"
                                f"视为全书结束"
                            )
                            await commit_chapter(
                                current_chapter, ch_blocks,
                                note_suffix=" [近书末连续失败收尾]",
                                allow_empty=True,
                            )
                            adopt_chapter_blocks([])
                            reached_end = True
                            break
                    # 绝不在目录仍开时 ArrowRight
                    if stale >= 8 and catalog_titles:
                        # 落到后面的目录跳转逻辑（复用）
                        pass
                    else:
                        continue
            if skip_first_turn:
                # 首页已定位到目标章：先处理当前屏，勿立刻 ArrowRight 翻走
                skip_first_turn = False
                turn_method = "hold"
            else:
                await page.evaluate("() => window.__wr_reset()")
                turn_method = turn_methods[turn_method_idx % len(turn_methods)]
                # 目录刚关不稳时优先点阅读区中右翻页，减少方向键被目录吞掉
                if catalog_blocking:
                    turn_method = "click_midright"
                await turn_reader_page(page, method=turn_method)
                await asyncio.sleep(SLEEP_READER_PAGE_TURN)
            # 默认短等：诗词页常不二次 fillText，长 timeout 会让日志长时间空白
            stable_count = await wait_stable(page, 0, timeout=2.5)
            if not stable_count or stable_count <= 0:
                # 只重绘，禁止再 reset（reset 会抹掉迟到的 fillText）
                await force_reader_repaint(page)
                await asyncio.sleep(max(0.08, float(SLEEP_READER_PAGE_RENDER)))
                stable_count = await wait_stable(page, 0, timeout=2.0)

            new_chapter = await read_chapter_title(page, catalog_titles)
            # 顶栏与逻辑章不一致时打点，方便对照「页面 vs 日志」
            if (
                new_chapter
                and current_chapter
                and compact_title_key(new_chapter) != compact_title_key(current_chapter)
            ):
                # 节流：仅 stale 奇数或刚切章后
                if stale in (0, 1, 3, 5, 8):
                    print(
                        f"    … 顶栏「{new_chapter[:20]}」/逻辑「{(current_chapter or '')[:20]}」"
                    )
            if new_chapter and should_follow_header_title(
                    catalog_titles, current_chapter, new_chapter):
                # 标题栏前进：先把已窜入上一章末尾的新章内容剥回
                # 注意：顶栏落后于内容切章时必须忽略，否则会目录回退死循环
                before, after = split_blocks_at_chapter_start(ch_blocks, new_chapter)
                if after:
                    # 仅剥掉 after，保留 before 为本章缓冲并同步去重集
                    ch_blocks = list(before or [])
                    chapter_seen_lines.clear()
                    chapter_seen_lines.update(chapter_text_line_set(ch_blocks))
                had_text = any(
                    b.get("type") == "text" and (b.get("text") or "").strip()
                    for b in (ch_blocks or [])
                )
                # 当前章还没抓到正文，顶栏却已到下一章：
                # 先原地重抓；仍空则空章前进（短诗/双页顶栏常见），禁止立刻重开死循环。
                if (not had_text) and (not after) and catalog_titles:
                    empty_header_resync += 1
                    print(
                        f"    … 顶栏「{new_chapter[:20]}」超前且「"
                        f"{(current_chapter or '')[:20]}」无正文，"
                        f"原地重抓（{empty_header_resync}/2）"
                    )
                    await force_reader_repaint(page)
                    await recover_reader_text_after_nav(
                        page, allow_nudge=empty_header_resync <= 1)
                    got_retry = await capture_current_page()
                    before2, after2 = split_blocks_at_chapter_start(
                        ch_blocks, new_chapter)
                    if after2:
                        ch_blocks = list(before2 or [])
                        chapter_seen_lines.clear()
                        chapter_seen_lines.update(chapter_text_line_set(ch_blocks))
                        after = after2
                    had_text = any(
                        b.get("type") == "text" and (b.get("text") or "").strip()
                        for b in (ch_blocks or [])
                    )
                    if (not had_text) and (not after):
                        if empty_header_resync <= 1 and not got_retry:
                            # 再给一轮键前捕获机会：不翻页 continue 到 capture 分支不成立
                            # 直接空章前进到顶栏章，避免卡死
                            print(
                                f"    … 仍无正文，空章跳过「"
                                f"{(current_chapter or '')[:20]}」→「{new_chapter[:20]}」"
                            )
                        else:
                            print(
                                f"    … 仍无正文，空章跳过「"
                                f"{(current_chapter or '')[:20]}」→「{new_chapter[:20]}」"
                            )
                        # fall through to commit empty + follow header
                    # 若重抓到了正文，fall through 正常 commit

                is_last = is_last_catalog_chapter(current_chapter, catalog_titles)
                # 顶栏已到下一章且当前缓冲仍空：允许写空章占位，避免续传死卡
                n, _imgs, saved = await commit_chapter(
                    current_chapter, ch_blocks,
                    allow_empty=not any(
                        b.get("type") == "text" and (b.get("text") or "").strip()
                        for b in (ch_blocks or [])
                    ),
                )
                if saved:
                    ch_idx += 1
                adopt_chapter_blocks(after)
                current_chapter = resolve_chapter_title(new_chapter, catalog_titles) or new_chapter
                page_num = 0
                stale = 0
                turn_method_idx = 0
                empty_header_resync = 0
                header_multi_ahead_hits = 0
                if not is_last and chapter_saved(n, _imgs):
                    await sleep_between_chapters(n)
                await capture_current_page()
                # 当前页也可能继续跨到再下一章
                while await split_if_next_chapter_started():
                    if reached_end:
                        break
                if is_last:
                    reached_end = True
                    break
                continue

            got_new = await capture_current_page()
            # 偶发：页已翻但 fillText 迟到 → 短重绘再抓（禁止 repaint 后再 reset）
            # 注意：完整 recover（含左右键）绝不能每轮空翻都跑，否则单次 10s+ 像卡死。
            if not got_new:
                await force_reader_repaint(page)
                await asyncio.sleep(max(0.05, float(SLEEP_READER_PAGE_RENDER)))
                await wait_stable(page, 0, timeout=0.8)
                got_new = await capture_current_page()
                if not got_new and stale >= 2:
                    # 仅在连续空翻后做轻量 recover，仍禁止每轮左右键
                    n = await recover_reader_text_after_nav(page, allow_nudge=False)
                    if n and n > 0:
                        got_new = await capture_current_page()
                if got_new:
                    print(
                        f"    … 重绘后抓到新内容 当前「{(current_chapter or '')[:24]}」"
                    )
            split = False
            while await split_if_next_chapter_started():
                split = True
                if reached_end:
                    break
            if reached_end:
                break
            if split:
                # 内容切章成功说明本页已贡献进度，下轮继续翻页
                stale = 0
                turn_method_idx = 0
                page_num += 1
                continue
            n_lines = sum(
                1 for b in ch_blocks if b.get("type") == "text"
            )

            if got_new:
                page_num += 1  # 原先只在 split 分支 +1，导致有抓取也无页进度心跳
                stale = 0
                empty_page_streak = 0
                near_end_fail_streak = 0
                turn_method_idx = 0
                empty_header_resync = 0
                if page_num == 1 or page_num % 2 == 0:
                    print(
                        f"    … 翻页中 p={page_num} 本章约 {n_lines} 行 "
                        f"「{(current_chapter or '')[:24]}」 key={turn_method}"
                    )

                # 线性策略：中途不点目录。顶栏/正文显示已越过当前章时，只按正文切开。
                recovered = False
                if (
                    catalog_titles
                    and current_chapter
                    and not is_last_catalog_chapter(current_chapter, catalog_titles)
                ):
                    delta = catalog_index_delta(
                        catalog_titles, current_chapter, new_chapter
                    )
                    if delta is not None and delta > 1:
                        header_multi_ahead_hits += 1
                    else:
                        header_multi_ahead_hits = 0

                    need_overrun_check = (
                        header_multi_ahead_hits >= HEADER_MULTI_AHEAD_CONFIRM
                        or bool(
                            skipped_next_chapter_evidence(
                                ch_blocks, catalog_titles, current_chapter
                            )
                        )
                        or is_soft_runaway_chapter(n_lines, page_num)
                    )
                    if need_overrun_check:
                        # 顶栏跨章很远时放大搜索窗口，否则默认 40 章够不到文末附录
                        max_ahead = 40
                        if delta is not None and delta > 1:
                            max_ahead = max(40, min(int(delta) + 5, 200))
                        while True:
                            hit = content_overrun_split(
                                ch_blocks,
                                catalog_titles,
                                current_chapter,
                                max_ahead=max_ahead,
                            )
                            if not hit:
                                break
                            hit_title, before, after = hit
                            print(
                                f"    … 正文越章：逻辑「{(current_chapter or '')[:16]}」"
                                f"→「{hit_title[:16]}」（不点目录）"
                            )
                            if before:
                                n, _imgs, saved = await commit_chapter(
                                    current_chapter,
                                    before,
                                    note_suffix=" [正文越章切分]",
                                )
                                if saved:
                                    ch_idx += 1
                                    await sleep_between_chapters(n)
                            adopt_chapter_blocks(after)
                            current_chapter = resolve_chapter_title(
                                hit_title, catalog_titles
                            ) or hit_title
                            page_num = 0
                            stale = 0
                            header_multi_ahead_hits = 0
                            recovered = True
                            if is_last_catalog_chapter(
                                    current_chapter, catalog_titles):
                                break
                        while await split_if_next_chapter_started():
                            recovered = True
                            if reached_end:
                                break
                        if reached_end:
                            break

                    # 长章会超过软阈值：只要本页仍有新正文，就继续翻页，
                    # 绝不能丢弃缓冲重开（否则会在同一长章上死循环）。
                    n_lines = chapter_text_line_count(ch_blocks)
                    if (
                        not recovered
                        and not is_last_catalog_chapter(
                            current_chapter, catalog_titles
                        )
                        and is_soft_runaway_chapter(n_lines, page_num)
                        and (
                            page_num == 1
                            or page_num % 10 == 0
                            or is_hard_runaway_chapter(n_lines, page_num)
                        )
                    ):
                        print(
                            f"    … 本章较长 p={page_num} lines={n_lines} "
                            f"「{(current_chapter or '')[:20]}」，"
                            f"继续翻页抓取（不因长章重开）"
                        )

                # 翻页空转 / 顶栏落后：目录有限次纠偏，避免同行数膨胀
                if await break_page_cycle_or_lag():
                    if reached_end or request_reopen:
                        break
                    continue

                continue

            # ---- 本页无新内容 ----
            header_multi_ahead_hits = 0
            stale += 1
            # 统计「完全无字」空页：书末黑屏常见，与「有字但全被去重」区分
            try:
                n_chars_now = int(
                    await page.evaluate(
                        "() => (window.__wr_chars && window.__wr_chars.length) || 0"
                    )
                    or 0
                )
            except Exception:
                n_chars_now = 0
            if n_chars_now <= 0:
                empty_page_streak += 1
            else:
                empty_page_streak = 0

            is_terminal_now = is_export_terminal_chapter(
                current_chapter, catalog_titles)
            stale_limit = (
                LAST_CHAPTER_STALE_LIMIT if is_terminal_now else STALE_PAGE_LIMIT
            )
            if (
                stale == 1
                or stale in (3, 5, 8)
                or stale % 2 == 0
                or stale >= stale_limit
                or (is_terminal_now and empty_page_streak >= LAST_CHAPTER_EMPTY_STREAK)
            ):
                print(
                    f"    … 翻页无新内容 stale={stale}/{stale_limit} "
                    f"当前「{(current_chapter or '')[:24]}」 "
                    f"cycle={page_cycle_hits} empty={empty_page_streak} "
                    f"key={turn_method}"
                    + (" [文末]" if is_terminal_now else "")
                )
            if stale >= 2 and not is_terminal_now:
                turn_method_idx += 1

            # 末章/最后的附录等：黑屏或短时无新内容 → 直接收尾，避免书末空翻页反复重开
            if is_terminal_now and (
                empty_page_streak >= LAST_CHAPTER_EMPTY_STREAK
                or stale >= LAST_CHAPTER_STALE_LIMIT
            ):
                print(
                    f"    … 文末章「{(current_chapter or '')[:20]}」"
                    f"无更多正文（stale={stale}, empty={empty_page_streak}），"
                    f"按全书末尾收尾"
                )
                await commit_chapter(
                    current_chapter, ch_blocks, note_suffix=" [全书末尾]",
                    allow_empty=True)
                adopt_chapter_blocks([])
                reached_end = True
                break

            # 书末纯图页（左右栏皆图、canvas 无字）：进度已高时少次空抓即完成，
            # 避免双页图在左右/上下键间空转，不必等普通章 STALE_PAGE_LIMIT=8。
            if (
                not is_terminal_now
                and empty_page_streak >= IMAGE_ONLY_NEAR_END_EMPTY_STREAK
                and stale >= IMAGE_ONLY_NEAR_END_STALE_LIMIT
                and n_chars_now <= 0
            ):
                pct = await read_reader_progress_percent(page)
                if pct is not None and pct >= NEAR_END_PROGRESS_PERCENT:
                    print(
                        f"    … 近书末纯图/无字页（进度 {pct}%，"
                        f"stale={stale}, empty={empty_page_streak}），"
                        f"视为全书结束"
                    )
                    await commit_chapter(
                        current_chapter, ch_blocks,
                        note_suffix=" [近书末纯图页收尾]",
                        allow_empty=True)
                    adopt_chapter_blocks([])
                    reached_end = True
                    break

            # 仅在「足够多次整页重复」后才走空转处理；半页重叠的 stale 继续换翻页方式
            if page_cycle_hits >= MAX_PAGE_CYCLE_HITS and stale >= 4:
                if await break_page_cycle_or_lag():
                    if reached_end or request_reopen:
                        break
                    continue

            if stale < stale_limit:
                continue

            # stale 达上限：
            # 0) 近书末：进度≥99% 且连续失败 → 直接收尾
            # 1) 文末章 → 落盘收尾
            # 2) 中间章 → 落盘已抓内容并目录前进到下一章（避免同章无限重开）
            # 3) 无法定位 → 落盘后结束
            near_end_fail_streak += 1
            if near_end_fail_streak >= NEAR_END_FAIL_LIMIT:
                pct = await read_reader_progress_percent(page)
                if pct is not None and pct >= NEAR_END_PROGRESS_PERCENT:
                    print(
                        f"    … 阅读进度 {pct}% 且连续失败"
                        f"{near_end_fail_streak} 次（翻页停滞），"
                        f"视为全书结束"
                    )
                    await commit_chapter(
                        current_chapter, ch_blocks,
                        note_suffix=" [近书末连续失败收尾]",
                        allow_empty=True,
                    )
                    adopt_chapter_blocks([])
                    reached_end = True
                    break
            if is_export_terminal_chapter(current_chapter, catalog_titles):
                print(
                    f"    … 文末章「{(current_chapter or '')[:20]}」"
                    f"翻页停滞，落盘已抓内容并结束全书（不重开）"
                )
                await commit_chapter(
                    current_chapter, ch_blocks, note_suffix=" [全书末尾]",
                    allow_empty=True)
                adopt_chapter_blocks([])
                reached_end = True
                break
            if not catalog_titles or catalog_index(
                    catalog_titles, current_chapter) is None:
                await commit_chapter(
                    current_chapter, ch_blocks, note_suffix=" [无更多新内容]",
                    allow_empty=True)
                adopt_chapter_blocks([])
                reached_end = True
                break

            advance_to = resolve_stale_advance_target(
                current_chapter, catalog_titles
            )
            if advance_to:
                delta = catalog_index_delta(
                    catalog_titles, current_chapter, new_chapter
                )
                why = (
                    f"顶栏超前「{(new_chapter or '')[:16]}」"
                    if delta is not None and delta > 0
                    else "翻页无新内容"
                )
                print(
                    f"    … 停滞前进：{why}，「{(current_chapter or '')[:20]}」"
                    f"→「{advance_to[:20]}」（落盘后目录跳转，避免同章反复重开）"
                )
                n, _imgs, saved = await commit_chapter(
                    current_chapter,
                    ch_blocks,
                    note_suffix=" [停滞前进]",
                    allow_empty=True,
                )
                if saved:
                    ch_idx += 1
                    if chapter_saved(n, _imgs):
                        await sleep_between_chapters(n)
                adopt_chapter_blocks([])
                landed = await jump_catalog_and_reanchor(
                    advance_to,
                    reason="停滞前进到下一章",
                    clear_buffer=True,
                )
                if landed:
                    current_chapter = landed
                    page_num = 0
                    stale = 0
                    turn_method_idx = 0
                    empty_page_streak = 0
                    empty_header_resync = 0
                    header_multi_ahead_hits = 0
                    await capture_current_page()
                    while await split_if_next_chapter_started():
                        if reached_end:
                            break
                    if reached_end:
                        break
                    continue
                # 已落盘：重开后外层会从下一章续传，不会再卡死在同一章
                print(
                    f"    … 目录跳到「{advance_to[:20]}」未确认，"
                    f"结束本会话重开续传（本章已落盘）"
                )
                request_reopen = True
                break

            # 无下一章可前进：按书末处理
            print(
                f"    … 翻页停滞且无下一章「{(current_chapter or '')[:20]}」，"
                f"按全书末尾收尾"
            )
            await commit_chapter(
                current_chapter, ch_blocks, note_suffix=" [全书末尾]",
                allow_empty=True)
            adopt_chapter_blocks([])
            reached_end = True
            break

        # reached_end 时若缓冲仍有未 commit 的正文，再落盘一次（避免与上面重复）
        if reached_end and ch_blocks:
            n, imgs, saved = await commit_chapter(
                current_chapter, ch_blocks, note_suffix=" [末章]", allow_empty=False)
            if saved:
                print(f"  [{ch_idx:4d}] {(current_chapter or '')[:32]:32s} {n:6d}字 [末章]")

        await page.close(); await ctx.close()
        return book_title, book_author, chapters_this_session, total_chars, ch_idx, reached_end, request_reopen


def download_all_images(raw_dir, img_dir):
    os.makedirs(img_dir, exist_ok=True)
    tasks = []
    for jf in sorted(os.listdir(raw_dir)):
        if jf.endswith(".json"):
            for img in json.load(open(os.path.join(raw_dir, jf))).get("images", []):
                tasks.append((img["url"], img["file"]))
    if not tasks:
        print("  (无图片)"); return 0
    print(f"\n  下载 {len(tasks)} 张图片...")
    ok = 0
    for url, fname in tasks:
        fp = os.path.join(img_dir, fname)
        if os.path.exists(fp) and os.path.getsize(fp) > 1000:
            ok += 1; continue
        try:
            req = urllib.request.Request(url, headers={
                "Referer": "https://weread.qq.com/", "User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=20).read()
            if len(raw) > 500:
                open(fp, "wb").write(raw); ok += 1
                if ok % 20 == 0:
                    print(f"    {ok}/{len(tasks)}...")
        except Exception as e:
            print(f"    ⚠️  {fname} 失败: {e}")
    print(f"  ✅ 图片下载完成 {ok}/{len(tasks)}")
    return ok


def finalize_book_json(book_id, book_title, book_author, book_dir, out_dir):
    """从中间产物组装并写出兼容 JSON；返回写出路径。"""
    pairs = load_chapters_from_export_dir(book_dir)
    if not pairs:
        raise RuntimeError(f"未找到可写出的章节：{book_dir}/chapters")
    book = build_book_json_from_chapter_mds(
        book_id=book_id,
        title=book_title or book_id,
        author=book_author or "",
        chapter_files=pairs,
    )
    path = write_book_json(out_dir, book)
    return path, book


async def export_one_book(
    book_id,
    *,
    out_dir=DEFAULT_BOOKS_DIR,
    force=False,
    download_images=False,
    shelf_title="",
    shelf_author="",
    headless=False,
    reader_width=None,
    reader_height=None,
    force_single_page=None,
    prefer_largest=None,
):
    """导出单本：中间产物写 output/<id>，成功后写 out_dir JSON。

    返回 ("skipped"|"ok", message)
    """
    out_dir = Path(out_dir)
    if not force and book_json_exists(out_dir, book_id):
        msg = f"已存在 JSON，跳过 {book_id}（使用 --force 可重导）"
        print(f"  ⏭  {msg}")
        return "skipped", msg

    print("=" * 60)
    print("  weread-exporter — 精确图文导出 v3 + JSON")
    print("=" * 60)
    started_at = time.monotonic()
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    book_dir = os.path.join("output", book_id)
    md_dir = os.path.join(book_dir, "chapters")
    raw_dir = os.path.join(book_dir, "raw")
    img_dir = os.path.join(book_dir, "images")
    for d in (md_dir, raw_dir, img_dir):
        os.makedirs(d, exist_ok=True)

    seen_imgs = set()
    for jf in os.listdir(raw_dir):
        if jf.endswith(".json"):
            for img in json.load(open(os.path.join(raw_dir, jf))).get("images", []):
                seen_imgs.add(img["url"])

    catalog_path = os.path.join(book_dir, "_catalog.json")
    book_title = shelf_title or ""
    book_author = shelf_author or ""
    session = 0
    stall_reopen_count = 0
    MAX_STALL_REOPENS = 6
    while True:
        session += 1
        last_title, last_idx = get_last_chapter_title(md_dir)
        start_idx = last_idx + 1 if last_idx > 0 else 1
        # 已落盘目录最后一章，或其后仅剩封底等无正文项：视为全书完成
        pre_catalog = load_catalog_titles(catalog_path) if os.path.isfile(catalog_path) else []
        if last_title and pre_catalog and is_export_complete_after(last_title, pre_catalog):
            print(f"\n--- 会话 {session} ---")
            if is_last_catalog_chapter(last_title, pre_catalog):
                print(
                    f"  上次已是目录末章「{last_title[:32]}」(编号 {last_idx})，"
                    f"全书导出完成。"
                )
            else:
                rest = catalog_titles_after(pre_catalog, last_title)
                rest_hint = "、".join((t[:12] for t in rest[:3])) if rest else ""
                print(
                    f"  上次「{last_title[:32]}」(编号 {last_idx}) 后仅剩"
                    f"非正文目录项（{rest_hint}），全书导出完成。"
                )
            break
        print(f"\n--- 会话 {session} ---")
        print(f"  上次: {last_title or '(无)'}, 编号: {last_idx}")
        goto_first = (session == 1 and last_idx == 0)
        title, author, added, chars_added, end_idx, reached_end, need_reopen = await run_session(
            book_id, md_dir, raw_dir, start_idx, seen_imgs,
            goto_first=goto_first, catalog_path=catalog_path, headless=headless,
            reader_width=reader_width, reader_height=reader_height,
            force_single_page=force_single_page,
            prefer_largest=prefer_largest)
        if title:
            book_title = title
        if author:
            book_author = author
        print(f"\n  本次: +{added} 章, +{chars_added:,} 字")
        if reached_end:
            print("\n  ✅ 已到全书最后一章，导出完成。")
            break
        if need_reopen:
            # 重开前再读一次末章：若已是目录最后一项，勿再空转重开
            check_title, _check_idx = get_last_chapter_title(md_dir)
            check_cat = load_catalog_titles(catalog_path) if os.path.isfile(catalog_path) else []
            if check_title and check_cat and is_export_complete_after(check_title, check_cat):
                print(
                    f"\n  ✅ 已导出「{check_title[:32]}」且无更多正文目录项，"
                    f"停止重开，全书完成。"
                )
                break
            stall_reopen_count += 1
            if stall_reopen_count > MAX_STALL_REOPENS:
                raise RuntimeError(
                    f"连续重开阅读器仍无法前进（{stall_reopen_count} 次）：{book_id}。"
                    "请检查登录态/目录，或删除最近异常章节后重试续传。"
                )
            print(
                f"  会话请求重开续传"
                f"（{stall_reopen_count}/{MAX_STALL_REOPENS}），"
                f"{SLEEP_READER_REOPEN} 秒后重新打开电子书..."
            )
            await asyncio.sleep(SLEEP_READER_REOPEN)
            continue
        if added == 0:
            # 若已有章节产物则视为完成（续传场景）
            if any(fn.endswith(".md") for fn in os.listdir(md_dir)):
                print("\n  无新章节，按已有产物收尾。")
                break
            raise RuntimeError(f"未能导出任何章节：{book_id}")
        # 有进度但未到末尾：正常重开会话续传
        stall_reopen_count = 0
        print(f"  {SLEEP_READER_REOPEN} 秒后自动重开继续...")
        await asyncio.sleep(SLEEP_READER_REOPEN)

    if download_images:
        download_all_images(raw_dir, img_dir)
    else:
        print("  （跳过图片下载；需要时加 --download-images）")

    total_files = sorted(f for f in os.listdir(md_dir) if f.endswith(".md"))
    if not total_files:
        raise RuntimeError(f"章节目录为空，无法写 JSON：{md_dir}")
    if not book_title:
        book_title = book_id

    # 仍写出合并 md，便于人工预览
    safe = re.sub(r'[<>:"/\\|?*]', '_', book_title)
    merged = os.path.join("output", f"{safe}.md")
    with open(merged, "w", encoding="utf-8") as out:
        out.write(f"# {book_title}\n\n**{book_author}**\n\n---\n\n")
        for fn in total_files:
            out.write(open(os.path.join(md_dir, fn), encoding="utf-8").read())
            out.write("\n\n---\n\n")

    json_path, book = finalize_book_json(
        book_id, book_title, book_author, book_dir, out_dir)
    img_count = len([f for f in os.listdir(img_dir) if not f.startswith(".")]) if os.path.isdir(img_dir) else 0
    elapsed = time.monotonic() - started_at
    word_count = int(book.get("word_count") or 0)
    print(f"\n{'=' * 60}")
    print(f"  ✅ 全书导出完成!  📖 {book_title} — {book_author}")
    print(f"  📄 {len(total_files)} 章, word_count={word_count},  🖼 {img_count} 张图")
    print(f"  📝 共 {word_count:,} 字，耗时 {format_elapsed(elapsed)}")
    print(f"  📦 md: {merged}")
    print(f"  📦 json: {json_path}")
    print(f"{'=' * 60}")
    return "ok", str(json_path)


async def export_batch(
    *,
    list_path=DEFAULT_NEW_BOOKS,
    out_dir=DEFAULT_BOOKS_DIR,
    force=False,
    download_images=False,
    book_interval=None,
    headless=False,
    reader_width=None,
    reader_height=None,
    force_single_page=None,
    prefer_largest=None,
):
    """批量导出 new_books：跳过已存在；失败即停。"""
    out_dir = Path(out_dir)
    list_path = Path(list_path)
    interval = SLEEP_BOOK_INTERVAL if book_interval is None else float(book_interval)
    books = iter_batch_book_ids(list_path)
    if not books:
        print(f"  清单为空或不存在：{list_path}")
        return 0
    books, forbidden = filter_forbidden_books(books)
    if forbidden:
        print(f"  禁止列表跳过 {len(forbidden)} 本：")
        for book_id, title, author in forbidden:
            label = f"{title} — {author}".strip(" —")
            if label:
                print(f"    - {book_id}  {label}")
            else:
                print(f"    - {book_id}")
    pending = books if force else filter_pending_books(books, out_dir)
    print(f"  清单 {len(books)} 本（已剔除禁止），待处理 {len(pending)} 本 → {out_dir}")
    if not pending:
        print("  没有待导出的书。")
        return 0

    for i, (book_id, title, author) in enumerate(pending):
        print(f"\n##### 批量 {i + 1}/{len(pending)}  {book_id}  {title} — {author}")
        status, msg = await export_one_book(
            book_id,
            out_dir=out_dir,
            force=force,
            download_images=download_images,
            shelf_title=title,
            shelf_author=author,
            headless=headless,
            reader_width=reader_width,
            reader_height=reader_height,
            force_single_page=force_single_page,
            prefer_largest=prefer_largest,
        )
        if status == "ok" and i < len(pending) - 1:
            print(f"  书间等待 {interval:.0f}s ...")
            await asyncio.sleep(interval)
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="微信读书导出为兼容 dedao/json 的书稿 JSON（并保留 md 中间产物）",
    )
    parser.add_argument(
        "book",
        nargs="?",
        default=None,
        help="book_id 或 weread reader URL；省略则批量处理 data/new_books.txt",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使默认 books 目录已存在同 id 的 JSON 也强制重导",
    )
    parser.add_argument(
        "--download-images",
        action="store_true",
        help="导出结束后下载插图（默认不下载）",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_BOOKS_DIR),
        help=f"JSON 输出目录（默认 {DEFAULT_BOOKS_DIR})",
    )
    parser.add_argument(
        "--list",
        dest="list_path",
        default=str(DEFAULT_NEW_BOOKS),
        help=f"批量清单路径（默认 {DEFAULT_NEW_BOOKS}）",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="无头模式（仅当 cache/browser_profile 已有登录信息时生效；否则回退有头）",
    )
    parser.add_argument(
        "--reader-width",
        type=int,
        default=READER_VIEWPORT_WIDTH,
        help=(
            f"阅读器视口宽度（默认 {READER_VIEWPORT_WIDTH}，"
            "0=自动匹配本机屏幕宽度；来自 .env READER_VIEWPORT_WIDTH）"
        ),
    )
    parser.add_argument(
        "--reader-height",
        type=int,
        default=READER_VIEWPORT_HEIGHT,
        help=(
            f"阅读器视口高度（默认 {READER_VIEWPORT_HEIGHT}，"
            "0=自动匹配本机屏幕高度；来自 .env READER_VIEWPORT_HEIGHT）"
        ),
    )
    parser.add_argument(
        "--force-single-page",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "双页布局时是否收窄视口强制单页；默认读取 "
            f".env READER_FORCE_SINGLE_PAGE={int(bool(READER_FORCE_SINGLE_PAGE))}"
        ),
    )
    parser.add_argument(
        "--prefer-largest-screen",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "多显示器时是否优先外接大屏（--prefer-largest-screen）"
            "或笔记本内建屏（--no-prefer-largest-screen）；必须显式指定"
        ),
    )
    args = parser.parse_args(argv)
    if args.prefer_largest_screen is None:
        parser.error(
            "必须指定 --prefer-largest-screen 或 --no-prefer-largest-screen"
        )
    return args


def resolve_book_id(raw: str) -> str:
    text = (raw or "").strip().rstrip("/")
    if not text:
        raise ValueError("empty book id")
    if "weread.qq.com" in text:
        return text.split("/")[-1]
    return text


async def async_main(argv=None):
    args = parse_args(argv)
    headless = resolve_headless(args.headless)
    if args.headless and headless:
        print("  🕶️  无头模式已启用（检测到 cache 登录信息）")
    force_single_page = (
        READER_FORCE_SINGLE_PAGE
        if args.force_single_page is None
        else bool(args.force_single_page)
    )
    prefer_largest = bool(args.prefer_largest_screen)
    print(
        "  🖥️  目标屏幕: "
        + ("优先外接大屏" if prefer_largest else "优先笔记本内建屏")
    )
    if args.book:
        book_id = resolve_book_id(args.book)
        print(f"  Book ID: {book_id}")
        status, msg = await export_one_book(
            book_id,
            out_dir=args.out_dir,
            force=args.force,
            download_images=args.download_images,
            headless=headless,
            reader_width=args.reader_width,
            reader_height=args.reader_height,
            force_single_page=force_single_page,
            prefer_largest=prefer_largest,
        )
        if status == "skipped":
            return 0
        return 0
    # batch
    await export_batch(
        list_path=args.list_path,
        out_dir=args.out_dir,
        force=args.force,
        download_images=args.download_images,
        headless=headless,
        reader_width=args.reader_width,
        reader_height=args.reader_height,
        force_single_page=force_single_page,
        prefer_largest=prefer_largest,
    )
    return 0


def main(argv=None):
    try:
        return asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except Exception as e:
        print(f"\n❌ 导出失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
