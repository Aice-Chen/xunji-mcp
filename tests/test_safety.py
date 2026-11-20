import asyncio
import copy
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubProvider
from key_value.aio.stores.filetree import FileTreeStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from starlette.testclient import TestClient

from server import create_server
from xunji.api import API, UserError
from xunji.auth import OwnerGitHubProvider, identity_allowed
from xunji.service import Service
from xunji.validation import date_range, movements


@pytest.fixture
def env(monkeypatch, tmp_path):
    values = {
        "DATA_DIR": str(tmp_path),
        "BASE_URL": "https://xunji.example.com",
        "ALLOWED_GITHUB_LOGIN": "owner",
        "ALLOWED_GITHUB_ID": "123",
        "GITHUB_CLIENT_ID": "test-client",
        "GITHUB_CLIENT_SECRET": "test-secret",
        "JWT_SIGNING_KEY": "test-only-signing-material-" * 3,
        "STORAGE_ENCRYPTION_SECRET": "test-only-storage-material-" * 3,
    }
    for name in ("TRAIN", "FOOD", "FOOD_SEARCH", "BODY", "TEMPLATE"):
        values[f"XUNJI_{name}_KEY"] = "test-only-not-a-real-key"
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ENABLE_WRITES", "false")


@pytest.fixture
def fake():
    now = [1000.0]
    requests = []
    answer = [{"success": True, "res": {"records": []}}]

    async def handler(request):
        requests.append(request)
        if isinstance(answer[0], Exception):
            raise answer[0]
        return httpx.Response(200, json=copy.deepcopy(answer[0]))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = API(
        {k: "fake-credential" for k in ("TRAIN", "FOOD", "FOOD_SEARCH", "BODY", "TEMPLATE")},
        http,
        clock=lambda: now[0],
    )
    service = Service(api, {"杠铃卧推", "引体向上"}, writes_enabled=True, clock=lambda: now[0])
    return SimpleNamespace(now=now, requests=requests, answer=answer, api=api, service=service)


async def test_cache_deepcopy_and_shared_endpoint_cooldown(fake):
    result = await fake.api.call("body_query", {"start_date": "2026-01-01"})
    result["res"]["records"].append("modified")
    assert (await fake.api.call("body_query", {"start_date": "2026-01-01"}))["res"]["records"] == []
    with pytest.raises(UserError, match="15 秒"):
        await fake.api.call("body_query", {"start_date": "2026-01-02"})
    assert len(fake.requests) == 1


async def test_train_success_does_not_require_success_flag(fake):
    fake.answer[0] = {"res": {"trains": []}}
    assert (await fake.service.training_get("2026-01-01"))["res"] == {"trains": []}


async def test_upstream_error_does_not_leak_secret(fake):
    fake.answer[0] = {"success": False, "error": "apikey invalid fake-credential private-health-record"}
    with pytest.raises(UserError) as error:
        await fake.api.call("food_query", {})
    assert str(error.value) == "apikey invalid"


async def test_retry_after_persists(fake):
    fake.answer[0] = {"success": False, "msg": "too frequent", "retry_after_ms": 45000}
    with pytest.raises(UserError, match="45 秒"):
        await fake.api.call("body_query", {})
    fake.now[0] += 16
    with pytest.raises(UserError, match="29 秒"):
        await fake.api.call("body_query", {})
    assert len(fake.requests) == 1


async def test_preview_binds_payload_owner_expiry_and_single_use(fake):
    payload = {"records": [{"datestr": "2026-01-01", "type": "weight", "value": 70}]}
    preview = fake.service.preview("body_write", payload, "owner")
    payload["records"][0]["value"] = 90
    preview["changes"]["records"][0]["value"] = 99
    with pytest.raises(UserError):
        await fake.service.commit(preview["confirmation_id"], "intruder")
    results = await asyncio.gather(
        fake.service.commit(preview["confirmation_id"], "owner"),
        fake.service.commit(preview["confirmation_id"], "owner"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, UserError) for result in results) == 1
    sent = json.loads(fake.requests[0].content)
    assert sent["records"][0]["value"] == 70 and sent["confirmed"] is True
    expired = fake.service.preview("body_write", payload, "owner")
    fake.now[0] += 901
    with pytest.raises(UserError):
        await fake.service.commit(expired["confirmation_id"], "owner")


async def test_timeout_is_not_retried_and_confirmation_is_consumed(fake):
    fake.answer[0] = httpx.ReadTimeout("private error detail")
    preview = fake.service.preview("food_write", {"foods": []}, "owner")
    with pytest.raises(UserError, match="结果可能未知"):
        await fake.service.commit(preview["confirmation_id"], "owner")
    with pytest.raises(UserError, match="已提交"):
        await fake.service.commit(preview["confirmation_id"], "owner")
    assert len(fake.requests) == 1


async def test_read_only_mode_blocks_commit_and_cancel_sends_nothing(fake):
    fake.service.writes_enabled = False
    p = fake.service.preview("body_write", {}, "owner")
    with pytest.raises(UserError, match="尚未启用"):
        await fake.service.commit(p["confirmation_id"], "owner")
    assert fake.service.cancel(p["confirmation_id"], "owner") == {"cancelled": True}
    assert fake.requests == []


def training():
    return {
        "localid": 1,
        "datestr": "2026-01-01",
        "start": 100,
        "end": 200,
        "note": json.dumps({"text": "keep", "heartRate": [80, 90]}),
        "movements": [
            {"name": "杠铃卧推", "sets": [{"weight": "40", "reps": "8", "done": False, "rpe": "7"}]}
        ],
    }


async def test_training_patch_preserves_unfinished_groups_notes_and_times(fake):
    original = training()
    fake.answer[0] = {"res": {"trains": [original]}}
    p = await fake.service.training_preview(
        "2026-01-01", [{"localid": 1, "note": {"trainColor": "#FF0000"}}], "owner"
    )
    updated = p["changes"]["res"][0]
    assert updated["start"] == 100 and updated["end"] == 200
    assert updated["note"] == {"text": "keep", "heartRate": [80, 90], "trainColor": "#FF0000"}
    assert updated["movements"] == original["movements"]
    # App edit between preview and commit prevents the write.
    fake.now[0] += 31
    fake.answer[0]["res"]["trains"][0]["title"] = "edited in app"
    with pytest.raises(UserError, match="原数据已变化"):
        await fake.service.commit(p["confirmation_id"], "owner")
    assert all("upsert" not in r.url.path for r in fake.requests)


async def test_training_patch_cannot_drop_groups(fake):
    fake.answer[0] = {"res": {"trains": [training()]}}
    with pytest.raises(UserError, match="删除"):
        await fake.service.training_preview("2026-01-01", [{"localid": 1, "movements": []}], "owner")


async def test_body_preview_only_dry_runs(fake):
    fake.answer[0] = {"success": True, "res": {"summary": "weight 70 kg"}}
    p = await fake.service.body_preview([{"datestr": "2026-01-01", "type": "weight", "value": 70}], "owner")
    sent = json.loads(fake.requests[0].content)
    assert sent["dry_run"] is True and "confirmed" not in sent
    assert p["server_preview"] == {"summary": "weight 70 kg"}


def test_food_date_window_and_unknown_movement():
    with pytest.raises(UserError):
        date_range("2024-01-01", "2024-01-02", food=True, today=date(2026, 1, 1))
    with pytest.raises(UserError):
        date_range("2026-01-01", "2026-04-03", max_days=92)
    with pytest.raises(UserError, match="标准中文"):
        movements([{"name": "imaginary", "sets": [{"reps": 5}]}], {"杠铃卧推"})


async def test_template_capacity_and_deleted_folder(fake):
    fake.answer[0] = {"res": {"folder_deleted": True, "folder": None}}
    item = {"client_id": "new", "name": "推 A", "movements": [{"name": "杠铃卧推", "sets": [{"reps": 8}]}]}
    with pytest.raises(UserError, match="不会自动重建"):
        await fake.service.templates_preview([item], [], None, "owner")
    fake.now[0] += 16
    fake.answer[0] = {
        "res": {
            "folder": {"version": 1},
            "template_versions": {str(i): 1 for i in range(14)},
            "templates": [],
        }
    }
    with pytest.raises(UserError, match="最多 14"):
        await fake.service.templates_preview([item], [], None, "owner")


@pytest.mark.parametrize(
    "claims,allowed",
    [
        ({"sub": "123", "login": "OWNER"}, True),
        ({"sub": "456", "login": "owner"}, False),
        ({"sub": "123", "login": "other"}, False),
        ({}, False),
    ],
)
def test_identity_requires_both_id_and_login(claims, allowed):
    assert identity_allowed(SimpleNamespace(claims=claims), "owner", "123") == allowed


async def test_oauth_storage_encrypted_and_survives_recreation(tmp_path):
    store = FileTreeStore(data_directory=tmp_path)
    wrapper = FernetEncryptionWrapper(store, source_material="test-material" * 5, salt="test")
    await wrapper.put("token", {"access_token": "SECRET_TEST_SENTINEL"}, collection="tokens")
    assert not any(b"SECRET_TEST_SENTINEL" in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
    reopened = FernetEncryptionWrapper(
        FileTreeStore(data_directory=tmp_path), source_material="test-material" * 5, salt="test"
    )
    assert (await reopened.get("token", collection="tokens"))["access_token"] == "SECRET_TEST_SENTINEL"


def test_http_health_discovery_and_unauthorized(env):
    mcp = create_server()
    app = mcp.http_app(stateless_http=True, json_response=True)
    with TestClient(app) as client:
        assert client.get("/health").text == "ok"
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 401
        assert "resource_metadata" in response.headers["www-authenticate"]
        assert client.get("/.well-known/oauth-authorization-server").status_code == 200


def test_http_owner_can_list_tools_other_account_cannot(env, monkeypatch):
    token = AccessToken(
        token="test", client_id="test", scopes=["read:user"], claims={"sub": "123", "login": "owner"}
    )
    mock = AsyncMock(return_value=token)
    monkeypatch.setattr(GitHubProvider, "load_access_token", mock)
    mcp = create_server()
    assert isinstance(mcp.auth, OwnerGitHubProvider)
    with TestClient(mcp.http_app(stateless_http=True, json_response=True)) as client:
        headers = {
            "Authorization": "Bearer test",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }
        response = client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        assert response.status_code == 200, response.text
        tools = {t["name"]: t for t in response.json()["result"]["tools"]}
        assert len(tools) == 15
        assert tools["commit_write"]["annotations"]["destructiveHint"] is True
        token.claims["sub"] = "999"
        assert (
            client.post(
                "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
            ).status_code
            == 401
        )
