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
    SLEEP_BOOK_INTERVAL,
    SLEEP_CHAPTER_MAX,
    SLEEP_CHAPTER_MIN,
    SLEEP_CHAPTER_PER_1K_CHARS,
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


def reader_viewport(width=None, height=None):
    """导出用阅读器视口。默认使用桌面宽度，避免微信读书进入窄屏排版。"""
    w = max(360, int(width if width is not None else (READER_VIEWPORT_WIDTH or 1200)))
    h = max(480, int(height if height is not None else (READER_VIEWPORT_HEIGHT or 900)))
    return {"width": w, "height": h}


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
    """确保页面使用配置的阅读器视口；有头窗口缩小时强制 set_viewport_size。"""
    desired = {
        "width": int(viewport["width"]),
        "height": int(viewport["height"]),
    }
    actual = await page_viewport(page, desired)
    if actual == desired:
        return desired

    print(
        f"  🪟 页面视口: {actual['width']}x{actual['height']} "
        f"（配置 {desired['width']}x{desired['height']}），尝试强制配置值"
    )
    try:
        await page.set_viewport_size(desired)
        await asyncio.sleep(SLEEP_READER_PAGE_RENDER)
        actual = await page_viewport(page, desired)
        if actual == desired:
            print(
                f"  ✅ 已强制视口 {desired['width']}x{desired['height']}"
            )
            return desired
        print(
            f"  ⚠️  强制后视口仍为 {actual['width']}x{actual['height']}，"
            f"继续按配置 {desired['width']}x{desired['height']} 处理"
        )
        return desired
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


async def recover_reader_text_after_nav(page) -> int:
    """目录跳转/重定位后尽量保住 canvas 文字，禁止先 reset 再空等。

    历史 bug：跳转等待期间 fillText 已写入 __wr_chars，随后 __wr_reset
    一把清掉，再靠微扰动很难重绘 → 页面明明有字、日志一直 stale。
    返回稳定后的字符计数（可能为 0）。
    """
    # 1) 先等已有绘制落稳（不 reset）
    count = await wait_stable(page, 0, timeout=2.5)
    if count and count > 0:
        return count
    # 2) 扰动重绘，仍不 reset
    await force_reader_repaint(page)
    count = await wait_stable(page, 0, timeout=2.0)
    if count and count > 0:
        return count
    # 3) 左右键轻推一页再回到当前，迫使重新 fillText
    try:
        await dismiss_reader_search(page)
        await focus_reader_for_keyboard(page)
        await blur_reader_inputs(page)
        await page.keyboard.press("ArrowLeft")
        await asyncio.sleep(max(0.15, float(SLEEP_READER_PAGE_TURN) * 0.5))
        await page.evaluate("() => window.__wr_reset && window.__wr_reset()")
        await page.keyboard.press("ArrowRight")
        await asyncio.sleep(max(0.2, float(SLEEP_READER_PAGE_TURN)))
        await force_reader_repaint(page)
        count = await wait_stable(page, 0, timeout=3.0)
        if count and count > 0:
            return count
    except Exception:
        pass
    return int(count or 0)


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
    """
    vp = dict(viewport)
    n = await count_reader_canvases(page)
    if n <= 1:
        if n == 1:
            print("  📄 阅读布局: 单页")
        return vp

    if not force_single_page:
        print(f"  📖 阅读布局: 双页（canvas={n}），保持桌面排版并按 canvas 拆页")
        return vp

    print(f"  ⚠️  检测到双页布局（canvas={n}），尝试收窄视口强制单页…")
    for w in (720, 640, 560, 480):
        if w >= int(vp.get("width") or 0):
            continue
        vp = {"width": w, "height": int(vp.get("height") or 900)}
        await page.set_viewport_size(vp)
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
    return vp


CANVAS_HOOK = r"""
(function() {
    window.__wr_chars = [];
    var origFill = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y) {
        if (text && String(text).trim()) {
            var cl = 0, ct = 0, s = null;
            try {
                var canvas = this.canvas;
                if (canvas && canvas.getBoundingClientRect) {
                    var r = canvas.getBoundingClientRect();
                    cl = Math.round(r.left);
                    ct = Math.round(r.top);
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
                s: s
            });
        }
        return origFill.apply(this, arguments);
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
# 标题前缀后若接这些成分，视为正文提及而非新章起始
_NOT_CHAPTER_START_REST = re.compile(
    r"^(的|与|和|在|是|了|也|都|就|还|曾|并|便|则|却|又|已|将|会|能|要|"
    r"把|被|让|从|向|对|比|因|而|但|曾经|这首|早在|不过|与他|便是)"
)


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

    极短标题（≤2 字压缩键，如「云」「雪」「雁」）只允许整行精确/压缩全等，
    禁止前缀命中正文「云破月来花弄影」等，否则会窜到目录后部短章名。
    """
    t = (text or "").strip()
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
    """
    title = (chapter_title or "").strip()
    if not title or not blocks:
        return list(blocks or []), []
    for i, b in enumerate(blocks):
        if b.get("type") != "text":
            continue
        if is_chapter_start_text(b.get("text") or "", title):
            return blocks[:i], blocks[i:]
    return list(blocks), []


# 目录项 textContent 偶发粘上阅读进度，如「王国维当前读到 99%」
_CATALOG_PROGRESS_RE = re.compile(
    r"(当前读到|已读到|读到)\s*\d+\s*%?\s*$"
)
_CATALOG_PERCENT_RE = re.compile(r"\s*\d+\s*%\s*$")


def normalize_catalog_title(text: str) -> str:
    """清洗目录/顶栏章名：去进度文案与首尾空白。"""
    s = (text or "").strip()
    if not s:
        return ""
    s = _CATALOG_PROGRESS_RE.sub("", s).strip()
    s = _CATALOG_PERCENT_RE.sub("", s).strip()
    # 仅剩 # 之类无意义标记时视为空
    if s in {"#", "·", "-", "—"}:
        return ""
    return s


def compact_title_key(text: str) -> str:
    """用于标题比对的压缩键：去空白，降低「沈佺期 三首」vs「沈佺期三首」漏切。"""
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


def group_chars_by_canvas(chars):
    """按 canvas 屏幕 left(cl) 把字符分到各页，从左到右返回。

    fillText 的 x/y 是 canvas 局部坐标；双页时左右页 y 区间重叠，
    若不按 canvas 拆开再分行，会把左右页同一 y 的字交错拼成乱码。
    """
    if not chars:
        return []
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
            # 无 cl 的旧数据：并入左页，避免丢字
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
    pages = split_spread(chars)

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
    """
    return await page.evaluate(
        """() => {
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
                if (t) return t;
            }
            // 目录仍打开时，取选中项
            const active = document.querySelector(
                '.readerCatalog_list_item_selected, .readerCatalog_list_item.selected, .readerCatalog_list_item.isActive, [class*="readerCatalog_list_item"][class*="selected"], [class*="readerCatalog_list_item"][class*="active"]'
            );
            const at = (active?.textContent || '').trim();
            return at || '';
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


def save_chapter(ch_title, blocks, ch_idx, md_dir, raw_dir):
    title = display_chapter_title(ch_title, ch_idx)
    body, img_records = render_chapter_md(title, blocks, ch_idx)
    text_len = sum(len(b["text"]) for b in blocks if b["type"] == "text")
    if text_len == 0 and not img_records:
        return 0, []
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
                const compact = (s) => String(s || '').replace(/\s+/g, '');
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


async def goto_catalog_chapter(page, target_title: str) -> str:
    """打开目录并点击目标章名；成功返回实际点到的清洗标题，失败返回空串。

    用于键盘翻页失效/同页空转时，强制跳到目录中的下一章，打破死循环。
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

        for _ in range(60):
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
                    for _ in range(3):
                        if not await is_reader_catalog_open(page):
                            break
                        await close_reader_catalog(page)
                    return hit or target

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
                      force_single_page=None):
    reached_end = False
    catalog_titles = load_catalog_titles(catalog_path) if catalog_path else []
    last_cat_title = catalog_titles[-1] if catalog_titles else ""
    async with async_playwright() as p:
        viewport = reader_viewport(reader_width, reader_height)
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
            return "", "", 0, 0, start_idx, False

        page = await ctx.new_page()
        await page.add_init_script(CANVAS_HOOK)
        print("\n  打开阅读器...")
        await page.goto(f"https://weread.qq.com/web/reader/{book_id}",
                        wait_until="networkidle", timeout=30000)
        await asyncio.sleep(SLEEP_READER_AFTER_LOAD)
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
            nxt = next_catalog_title(catalog_titles, anchor) if anchor else None
            if nxt:
                cur_idx = catalog_index(catalog_titles, current_chapter or "")
                nxt_idx = catalog_index(catalog_titles, nxt)
                if cur_idx is None or nxt_idx is None or abs(cur_idx - nxt_idx) > 1:
                    print(
                        f"  … 续传定位：目录跳到「{nxt[:32]}」"
                        f"（接在「{(anchor or '')[:24]}」后）"
                    )
                    jumped = await goto_catalog_chapter(page, nxt)
                    if jumped:
                        current_chapter = resolve_chapter_title(
                            jumped, catalog_titles) or jumped
                    for _ in range(3):
                        await close_reader_catalog(page)
                        if not await is_reader_catalog_open(page):
                            break
                    await blur_reader_inputs(page)
                    await focus_reader_for_keyboard(page)
                    await recover_reader_text_after_nav(page)
        print(f"  📖 {book_title} — {book_author}")
        print(f"  会话开始:「{current_chapter}」")
        ch_idx = start_idx
        ch_blocks = []
        total_chars = total_imgs = 0
        chapters_this_session = 0
        stale = 0
        page_num = 0
        # 会话内已落盘章节指纹；重复则说明切章回退/停滞
        saved_chapter_fps: set[str] = set()
        dup_chapter_hits = [0]
        MAX_DUP_CHAPTER_HITS = 5
        # 仅与「上一页」去重。全局行/页集合会把双页预读到的后续正文永久吞掉，
        # 表现为：浏览器其实在翻页，日志却一直 stale（山居秋暝）。
        last_page_fp = ""
        last_page_lines: set[str] = set()
        # 优先方向键；停滞时再点阅读区中右（避开右侧工具条/搜索）
        turn_methods = ("arrow", "arrow2", "click_midright")
        turn_method_idx = 0
        catalog_jump_count = 0
        catalog_jump_failures = 0
        MAX_CATALOG_JUMPS = 8
        MAX_CATALOG_JUMP_FAILURES = 3
        # 顶栏已前进但当前章仍无正文时，优先目录回跳重抓，避免空跟章丢篇
        empty_header_resync = 0
        MAX_EMPTY_HEADER_RESYNC = 2

        def reset_page_dedupe():
            """换章或目录跳转后清空页级去重状态。"""
            nonlocal last_page_fp, last_page_lines
            last_page_fp = ""
            last_page_lines = set()

        async def capture_current_page():
            """抓当前页的有序块，累加到 ch_blocks；返回是否有新内容。

            去重：
            - 整页指纹与上一页相同 → 丢弃
            - 文本行若出现在「上一页」→ 跳过（双页重叠半页）
            - 不用会话级全局行集合
            """
            nonlocal ch_blocks, last_page_fp, last_page_lines
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
                    if (
                        ch_blocks
                        and ch_blocks[-1].get("type") == "text"
                        and (ch_blocks[-1].get("text") or "").strip() == t
                    ):
                        continue
                    ch_blocks.append({"type": "text", "text": t})
                    added += 1
                else:
                    ch_blocks.append(b)
                    added += 1

            if added > 0:
                if page_fp:
                    last_page_fp = page_fp
                last_page_lines = page_line_set
                return True
            if page_fp:
                last_page_fp = page_fp
            return False

        async def commit_chapter(title, blocks, *, note_suffix=""):
            """落盘一章并累计统计；返回 (text_len, imgs)。

            若同一章同一正文指纹反复出现，视为切章死循环并中止，
            避免像「王维」小节那样成千上万次重复落盘。
            """
            nonlocal total_chars, total_imgs, chapters_this_session, reached_end
            fp = chapter_blocks_fingerprint(title, blocks)
            if fp in saved_chapter_fps:
                dup_chapter_hits[0] += 1
                print(
                    f"  ⚠️  重复章节内容「{(title or '')[:32]}」"
                    f"（{dup_chapter_hits[0]}/{MAX_DUP_CHAPTER_HITS}），跳过落盘"
                )
                if dup_chapter_hits[0] >= MAX_DUP_CHAPTER_HITS:
                    raise RuntimeError(
                        "章节进度疑似死循环：相同标题与正文反复出现。"
                        "常见原因是顶栏停在卷/作者名而内容切章已前进，"
                        "随后又被顶栏回退。请更新导出逻辑后清理对应 "
                        f"output 目录重试。"
                    )
                return 0, []
            n, imgs = save_chapter(title, blocks, ch_idx, md_dir, raw_dir)
            if not chapter_saved(n, imgs):
                return n, imgs
            saved_chapter_fps.add(fp)
            total_chars += n
            total_imgs += len(imgs)
            note = f" +{len(imgs)}图" if imgs else ""
            note += note_suffix
            print(f"  [{ch_idx:4d}] {title[:32]:32s} {n:6d}字 ({page_num}页){note}")
            chapters_this_session += 1
            return n, imgs

        async def sleep_between_chapters(n_chars):
            # 0 字（空切章/重复跳过）不等待，避免刷屏空等
            if not n_chars or int(n_chars) <= 0:
                return
            wait_s = chapter_sleep_seconds(
                n_chars,
                per_1k=SLEEP_CHAPTER_PER_1K_CHARS,
                min_seconds=SLEEP_CHAPTER_MIN,
                max_seconds=SLEEP_CHAPTER_MAX,
            )
            print(f"    … 章间等待 {wait_s:.1f}s（按 {n_chars} 字）")
            await asyncio.sleep(wait_s)

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
                n, _imgs = await commit_chapter(
                    title_to_save, before, note_suffix=" [内容切章]")
                # 仅实际落盘成功才推进编号，避免 0 字空切制造空洞
                if chapter_saved(n, _imgs):
                    ch_idx += 1
                    if is_last:
                        ch_blocks = after
                        current_chapter = nxt
                        page_num = 0
                        stale = 0
                        reached_end = True
                        return True
                    await sleep_between_chapters(n)
                elif is_last:
                    ch_blocks = after
                    current_chapter = nxt
                    page_num = 0
                    stale = 0
                    reached_end = True
                    return True
            # before 为空：上一章已落盘或本页已属新章，仅把块归属切到下一章
            ch_blocks = after
            current_chapter = nxt
            page_num = 0
            stale = 0
            return True

        # 首页
        await page.evaluate("() => window.__wr_reset()")
        await wait_stable(page, 0)
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
                    if stale in (3, 5, 8):
                        print(
                            f"    … 目录未关闭，跳过翻页键 stale={stale}/8 "
                            f"当前「{(current_chapter or '')[:24]}」"
                        )
                    await dismiss_reader_overlays(page)
                    await page.keyboard.press("Escape")
                    await blur_reader_inputs(page)
                    await asyncio.sleep(0.15)
                    # 绝不在目录仍开时 ArrowRight
                    if stale >= 8 and catalog_titles:
                        # 落到后面的目录跳转逻辑（复用）
                        pass
                    else:
                        continue
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
                await force_reader_repaint(page)
                await page.evaluate("() => window.__wr_reset()")
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
                    ch_blocks = before
                had_text = any(
                    b.get("type") == "text" and (b.get("text") or "").strip()
                    for b in (ch_blocks or [])
                )
                # 当前章还没抓到正文，顶栏却已到下一章：目录回跳重抓，禁止空跟章丢篇
                if (not had_text) and (not after) and catalog_titles:
                    empty_header_resync += 1
                    if empty_header_resync <= MAX_EMPTY_HEADER_RESYNC:
                        print(
                            f"    … 顶栏「{new_chapter[:20]}」超前且「"
                            f"{(current_chapter or '')[:20]}」无正文，"
                            f"目录回跳重抓"
                            f"（{empty_header_resync}/{MAX_EMPTY_HEADER_RESYNC}）"
                        )
                        jumped = await goto_catalog_chapter(page, current_chapter)
                        for _ in range(4):
                            await dismiss_reader_search(page)
                            await close_reader_catalog(page)
                            if not await is_reader_catalog_open(page):
                                break
                            await asyncio.sleep(0.12)
                        await blur_reader_inputs(page)
                        await focus_reader_for_keyboard(page)
                        if jumped:
                            current_chapter = resolve_chapter_title(
                                jumped, catalog_titles) or current_chapter
                        reset_page_dedupe()
                        # 跳转期间已 fillText：切勿先 reset
                        await recover_reader_text_after_nav(page)
                        await capture_current_page()
                        while await split_if_next_chapter_started():
                            if reached_end:
                                break
                        stale = 0
                        turn_method_idx = 0
                        continue
                    print(
                        f"    … 回跳仍无正文，放弃「"
                        f"{(current_chapter or '')[:20]}」并跟到「"
                        f"{new_chapter[:20]}」"
                    )
                    empty_header_resync = 0

                is_last = is_last_catalog_chapter(current_chapter, catalog_titles)
                n, _imgs = await commit_chapter(current_chapter, ch_blocks)
                if chapter_saved(n, _imgs):
                    ch_idx += 1
                ch_blocks = after
                current_chapter = resolve_chapter_title(new_chapter, catalog_titles) or new_chapter
                page_num = 0
                stale = 0
                turn_method_idx = 0
                empty_header_resync = 0
                if not is_last and chapter_saved(n, _imgs):
                    await sleep_between_chapters(n)
                # 空缓冲跟章后，目录落到新章，避免浏览器已更超前
                if not after and catalog_titles:
                    jumped = await goto_catalog_chapter(page, current_chapter)
                    for _ in range(4):
                        await dismiss_reader_search(page)
                        await close_reader_catalog(page)
                        if not await is_reader_catalog_open(page):
                            break
                        await asyncio.sleep(0.12)
                    await blur_reader_inputs(page)
                    await focus_reader_for_keyboard(page)
                    if jumped:
                        current_chapter = resolve_chapter_title(
                            jumped, catalog_titles) or current_chapter
                    reset_page_dedupe()
                    await recover_reader_text_after_nav(page)
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
            if not got_new:
                await force_reader_repaint(page)
                await asyncio.sleep(max(0.08, float(SLEEP_READER_PAGE_RENDER)))
                await wait_stable(page, 0, timeout=1.5)
                got_new = await capture_current_page()
                if not got_new:
                    n = await recover_reader_text_after_nav(page)
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
            if got_new:
                page_num += 1  # 原先只在 split 分支 +1，导致有抓取也无页进度心跳
                stale = 0
                turn_method_idx = 0
                empty_header_resync = 0
                if page_num == 1 or page_num % 2 == 0:
                    n_lines = sum(
                        1 for b in ch_blocks if b.get("type") == "text"
                    )
                    print(
                        f"    … 翻页中 p={page_num} 本章约 {n_lines} 行 "
                        f"「{(current_chapter or '')[:24]}」 key={turn_method}"
                    )
                continue
            if not got_new:
                stale += 1
                # 每轮空转都给短心跳，避免「浏览器在翻、终端像卡死」
                if stale == 1 or stale in (3, 5, 8) or stale % 2 == 0:
                    print(
                        f"    … 翻页无新内容 stale={stale}/8 "
                        f"当前「{(current_chapter or '')[:24]}」 key={turn_method}"
                    )
                if stale >= 2:
                    turn_method_idx += 1

                # 顶栏已明显超前且抓空：提前只跳「下一章」，勿跟远处顶栏
                if stale >= 3 and catalog_titles and new_chapter:
                    h_idx = catalog_index(catalog_titles, new_chapter)
                    c_idx = catalog_index(catalog_titles, current_chapter)
                    if (
                        h_idx is not None
                        and c_idx is not None
                        and h_idx > c_idx + 1
                    ):
                        print(
                            f"    … 顶栏超前「{new_chapter[:20]}」"
                            f"/逻辑「{(current_chapter or '')[:20]}」，"
                            f"提前目录跳下一章"
                        )
                        stale = 8  # 复用下方目录跳转（仅 next）

                # 键盘翻页连续失效：才尝试目录跳章（阈值降低，避免目录卡死）
                if stale >= 8 and catalog_titles:
                    nxt = next_catalog_title(catalog_titles, current_chapter)
                    if not nxt:
                        await commit_chapter(
                            current_chapter, ch_blocks, note_suffix=" [全书末尾]")
                        reached_end = True
                        break
                    if catalog_jump_count >= MAX_CATALOG_JUMPS:
                        raise RuntimeError(
                            "目录强制跳转次数过多，疑似无法前进。"
                            f"当前章「{current_chapter}」，下一章「{nxt}」。"
                        )
                    # 先确保目录不挡着；再尝试跳转。失败则不推进章名，避免跳章。
                    await close_reader_catalog(page)
                    print(
                        f"    … 翻页停滞，目录跳转 →「{nxt[:32]}」"
                        f"（{catalog_jump_count + 1}/{MAX_CATALOG_JUMPS}）"
                    )
                    jumped = await goto_catalog_chapter(page, nxt)
                    catalog_jump_count += 1
                    if not jumped:
                        catalog_jump_failures += 1
                        print(
                            f"    ⚠️  目录跳转失败（"
                            f"{catalog_jump_failures}/{MAX_CATALOG_JUMP_FAILURES}），"
                            f"保持当前「{(current_chapter or '')[:24]}」"
                        )
                        await close_reader_catalog(page)
                        await close_reader_catalog(page)
                        await dismiss_reader_overlays(page)
                        await page.keyboard.press("Escape")
                        # 绝不能 stale=0：否则目录打不开时会永久空转
                        if catalog_jump_failures >= MAX_CATALOG_JUMP_FAILURES:
                            await commit_chapter(
                                current_chapter,
                                ch_blocks,
                                note_suffix=" [目录跳转失败]",
                            )
                            raise RuntimeError(
                                "目录跳转连续失败，停止空转以免丢章。"
                                f"当前章「{current_chapter}」，目标「{nxt}」。"
                                "可重跑续传；若仍失败请检查登录态与目录是否可打开。"
                            )
                        # 略降 stale，再试几轮键/点右缘翻页
                        stale = 5
                        turn_method_idx += 1
                        continue
                    catalog_jump_failures = 0

                    # 跳转成功才落盘当前章并推进
                    n, _imgs = await commit_chapter(
                        current_chapter, ch_blocks, note_suffix=" [目录跳转切章]")
                    if chapter_saved(n, _imgs):
                        ch_idx += 1
                        await sleep_between_chapters(n)
                    ch_blocks = []
                    reset_page_dedupe()
                    current_chapter = resolve_chapter_title(
                        jumped, catalog_titles) or jumped
                    stale = 0
                    page_num = 0
                    turn_method_idx = 0
                    # 跳章后面板常仍开着：硬关多轮，避免后续方向键在目录里乱跳回卷首
                    for _ in range(4):
                        await dismiss_reader_search(page)
                        await close_reader_catalog(page)
                        if not await is_reader_catalog_open(page):
                            break
                        await asyncio.sleep(0.15)
                    await blur_reader_inputs(page)
                    await focus_reader_for_keyboard(page)
                    await recover_reader_text_after_nav(page)
                    await capture_current_page()
                    while await split_if_next_chapter_started():
                        if reached_end:
                            break
                    continue

                if stale >= 8:
                    note = ""
                    if is_last_catalog_chapter(current_chapter, catalog_titles):
                        reached_end = True
                        note = " [全书末尾]"
                    elif not catalog_titles or catalog_index(
                            catalog_titles, current_chapter) is None:
                        reached_end = True
                        note = " [无更多新内容]"
                    elif page_num >= 20:
                        reached_end = True
                        note = " [无更多新内容]"
                    else:
                        note = " [翻页停滞]"
                    await commit_chapter(current_chapter, ch_blocks, note_suffix=note)
                    break

        # reached_end 时把最后一章存下
        if reached_end and ch_blocks:
            n, imgs = save_chapter(current_chapter, ch_blocks, ch_idx, md_dir, raw_dir)
            if n or imgs:
                total_chars += n; total_imgs += len(imgs)
                print(f"  [{ch_idx:4d}] {current_chapter[:32]:32s} {n:6d}字 [末章]")
                chapters_this_session += 1

        await page.close(); await ctx.close()
        return book_title, book_author, chapters_this_session, total_chars, ch_idx, reached_end


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
    while True:
        session += 1
        last_title, last_idx = get_last_chapter_title(md_dir)
        start_idx = last_idx + 1 if last_idx > 0 else 1
        print(f"\n--- 会话 {session} ---")
        print(f"  上次: {last_title or '(无)'}, 编号: {last_idx}")
        goto_first = (session == 1 and last_idx == 0)
        title, author, added, chars_added, end_idx, reached_end = await run_session(
            book_id, md_dir, raw_dir, start_idx, seen_imgs,
            goto_first=goto_first, catalog_path=catalog_path, headless=headless,
            reader_width=reader_width, reader_height=reader_height,
            force_single_page=force_single_page)
        if title:
            book_title = title
        if author:
            book_author = author
        print(f"\n  本次: +{added} 章, +{chars_added:,} 字")
        if reached_end:
            print("\n  ✅ 已到全书最后一章，导出完成。")
            break
        if added == 0:
            # 若已有章节产物则视为完成（续传场景）
            if any(fn.endswith(".md") for fn in os.listdir(md_dir)):
                print("\n  无新章节，按已有产物收尾。")
                break
            raise RuntimeError(f"未能导出任何章节：{book_id}")
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
):
    """批量导出 new_books：跳过已存在；失败即停。"""
    out_dir = Path(out_dir)
    list_path = Path(list_path)
    interval = SLEEP_BOOK_INTERVAL if book_interval is None else float(book_interval)
    books = iter_batch_book_ids(list_path)
    if not books:
        print(f"  清单为空或不存在：{list_path}")
        return 0
    pending = books if force else filter_pending_books(books, out_dir)
    print(f"  清单 {len(books)} 本，待处理 {len(pending)} 本 → {out_dir}")
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
        help=f"阅读器视口宽度（默认 {READER_VIEWPORT_WIDTH}，来自 .env READER_VIEWPORT_WIDTH）",
    )
    parser.add_argument(
        "--reader-height",
        type=int,
        default=READER_VIEWPORT_HEIGHT,
        help=f"阅读器视口高度（默认 {READER_VIEWPORT_HEIGHT}，来自 .env READER_VIEWPORT_HEIGHT）",
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
    return parser.parse_args(argv)


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
