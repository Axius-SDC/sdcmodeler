from sdcmodeler.pricing import ASSEMBLE_MODEL_CREDITS, MINT_COMPONENT_CREDITS, estimate_tree

TREE = {
    "label": "Vitals",
    "components": [{"ct_id": "abc123abc123abc123abc123"}, {"label": "Pulse", "data_type": "XdCount"}],
    "clusters": [
        {"label": "Context", "components": [{"label": "Note", "data_type": "XdString"}], "clusters": []},
        {"ct_id": "def456def456def456def456"},
    ],
}


def test_reuse_is_free_and_mints_are_counted():
    e = estimate_tree(TREE, "Vitals")
    assert [r["ct_id"] for r in e["reuse"]] == ["abc123abc123abc123abc123", "def456def456def456def456"]
    assert [m["label"] for m in e["mint"]] == ["Pulse", "Note"]
    assert e["mint"][1]["path"] == "Vitals/Context/Note"
    assert e["total_credits"] == ASSEMBLE_MODEL_CREDITS + 2 * MINT_COMPONENT_CREDITS
    assert e["mode"] == "mixed" and e["problems"] == []


def test_estimate_id_depends_on_title_and_tree_but_not_on_key_order():
    a = estimate_tree(TREE, "Vitals")
    b = estimate_tree({"clusters": TREE["clusters"], "components": TREE["components"], "label": "Vitals"}, "Vitals")
    c = estimate_tree(TREE, "Vitals 2")
    assert a["estimate_id"] == b["estimate_id"] != c["estimate_id"]
    assert len(a["estimate_id"]) == 12


def test_problems_are_named_not_hidden():
    e = estimate_tree({"label": "Root", "components": [{"label": "No type"}], "clusters": [{"components": []}]}, "t")
    assert len(e["problems"]) == 2 and "ct_id (reuse) or label and data_type (mint)" in e["problems"][0]
    assert e["mode"] == "reuse"
