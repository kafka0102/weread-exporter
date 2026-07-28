#!/usr/bin/env python3
"""从本地 .env / 进程环境变量读取运行时配置。

网页操作相关的 sleep 间隔统一经本模块读取，便于在 .env 中调整节奏、
降低被风控的风险。变量清单见 AGENTS.md 与 .env。
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = ROOT_DIR / ".env"


def load_dotenv(path: str | Path | None = None, *, override: bool = False) -> Path | None:
    """解析 KEY=VALUE 行写入 os.environ。默认不覆盖已有环境变量。

    支持空行与 # 注释；值两侧的单/双引号会被去掉。文件不存在时静默跳过。
    """
    env_path = Path(path) if path is not None else DEFAULT_ENV_PATH
    if not env_path.is_file():
        return None
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
    return env_path


def env_float(name: str, default: float) -> float:
    """读取浮点环境变量；缺失或非法时回退 default。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


def env_int(name: str, default: int) -> int:
    """读取整数环境变量；缺失或非法时回退 default。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return int(default)
    try:
        return int(float(raw))
    except ValueError:
        return int(default)


def env_bool(name: str, default: bool = False) -> bool:
    """读取布尔环境变量；支持 1/true/yes/on 与 0/false/no/off（大小写不敏感）。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def env_path(name: str, default: str | Path) -> Path:
    """读取路径环境变量；支持 ~ 展开；缺失或空白时回退 default。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        raw = default
    return Path(str(raw)).expanduser()


# 模块导入即加载项目根 .env（已存在的环境变量优先）
load_dotenv()


# --- 导出路径 ---
# 兼容 JSON 书稿默认输出目录（export_precise.py --out-dir 可覆盖）
DEFAULT_BOOKS_DIR_RAW = "~/data/weixin/books"
BOOKS_DIR = env_path("BOOKS_DIR", DEFAULT_BOOKS_DIR_RAW)

# 阅读器视口：0 表示自动匹配本机「最大单块屏幕」可用逻辑像素。
# 多显示器时不会使用虚拟桌面并集（避免窗口又高又怪、落在笔记本小屏上）。
# 若需要旧版单页策略，可开启 READER_FORCE_SINGLE_PAGE。
# 宽/高任一为 0 时，在 resolve_reader_viewport() 中用 detect_host_screen_size() 补齐。
READER_VIEWPORT_WIDTH = env_int("READER_VIEWPORT_WIDTH", 0)
READER_VIEWPORT_HEIGHT = env_int("READER_VIEWPORT_HEIGHT", 0)
READER_FORCE_SINGLE_PAGE = env_bool("READER_FORCE_SINGLE_PAGE", False)

# 无法探测本机屏幕时的兜底视口
_FALLBACK_SCREEN_WIDTH = 1200
_FALLBACK_SCREEN_HEIGHT = 900

# 外接大屏时默认视口上限：足够导出稳定，又不会整屏铺满外接显示器。
# 0=不限制（用满最大单屏）。可用 CLI --reader-width/--reader-height 覆盖。
READER_VIEWPORT_MAX_WIDTH = env_int("READER_VIEWPORT_MAX_WIDTH", 1600)
READER_VIEWPORT_MAX_HEIGHT = env_int("READER_VIEWPORT_MAX_HEIGHT", 1000)


def _screen_dict(
    *,
    left: int,
    top: int,
    width: int,
    height: int,
    visible_left: int | None = None,
    visible_top: int | None = None,
    visible_width: int | None = None,
    visible_height: int | None = None,
) -> dict[str, int]:
    """规范化单块屏幕几何（逻辑像素，原点在主屏左上，y 向下）。"""
    w = max(0, int(width))
    h = max(0, int(height))
    vw = max(0, int(visible_width if visible_width is not None else w))
    vh = max(0, int(visible_height if visible_height is not None else h))
    return {
        "left": int(left),
        "top": int(top),
        "width": w,
        "height": h,
        "visible_left": int(visible_left if visible_left is not None else left),
        "visible_top": int(visible_top if visible_top is not None else top),
        "visible_width": vw if vw >= 800 else w,
        "visible_height": vh if vh >= 500 else h,
    }


def _parse_ns_screens(raw: str) -> list[dict[str, int]]:
    """解析 NSScreen 探测脚本输出。

    格式：left,top,widthxheight|vis:vleft,vtop,vwidthxvheight;...
    坐标已是主屏左上原点、y 向下的 CSS/逻辑像素。
    """
    screens: list[dict[str, int]] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        m = re.match(
            r"(-?\d+),(-?\d+),(\d+)x(\d+)\|vis:(-?\d+),(-?\d+),(\d+)x(\d+)$",
            part,
        )
        if not m:
            continue
        screens.append(
            _screen_dict(
                left=int(m.group(1)),
                top=int(m.group(2)),
                width=int(m.group(3)),
                height=int(m.group(4)),
                visible_left=int(m.group(5)),
                visible_top=int(m.group(6)),
                visible_width=int(m.group(7)),
                visible_height=int(m.group(8)),
            )
        )
    return screens


_NS_SCREEN_SCRIPT = """
use framework "AppKit"
use scripting additions
set arr to current application's NSScreen's screens()
if (count of arr) is 0 then return ""
set mainH to 0
try
  set mf to current application's NSScreen's mainScreen()'s frame()
  set mainH to ((item 2 of (item 2 of mf)) as number)
end try
set out to {}
repeat with s in arr
  set f to s's frame()
  set o to item 1 of f
  set sz to item 2 of f
  set ox to (item 1 of o) as number
  set oy to (item 2 of o) as number
  set w to (item 1 of sz) as number
  set h to (item 2 of sz) as number
  set topY to mainH - (oy + h)
  set vf to s's visibleFrame()
  set vo to item 1 of vf
  set vsz to item 2 of vf
  set vox to (item 1 of vo) as number
  set voy to (item 2 of vo) as number
  set vw to (item 1 of vsz) as number
  set vh to (item 2 of vsz) as number
  set vTop to mainH - (voy + vh)
  set entry to ((ox as integer as text) & "," & (topY as integer as text) & "," & (w as integer as text) & "x" & (h as integer as text) & "|vis:" & (vox as integer as text) & "," & (vTop as integer as text) & "," & (vw as integer as text) & "x" & (vh as integer as text))
  set end of out to entry
end repeat
set AppleScript's text item delimiters to ";"
return out as text
"""


_HOST_SCREENS_CACHE: tuple[float, list[dict[str, int]]] | None = None
_HOST_SCREENS_TTL_SEC = 30.0


def detect_host_screens(*, force_refresh: bool = False) -> list[dict[str, int]]:
    """探测本机各块物理屏幕的逻辑像素几何。

    macOS：NSScreen.screens（逐屏，含位置），避免 Finder desktop bounds
    在多显示器下返回虚拟桌面并集（宽/高被夸大）。
    失败时尽量退回单屏探测。结果短缓存，避免频繁 osascript。
    """
    global _HOST_SCREENS_CACHE
    now = time.monotonic()
    if (
        not force_refresh
        and _HOST_SCREENS_CACHE is not None
        and (now - _HOST_SCREENS_CACHE[0]) < _HOST_SCREENS_TTL_SEC
    ):
        return [dict(s) for s in _HOST_SCREENS_CACHE[1]]

    screens: list[dict[str, int]] = []
    try:
        out = subprocess.check_output(
            ["osascript", "-e", _NS_SCREEN_SCRIPT],
            text=True,
            timeout=8,
            stderr=subprocess.DEVNULL,
        ).strip()
        parsed = _parse_ns_screens(out)
        screens = [s for s in parsed if s["width"] >= 800 and s["height"] >= 500]
    except Exception:
        screens = []

    # macOS / 通用兜底：Finder desktop bounds 在单屏时可用；多屏是并集，仅作最后手段
    if not screens:
        try:
            out = subprocess.check_output(
                [
                    "osascript",
                    "-e",
                    'tell application "Finder" to get bounds of window of desktop',
                ],
                text=True,
                timeout=5,
                stderr=subprocess.DEVNULL,
            ).strip()
            nums = [int(x) for x in re.findall(r"-?\d+", out)]
            if len(nums) >= 4:
                left, top, right, bottom = nums[0], nums[1], nums[2], nums[3]
                w = right - left
                h = bottom - top
                if w >= 800 and h >= 600:
                    screens = [_screen_dict(left=left, top=top, width=w, height=h)]
        except Exception:
            pass

    # Linux: xdpyinfo（多为虚拟桌面尺寸；多屏场景精度有限）
    if not screens:
        try:
            out = subprocess.check_output(
                ["xdpyinfo"],
                text=True,
                timeout=5,
                stderr=subprocess.DEVNULL,
            )
            m = re.search(r"dimensions:\s*(\d+)x(\d+)", out)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                if w >= 800 and h >= 600:
                    screens = [_screen_dict(left=0, top=0, width=w, height=h)]
        except Exception:
            pass

    _HOST_SCREENS_CACHE = (now, [dict(s) for s in screens])
    return [dict(s) for s in screens]


def preferred_host_screen() -> dict[str, int] | None:
    """返回面积最大的单块屏幕；无探测结果时 None。"""
    screens = detect_host_screens()
    if not screens:
        return None
    return max(
        screens,
        key=lambda s: (
            int(s.get("visible_width") or s["width"])
            * int(s.get("visible_height") or s["height"]),
            int(s["width"]) * int(s["height"]),
        ),
    )


def detect_host_screen_size() -> tuple[int, int]:
    """探测本机用于阅读器的目标屏幕可用逻辑像素 (width, height)。

    多显示器时取「面积最大的单块屏幕」可视区域，而不是虚拟桌面并集。
    失败时回退 1200x900。
    """
    best = preferred_host_screen()
    if best is not None:
        w = int(best.get("visible_width") or best["width"])
        h = int(best.get("visible_height") or best["height"])
        if w >= 800 and h >= 500:
            return w, h
    return _FALLBACK_SCREEN_WIDTH, _FALLBACK_SCREEN_HEIGHT


def preferred_window_bounds(
    width: int | None = None,
    height: int | None = None,
    *,
    chrome_w: int = 16,
    chrome_h: int = 96,
) -> dict[str, int]:
    """为浏览器窗口计算 outer bounds（尽量落在最大单屏可视区内）。

    返回 left/top/width/height，可供 CDP Browser.setWindowBounds 使用。
    """
    vp = resolve_reader_viewport(width, height)
    outer_w = max(360, int(vp["width"]) + int(chrome_w))
    outer_h = max(480, int(vp["height"]) + int(chrome_h))
    screen = preferred_host_screen()
    if screen is None:
        return {"left": 0, "top": 0, "width": outer_w, "height": outer_h}

    area_w = int(screen.get("visible_width") or screen["width"])
    area_h = int(screen.get("visible_height") or screen["height"])
    area_left = int(screen.get("visible_left", screen["left"]))
    area_top = int(screen.get("visible_top", screen["top"]))

    # 留一点边，避免贴边/被刘海/Dock 裁切
    margin = 8
    max_w = max(800, area_w - margin * 2)
    max_h = max(500, area_h - margin * 2)
    outer_w = min(outer_w, max_w)
    outer_h = min(outer_h, max_h)
    left = area_left + max(0, (area_w - outer_w) // 2)
    top = area_top + max(margin, (area_h - outer_h) // 2)
    return {
        "left": int(left),
        "top": int(top),
        "width": int(outer_w),
        "height": int(outer_h),
    }


def resolve_reader_viewport(
    width: int | None = None,
    height: int | None = None,
) -> dict[str, int]:
    """解析阅读器视口；宽/高为 0/None 时自动匹配最大单屏。

    显式传入正整数优先生效；模块配置 0 表示 auto。
    auto 时会再按 READER_VIEWPORT_MAX_* 做上限裁剪（默认 1600x1000），
    让外接大屏上的微信读书窗口够大但不至于整屏铺满。
    """
    w = READER_VIEWPORT_WIDTH if width is None else int(width)
    h = READER_VIEWPORT_HEIGHT if height is None else int(height)
    auto_w = w <= 0
    auto_h = h <= 0
    if auto_w or auto_h:
        sw, sh = detect_host_screen_size()
        if auto_w:
            w = sw
        if auto_h:
            h = sh
    # 仅自动探测尺寸时应用上限；用户显式写死宽高时尊重原值
    if auto_w and READER_VIEWPORT_MAX_WIDTH > 0:
        w = min(int(w), int(READER_VIEWPORT_MAX_WIDTH))
    if auto_h and READER_VIEWPORT_MAX_HEIGHT > 0:
        h = min(int(h), int(READER_VIEWPORT_MAX_HEIGHT))
    return {
        "width": max(360, int(w)),
        "height": max(480, int(h)),
    }


# --- 网页操作 sleep（秒）---
# 命名约定：SLEEP_<场景>_<动作>

# 登录会话 weread_session.py
SLEEP_LOGIN_SWITCH_PC = env_float("SLEEP_LOGIN_SWITCH_PC", 1.0)
SLEEP_LOGIN_AFTER_GOTO = env_float("SLEEP_LOGIN_AFTER_GOTO", 3.0)
SLEEP_LOGIN_POLL = env_float("SLEEP_LOGIN_POLL", 2.0)

# 书架抓取 fetch_shelf.py
SLEEP_SHELF_AFTER_LOAD = env_float("SLEEP_SHELF_AFTER_LOAD", 3.0)
SLEEP_SHELF_SCROLL = env_float("SLEEP_SHELF_SCROLL", 3.0)
# 打开下一本缺作者详情前的间隔（默认 5s）
SLEEP_BOOK_DETAIL_INTERVAL = env_float("SLEEP_BOOK_DETAIL_INTERVAL", 5.0)
# 打开详情/阅读器后等待页面就绪
SLEEP_BOOK_DETAIL_LOAD = env_float("SLEEP_BOOK_DETAIL_LOAD", 3.0)
# 详情页点开目录面板后的等待
SLEEP_BOOK_DETAIL_CATALOG = env_float("SLEEP_BOOK_DETAIL_CATALOG", 1.5)

# 阅读器导出 export_precise.py
SLEEP_READER_AFTER_LOAD = env_float("SLEEP_READER_AFTER_LOAD", 5.0)
SLEEP_READER_CATALOG_OPEN = env_float("SLEEP_READER_CATALOG_OPEN", 1.5)
SLEEP_READER_CATALOG_SCROLL = env_float("SLEEP_READER_CATALOG_SCROLL", 1.0)
SLEEP_READER_CATALOG_CLICK = env_float("SLEEP_READER_CATALOG_CLICK", 3.0)
SLEEP_READER_CATALOG_CLOSE = env_float("SLEEP_READER_CATALOG_CLOSE", 2.0)
SLEEP_READER_AFTER_HOOK = env_float("SLEEP_READER_AFTER_HOOK", 0.5)
SLEEP_READER_PAGE_TURN = env_float("SLEEP_READER_PAGE_TURN", 1.0)
SLEEP_READER_PAGE_RENDER = env_float("SLEEP_READER_PAGE_RENDER", 0.3)
SLEEP_READER_REOPEN = env_float("SLEEP_READER_REOPEN", 3.0)
SLEEP_READER_STABLE_POLL = env_float("SLEEP_READER_STABLE_POLL", 0.5)

# 书间 / 章间节奏（批量与导出）
# 批量导出相邻两本书之间的固定间隔
SLEEP_BOOK_INTERVAL = env_float("SLEEP_BOOK_INTERVAL", 60.0)
# 章完成后按字数动态等待：ceil(chars/2000)*PER_2K，再夹到 [MIN, MAX]
SLEEP_CHAPTER_PER_2K_CHARS = env_float("SLEEP_CHAPTER_PER_2K_CHARS", 0.5)
SLEEP_CHAPTER_MIN = env_float("SLEEP_CHAPTER_MIN", 0.3)
SLEEP_CHAPTER_MAX = env_float("SLEEP_CHAPTER_MAX", 2.0)
