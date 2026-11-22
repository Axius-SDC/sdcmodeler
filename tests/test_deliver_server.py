"""Phase 4: the run against SDCStudio with the build-and-deliver release (the server prices, funds, holds and delivers)."""
import hashlib
import json

import pytest

from sdcmodeler import deliver as runs
from sdcmodeler import mcp_server
from sdcmodeler.client import InsufficientCredits
from sdcmodeler.pricing import estimate_tree
from tests.conftest import BASE, KEY, body_of

REUSE_TREE = {"label": "Vitals", "components": [{"ct_id": "abc123abc123abc123abc123"}], "clusters": []}
MIXED_TREE = {"label": "Vitals", "components": [{"ct_id": "abc123abc123abc123abc123"}, {"label": "Pulse", "data_type": "XdCount"}], "clusters": []}
KIT = {
    "generator": "4.0.0", "models": [{"ctId": "m1", "title": "Vitals"}], "charged": False,
    "files": {"supabase/functions/_shared/sdc/models/dm-m1.ts": "x\n"},
    "base_migration": {"name": "sdc_base.sql", "sql": "-- base\n"},
    "migrations": [{"ctId": "m1", "name": "sdc_dm_m1.sql", "sql": "-- m1\n"}],
}


def new_server(fake, *, balance=10000, auto_spend=False, would_cover=False, blocked=None, remaining=None):
    """An SDCStudio that prices deliverables and funds the run (the shapes of SDCStudio PR #1025)."""
    def server_estimate(request):
        body = json.loads(request.content)
        deliver = body.get("deliver") or []
        est = estimate_tree(body["data"], body["title"], deliver)
        total = est["total_credits"]
        shortfall = max(0, total - balance)
        est.update({
            "deliver": deliver, "deliver_credits": est.get("deliver_credits", 0),
            "build_credits": est.get("build_credits", total),
            "will_publish": {"project": {"ct_id": "p-default", "name": "Agent models", "public": False},
                             "items": [{"kind": "model", "path": "", "label": body["title"], "data_type": "DM"}]},
            "funding": {"balance_credits": balance, "shortfall_credits": shortfall, "covered": shortfall == 0,
                        "blocked_reason": blocked if shortfall else None,
                        "auto_reload": {"enabled": True, "confirmed": True, "card_saved": True, "amount_usd": "10.00",
                                        "reloads_needed": 1 if shortfall else 0, "reload_total_usd": "10.00",
                                        "would_cover": would_cover}},
            "auto_spend": auto_spend,
            "wallet": {"balance_credits": balance, "can_pay": shortfall == 0},
            "key": {"remaining_credits": remaining, "within_limit": remaining is None or total <= remaining},
        })
        return est

    fake.on("POST", "/api/v1/dmgen/assemble/estimate/", body=server_estimate)
    fake.on("GET", "/api/v1/auth/api-tokens/", body={"tokens": [{"prefix": KEY[:12], "scopes": ["read", "build", "spend"],
                                                                 "spend_limit_credits": None, "auto_spend": auto_spend,
                                                                 "reserved_credits": 0}]})

    def reuse_assemble(request):
        deliver = json.loads(request.content).get("deliver") or []
        delivered = [{"item": d, "status": "ready", "download": f"/api/v1/dmgen/dms/m1/{d}/"} for d in deliver]
        return {"ct_id": "m1", "published": True, "generated": True, "delivered": delivered,
                "deliver_charged_credits": 1500 if set(deliver) & {"kit", "app:lightweight", "app:enterprise"} else 0}

    fake.on("POST", "/api/v1/dmgen/assemble/", body=reuse_assemble)
    fake.on("GET", "/api/v1/dmgen/dms/m1/download_package/", body=b"PK", headers={"content-disposition": 'attachment; filename="dm-m1.zip"'})
    fake.on("GET", "/api/v1/dmgen/dms/m1/supabase_kit/", body=KIT)
    fake.on("GET", "/api/v1/dmgen/dms/m1/download_app/", body=b"PK")
    return fake


def assembled(fake):
    return [c for c in fake.calls if c.url.path.endswith("/assemble/")]


# ------------------------------------------------------------------ the id and the client

def test_the_estimate_id_binds_deliver_only_when_there_is_one():
    plain = estimate_tree(MIXED_TREE, "Vitals")
    assert estimate_tree(MIXED_TREE, "Vitals", [])["estimate_id"] == plain["estimate_id"]
    canonical = json.dumps({"title": "Vitals", "tree": MIXED_TREE, "deliver": ["package", "kit"]}, sort_keys=True, separators=(",", ":"))
    with_deliver = estimate_tree(MIXED_TREE, "Vitals", ["package", "kit"])
    assert with_deliver["estimate_id"] == hashlib.sha256(canonical.encode()).hexdigest()[:12] != plain["estimate_id"]
    assert (with_deliver["build_credits"], with_deliver["deliver_credits"], with_deliver["total_credits"]) == (700, 1500, 2200)


def test_the_client_sends_deliver_and_scope_only_when_given(fake):
    new_server(fake)
    c = fake.client()
    c.estimate(REUSE_TREE, "Vitals")
    assert "deliver" not in body_of(fake.calls[-1])
    c.estimate(REUSE_TREE, "Vitals", deliver=["package"])
    assert body_of(fake.calls[-1])["deliver"] == ["package"]
    fake.on("GET", "/api/v1/catalog/components/", body={"count": 0, "results": [], "scope": "mine"})
    c.search_components("pulse")
    assert "scope" not in dict(fake.calls[-1].url.params)
    c.search_components("pulse", scope="mine")
    assert dict(fake.calls[-1].url.params)["scope"] == "mine"
    with pytest.raises(ValueError, match="needs an API key"):
        fake.client(api_key="").search_components("pulse", scope="mine")


def test_a_402_carries_the_reason_and_the_wallet_page(fake):
    fake.on("POST", "/api/v1/dmgen/assemble/", status=402, body={
        "error": "Auto-reload is on by default but you have not confirmed it.", "error_type": "insufficient_funds",
        "reason": "auto_reload_unconfirmed", "estimated_cost_credits": 2100, "balance_credits": 500,
        "shortfall_credits": 1600, "wallet_url": "/app/settings"})
    with pytest.raises(InsufficientCredits) as exc:
        fake.client().assemble("Vitals", REUSE_TREE)
    assert (exc.value.reason, exc.value.shortfall_credits, exc.value.wallet_url) == ("auto_reload_unconfirmed", 1600, "/app/settings")


# ------------------------------------------------------------------ the plan on the server

def test_a_short_balance_a_reload_covers_is_not_refused(fake):
    p = runs.plan(new_server(fake, balance=500, would_cover=True).client(), MIXED_TREE, "Vitals", "package,kit")
    assert p["mode"] == "server" and p["refusals"] == [] and p["total_credits"] == 2200
    assert p["will_publish"]["project"]["public"] is False


def test_a_short_balance_no_reload_covers_is_refused_with_the_servers_reason(fake):
    p = runs.plan(new_server(fake, balance=500, blocked="ceiling").client(), MIXED_TREE, "Vitals", "kit")
    assert len(p["refusals"]) == 1 and "(ceiling)" in p["refusals"][0]
    assert f"{BASE}/app/settings?tab=wallet" in p["refusals"][0] and "raise the monthly auto-reload maximum" in p["refusals"][0]
    p = runs.plan(new_server(fake, balance=500, blocked="auto_reload_unconfirmed").client(), MIXED_TREE, "Vitals", "kit")
    assert "confirm auto-reload in the wallet while signed in" in p["refusals"][0]


def test_the_keys_reserved_credits_count_against_its_limit(fake):
    new_server(fake)
    fake.on("GET", "/api/v1/auth/api-tokens/", body={"tokens": [{"prefix": KEY[:12], "spend_limit_credits": 3000,
                                                                 "spent_credits": 500, "reserved_credits": 1000}]})
    c = fake.client()
    assert runs._remaining_on_key(c, {"source": "local"}) == 1500


# ------------------------------------------------------------------ the run on the server

def test_a_server_run_sends_deliver_and_downloads_what_the_server_made(fake, tmp_path):
    c = new_server(fake).client()
    (tmp_path / "app").mkdir()
    p = runs.plan(c, REUSE_TREE, "Vitals", "package,kit")
    out = runs.run(c, REUSE_TREE, "Vitals", p, output_dir=tmp_path, project_dir=tmp_path / "app")
    assert out["mode"] == "server" and out["failed"] == {} and out["deliver_charged_credits"] == 1500
    assert body_of(assembled(fake)[-1])["deliver"] == ["package", "kit"]
    assert body_of(assembled(fake)[-1])["estimate_id"] == p["estimate_id"]
    assert (tmp_path / "dm-m1.zip").exists()
    assert len(list((tmp_path / "app" / "supabase" / "migrations").glob("*_sdc_dm_m1.sql"))) == 1


def test_a_server_app_is_downloaded_not_generated_again(fake, tmp_path):
    c = new_server(fake).client()
    out = runs.run(c, REUSE_TREE, "Vitals", runs.plan(c, REUSE_TREE, "Vitals", "app"), output_dir=tmp_path)
    assert "app:lightweight" in out["delivered"]
    assert not any(call.url.path.endswith("/generate_app/") for call in fake.calls)


def test_a_failed_job_that_published_reports_the_failed_delivery_and_fetches_the_rest(fake, tmp_path):
    c = new_server(fake).client()
    (tmp_path / "app").mkdir()
    fake.on("POST", "/api/v1/dmgen/assemble/", status=202, body={"status": "processing", "job_ct_id": "j1"})
    fake.on("GET", "/api/v1/jobs/j1/", body={"ct_id": "j1", "status": "failed", "error": "Not delivered: kit", "result": {
        "dm_ct_id": "m1", "published": True, "generated": True, "finalize_error": "Not delivered: kit",
        "delivered": [{"item": "package", "status": "ready", "download": "/api/v1/dmgen/dms/m1/download_package/"},
                      {"item": "kit", "status": "failed", "download": None, "error": "This model cannot be made into a kit"}],
        "deliver_charged_credits": 0}})
    p = runs.plan(c, MIXED_TREE, "Vitals", "package,kit")
    out = runs.run(c, MIXED_TREE, "Vitals", p, output_dir=tmp_path, project_dir=tmp_path / "app")
    assert out["ct_id"] == "m1" and "package" in out["delivered"]
    assert "cannot be made into a kit" in out["failed"]["kit"]
    assert not any(call.url.path.endswith("/supabase_kit/") for call in fake.calls), "a failed item is not fetched outside the run"


def test_a_job_that_published_nothing_raises(fake, tmp_path):
    c = new_server(fake).client()
    fake.on("POST", "/api/v1/dmgen/assemble/", status=202, body={"status": "processing", "job_ct_id": "j1"})
    fake.on("GET", "/api/v1/jobs/j1/", body={"ct_id": "j1", "status": "failed", "error": "Not published: x",
                                             "result": {"dm_ct_id": "m1", "published": False}})
    with pytest.raises(runs.RunRefused, match="did not publish"):
        runs.run(c, MIXED_TREE, "Vitals", runs.plan(c, MIXED_TREE, "Vitals", "package"), output_dir=tmp_path)


# ------------------------------------------------------------------ auto-spend over MCP

def call(name, arguments):
    r = mcp_server._handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    text = r["result"]["content"][0]["text"]
    return (None, text) if r["result"].get("isError") else (json.loads(text), None)


def test_an_auto_spend_key_runs_without_a_prior_estimate(fake, tmp_path):
    mcp_server.configure(new_server(fake, auto_spend=True).client(), output_dir=tmp_path)
    out, _ = call("build_and_deliver", {"title": "Vitals", "tree": REUSE_TREE, "deliver": ["package"]})
    assert out["authorized_by"] == "auto_spend" and out["plan"]["total_credits"] == 500 and (tmp_path / "dm-m1.zip").exists()


def test_without_auto_spend_the_shown_estimate_is_still_required(fake, tmp_path):
    mcp_server.configure(new_server(fake).client(), output_dir=tmp_path)
    _, err = call("build_and_deliver", {"title": "Vitals", "tree": REUSE_TREE, "deliver": ["package"]})
    assert "estimate_model with deliver" in err and not assembled(fake)
    est, _ = call("estimate_model", {"title": "Vitals", "tree": REUSE_TREE, "deliver": ["package"]})
    assert est["estimate_id"] == est["run"]["estimate_id"] and est["run"]["mode"] == "server"
    out, _ = call("build_and_deliver", {"estimate_id": est["estimate_id"], "title": "Vitals", "tree": REUSE_TREE, "deliver": ["package"]})
    assert out["authorized_by"] == "estimate_shown"


def test_a_run_the_server_cannot_fund_names_the_reason_and_what_to_do(fake, tmp_path):
    mcp_server.configure(new_server(fake, auto_spend=True).client(), output_dir=tmp_path)
    fake.on("POST", "/api/v1/dmgen/assemble/", status=402, body={
        "error": "Auto-reload is off.", "error_type": "insufficient_funds", "reason": "auto_reload_off",
        "estimated_cost_credits": 500, "balance_credits": 0, "shortfall_credits": 500, "wallet_url": "/app/settings"})
    _, err = call("build_and_deliver", {"title": "Vitals", "tree": REUSE_TREE, "deliver": ["package"]})
    assert "auto_reload_off" in err and f"{BASE}/app/settings" in err and "turn auto-reload on" in err


def test_search_puts_the_modelers_own_components_first_by_default(fake, tmp_path):
    mcp_server.configure(new_server(fake).client(), output_dir=tmp_path)
    fake.on("GET", "/api/v1/catalog/components/", body={"count": 2, "scope": "mine", "results": [
        {"ct_id": "own1", "label": "Pulse", "scope": "mine"}, {"ct_id": "pub1", "label": "Pulse", "scope": "public"}]})
    out, _ = call("search_components", {"query": "pulse"})
    assert [r["scope"] for r in out["results"]] == ["mine", "public"]
    call("search_components", {"query": "pulse", "scope": "public"})
    assert dict(fake.calls[-1].url.params)["scope"] == "public"


def test_a_run_still_building_when_the_wait_ends_says_so_and_never_builds_again(fake, tmp_path):
    """An agent told "defect" would build again and pay twice; the run is funded and finishes on the server."""
    c = new_server(fake).client()
    fake.on("POST", "/api/v1/dmgen/assemble/", status=202, body={"status": "processing", "job_ct_id": "j7"})
    fake.on("GET", "/api/v1/jobs/j7/", body={"ct_id": "j7", "status": "running", "step": "modeling"})
    out = runs.run(c, MIXED_TREE, "Vitals", runs.plan(c, MIXED_TREE, "Vitals", "package"), output_dir=tmp_path, timeout=0)
    assert out["status"] == "in_flight" and out["job_ct_id"] == "j7" and "Do not build again" in out["next"]
    assert len(assembled(fake)) == 1


def test_build_and_deliver_reports_a_run_in_flight_not_a_defect(fake, tmp_path, monkeypatch):
    mcp_server.configure(new_server(fake, auto_spend=True).client(), output_dir=tmp_path)
    fake.on("POST", "/api/v1/dmgen/assemble/", status=202, body={"status": "processing", "job_ct_id": "j7"})
    fake.on("GET", "/api/v1/jobs/j7/", status=503, body={"error": "unavailable"})
    out, err = call("build_and_deliver", {"title": "Vitals", "tree": MIXED_TREE, "deliver": ["package"]})
    assert err is None and out["status"] == "in_flight" and "j7" in out["next"]
