import json

from sdcmodeler.cli import main


def test_estimate_from_a_file(tmp_path, capsys):
    tree = tmp_path / "tree.json"
    tree.write_text(json.dumps({"label": "R", "components": [{"label": "A", "data_type": "XdString"}], "clusters": []}))
    assert main(["estimate", str(tree), "--title", "R"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total_credits"] == 700 and out["mode"] == "mixed"


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "sdcmodeler" in capsys.readouterr().out


def _cli_on(fake, monkeypatch):
    from sdcmodeler import cli
    monkeypatch.setattr(cli, "SDCStudioClient", lambda base_url=None: fake.client())


def test_build_draft_prints_the_bench_link(tmp_path, capsys, monkeypatch, fake):
    _cli_on(fake, monkeypatch)
    fake.on("POST", "/api/v1/dmgen/assemble/", body={"ct_id": "m1", "published": False, "bench_url": "/app/models/bench/m1"})
    tree = tmp_path / "tree.json"
    tree.write_text(json.dumps({"label": "R", "components": [], "clusters": []}))
    assert main(["build", str(tree), "--title", "R", "--draft"]) == 0
    err = capsys.readouterr().err
    assert "Open the draft on the Bench: https://sdcstudio.test.local/app/models/bench/m1" in err


def test_build_draft_on_the_pipeline_path_says_where_the_link_will_be(tmp_path, capsys, monkeypatch, fake):
    _cli_on(fake, monkeypatch)
    fake.on("POST", "/api/v1/dmgen/assemble/", status=202, body={"status": "processing", "job_ct_id": "j9"})
    tree = tmp_path / "tree.json"
    tree.write_text(json.dumps({"label": "R", "components": [], "clusters": []}))
    assert main(["build", str(tree), "--title", "R", "--draft"]) == 0
    assert "bench_url opens the draft on the Bench" in capsys.readouterr().err


def test_a_published_build_prints_no_bench_line(tmp_path, capsys, monkeypatch, fake):
    _cli_on(fake, monkeypatch)
    fake.on("POST", "/api/v1/dmgen/assemble/", body={"ct_id": "m1", "published": True})
    tree = tmp_path / "tree.json"
    tree.write_text(json.dumps({"label": "R", "components": [], "clusters": []}))
    assert main(["build", str(tree), "--title", "R"]) == 0
    assert "Bench" not in capsys.readouterr().err
