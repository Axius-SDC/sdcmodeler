import json

import pytest

from sdcmodeler import deliver as runs
from sdcmodeler import mcp_server
from sdcmodeler.cli import main
from sdcmodeler.pricing import estimate_tree
from tests.conftest import KEY

REUSE_TREE = {"label": "Vitals", "components": [{"ct_id": "abc123abc123abc123abc123"}], "clusters": []}
MIXED_TREE = {"label": "Vitals", "components": [{"ct_id": "abc123abc123abc123abc123"}, {"label": "Pulse", "data_type": "XdCount"}], "clusters": []}
KIT = {
    "generator": "4.0.0", "models": [{"ctId": "m1", "title": "Vitals"}], "charged": True,
    "files": {"supabase/functions/_shared/sdc/models/dm-m1.ts": "x\n"},
    "base_migration": {"name": "sdc_base.sql", "sql": "-- base\n"},
    "migrations": [{"ctId": "m1", "name": "sdc_dm_m1.sql", "sql": "-- m1\n"}],
}


def scripted(fake, *, balance=10000, remaining=None):
    """The server's estimate with a wallet and a key position, the reuse build, the package and the kit."""
    def server_estimate(request):
        body = json.loads(request.content)
        local = estimate_tree(body["data"], body["title"])
        key = {"remaining_credits": remaining, "within_limit": remaining is None or local["total_credits"] <= remaining}
        return {**local, "wallet": {"balance_credits": balance, "can_pay": True}, "key": key}

    fake.on("POST", "/api/v1/dmgen/assemble/estimate/", body=server_estimate)
    fake.on("GET", "/api/v1/auth/api-tokens/", body={"tokens": [{"prefix": KEY[:12], "scopes": ["read", "build", "spend"], "spend_limit_credits": None}]})
    fake.on("POST", "/api/v1/dmgen/assemble/", body={"ct_id": "m1", "published": True, "generated": True})
    fake.on("GET", "/api/v1/dmgen/dms/m1/download_package/", body=b"PK", headers={"content-disposition": 'attachment; filename="dm-m1.zip"'})
    fake.on("GET", "/api/v1/dmgen/dms/m1/supabase_kit/", body=KIT)
    fake.on("POST", "/api/v1/dmgen/dms/m1/generate_app/", body={"success": True})
    fake.on("GET", "/api/v1/dmgen/dms/m1/download_app/", body=b"PK")
    return fake


def assembled(fake):
    return [c for c in fake.calls if c.url.path.endswith("/assemble/")]


# ------------------------------------------------------------------ deliverables

def test_parse_deliver_reads_the_environment_and_orders_the_items(monkeypatch):
    monkeypatch.delenv("SDCMODELER_DELIVER", raising=False)
    assert runs.parse_deliver() == ["package"]
    monkeypatch.setenv("SDCMODELER_DELIVER", "kit, app ,package,kit")
    assert runs.parse_deliver() == ["package", "app:lightweight", "kit"]
    assert runs.parse_deliver("") == [] and runs.parse_deliver([]) == []
    with pytest.raises(runs.RunRefused, match="Unknown deliverable"):
        runs.parse_deliver("pdf")


# ------------------------------------------------------------------ the plan

def test_plan_adds_one_application_charge_for_an_app_and_a_kit(fake):
    c = scripted(fake).client()
    assert runs.plan(c, MIXED_TREE, "Vitals", "package")["total_credits"] == 600
    p = runs.plan(c, MIXED_TREE, "Vitals", ["package", "app", "kit"])
    assert (p["build_credits"], p["deliver_credits"], p["total_credits"]) == (600, 1500, 2100)
    assert p["refusals"] == [] and p["deliver"] == ["package", "app:lightweight", "kit"]
    assert not assembled(fake), "a plan spends nothing"


def test_plan_refuses_a_run_the_wallet_or_the_key_cannot_pay(fake):
    p = runs.plan(scripted(fake, balance=2000).client(), MIXED_TREE, "Vitals", "kit")
    assert len(p["refusals"]) == 1 and p["refusals"][0].startswith("The run needs 2100 credits and the wallet has 2000.")
    assert "/app/settings?tab=wallet (fund the wallet)" in p["refusals"][0]
    p = runs.plan(scripted(fake, remaining=1000).client(), MIXED_TREE, "Vitals", "kit")
    assert p["refusals"] == ["The run needs 2100 credits and this key may still spend 1000."]


# ------------------------------------------------------------------ the run

def test_a_reuse_run_publishes_and_delivers_the_package_and_the_kit(fake, tmp_path):
    c = scripted(fake).client()
    (tmp_path / "app").mkdir()
    p = runs.plan(c, REUSE_TREE, "Vitals", "package,kit")
    out = runs.run(c, REUSE_TREE, "Vitals", p, output_dir=tmp_path, project_dir=tmp_path / "app")
    assert out["ct_id"] == "m1" and out["failed"] == {}
    sent = json.loads(assembled(fake)[-1].content)
    assert sent["estimate_id"] == p["estimate_id"] and "publish" not in sent and "generate" not in sent
    assert (tmp_path / "dm-m1.zip").exists()
    assert out["delivered"]["kit"]["charged"] is True
    assert len(list((tmp_path / "app" / "supabase" / "migrations").glob("*_sdc_dm_m1.sql"))) == 1


def test_a_mixed_run_waits_for_the_job_then_delivers(fake, tmp_path):
    c = scripted(fake).client()
    fake.on("POST", "/api/v1/dmgen/assemble/", status=202, body={"status": "processing", "job_ct_id": "j1"})
    fake.on("GET", "/api/v1/jobs/j1/", body={"ct_id": "j1", "status": "completed", "result": {"dm_ct_id": "m1", "published": True}})
    out = runs.run(c, MIXED_TREE, "Vitals", runs.plan(c, MIXED_TREE, "Vitals", "package"), output_dir=tmp_path)
    assert out["ct_id"] == "m1" and "package" in out["delivered"]


def test_a_failed_job_delivers_nothing(fake, tmp_path):
    c = scripted(fake).client()
    fake.on("POST", "/api/v1/dmgen/assemble/", status=202, body={"status": "processing", "job_ct_id": "j1"})
    fake.on("GET", "/api/v1/jobs/j1/", body={"ct_id": "j1", "status": "failed", "error": "modeling failed"})
    with pytest.raises(runs.RunRefused, match="modeling failed"):
        runs.run(c, MIXED_TREE, "Vitals", runs.plan(c, MIXED_TREE, "Vitals", "package"), output_dir=tmp_path)
    assert not any("download" in call.url.path for call in fake.calls)


def test_nothing_is_built_when_the_plan_refuses_or_the_kit_has_no_project(fake, tmp_path):
    c = scripted(fake, balance=100).client()
    with pytest.raises(runs.RunRefused, match="wallet has 100"):
        runs.run(c, REUSE_TREE, "Vitals", runs.plan(c, REUSE_TREE, "Vitals", "package"), output_dir=tmp_path)
    c = scripted(fake).client()
    with pytest.raises(runs.RunRefused, match="is not a directory"):
        runs.run(c, REUSE_TREE, "Vitals", runs.plan(c, REUSE_TREE, "Vitals", "kit"), output_dir=tmp_path,
                 project_dir=tmp_path / "missing")
    p = runs.plan(c, REUSE_TREE, "Vitals", "package")
    with pytest.raises(runs.RunRefused, match="changed since the plan"):
        runs.run(c, REUSE_TREE, "Vitals renamed", p, output_dir=tmp_path)
    assert not assembled(fake)


def test_a_deliverable_that_fails_after_publishing_is_reported_and_the_rest_still_run(fake, tmp_path):
    c = scripted(fake).client()
    fake.on("GET", "/api/v1/dmgen/dms/m1/download_package/", status=500, body={"error": "storage down"})
    (tmp_path / "app").mkdir()
    out = runs.run(c, REUSE_TREE, "Vitals", runs.plan(c, REUSE_TREE, "Vitals", "package,kit"),
                   output_dir=tmp_path, project_dir=tmp_path / "app")
    assert "storage down" in out["failed"]["package"] and "kit" in out["delivered"]


# ------------------------------------------------------------------ the MCP tool

def call(name, arguments):
    r = mcp_server._handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    text = r["result"]["content"][0]["text"]
    return (None, text) if r["result"].get("isError") else (json.loads(text), None)


def test_build_and_deliver_needs_the_run_the_person_saw(fake, tmp_path):
    mcp_server.configure(scripted(fake).client(), output_dir=tmp_path)
    est, _ = call("estimate_model", {"title": "Vitals", "tree": REUSE_TREE})
    assert "run" not in est
    _, err = call("build_and_deliver", {"estimate_id": est["estimate_id"], "title": "Vitals", "tree": REUSE_TREE, "deliver": ["kit"]})
    assert "estimate_model with deliver" in err
    est, _ = call("estimate_model", {"title": "Vitals", "tree": REUSE_TREE, "deliver": ["package"]})
    assert est["run"]["total_credits"] == 500 and est["run"]["deliver"] == ["package"]
    _, err = call("build_and_deliver", {"estimate_id": est["estimate_id"], "title": "Vitals", "tree": REUSE_TREE, "deliver": ["kit"]})
    assert "deliver (kit)" in err
    assert not assembled(fake)
    out, _ = call("build_and_deliver", {"estimate_id": est["estimate_id"], "title": "Vitals", "tree": REUSE_TREE, "deliver": ["package"]})
    assert out["ct_id"] == "m1" and (tmp_path / "dm-m1.zip").exists() and out["failed"] == {}


def test_build_and_deliver_takes_the_kit_charge_without_confirm_charge(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("SDCMODELER_DELIVER", "kit")
    monkeypatch.setenv("SDCMODELER_PROJECT_DIR", "app")
    (tmp_path / "app").mkdir()
    mcp_server.configure(scripted(fake).client(), output_dir=tmp_path)
    est, _ = call("estimate_model", {"title": "Vitals", "tree": REUSE_TREE, "deliver": ["kit"]})
    assert est["run"]["deliver_credits"] == 1500
    out, _ = call("build_and_deliver", {"estimate_id": est["estimate_id"], "title": "Vitals", "tree": REUSE_TREE})
    assert out["delivered"]["kit"]["project"] == str(tmp_path / "app")


def test_build_and_deliver_refuses_a_run_the_key_cannot_pay(fake, tmp_path):
    mcp_server.configure(scripted(fake, remaining=1000).client(), output_dir=tmp_path)
    (tmp_path / "app").mkdir()
    est, _ = call("estimate_model", {"title": "Vitals", "tree": REUSE_TREE, "deliver": ["kit"]})
    assert est["run"]["refusals"]
    _, err = call("build_and_deliver", {"estimate_id": est["estimate_id"], "title": "Vitals", "tree": REUSE_TREE,
                                        "deliver": ["kit"], "project_dir": "app"})
    assert "Nothing was built" in err and "may still spend 1000" in err
    assert not assembled(fake)


# ------------------------------------------------------------------ the CLI

@pytest.fixture
def cli_client(fake, monkeypatch):
    scripted(fake)
    monkeypatch.setattr("sdcmodeler.cli.SDCStudioClient", lambda base_url=None: fake.client())
    return fake


def test_ship_plan_prints_the_total_and_spends_nothing(cli_client, tmp_path, capsys):
    tree = tmp_path / "tree.json"; tree.write_text(json.dumps(MIXED_TREE))
    assert main(["ship", str(tree), "--title", "Vitals", "--deliver", "package,kit", "--plan"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total_credits"] == 2100 and "estimate" not in out
    assert not assembled(cli_client)


def test_ship_without_approval_builds_nothing(cli_client, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    tree = tmp_path / "tree.json"; tree.write_text(json.dumps(REUSE_TREE))
    assert main(["ship", str(tree), "--title", "Vitals", "--dir", str(tmp_path)]) == 1
    assert "Not approved" in capsys.readouterr().err
    assert not assembled(cli_client)


def test_ship_yes_runs_and_delivers(cli_client, tmp_path, capsys):
    tree = tmp_path / "tree.json"; tree.write_text(json.dumps(REUSE_TREE))
    assert main(["ship", str(tree), "--title", "Vitals", "--deliver", "package", "--dir", str(tmp_path), "--yes"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ct_id"] == "m1" and (tmp_path / "dm-m1.zip").exists() and out["authorized_by"] == "yes_flag"


def test_ship_exits_2_when_a_deliverable_fails_after_publishing(cli_client, tmp_path, capsys):
    cli_client.on("GET", "/api/v1/dmgen/dms/m1/download_package/", status=500, body={"error": "storage down"})
    tree = tmp_path / "tree.json"; tree.write_text(json.dumps(REUSE_TREE))
    assert main(["ship", str(tree), "--title", "Vitals", "--deliver", "package", "--dir", str(tmp_path), "--yes"]) == 2
    assert "storage down" in json.loads(capsys.readouterr().out)["failed"]["package"]
