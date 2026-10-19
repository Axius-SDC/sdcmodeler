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
    # The new nested cluster Context is minted too; the root cluster is the model's (SDCStudio 4.17.5).
    assert [(m["label"], m["data_type"]) for m in e["mint"]] == [("Pulse", "XdCount"), ("Context", "Cluster"), ("Note", "XdString")]
    assert e["mint"][2]["path"] == "Vitals/Context/Note"
    assert e["total_credits"] == ASSEMBLE_MODEL_CREDITS + 3 * MINT_COMPONENT_CREDITS == 800
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


def test_a_new_cluster_alone_is_billed_but_takes_the_reuse_path():
    tree = {"label": "Root", "components": [{"ct_id": "a" * 24}],
            "clusters": [{"label": "Group", "components": [{"ct_id": "b" * 24}], "clusters": []}]}
    e = estimate_tree(tree, "Grouped")
    assert (e["mode"], e["total_credits"]) == ("reuse", 600)


def test_the_title_is_trimmed_before_hashing_as_the_server_does():
    assert estimate_tree(TREE, "  Vitals ")["estimate_id"] == estimate_tree(TREE, "Vitals")["estimate_id"]
