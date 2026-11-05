"""Resolve code and user workspace independently, without reading credentials.

三个位置分开：

* 代码目录 `CODE_ROOT`：这些工作流脚本所在处（`deliver_round_v2.py` 等）；
* 用户工作区 `workspace_root()`：配置、台账、语料、运行记录；
* 核心库目录 `core_root()`：`boss_core` 包所在处（当前在应用源码根）。

`ensure_core_path()` 把核心库放进 `sys.path`，让 `import boss_core` 在
"应用在 A、工作流脚本在 W" 的布局下也能用。找不到时不静默降级，直接报缺什么。
"""

import json
import os
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent


def workspace_root() -> Path:
    """用户数据根；默认就是脚本所在目录（旧布局）。"""
    return Path(os.environ.get("BOSS_WORKFLOW_DIR") or CODE_ROOT).expanduser().resolve()


def api_base() -> str:
    """控制台地址。"""
    return (os.environ.get("BOSS_API_BASE") or "http://127.0.0.1:8787").rstrip("/")


def install_manifest() -> dict:
    """安装清单：`<workspace>/runs/install.json`，由安装/迁移工具写入。"""
    path = workspace_root() / "runs" / "install.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def core_root() -> Path | None:
    """`boss_core` 所在目录；找不到返回 None。

    顺序：显式环境变量 → 工作流目录自己 → 安装清单 → 常见的同级布局。
    不写死任何用户路径。
    """
    candidates: list[Path] = []
    for name in ("BOSS_CORE_ROOT", "BOSS_APP_SOURCE"):
        raw = os.environ.get(name)
        if raw:
            candidates.append(Path(raw).expanduser())
    manifest = install_manifest()
    for key in ("core_root", "app_source"):
        if manifest.get(key):
            candidates.append(Path(str(manifest[key])).expanduser())
    candidates.append(CODE_ROOT)
    candidates.append(CODE_ROOT.parent)
    candidates.append(CODE_ROOT.parent / "source-tuned")
    for candidate in candidates:
        try:
            if (candidate / "boss_core" / "__init__.py").is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def app_source() -> Path:
    """应用源码根（旧名字，等价于 `core_root()`，找不到时退回脚本目录）。"""
    return core_root() or CODE_ROOT


def ensure_core_path() -> Path:
    """确保 `boss_core` 可导入；返回它所在目录，找不到抛 `RuntimeError`。"""
    root = core_root()
    if root is None:
        raise RuntimeError(
            "找不到 boss_core 核心库：设置 BOSS_CORE_ROOT，或让安装工具写入 "
            "runs/install.json（core_root 指向应用源码目录）")
    text = str(root)
    if text not in sys.path:
        sys.path.insert(0, text)
    return root
