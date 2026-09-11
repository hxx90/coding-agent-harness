from __future__ import annotations

import json
import shutil

import pytest

from agent_harness.javascript import check_website_runtime


def _runtime_site_source(factory_body: str) -> str:
    return """
(function () {
  const games = new Map();
  const stage = document.getElementById('game-stage');
  const list = document.getElementById('game-list');
  window.GameHub = {
    registerGame(id, meta, factory) {
      games.set(id, { meta: Object.assign({ id: id }, meta), factory: factory });
    },
    get(id) { return games.get(id); },
    list() { return Array.from(games.values()); }
  };
  GameHub.registerGame('demo', { id: 'demo', name: 'Demo' }, function () {
    %s
  });
  GameHub.list().forEach(function (entry) {
    const card = document.createElement('li');
    card.addEventListener('click', function () {
      while (stage.firstChild) stage.removeChild(stage.firstChild);
      entry.factory(stage, {});
    });
    list.appendChild(card);
  });
})();
""" % factory_body


def _write_runtime_site(tmp_path, factory_body: str):
    app_path = tmp_path / "app.js"
    manifest_path = tmp_path / "game-manifest.json"
    app_path.write_text(_runtime_site_source(factory_body), encoding="utf-8")
    manifest_path.write_text(
        json.dumps([{"id": "demo", "name": "Demo"}]), encoding="utf-8"
    )
    return app_path, manifest_path


@pytest.mark.skipif(not shutil.which("node"), reason="requires Node.js")
def test_website_runtime_smoke_launches_registered_game(tmp_path):
    app_path, manifest_path = _write_runtime_site(
        tmp_path,
        "stage.appendChild(document.createElement('canvas'));",
    )

    result = check_website_runtime(app_path, manifest_path, tmp_path)

    if not result["available"]:
        pytest.skip(result["message"])
    assert result["passed"] is True
    assert result["checked_games"] == 1
    assert result["failures"] == []


@pytest.mark.skipif(not shutil.which("node"), reason="requires Node.js")
def test_website_runtime_smoke_reports_factory_error(tmp_path):
    app_path, manifest_path = _write_runtime_site(
        tmp_path,
        "throw new Error('factory exploded');",
    )

    result = check_website_runtime(app_path, manifest_path, tmp_path)

    if not result["available"]:
        pytest.skip(result["message"])
    assert result["passed"] is False
    assert result["checked_games"] == 1
    assert result["failures"][0]["id"] == "demo"
    assert "factory exploded" in result["diagnostics"]
