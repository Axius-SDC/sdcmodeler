import json

import pytest

from sdcmodeler.client import AssemblyRefused, InsufficientCredits, KeyPolicyRefused, SDCStudioError
from tests.conftest import BASE, KEY, body_of

TREE = {"label": "Vitals", "components": [{"ct_id": "abc123abc123abc123abc123"}], "clusters": []}


def test_search_sends_the_key_and_the_filters(fake):
    fake.on("GET", "/api/v1/catalog/components/", body={"count": 1, "results": [{"ct_id": "x", "label": "Blood Pressure"}]})
    c = fake.client()
    r = c.search_components("blood", type="xdquantity", page_size=10)
    assert r["results"][0]["label"] == "Blood Pressure"
    req = fake.calls[-1]
    assert req.headers["authorization"] == f"Token {KEY}"
    assert dict(req.url.params) == {"search": "blood", "type": "xdquantity", "page": "1", "page_size": "10"}


def test_the_catalog_works_without_a_key(fake):
    fake.on("GET", "/api/v1/catalog/dms/", body={"count": 0, "results": []})
    c = fake.client(api_key=None)
    assert c.list_models("x") == {"count": 0, "results": []}
    assert "authorization" not in fake.calls[-1].headers


def test_artifact_download_follows_the_storage_pointer_without_the_key(fake, tmp_path):
    fake.on("GET", "/api/v1/catalog/dm/m1/xsd/", body={"download_url": "https://storage.example/dm-m1.xsd", "filename": "dm-m1.xsd", "storage": "gcs"})
    fake.on("GET", "https://storage.example/dm-m1.xsd", body=b"<xs:schema/>")
    path = fake.client().download_artifact("m1", "xsd", tmp_path)
    assert path.name == "dm-m1.xsd" and path.read_bytes() == b"<xs:schema/>"
    storage_call = fake.calls[-1]
    assert storage_call.url.host == "storage.example" and "authorization" not in storage_call.headers


def test_artifact_download_streams_bytes_on_a_local_deployment(fake, tmp_path):
    fake.on("GET", "/api/v1/catalog/dm/m1/ttl/", body=b"@prefix x: <y> .", headers={"Content-Type": "text/turtle", "Content-Disposition": 'attachment; filename="dm-m1.ttl"'})
    path = fake.client().download_artifact("m1", "ttl", tmp_path)
    assert path.name == "dm-m1.ttl" and path.read_bytes().startswith(b"@prefix")


def test_assemble_reuse_is_sync_and_mixed_is_async(fake):
    fake.on("POST", "/api/v1/dmgen/assemble/", body={"ct_id": "m1", "title": "Vitals", "artifacts": {"xsd": "/api/v1/catalog/dm/m1/xsd/"}})
    r = fake.client().assemble("Vitals", TREE, description="d")
    assert r["mode"] == "reuse" and r["ct_id"] == "m1"
    assert body_of(fake.calls[-1]) == {"title": "Vitals", "description": "d", "data": TREE}
    fake.on("POST", "/api/v1/dmgen/assemble/", status=202, body={"status": "processing", "task_id": "t", "data_source_ct_id": "ds1", "estimated_cost_credits": 600, "new_components": 1})
    r = fake.client().assemble("Vitals", TREE)
    assert r["mode"] == "mixed" and r["data_source_ct_id"] == "ds1"


def test_the_three_refusals_are_typed(fake):
    c = fake.client()
    fake.on("POST", "/api/v1/dmgen/assemble/", status=402, body={"error": "Insufficient credits. This needs 500 credits; you have 20.", "error_type": "insufficient_funds", "estimated_cost_credits": 500, "balance_credits": 20})
    with pytest.raises(InsufficientCredits) as e:
        c.assemble("V", TREE)
    assert (e.value.estimated_cost_credits, e.value.balance_credits) == (500, 20)
    fake.on("POST", "/api/v1/dmgen/assemble/", status=403, body={"error": "This API key lacks the 'spend' scope", "error_type": "key_scope", "scope": "spend"})
    with pytest.raises(KeyPolicyRefused) as e:
        c.assemble("V", TREE)
    assert e.value.error_type == "key_scope"
    fake.on("POST", "/api/v1/dmgen/assemble/", status=409, body={"error": "Component abc is retired; successor def"})
    with pytest.raises(AssemblyRefused, match="retired"):
        c.assemble("V", TREE)
    fake.on("POST", "/api/v1/dmgen/assemble/", status=500, body="boom")
    with pytest.raises(SDCStudioError) as e:
        c.assemble("V", TREE)
    assert e.value.status == 500


def test_key_policy_finds_this_keys_own_row(fake):
    fake.on("GET", "/api/v1/auth/api-tokens/", body={"tokens": [
        {"prefix": "sdcs_bbbbbbb", "scopes": ["read"], "spend_limit_credits": None},
        {"prefix": KEY[:12], "scopes": ["read", "build", "spend"], "spend_limit_credits": 5000, "spent_credits": 600},
    ]})
    assert fake.client().key_policy()["spend_limit_credits"] == 5000
    assert fake.client(api_key=None).key_policy() is None


def test_ensure_default_project_creates_one_when_the_modeler_has_none(fake):
    fake.on("GET", "/api/v1/auth/modeler/", body={"project_ct_id": None, "projects": []})
    fake.on("POST", "/api/v1/dmgen/projects/", status=201, body={"ct_id": "p1", "name": "Agent models"})
    fake.on("PATCH", "/api/v1/auth/modeler/update/", body={"project_ct_id": "p1"})
    assert fake.client().ensure_default_project() == "p1"
    assert body_of(fake.calls[-1]) == {"project_ct_id": "p1"}


def test_download_app_reports_the_servers_charge(fake, tmp_path):
    fake.on("GET", "/api/v1/dmgen/dms/m1/download_app/", body={"download_url": "https://storage.example/app.zip", "filename": "app.zip", "storage": "gcs", "charged": False})
    fake.on("GET", "https://storage.example/app.zip", body=b"PK")
    path, charged = fake.client().download_app("m1", tmp_path)
    assert path.name == "app.zip" and charged is False
    with pytest.raises(ValueError):
        fake.client().generate_app("m1", "huge")
