import json

import pytest

from sdcmodeler import mcp_server
from tests.conftest import BASE, KEY

TREE = {"label": "Vitals", "components": [{"ct_id": "abc123abc123abc123abc123"}, {"label": "Pulse", "data_type": "XdCount"}], "clusters": []}


def rpc(method, params=None, id=1):
    return mcp_server._handle_request({"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})


def call(name, arguments):
    r = rpc("tools/call", {"name": name, "arguments": arguments})
    result = r["result"]
    text = result["content"][0]["text"]
    return (json.loads(text) if not result.get("isError") else None), (text if result.get("isError") else None)


@pytest.fixture(autouse=True)
def bound(fake, tmp_path):
    fake.on("GET", "/api/v1/auth/api-tokens/", body={"tokens": [{"prefix": KEY[:12], "scopes": ["read", "build", "spend"], "spend_limit_credits": 1000, "spent_credits": 300}]})
    # The server's estimate (4.17.0) for TREE: 500 + 100, the key at 700 remaining.
    from sdcmodeler.pricing import estimate_tree

    def server_estimate(request):
        body = json.loads(request.content)
        local = estimate_tree(body["data"], body["title"])
        return {**local, "wallet": {"balance_credits": 10000, "can_pay": True},
                "key": {"remaining_credits": 700, "within_limit": local["total_credits"] <= 700}}

    fake.on("POST", "/api/v1/dmgen/assemble/estimate/", body=server_estimate)
    mcp_server.configure(fake.client(), output_dir=tmp_path)
    return fake


def test_initialize_and_tools_list():
    r = rpc("initialize", {"protocolVersion": "2025-06-18"})
    assert r["result"]["serverInfo"] == {"name": "sdcmodeler", "version": mcp_server.SERVER_VERSION}
    assert r["result"]["protocolVersion"] == "2025-06-18"
    assert "wallet" in r["result"]["instructions"]
    tools = rpc("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["search_components", "list_models", "get_model", "estimate_model", "build_model", "job_status", "generate_app", "wallet"]


def test_build_needs_the_estimate_the_person_saw(bound):
    bound.on("POST", "/api/v1/dmgen/assemble/", status=202, body={"status": "processing", "task_id": "t", "data_source_ct_id": "ds1"})
    _, err = call("build_model", {"estimate_id": "nothing", "title": "Vitals", "tree": TREE})
    assert "estimate_model" in err
    est, _ = call("estimate_model", {"title": "Vitals", "tree": TREE})
    assert est["total_credits"] == 600 and est["within_key_limit"] is True and est["key_remaining_credits"] == 700
    _, err = call("build_model", {"estimate_id": est["estimate_id"], "title": "Vitals renamed", "tree": TREE})
    assert "exact title and tree" in err
    built, _ = call("build_model", {"estimate_id": est["estimate_id"], "title": "Vitals", "tree": TREE})
    assert built["mode"] == "mixed" and built["estimate"]["estimate_id"] == est["estimate_id"]
    assert built["estimate"]["source"] == "server"
    assert json.loads([c for c in bound.calls if c.url.path.endswith("/assemble/")][-1].content)["estimate_id"] == est["estimate_id"]


def test_build_stops_at_the_keys_limit(bound):
    def server_estimate(request):
        from sdcmodeler.pricing import estimate_tree
        body = json.loads(request.content)
        local = estimate_tree(body["data"], body["title"])
        return {**local, "wallet": {"balance_credits": 10000, "can_pay": True}, "key": {"remaining_credits": 500, "within_limit": False}}
    bound.on("POST", "/api/v1/dmgen/assemble/estimate/", body=server_estimate)
    est, _ = call("estimate_model", {"title": "Vitals", "tree": TREE})
    assert est["within_key_limit"] is False
    _, err = call("build_model", {"estimate_id": est["estimate_id"], "title": "Vitals", "tree": TREE})
    assert "may still spend 500" in err and "needs 600" in err
    assert not any(c.url.path.endswith("/assemble/") for c in bound.calls)


def test_a_402_names_the_wallet_page_and_stops(bound):
    bound.on("POST", "/api/v1/dmgen/assemble/", status=402, body={"error": "x", "estimated_cost_credits": 600, "balance_credits": 20})
    est, _ = call("estimate_model", {"title": "Vitals", "tree": TREE})
    _, err = call("build_model", {"estimate_id": est["estimate_id"], "title": "Vitals", "tree": TREE})
    assert "600 credits" in err and f"{BASE}/app/wallet" in err


def test_generate_app_needs_confirmation_and_reports_the_charge(bound, tmp_path):
    _, err = call("generate_app", {"ct_id": "m1"})
    assert "1500 credits" in err and "confirm_charge" in err
    bound.on("POST", "/api/v1/dmgen/dms/m1/generate_app/", body={"success": True, "profile": "lightweight"})
    bound.on("GET", "/api/v1/dmgen/dms/m1/download_app/", body={"download_url": "https://storage.example/app.zip", "filename": "app.zip", "storage": "gcs", "charged": True})
    bound.on("GET", "https://storage.example/app.zip", body=b"PK")
    out, _ = call("generate_app", {"ct_id": "m1", "confirm_charge": True})
    assert out["charged"] is True and out["path"].endswith("app.zip") and (tmp_path / "app.zip").exists()


def test_malformed_input_is_answered_not_fatal():
    assert mcp_server._handle_request([])["error"]["code"] == -32600
    assert mcp_server._handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "x"})["error"]["code"] == -32600
    assert mcp_server._handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": 7}})["error"]["code"] == -32602
    assert rpc("tools/call", {"name": "nope", "arguments": {}})["error"]["code"] == -32601
    assert rpc("nothing")["error"]["code"] == -32601
    assert mcp_server._handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    _, err = call("estimate_model", {"title": "t", "tree": "not an object"})
    assert "must be an object" in err


def test_job_status_takes_the_job_id(bound):
    bound.on("GET", "/api/v1/jobs/j1/", body={"ct_id": "j1", "status": "running", "step": "modeling", "cost_credits": 100})
    out, _ = call("job_status", {"job_ct_id": "j1"})
    assert out["step"] == "modeling"
