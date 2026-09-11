"""Create a zero-code website workspace for autonomous model experiments."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .workspace import atomic_write_text


MAX_BOOTSTRAP_TASK_CHARS = 20_000
GAME_COUNT_RE = re.compile(
    r"(?P<count>\d{1,3})\s*(?:个|款|种)\s*(?:童年)?(?:的)?(?:小)?游戏"
)


VALIDATION_TEST = r'''from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = json.loads(
    (ROOT / "site-requirements.json").read_text(encoding="utf-8")
)


class PageInspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags = []
        self.ids = set()

    def handle_starttag(self, tag, attrs) -> None:
        values = dict(attrs)
        self.tags.append((tag, values))
        if values.get("id"):
            self.ids.add(values["id"])


def required_text(relative: str) -> str:
    path = ROOT / relative
    assert path.is_file(), f"缺少必需网站文件：{relative}"
    return path.read_text(encoding="utf-8")


def registered_game_ids(source: str, manifest_ids):
    expected = set(manifest_ids)
    found = set()
    call_re = re.compile(
        r"(?<!function )\b(?:(?:window\.)?(?:GameHub|Hub)\.)?registerGame\s*\("
    )
    literal_re = re.compile(r"\s*(['\"])(?P<id>[^'\"]+)\1")
    object_id_re = re.compile(r"\bid\s*:\s*(['\"])(?P<id>[^'\"]+)\1")
    previous_call_end = 0
    for call in call_re.finditer(source):
        arguments = source[call.end() : call.end() + 300]
        literal = literal_re.match(arguments)
        if literal and literal.group("id") in expected:
            found.add(literal.group("id"))
        else:
            first_argument = arguments.split(",", 1)[0]
            if re.match(r"\s*[A-Za-z_$][\w$]*\.id\b", first_argument):
                segment = source[max(previous_call_end, call.start() - 12_000) : call.start()]
                candidates = [
                    match.group("id")
                    for match in object_id_re.finditer(segment)
                    if match.group("id") in expected
                ]
                if candidates:
                    found.add(candidates[-1])
        previous_call_end = call.end()
    return [game_id for game_id in manifest_ids if game_id in found]


def test_static_website_entry_files_exist():
    for relative in ("index.html", "styles.css", "app.js"):
        required_text(relative)


def test_html_loads_local_assets_and_supports_mobile():
    html = required_text("index.html")
    inspector = PageInspector()
    inspector.feed(html)
    assert any(tag == "main" for tag, _ in inspector.tags), "缺少 main 语义区域"
    assert any(
        tag == "meta" and "width=device-width" in attrs.get("content", "")
        for tag, attrs in inspector.tags
    ), "缺少移动端 viewport"
    assert any(
        tag == "link" and attrs.get("href") == "styles.css"
        for tag, attrs in inspector.tags
    ), "index.html 必须引用本地 styles.css"
    assert any(
        tag == "script" and attrs.get("src") == "app.js"
        for tag, attrs in inspector.tags
    ), "index.html 必须引用本地 app.js"


def test_site_is_offline_and_not_a_placeholder():
    combined = "\n".join(
        required_text(relative) for relative in ("index.html", "styles.css", "app.js")
    )
    assert not re.search(r"https?://|//cdn\.|@import\s+url", combined, re.I), (
        "从零建站 MVP 必须离线运行，不得依赖外部 CDN 或素材"
    )
    assert not re.search(
        r"TODO|coming\s+soon|敬请期待|即将上线|暂未开放|占位",
        combined,
        re.I,
    ), "网站不得使用未实现占位内容"
    assert "[HARNESS_COMPACTED" not in combined, (
        "Harness 历史摘要被误写进了网站源码"
    )


def test_responsive_and_keyboard_baseline():
    css = required_text("styles.css")
    assert "@media" in css and "max-width" in css, "缺少手机端响应式样式"
    assert ":focus-visible" in css, "缺少键盘焦点样式"
    assert "prefers-reduced-motion" in css, "缺少减少动画偏好处理"
    assert re.search(r"--[\w-]+\s*:", css), "页面应使用 CSS 变量保持视觉统一"


def test_interactive_javascript_baseline():
    source = required_text("app.js")
    assert len(source) >= 1_000, "JavaScript 实现过于简略"
    assert "addEventListener" in source, "网站必须实现真实用户交互"


def test_game_site_requirements_when_requested():
    expected_count = REQUIREMENTS.get("expected_game_count")
    if expected_count is None:
        return
    manifest_path = ROOT / "game-manifest.json"
    assert manifest_path.is_file(), "多游戏网站必须创建 game-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(manifest, list), "game-manifest.json 顶层必须是数组"
    assert len(manifest) == expected_count, (
        f"任务要求 {expected_count} 款游戏，当前为 {len(manifest)}"
    )
    ids = [item.get("id") for item in manifest if isinstance(item, dict)]
    assert len(ids) == expected_count and len(ids) == len(set(ids)), (
        "每款游戏必须有唯一 ID"
    )
    for item in manifest:
        assert all(item.get(key) for key in ("id", "name", "instructions", "controls")), (
            "每款游戏必须提供 id、name、instructions 和 controls"
        )
    source = required_text("app.js")
    assert "registerGame" in source, "多游戏网站必须提供 registerGame 注册机制"
    registered = registered_game_ids(source, ids)
    missing = [game_id for game_id in ids if game_id not in registered]
    assert not missing, "以下游戏未注册可玩实现：" + "、".join(missing)
    assert len(source) >= max(4_000, expected_count * 500), "多游戏实现过于简略"


def test_required_named_games_and_minecraft_signals():
    expected_names = REQUIREMENTS.get("required_game_names", [])
    if not expected_names:
        return
    manifest = json.loads(required_text("game-manifest.json"))
    names = [str(item.get("name", "")) for item in manifest]
    for expected in expected_names:
        assert any(expected in name for name in names), f"游戏清单缺少：{expected}"
    if "我的世界" in expected_names:
        source = required_text("app.js")
        signals = {
            "canvas": "Canvas 世界",
            "generateWorld": "地形生成",
            "breakBlock": "破坏方块",
            "placeBlock": "放置方块",
            "hotbar": "快捷栏",
            "localStorage": "世界保存",
        }
        missing = [label for key, label in signals.items() if key not in source]
        assert not missing, "简化网页版我的世界缺少：" + "、".join(missing)
'''


def infer_website_requirements(task_text: str) -> Dict[str, Any]:
    """Extract only deterministic, independently testable task signals."""
    count_match = GAME_COUNT_RE.search(task_text)
    expected_game_count: Optional[int] = None
    if count_match:
        candidate = int(count_match.group("count"))
        if 1 <= candidate <= 50:
            expected_game_count = candidate
    required_game_names = ["我的世界"] if "我的世界" in task_text else []
    return {
        "schema_version": 1,
        "project_type": "static_website",
        "expected_game_count": expected_game_count,
        "required_game_names": required_game_names,
        "offline_required": True,
        "manual_visual_review_required": True,
    }


def _task_brief(task_text: str, requirements: Dict[str, Any]) -> str:
    additions = [
        "只使用原生 HTML、CSS 和 JavaScript，不使用外部依赖或 CDN。",
        "必须创建 index.html、styles.css 和 app.js，双击 index.html 可离线运行。",
        "为便于页面直接预览，运行时 JavaScript 集中在 app.js，不要依赖 fetch 读取本地文件。",
        "不修改 TASK.md、site-requirements.json、tests/ 或 .agent-harness.json。",
        "自动验收通过不代表可以使用占位代码；产品必须真实可交互。",
    ]
    count = requirements.get("expected_game_count")
    if count is not None:
        additions.extend(
            [
                f"创建 game-manifest.json，顶层数组恰好包含 {count} 款游戏。",
                "每项必须有唯一 id、name、instructions 和非空 controls。",
                "统一使用 window.GameHub：提供 registerGame(id, meta, factory)、get(id) 和 list()；"
                "在 index.html 提供 #game-list 与 #game-stage。大厅卡片追加到 #game-list，点击后"
                "必须通过统一入口调用对应 factory(stage, runtime)，并在 #game-stage 渲染内容。",
                "所有游戏 factory 使用同一参数和返回约定，不得混用不同版本的 Hub/工具接口；"
                "可返回含 destroy() 的实例或清理函数。",
                "每款游戏都需有输入、状态反馈、重新开始和返回大厅，不得使用‘即将上线’占位。",
            ]
        )
        if count >= 10:
            additions.extend(
                [
                    "为避免模型服务因单次输出过大而超时：先用 write_file replace "
                    "写 app.js 核心框架，再用 append 分批追加游戏；单次 content 不超过 "
                    "12000 字符，每批最多实现 2 款游戏，等待每次工具结果后再继续。",
                    "推荐结构：核心框架通过 window.GameHub（或同类全局对象）暴露 "
                    "registerGame；核心闭包正常结束，后续每批游戏作为独立 IIFE 在文件末尾"
                    "调用该全局 API。这是合法结构，不要把追加模块重新移回核心闭包。",
                    "每次修改 app.js 后检查工具返回的 syntax_check；若语法未通过，"
                    "先修复诊断再继续追加。最终 run_validation 会独立执行 JavaScript 语法检查，"
                    "并逐个启动 manifest 中的全部游戏；任一启动异常或空白舞台都算失败。",
                ]
            )
    if "我的世界" in requirements.get("required_game_names", []):
        additions.append(
            "‘我的世界’按原创简化方块沙盒实现：Canvas 世界、"
            "generateWorld、breakBlock、placeBlock、至少 3 种方块的 hotbar，"
            "并用 localStorage 保存世界；不使用官方素材，不要求复刻官方 3D 游戏。"
        )
    return (
        "# 用户网站任务\n\n"
        + task_text.strip()
        + "\n\n## Harness 生成的建站边界\n\n"
        + "\n".join(f"{index}. {item}" for index, item in enumerate(additions, 1))
        + "\n"
    )


def create_website_workspace(
    task_text: str,
    *,
    parent_dir: Optional[Path] = None,
) -> Path:
    """Create a disposable, zero-product-code workspace for one website task."""
    if not isinstance(task_text, str) or not task_text.strip():
        raise ValueError("网站任务不能为空")
    if len(task_text) > MAX_BOOTSTRAP_TASK_CHARS:
        raise ValueError("网站任务不能超过 20000 字符")
    if parent_dir is not None:
        parent_dir.mkdir(parents=True, exist_ok=True)
    root = Path(
        tempfile.mkdtemp(
            prefix="harness-new-website-",
            dir=str(parent_dir) if parent_dir is not None else None,
        )
    )
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    requirements = infer_website_requirements(task_text)
    config = {
        "schema_version": 1,
        "validation_command": [
            "python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        "validation_timeout_seconds": 45,
        "editable_paths": [
            "index.html",
            "styles.css",
            "app.js",
            "game-manifest.json",
            "assets",
        ],
        "protected_paths": ["tests", "TASK.md", "site-requirements.json"],
        "sensitive_paths": [".git", ".env", ".venv"],
    }
    atomic_write_text(
        root / ".agent-harness.json",
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        0o600,
    )
    atomic_write_text(root / "TASK.md", _task_brief(task_text, requirements), 0o600)
    atomic_write_text(
        root / "site-requirements.json",
        json.dumps(requirements, ensure_ascii=False, indent=2) + "\n",
        0o600,
    )
    atomic_write_text(
        root / "tests" / "test_site_acceptance.py",
        VALIDATION_TEST,
        0o600,
    )
    return root
