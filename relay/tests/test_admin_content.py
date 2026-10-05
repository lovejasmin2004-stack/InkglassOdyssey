"""Admin Workshop content routes: excluded files are not editable as their directory's type.

world_config.json (regions/) and blueprints.json (scenarios/) share a directory
with a content type whose schema they do not follow. The Workshop must neither
list them nor read, overwrite or delete them through that type's routes.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import relay.admin.reload as reload
from relay.admin.app import app
from relay.config import settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORLD = "inkglass_dark"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Admin app pointed at a copy of the content, so no test can touch real files."""
    for d in ("regions", "scenarios"):
        shutil.copytree(_REPO_ROOT / d, tmp_path / d)
    monkeypatch.setattr(reload, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(settings, "admin_secret", "")
    reload._index_cache.clear()
    yield TestClient(app, raise_server_exceptions=False)
    reload._index_cache.clear()


@pytest.mark.parametrize(("content_type", "file_id"), [("regions", "world_config"), ("scenarios", "blueprints")])
class TestExcludedFiles:
    def test_not_listed(self, client, content_type, file_id):
        resp = client.get(f"/api/content/{content_type}/{_WORLD}")
        assert resp.status_code == 200
        assert file_id not in [item["id"] for item in resp.json()["items"]]

    def test_read_rejected(self, client, content_type, file_id):
        assert client.get(f"/api/content/{content_type}/{_WORLD}/{file_id}").status_code == 404

    def test_write_rejected_and_file_unchanged(self, client, tmp_path, content_type, file_id):
        path = tmp_path / content_type / _WORLD / f"{file_id}.json"
        before = path.read_bytes()
        resp = client.put(f"/api/content/{content_type}/{_WORLD}/{file_id}", json={"id": file_id})
        assert resp.status_code == 404
        assert path.read_bytes() == before

    def test_delete_rejected_and_file_kept(self, client, tmp_path, content_type, file_id):
        resp = client.delete(f"/api/content/{content_type}/{_WORLD}/{file_id}")
        assert resp.status_code == 404
        assert (tmp_path / content_type / _WORLD / f"{file_id}.json").exists()


def test_ordinary_scenario_still_readable(client):
    resp = client.get(f"/api/content/scenarios/{_WORLD}/tinte_recruitment")
    assert resp.status_code == 200
    assert resp.json()["id"] == "tinte_recruitment"


def test_excluded_path_guard_in_reload_layer():
    """Defence in depth: the file helpers refuse excluded IDs even if a route forgets."""
    with pytest.raises(ValueError):
        reload._content_path("scenarios", _WORLD, "blueprints")


def test_blueprints_file_is_an_array():
    """Guards the reason for the exclusion: if this changes, revisit CONTENT_TYPES."""
    data = json.loads((_REPO_ROOT / "scenarios" / _WORLD / "blueprints.json").read_text(encoding="utf-8"))
    assert isinstance(data, list)


def test_index_versions_assets_so_browsers_reload_them(client):
    """After an update the page must point at new asset URLs, or browsers keep the old script."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    assert "/static/app.js?v=" in resp.text
    assert "/static/style.css?v=" in resp.text
