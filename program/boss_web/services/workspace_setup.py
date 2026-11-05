"""工作区配置与持久化：不设环境变量也能把程序跑起来。

职责很小，只做四件事：

1. 解析数据目录（复用 :mod:`boss_web.runtime`，与 `boss.db` 落在同一处）；
2. 给出默认工作区目录 ``<数据目录>/workspace``；
3. 把用户选过的路径写进 ``<数据目录>/launch.json``，下次启动自动生效；
4. 首次配置时用 :class:`boss_core.workspace.Workspace` 建工作区，并设置进程内环境变量。

优先级一句话：**环境变量 > launch.json > 默认目录**。
:func:`apply_saved` 只补空缺（等价于对每个变量做 ``os.environ.setdefault``），
部署 / 测试里显式设过的 ``BOSS_WORKFLOW_DIR`` 不会被悄悄覆盖；:func:`configure`
是用户主动点的按钮，才会覆盖进程内变量。

``launch.json`` 的形状::

    {"workspace": "<绝对路径>", "code": "<绝对路径或空串>", "updated_at": "..."}

它**不是**工作区元信息：简历、方案、审计记录都在 ``workspace.json`` 那边。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any

from .. import runtime

logger = logging.getLogger(__name__)

ENV_WORKSPACE = "BOSS_WORKFLOW_DIR"

ENV_CODE = "BOSS_WORKFLOW_CODE"

LAUNCH_FILE_NAME = "launch.json"

WORKSPACE_DIR_NAME = "workspace"

META_NAME = "workspace.json"

CODE_ENTRY = "deliver_round_v2.py"

#: 阶段脚本：(文件名, 负责什么, 缺了算不算「不可执行」)。
#: 顺序就是出问题的先后：先没代码目录，再缺发送，再缺采集。
STAGE_SCRIPTS = (
    ("deliver_round_v2.py", "写稿与发送（prepare / send / auto_send）", True),
    ("panel_action.py", "采集、匹配、反馈（collect / match / feedback）", True),
    ("collector.py", "常驻采集器（不随面板起，手动或开机自启）", False),
)

#: 清单里固定出现的项；顺序就是「先配工作区，再谈其它」的顺序
CHECK_IDS = ("workspace", "code", "candidate", "plan", "login", "model")


# ---------------------------------------------------------------- 目录约定


def app_data_dir() -> Path:
    """数据目录：与状态库同级；`BOSS_APP_DATA` 优先，打包态为 exe 同目录 `data/`。

    统一解析成绝对路径（`runtime` 那边可能拿到 ``C:\\Users\\ALITAI~1\\...`` 这种
    8.3 短名），免得同一份回包里「数据目录」和「工作区记忆」两个字段长得不一样。
    """
    return _resolve(runtime.app_data_dir())


def default_workspace_dir() -> Path:
    """没有配置时用的工作区目录：``<数据目录>/workspace``。

    放在数据目录里而不是代码目录里：用户数据跟着数据目录走，换台机器、
    换份代码都不会把别人的简历和台账一起搬过去。
    """
    return app_data_dir() / WORKSPACE_DIR_NAME


def launch_file() -> Path:
    """记住工作区选择的文件：``<数据目录>/launch.json``。"""
    return app_data_dir() / LAUNCH_FILE_NAME


def bundled_workflow_dir() -> Path | None:
    """程序自带的工作流脚本目录；没有就返回 None。

    打包态找 ``resource_root()/workflow`` 与 ``executable_dir()/workflow``；
    源码树找仓库根的 ``workflow/`` 与仓库根本身。**目录里真有
    ``deliver_round_v2.py`` 才算**——宁可返回 None 让人去选目录，也不要指一个
    空目录，让人以为已经配好了。源码树里工作流脚本通常不在程序目录（各人一份），
    所以源码模式默认就是 None，这是实话，不是缺陷。
    """
    if runtime.is_frozen():
        bases = (runtime.resource_root(), runtime.executable_dir())
        candidates = [base / "workflow" for base in bases] + list(bases)
    else:
        root = Path(__file__).resolve().parents[2]
        candidates = [root / "workflow", root]
    for candidate in candidates:
        if (candidate / CODE_ENTRY).is_file():
            return candidate
    return None


# ---------------------------------------------------------------- 小工具


def _env(name: str) -> str:
    """读环境变量并去掉首尾空白；没设返回空串。"""
    return (os.environ.get(name) or "").strip()


def _as_path(raw: Any) -> Path | None:
    """把配置里的字符串转成路径；空值返回 None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    return Path(text).expanduser()


def _resolve(path: Path, *, strict: bool = False) -> Path:
    """解析成绝对路径（不要求存在）；``strict`` 时允许把异常抛给调用方。"""
    try:
        return path.expanduser().resolve(strict=strict)
    except OSError:  # pragma: no cover - 只有路径被占死/权限异常才走到
        return path.expanduser().absolute()


def has_workspace(folder: Path | str | None) -> bool:
    """这个目录是不是已经初始化过的工作区（有 ``workspace.json``）。"""
    if folder is None:
        return False
    return (Path(folder) / META_NAME).is_file()


def _read_launch() -> dict[str, Any]:
    """读 ``launch.json``；缺失或坏掉按「没配置」处理，绝不拦住启动。"""
    path = launch_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("launch.json 读不出来，按未配置处理：%s", exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("launch.json 必须是 JSON 对象，按未配置处理")
        return {}
    return data


def _write_launch(folder: Path, code: Path | None) -> Path:
    """原子写 ``launch.json``；返回文件路径。"""
    path = launch_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "workspace": str(folder),
        "code": str(code) if code is not None else "",
        "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    return path


def _code_from(path: Path | None) -> Path | None:
    """这个目录里真有 ``deliver_round_v2.py`` 才算代码目录。"""
    if path is None:
        return None
    folder = _resolve(path)
    return folder if (folder / CODE_ENTRY).is_file() else None


def _resolve_code(folder: Path, saved: dict[str, Any]) -> tuple[Path | None, str]:
    """代码目录从哪来：env → launch.json → 状态目录 → 自带 workflow/ → 没有。

    Returns:
        ``(目录, 来源)``；来源 ∈ ``env`` / ``saved`` / ``state`` / ``bundled`` / ``none``。
    """
    env_code = _code_from(_as_path(_env(ENV_CODE)))
    if env_code is not None:
        return env_code, "env"
    saved_code = _code_from(_as_path(saved.get("code")))
    if saved_code is not None:
        return saved_code, "saved"
    if _code_from(folder) is not None:
        return _resolve(folder), "state"
    bundled = _code_from(bundled_workflow_dir())
    if bundled is not None:
        return bundled, "bundled"
    return None, "none"


# ---------------------------------------------------------------- 启动时恢复


def apply_saved() -> dict[str, Any]:
    """启动时把 ``launch.json`` 里的路径补进环境变量（只填空缺）。

    显式设置的环境变量优先：这里只补空的变量（等价于 ``setdefault``），不覆盖
    任何已存在的值，所以测试 / 部署脚本里传的 ``BOSS_WORKFLOW_DIR`` 永远说了算。

    Returns:
        ``{"applied": {变量名: 值}, "saved": bool}``；没恢复任何东西时字典为空。
    """
    saved = _read_launch()
    applied: dict[str, str] = {}
    for env_name, field in ((ENV_WORKSPACE, "workspace"), (ENV_CODE, "code")):
        raw = str(saved.get(field) or "").strip()
        if not raw or _env(env_name):
            continue
        os.environ[env_name] = raw
        applied[env_name] = raw
    if applied:
        logger.info("已按 launch.json 恢复工作区配置：%s", applied)
    return {"applied": applied, "saved": bool(saved)}


def code_scripts(code: Path | None) -> dict[str, bool]:
    """代码目录里每个阶段脚本在不在；没有代码目录时全是 False。"""
    return {name: bool(code is not None and (code / name).is_file())
            for name, _label, _required in STAGE_SCRIPTS}


def readiness(state: dict[str, Any] | None = None) -> dict[str, Any]:
    """三档就绪：``empty`` / ``configured`` / ``executable``，以及**缺什么**。

    为什么不能只给一个布尔值：``ready=true`` 在新工作区建好那一刻就成立了，
    可那时五个阶段一个都跑不了——代码目录是空的。复核时正是撞在这上面：
    接口说"就绪"，授权入口却回 422。

    * ``empty``：还没有 ``workspace.json``；
    * ``configured``：工作区有了，但代码目录或阶段脚本还缺；
    * ``executable``：两者都在，阶段可以真的跑起来。

    Returns:
        含 ``stage`` / ``executable`` / ``missing`` / ``scripts`` / ``code_dir_ok``。
        ``missing`` 每条都是 ``{id, label, message, action}``，直接能当"下一步"用。
    """
    state = state or current()
    code = _as_path(state.get("code_dir"))
    scripts = code_scripts(code)
    missing: list[dict[str, Any]] = []

    def _miss(item_id: str, label: str, message: str, action: str) -> None:
        missing.append({"id": item_id, "label": label, "message": message, "action": action})

    if not state.get("configured"):
        _miss("workspace", "工作区",
              f"还没有工作区（默认目录：{state.get('default_dir') or ''}）",
              "填一个目录并点「创建/绑定」")
    if code is None:
        _miss("code_dir", "代码目录",
              "还没找到工作流代码目录（需要含 deliver_round_v2.py）",
              "在设置里选择代码目录，或把 workflow/ 放到程序目录下")
    else:
        for name, label, required in STAGE_SCRIPTS:
            if required and not scripts.get(name):
                _miss(f"script:{name}", label,
                      f"代码目录里缺 {name}，这类阶段跑不了：{code}",
                      f"把 {name} 放进代码目录")

    stage = "empty" if not state.get("configured") else ("executable" if not missing else "configured")
    return {"stage": stage, "executable": stage == "executable",
            "missing": missing, "scripts": scripts, "code_dir_ok": code is not None}


# ---------------------------------------------------------------- 现状


def current() -> dict[str, Any]:
    """现在用的是哪个目录、配没配好、值是哪来的。

    ``ready`` 只说明"目录在、工作区建过"，**不代表阶段能跑**——那要看
    ``stage`` / ``executable`` / ``missing``（见 :func:`readiness`）。

    Returns:
        含 ``configured`` / ``ready`` / ``stage`` / ``executable`` / ``missing`` /
        ``scripts`` / ``dir`` / ``code_dir`` / ``source`` / ``code_source`` /
        ``launch_file`` / ``default_dir`` / ``data_dir`` 的字典。
        ``source`` ∈ ``env`` / ``saved`` / ``default``；``code_source`` 多一个 ``none``。
    """
    saved = _read_launch()
    env_dir = _as_path(_env(ENV_WORKSPACE))
    saved_dir = _as_path(saved.get("workspace"))
    if env_dir is not None:
        folder, source = env_dir, "env"
    elif saved_dir is not None:
        folder, source = saved_dir, "saved"
    else:
        folder, source = default_workspace_dir(), "default"
    folder = _resolve(folder)
    # 刚在设置页点完「创建/绑定」时，环境变量和 launch.json 会指向同一处；
    # 这时候说「上次设置」比说「环境变量」更贴近用户看到的事实。
    if source == "env" and saved_dir is not None and _resolve(saved_dir) == folder:
        source = "saved"

    configured = has_workspace(folder)
    code, code_source = _resolve_code(folder, saved)
    state = {
        "configured": configured,
        "ready": configured and folder.is_dir(),
        "executable": False,
        "stage": "empty",
        "missing": [],
        "scripts": {},
        "dir": str(folder),
        "code_dir": str(code) if code is not None else "",
        "source": source,
        "code_source": code_source,
        "launch_file": str(launch_file()),
        "default_dir": str(default_workspace_dir()),
        "data_dir": str(app_data_dir()),
    }
    state.update(readiness(state))
    return state


def _check(check_id: str, ok: bool, label: str, message: str, action: str = "") -> dict[str, Any]:
    """拼一条「下一步」清单项。"""
    return {"id": check_id, "ok": bool(ok), "label": label, "message": message, "action": action}


def _workspace_checks(folder: Path) -> tuple[bool, bool, bool, str, str]:
    """读工作区里的简历 / 方案，返回 ``(candidate_ok, plan_ok, opened, 简历说明, 方案说明)``。

    任何一步读不出来都按「没配好」报告，并把原因带出来；绝不让清单接口 500。
    """
    if not has_workspace(folder):
        return False, False, False, "还没有工作区，先创建/绑定", "还没有工作区，先创建/绑定"
    try:
        from boss_core.errors import CoreError
        from boss_core.workspace import Workspace

        ws = Workspace.open(folder)
        candidate = ws.active_candidate()
        plan = ws.active_plan()
    except CoreError as exc:
        return False, False, False, f"工作区读不出来：{exc.message}", f"工作区读不出来：{exc.message}"
    except Exception as exc:  # noqa: BLE001 - 清单不能把整个接口带崩
        return False, False, False, f"工作区读不出来：{exc}", f"工作区读不出来：{exc}"
    return (
        candidate is not None,
        plan is not None,
        True,
        "已导入简历" if candidate is not None else "还没导入简历",
        "已设置求职目标" if plan is not None else "还没设置求职目标",
    )


def _login_check() -> tuple[bool, str]:
    """登录态：读启动自检的缓存，不在这里拉 Chrome。"""
    from .. import demo
    if demo.enabled():
        return True, '离线体验不需要登录；平台操作已禁用'
    try:
        from . import session_check

        snap = session_check.snapshot()
    except Exception as exc:  # noqa: BLE001 - 读不到就如实说
        return False, f"登录态读不出来：{exc}"
    if snap.get("logged_in"):
        return True, str(snap.get("message") or "已登录")
    return False, str(snap.get("message") or "还没检测登录态（打开「登录」页确认）")


def _model_check() -> tuple[bool, str]:
    """模型配置：读状态库里的 LLM 配置，不发任何网络请求。"""
    try:
        from .llm_config_store import load_config

        config = load_config()
    except Exception as exc:  # noqa: BLE001 - 读不到就如实说
        return False, f"模型配置读不出来：{exc}"
    if config.configured:
        return True, f"模型已配置：{config.model}"
    return False, "还没配置模型（API Key / 接口地址 / 模型名）"


def checklist(state: dict[str, Any] | None = None) -> dict[str, Any]:
    """「下一步该做什么」：工作区 / 简历 / 求职目标 / 登录 / 模型五项。

    Args:
        state: 已算好的 :func:`current` 结果；省略则现算。

    Returns:
        ``{"checks": [...], "next_steps": [...]}``；``next_steps`` 只装没做好的项。
    """
    state = state or current()
    folder = Path(state["dir"])
    candidate_ok, plan_ok, _opened, candidate_msg, plan_msg = _workspace_checks(folder)
    login_ok, login_msg = _login_check()
    model_ok, model_msg = _model_check()

    workspace_ok = bool(state["configured"])
    scripts = state.get("scripts") or {}
    required_ok = all(scripts.get(name) for name, _label, required in STAGE_SCRIPTS if required)
    code_ok = bool(state.get("code_dir")) and required_ok
    if code_ok:
        code_msg = f"代码目录已就绪：{state['code_dir']}"
    elif state.get("code_dir"):
        lacking = [name for name, _label, required in STAGE_SCRIPTS
                   if required and not scripts.get(name)]
        code_msg = f"代码目录缺脚本：{'、'.join(lacking)}（{state['code_dir']}）"
    else:
        code_msg = "还没找到代码目录（deliver_round_v2.py 所在处）"
    checks = [
        _check(
            "workspace",
            workspace_ok,
            "工作区",
            f"已就绪：{state['dir']}" if workspace_ok else f"还没有工作区（默认目录：{state['default_dir']}）",
            "" if workspace_ok else "在上面填一个目录，点「创建/绑定」",
        ),
        _check("code", code_ok, "工作流代码", code_msg,
               "" if code_ok else (f"把 {CODE_ENTRY} 与 panel_action.py 放进代码目录，"
                                   "或在设置里选择代码目录")),
        _check("candidate", candidate_ok, "简历", candidate_msg,
               "" if candidate_ok else "到「简历」页导入并解析一份简历"),
        _check("plan", plan_ok, "求职目标", plan_msg,
               "" if plan_ok else "设置目标城市 / 薪资 / 岗位方向，保存成求职方案"),
        _check("login", login_ok, "登录", login_msg,
               "" if login_ok else "到「登录」页扫码或短信登录 BOSS 直聘"),
        _check("model", model_ok, "模型", model_msg,
               "" if model_ok else "到「系统配置 → AI 设置」填 API Key、接口地址和模型名"),
    ]
    return {"checks": checks, "next_steps": [c["action"] for c in checks if not c["ok"] and c["action"]]}


# ---------------------------------------------------------------- 首次配置


def _remove_quietly(folder: Path, *, only_if_empty: bool) -> None:
    """收尾清理：删掉这次刚建出来、且还是空的工作区目录。"""
    try:
        if only_if_empty and any(folder.iterdir()):
            return
        folder.rmdir()
    except OSError:  # pragma: no cover - 清理失败不影响给用户的报错
        logger.debug("清理目录失败：%s", folder, exc_info=True)


def configure(
    target: str | None = None,
    *,
    code: str | None = None,
    actor: str = "setup",
) -> dict[str, Any]:
    """创建工作区（已存在就打开）并把它记进 ``launch.json``。

    校验都发生在建目录之前，失败时不会留下半截工作区：

    * 路径已经是个文件 → 422；
    * 路径建不出来 / 写不进去 → 422；
    * 和代码目录是同一个目录 → 422；
    * 目录里有 ``workspace.json`` 但 schema 不对 → 409，原文件一个字节都不动。

    Args:
        target: 工作区目录；空 = :func:`default_workspace_dir`。
        code: 代码目录；空 = 按 env → launch.json → 状态目录 → 自带 workflow/ 解析。
        actor: 写进审计的操作者标签。

    Returns:
        :func:`current` 的结果，外加 ``ok`` / ``changed`` / ``created`` / ``launch_file``。
        ``changed`` 只在这次**新建了** ``workspace.json`` 时为 true。
    """
    from boss_core.errors import CoreError
    from boss_core.workspace import Workspace

    from ..errors import ConflictError, ValidationWebError

    raw = str(target or "").strip()
    folder = Path(raw).expanduser() if raw else default_workspace_dir()
    folder = _resolve(folder)

    if folder.exists() and not folder.is_dir():
        raise ValidationWebError(f"这个路径已经是个文件，不能当工作区：{folder}")

    saved = _read_launch()
    explicit_code = _as_path(code)
    if explicit_code is not None:
        code_dir = _resolve(explicit_code)
        if not code_dir.is_dir():
            raise ValidationWebError(f"代码目录不存在：{code_dir}")
    else:
        code_dir, _source = _resolve_code(folder, saved)

    if code_dir is not None and folder == code_dir:
        raise ValidationWebError(
            f"工作区不能和代码目录是同一个目录：{folder}；工作区放数据，代码目录放脚本，换一个目录"
        )

    created_dir = False
    if not folder.exists():
        try:
            folder.mkdir(parents=True)
            created_dir = True
        except OSError as exc:
            _remove_quietly(folder, only_if_empty=True)
            raise ValidationWebError(f"建不了工作区目录：{folder}（{exc}）") from exc

    if not runtime.is_writable(folder):
        if created_dir:
            _remove_quietly(folder, only_if_empty=True)
        raise ValidationWebError(f"这个目录写不进去：{folder}（检查权限，或换一个目录）")

    existed_meta = (folder / META_NAME).is_file()
    try:
        if existed_meta:
            # 已经初始化过就打开，绝不覆盖：里面可能是用户唯一的简历和台账。
            Workspace.open(folder)
        else:
            Workspace.create(folder, name=folder.name, actor=actor)
    except CoreError as exc:
        if created_dir:
            _remove_quietly(folder, only_if_empty=True)
        raise ConflictError(
            f"这个目录里的 {META_NAME} 打不开，已原样保留：{exc.message}"
        ) from exc
    except OSError as exc:
        if created_dir:
            _remove_quietly(folder, only_if_empty=True)
        raise ValidationWebError(f"写不了 {META_NAME}：{folder}（{exc}）") from exc

    launch = _write_launch(folder, code_dir)
    os.environ[ENV_WORKSPACE] = str(folder)
    if code_dir is not None:
        os.environ[ENV_CODE] = str(code_dir)

    result = current()
    result.update({
        "ok": True,
        "changed": not existed_meta,
        "created": created_dir,
        "launch_file": str(launch),
    })
    logger.info("工作区已配置：%s（新建=%s）", folder, not existed_meta)
    return result
