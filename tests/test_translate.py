"""Phase 3 of SDCStudio's translate PRD: translate a published model or component (the shapes of SDCStudio 4.28.0)."""
import hashlib
import json

import pytest

from sdcmodeler import mcp_server
from sdcmodeler import translate as translations
from sdcmodeler.cli import main
from sdcmodeler.client import TranslationRefused
from sdcmodeler.deliver import RunRefused
from tests.conftest import BASE, KEY, body_of

SRC = "dmsrc0000000000000000001"


def translating(fake, *, balance=10000, auto_spend=False, would_cover=False, blocked=None, within=True,
                problems=None, unanchored=None, job=None):
    """An SDCStudio that translates: 2 copies, 1 reused; 500 + 200 drafts + 200 publication (publish true)."""
    def estimate(request):
        b = json.loads(request.content)
        publish = b["publish"]
        total = 500 + 200 + (200 if publish else 0)
        shortfall = max(0, total - balance)
        bound = json.dumps({k: b.get(k) for k in ("source", "language", "project", "publish")}, sort_keys=True)
        return {
            "estimate_id": hashlib.sha256(bound.encode()).hexdigest()[:12],
            "source": {"ct_id": b["source"], "kind": "model", "type": "DM", "label": "Vitals", "lang": "en-US"},
            "language": b["language"], "project": {"ct_id": "p-default", "name": "Agent models", "public": False},
            "publish": publish,
            "reuse": [{"source_ct_id": "c1", "ct_id": "c1pt", "type": "XdCount", "label": "Pulso", "published": True}],
            "copy": [{"source_ct_id": "c2", "type": "XdString", "label": "Note"}, {"source_ct_id": "k1", "type": "Cluster", "label": "Vitals"}],
            "unanchored": unanchored or [],
            "model_credits": 500, "draft_credits": 200, "publish_credits": 200 if publish else 0, "total_credits": total,
            "funding": {"balance_credits": balance, "shortfall_credits": shortfall, "covered": shortfall == 0,
                        "blocked_reason": blocked if shortfall else None,
                        "auto_reload": {"would_cover": would_cover, "reloads_needed": 1, "reload_total_usd": "10.00"}},
            "auto_spend": auto_spend, "within_key_limit": within, "problems": problems or [],
        }

    fake.on("POST", "/api/v1/dmgen/translate/estimate/", body=estimate)
    fake.on("POST", "/api/v1/dmgen/translate/", status=202,
            body={"status": "processing", "job_ct_id": "t1", "job_url": "/api/v1/jobs/t1/", "estimated_cost_credits": 1100})
    fake.on("GET", "/api/v1/jobs/t1/", body=job or {"ct_id": "t1", "kind": "translate", "status": "completed", "result": {
        "translated_ct_id": "dmpt", "language": "pt-BR", "published": True, "generated": True,
        "created": [{"source_ct_id": "c2", "ct_id": "c2pt", "type": "XdString", "label": "Nota"}],
        "reused": [{"source_ct_id": "c1", "ct_id": "c1pt", "type": "XdCount", "label": "Pulso", "published": True}],
        "unanchored": [], "dm_ct_id": "dmpt", "model_url": f"{BASE}/app/models/dmpt"}})
    fake.on("GET", "/api/v1/auth/api-tokens/", body={"tokens": [{"prefix": KEY[:12], "spend_limit_credits": None,
                                                                 "auto_spend": auto_spend, "reserved_credits": 0}]})
    return fake


def posts(fake, path):
    return [c for c in fake.calls if c.method == "POST" and c.url.path == path]


def call(name, arguments):
    r = mcp_server._handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    text = r["result"]["content"][0]["text"]
    return (None, text) if r["result"].get("isError") else (json.loads(text), None)


# ------------------------------------------------------------------ client and plan

def test_publish_is_sent_explicitly_and_the_bodies_match(fake):
    c = translating(fake).client()
    p = translations.plan(c, SRC, "pt-BR", publish=False)
    out = translations.run(c, p)
    est, tr = body_of(posts(fake, "/api/v1/dmgen/translate/estimate/")[-1]), body_of(posts(fake, "/api/v1/dmgen/translate/")[0])
    assert est == {"source": SRC, "language": "pt-BR", "publish": False}
    assert tr == {**est, "estimate_id": p["estimate_id"]}
    assert out["status"] == "completed" and out["translated_ct_id"] == "dmpt" and out["model_url"].endswith("/dmpt")


def test_the_plan_prices_drafts_and_publication_and_names_unanchored_values(fake):
    c = translating(fake, unanchored=[{"source_ct_id": "c2", "label": "Note", "value": "mild"}]).client()
    p = translations.plan(c, SRC, "pt-BR", project="p9")
    assert (p["model_credits"], p["draft_credits"], p["publish_credits"], p["total_credits"]) == (500, 200, 200, 900)
    assert p["refusals"] == [] and body_of(posts(fake, "/api/v1/dmgen/translate/estimate/")[-1])["project"] == "p9"
    text = translations.summary(p)
    assert "2 new draft(s), 1 published translation(s) reused free; 900 credits" in text and "mild" in text and "permanent" in text


def test_problems_the_key_and_funding_refuse_before_anything_is_created(fake):
    c = translating(fake, problems=[{"reason": "retired", "error_type": "retired", "ct_id": SRC, "successor": "dmnew"}],
                    within=False, balance=100, blocked="ceiling").client()
    p = translations.plan(c, SRC, "pt-BR")
    assert len(p["refusals"]) == 3 and "dmnew" in p["refusals"][0] and "key may still spend" in p["refusals"][1]
    assert "ceiling" in p["refusals"][2] and p["refusals"][2].startswith("The translation needs")
    with pytest.raises(RunRefused, match="Nothing was translated"):
        translations.run(c, p)
    assert not posts(fake, "/api/v1/dmgen/translate/")


def test_a_short_balance_a_reload_covers_is_not_refused(fake):
    c = translating(fake, balance=100, would_cover=True).client()
    assert translations.plan(c, SRC, "pt-BR")["refusals"] == []


def test_a_409_is_a_translation_refused_with_its_successor(fake):
    c = translating(fake).client()
    fake.on("POST", "/api/v1/dmgen/translate/", status=409, body={"error": "Retired.", "error_type": "retired", "successor": "dmnew"})
    with pytest.raises(TranslationRefused) as exc:
        translations.run(c, translations.plan(c, SRC, "pt-BR"))
    assert (exc.value.error_type, exc.value.successor) == ("retired", "dmnew")


def test_a_deployment_without_translate_says_so(fake):
    c = fake.client()
    fake.on("POST", "/api/v1/dmgen/translate/estimate/", status=404, body=None)
    fake.routes[("POST", "/api/v1/dmgen/translate/estimate/")] = (404, b"<html>Not Found</html>", {"content-type": "text/html"})
    with pytest.raises(Exception, match="does not translate"):
        c.translate_estimate(SRC, "pt-BR")


# ------------------------------------------------------------------ the three outcomes

def test_publishing_failed_leaves_drafts_and_says_translate_again_to_finish(fake):
    c = translating(fake, job={"ct_id": "t1", "status": "failed", "error": "publish failed", "result": {
        "translated_ct_id": "dmpt", "created": [{"ct_id": "c2pt"}], "reused": [], "published": False,
        "finalize_error": "XSD validation failed for c2pt"}}).client()
    out = translations.run(c, translations.plan(c, SRC, "pt-BR"))
    assert out["status"] == "drafts_unpublished" and "XSD" in out["reason"] and "Translate again to finish" in out["next"]


def test_a_translation_that_failed_outright_created_nothing(fake):
    c = translating(fake, job={"ct_id": "t1", "status": "failed", "error": "LLM unavailable", "result": {}}).client()
    with pytest.raises(RunRefused, match="nothing was created and the hold was released"):
        translations.run(c, translations.plan(c, SRC, "pt-BR"))


def test_still_running_when_the_wait_ends_is_in_flight(fake):
    c = translating(fake, job={"ct_id": "t1", "status": "running"}).client()
    out = translations.run(c, translations.plan(c, SRC, "pt-BR"), timeout=0)
    assert out["status"] == "in_flight" and "Do not translate again" in out["next"]
    assert len(posts(fake, "/api/v1/dmgen/translate/")) == 1


def test_a_changed_total_refuses(fake):
    c = translating(fake).client()
    p = translations.plan(c, SRC, "pt-BR")
    p["total_credits"] = 1
    with pytest.raises(RunRefused, match="not the 1 the person saw"):
        translations.run(c, p)


# ------------------------------------------------------------------ MCP policy

def test_estimate_translation_never_spends_even_with_auto_spend(fake, tmp_path):
    mcp_server.configure(translating(fake, auto_spend=True).client(), output_dir=tmp_path)
    out, err = call("estimate_translation", {"source": SRC, "language": "pt-BR"})
    assert err is None and out["total_credits"] == 900 and "summary" in out and "request" not in out
    assert not posts(fake, "/api/v1/dmgen/translate/")


def test_translate_model_needs_the_shown_estimate_without_auto_spend(fake, tmp_path):
    mcp_server.configure(translating(fake).client(), output_dir=tmp_path)
    _, err = call("translate_model", {"source": SRC, "language": "pt-BR"})
    assert "estimate_translation" in err and not posts(fake, "/api/v1/dmgen/translate/")
    est, _ = call("estimate_translation", {"source": SRC, "language": "pt-BR", "review": True})
    _, err = call("translate_model", {"source": SRC, "language": "pt-BR", "estimate_id": est["estimate_id"]})
    assert "changed since the estimate" in err   # review differs, so the id does
    out, _ = call("translate_model", {"source": SRC, "language": "pt-BR", "review": True, "estimate_id": est["estimate_id"]})
    assert out["authorized_by"] == "estimate_shown" and body_of(posts(fake, "/api/v1/dmgen/translate/")[0])["publish"] is False


def test_an_auto_spend_key_translates_in_one_call(fake, tmp_path):
    mcp_server.configure(translating(fake, auto_spend=True).client(), output_dir=tmp_path)
    out, _ = call("translate_model", {"source": SRC, "language": "pt-BR"})
    assert out["authorized_by"] == "auto_spend" and out["status"] == "completed" and out["plan"]["total_credits"] == 900


def test_a_refusal_from_the_server_names_the_successor(fake, tmp_path):
    mcp_server.configure(translating(fake, auto_spend=True).client(), output_dir=tmp_path)
    fake.on("POST", "/api/v1/dmgen/translate/", status=409, body={"error": "Retired", "error_type": "retired", "successor": "dmnew"})
    _, err = call("translate_model", {"source": SRC, "language": "pt-BR"})
    assert "retired" in err and "dmnew" in err and "Nothing was created" in err


# ------------------------------------------------------------------ CLI

def test_cli_translate_plan_spends_nothing(fake, monkeypatch, capsys):
    c = translating(fake).client()
    monkeypatch.setattr("sdcmodeler.cli.SDCStudioClient", lambda base_url=None: c)
    assert main(["translate", SRC, "--language", "pt-BR", "--review", "--plan"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total_credits"] == 700 and out["publish"] is False and not posts(fake, "/api/v1/dmgen/translate/")


def test_cli_translate_yes_runs_and_exit_2_for_unpublished_drafts(fake, monkeypatch, capsys):
    c = translating(fake, job={"ct_id": "t1", "status": "failed", "result": {"translated_ct_id": "dmpt", "finalize_error": "x"}}).client()
    monkeypatch.setattr("sdcmodeler.cli.SDCStudioClient", lambda base_url=None: c)
    assert main(["translate", SRC, "--language", "pt-BR", "--yes"]) == 2
    assert json.loads(capsys.readouterr().out)["authorized_by"] == "yes_flag"


def test_cli_translate_without_a_key_is_refused_not_a_traceback(fake, monkeypatch, capsys):
    c = translating(fake).client(api_key="")
    monkeypatch.setattr("sdcmodeler.cli.SDCStudioClient", lambda base_url=None: c)
    assert main(["translate", SRC, "--language", "pt-BR", "--plan"]) == 1
    assert "SDCSTUDIO_API_KEY" in capsys.readouterr().err


def test_a_same_language_source_answers_problems_without_an_id_and_is_refused(fake, monkeypatch, capsys):
    """What SDCStudio 4.28.0 answers on the local stack: problems and error_type, no estimate_id."""
    c = fake.client()
    fake.on("POST", "/api/v1/dmgen/translate/estimate/", body={
        "problems": [{"reason": "The source is already in en-US.", "error_type": "same_language"}],
        "error": "The source is already in en-US.", "error_type": "same_language"})
    p = translations.plan(c, SRC, "en-US")
    assert p["estimate_id"] is None and p["refusals"] == ["The source is already in en-US."]
    monkeypatch.setattr("sdcmodeler.cli.SDCStudioClient", lambda base_url=None: c)
    assert main(["translate", SRC, "--language", "en-US", "--yes"]) == 1
    assert "already in en-US" in capsys.readouterr().err and not posts(fake, "/api/v1/dmgen/translate/")


def test_an_invalid_language_tag_is_a_translation_refused(fake):
    c = fake.client()
    fake.on("POST", "/api/v1/dmgen/translate/estimate/", status=400,
            body={"error": "Not a BCP 47 language tag: xx-!!", "error_type": "language"})
    with pytest.raises(TranslationRefused) as exc:
        translations.plan(c, SRC, "xx-!!")
    assert exc.value.error_type == "language" and exc.value.status == 400


def test_the_language_registry_is_searchable(fake):
    fake.on("GET", "/api/v1/dmgen/translate/languages/", body={"results": [{"tag": "sw", "name": "Swahili"}]})
    assert fake.client().translate_languages("swahili")["results"][0]["tag"] == "sw"
    assert dict(fake.calls[-1].url.params)["q"] == "swahili"
