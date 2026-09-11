from __future__ import annotations

import json
import shutil
import sys
import zipfile
from io import BytesIO

import pytest

from agent_harness.config import WorkspaceConfig
from agent_harness.project_bootstrap import (
    create_website_workspace,
    infer_website_requirements,
)
from agent_harness.snapshot import SnapshotManager
from agent_harness.tools import ToolExecutor
from agent_harness.validation import ValidationRunner
from agent_harness.website_output import build_inline_preview, build_website_zip


GAME_TASK = (
    "写一个小游戏网站，里面需要有20个童年的游戏，"
    "有一个需要是我的世界，需要直接在网页上就可以玩"
)


def test_infer_website_requirements_from_natural_language():
    requirements = infer_website_requirements(GAME_TASK)

    assert requirements["expected_game_count"] == 20
    assert requirements["required_game_names"] == ["我的世界"]
    assert requirements["manual_visual_review_required"] is True


def test_bootstrap_creates_zero_code_protected_workspace(tmp_path):
    root = create_website_workspace(GAME_TASK, parent_dir=tmp_path)
    config = WorkspaceConfig.load(str(root))

    assert not (root / "index.html").exists()
    assert not (root / "styles.css").exists()
    assert not (root / "app.js").exists()
    assert (root / "TASK.md").is_file()
    assert (root / "tests" / "test_site_acceptance.py").is_file()
    assert not config.can_write("TASK.md")
    assert not config.can_write("tests/test_site_acceptance.py")
    assert config.can_write("index.html")
    requirements = json.loads(
        (root / "site-requirements.json").read_text(encoding="utf-8")
    )
    assert requirements["expected_game_count"] == 20
    task_brief = (root / "TASK.md").read_text(encoding="utf-8")
    assert "registerGame" in task_brief
    assert "单次 content 不超过 12000 字符" in task_brief
    assert "每批最多实现 2 款游戏" in task_brief
    assert "window.GameHub" in task_brief
    assert "不要把追加模块重新移回核心闭包" in task_brief
    assert "syntax_check" in task_brief
    assert "factory(stage, runtime)" in task_brief
    assert "逐个启动 manifest 中的全部游戏" in task_brief

    result = ValidationRunner(config).run()
    assert result.exit_code not in (None, 0)
    assert "缺少必需网站文件" in result.stdout

    manifest_ids = ["minecraft", "snake"] + [
        "game-%s" % index for index in range(3, 21)
    ]
    (root / "game-manifest.json").write_text(
        json.dumps([{"id": game_id} for game_id in manifest_ids]),
        encoding="utf-8",
    )
    snapshot = SnapshotManager(config, tmp_path / "task-runtime")
    snapshot.create()
    tools = ToolExecutor(config, snapshot, ValidationRunner(config))
    core_result, _ = tools.execute(
        "write_file",
        {
            "path": "app.js",
            "content": "function registerGame(id, factory) {}\n"
            "window.GameHub = { registerGame };",
        },
    )
    assert core_result.ok
    assert core_result.data["website_progress"]["registered_count"] == 0
    if shutil.which("node"):
        assert core_result.data["syntax_check"]["passed"] is True
    progress_result, _ = tools.execute(
        "write_file",
        {
            "path": "app.js",
            "mode": "append",
            "content": (
                "const game = { id: 'minecraft' };\n"
                "GameHub.registerGame(game.id, function () {});\n"
                "GameHub.registerGame('snake', function () {});"
            ),
        },
    )
    assert progress_result.ok
    assert progress_result.data["inserted_separator"] is True
    progress = progress_result.data["website_progress"]
    assert progress["registered_count"] == 2
    assert progress["registered_ids"] == ["minecraft", "snake"]
    assert len(progress["missing_ids"]) == 18
    assert "合法结构" in progress["architecture_hint"]


def test_static_website_validation_rejects_invalid_javascript(tmp_path):
    if not shutil.which("node"):
        pytest.skip("static website syntax validation requires Node.js")
    root = create_website_workspace("创建一个简单网站", parent_dir=tmp_path)
    (root / "app.js").write_text("const broken = ;\n", encoding="utf-8")
    config = WorkspaceConfig.load(str(root))

    result = ValidationRunner(config).run()

    assert result.exit_code == 1
    assert result.runner_error is None
    assert "JavaScript syntax check failed" in result.stdout
    assert "SyntaxError" in result.stdout


def test_static_game_website_validation_rejects_runtime_failure(tmp_path):
    if not shutil.which("node"):
        pytest.skip("static website runtime validation requires Node.js")
    root = create_website_workspace("创建一个简单网站", parent_dir=tmp_path)
    (root / "game-manifest.json").write_text(
        json.dumps([{"id": "broken", "name": "Broken"}]), encoding="utf-8"
    )
    (root / "app.js").write_text(
        """
const games = new Map();
const stage = document.getElementById('game-stage');
const list = document.getElementById('game-list');
window.GameHub = {
  registerGame(id, meta, factory) { games.set(id, {meta, factory}); },
  get(id) { return games.get(id); },
  list() { return Array.from(games.values()); }
};
window.GameHub.registerGame('broken', {id: 'broken'}, function () {
  throw new Error('cannot launch game');
});
window.GameHub.list().forEach(function (entry) {
  const card = document.createElement('button');
  card.addEventListener('click', function () { entry.factory(stage, {}); });
  list.appendChild(card);
});
""",
        encoding="utf-8",
    )
    config = WorkspaceConfig.load(str(root))

    result = ValidationRunner(config).run()

    if result.runner_error and "sandbox-exec" in result.runner_error:
        pytest.skip(result.runner_error)
    assert result.exit_code == 1
    assert result.runner_error is None
    assert "Website runtime smoke failed" in result.stdout
    assert "broken: Error: cannot launch game" in result.stdout


def test_generated_website_can_be_previewed_and_exported(tmp_path):
    root = create_website_workspace("创建一个简单个人网站", parent_dir=tmp_path)
    (root / "index.html").write_text(
        "<!doctype html><html><head><link rel='stylesheet' href='styles.css'>"
        "</head><body><main>Hello</main><script src='app.js'></script></body></html>",
        encoding="utf-8",
    )
    (root / "styles.css").write_text("body { color: red; }\n", encoding="utf-8")
    (root / "app.js").write_text("document.body.dataset.ready = 'yes';\n", encoding="utf-8")
    config = WorkspaceConfig.load(str(root))

    preview = build_inline_preview(root)
    assert "<style>" in preview
    assert "body { color: red; }" in preview
    assert "<script>" in preview
    assert "document.body.dataset.ready" in preview
    assert "href='styles.css'" not in preview
    assert "src='app.js'" not in preview

    archive = zipfile.ZipFile(BytesIO(build_website_zip(config)))
    names = set(archive.namelist())
    assert "website/index.html" in names
    assert "website/styles.css" in names
    assert "website/app.js" in names
    assert "website/TASK.md" not in names
    assert "website/tests/test_site_acceptance.py" not in names


def test_bootstrap_validation_uses_current_python(tmp_path):
    root = create_website_workspace("创建一个介绍页", parent_dir=tmp_path)
    config = WorkspaceConfig.load(str(root))

    assert config.validation_command[0] == sys.executable
