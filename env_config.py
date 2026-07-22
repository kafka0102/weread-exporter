#!/usr/bin/env python3
"""从本地 .env / 进程环境变量读取运行时配置。

网页操作相关的 sleep 间隔统一经本模块读取，便于在 .env 中调整节奏、
降低被风控的风险。变量清单见 AGENTS.md 与 .env。
"""
from __future__ import annotations

import os
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
# 章完成后按字数动态等待：ceil(chars/1000)*PER_1K，再夹到 [MIN, MAX]
SLEEP_CHAPTER_PER_1K_CHARS = env_float("SLEEP_CHAPTER_PER_1K_CHARS", 1.0)
SLEEP_CHAPTER_MIN = env_float("SLEEP_CHAPTER_MIN", 1.0)
SLEEP_CHAPTER_MAX = env_float("SLEEP_CHAPTER_MAX", 8.0)

