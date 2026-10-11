"""Additional model tests: run separately in the isolated research environment."""

from copy import deepcopy
from datetime import datetime, timedelta

from research.entry_market_context import ALL, COIN, predictive


def records():
    rows = []
    for month in (1, 4, 7, 10):
        for day in range(80):
            stamp = datetime(2024, month, 1) + timedelta(days=day)
            label = day % 2
            rows.append(
                {
                    "decision_time": stamp.isoformat() + "Z",
                    "activated": bool(label),
                    "features": {k: float(label) for k in ALL},
                    "anchor_trade": {
                        "exit_time": (stamp + timedelta(hours=1)).isoformat() + "Z"
                    },
                }
            )
    return rows


def test_future_quarter_context_changes_cannot_change_earlier_model_folds():
    source = records()
    original = predictive(source)
    changed = deepcopy(source)
    for r in changed:
        if r["decision_time"] >= "2024-10-01":
            r["features"] = dict.fromkeys(ALL, -1000.0)
    updated = predictive(changed)
    for name, model in original["models"].items():
        assert model["fold_auc"][:2] == updated["models"][name]["fold_auc"][:2]
    assert source == records()


def test_fixed_feature_sets_exclude_outcome_labels_and_leave_inputs_unchanged():
    source = records()
    result = predictive(source)
    assert result["feature_sets"]["coin_local"] == COIN
    assert result["feature_sets"]["combined"] == ALL
    assert not set(ALL) & {
        "activation_075r_time",
        "realized_r",
        "exit_time",
        "exit_reason",
    }
    assert all(v["mean_auc"] == 1 for v in result["models"].values())
    assert source == records()
    assert all(
        f["train_last_label_available"] < f["test_start"] for f in result["folds"]
    )
