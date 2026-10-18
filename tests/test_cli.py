import json

from sdcmodeler.cli import main


def test_estimate_from_a_file(tmp_path, capsys):
    tree = tmp_path / "tree.json"
    tree.write_text(json.dumps({"label": "R", "components": [{"label": "A", "data_type": "XdString"}], "clusters": []}))
    assert main(["estimate", str(tree), "--title", "R"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total_credits"] == 600 and out["mode"] == "mixed"


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "sdcmodeler" in capsys.readouterr().out
