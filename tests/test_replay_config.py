"""One request schema for UI import/export, API creation and presets."""

import json
from copy import deepcopy
from unittest.mock import patch

import pytest
from test_replay import config
from test_replay_integration import token, web_case  # noqa: F401

from crypto_bot.replay.config import ConfigValidationError, input_config, validate


def imported():
    return input_config(
        config(
            name="Research configuration",
            start="2025-01-01T02:00:00+02:00",
            end="2025-01-03T02:00:00+02:00",
            universe_size=20,
            dataset_role="OUT_OF_SAMPLE",
            fallback_5m=True,
            conservative=False,
            minimum_quote_volume="5000000.25",
            spread_approximation_pct="0.023",
            variants=[
                dict(
                    name="Baseline",
                    strategy_parameters={
                        "min_score": "73.25",
                        "require_delta": False,
                        "require_watch": False,
                    },
                ),
                dict(
                    name="Trailing",
                    exit_policy="trailing_pct",
                    exit_parameters={"activation_pct": "1.55", "distance_pct": ".45"},
                    risk_parameters={
                        "initial_paper_balance": "1234.56",
                        "max_open_positions": 5,
                    },
                ),
            ],
        )
    )


def test_complete_config_round_trip_uses_request_schema():
    value = imported()
    before = deepcopy(value)
    encoded = json.dumps(value)
    snapshot = validate(json.loads(encoded))
    assert input_config(snapshot) == value == before
    assert "paper_settings" not in value["variants"][0]
    assert "schema_version" not in value
    assert (
        snapshot["variants"][1]["paper_settings"]["initial_paper_balance"] == "1234.56"
    )
    assert not value["variants"][0]["strategy_parameters"]["require_delta"]


def test_valid_import_is_pure_and_normal_post_accepts_same_json(request):
    client, db, _ = request.getfixturevalue("web_case")
    headers = token(client)
    value = imported()
    before = db.runs()
    with patch(
        "crypto_bot.replay.models.ReplayStore.create",
        side_effect=AssertionError("Validation must never create a run"),
    ):
        r = client.post("/api/replay/config/validate", json=value, headers=headers)
    assert r.status_code == 200 and r.json == {"valid": True, "config": value}
    assert db.runs() == before
    assert db.presets() == []
    started = client.post("/api/replay/runs", json=r.json["config"], headers=headers)
    assert started.status_code == 202
    assert db.get(started.json["run_id"])["config"] == validate(value)


@pytest.mark.parametrize(
    "changes,path",
    [
        ({"unknown": True}, "unknown"),
        ({"universe_size": "20"}, "universe_size"),
        ({"start": "not-a-date"}, "start"),
        ({"end": "2024-12-31"}, "end"),
        ({"start": 1735689600}, "start"),
        ({"end": "2025-01-03T00:01:00Z"}, "end"),
        ({"fallback_5m": "false"}, "fallback_5m"),
        ({"minimum_quote_volume": "bad"}, "minimum_quote_volume"),
        ({"spread_approximation_pct": ".3"}, "spread_approximation_pct"),
        ({"candidate_symbols": ["../BTCUSDT"]}, "candidate_symbols"),
        ({"variants": [{"strategy": "unknown-strategy"}]}, "variants[0].strategy"),
        ({"variants": [{"exit_policy": "live-exit"}]}, "variants[0].exit_policy"),
        (
            {
                "variants": [
                    {"exit_policy": "fixed_tp", "exit_parameters": {"tp_pct": "bad"}}
                ]
            },
            "variants[0].exit_parameters.tp_pct",
        ),
        (
            {"variants": [{"strategy_parameters": {"min_score": "bad"}}]},
            "variants[0].strategy_parameters.min_score",
        ),
        (
            {"variants": [{"strategy_parameters": {"min_score": "101"}}]},
            "variants[0].strategy_parameters.min_score",
        ),
        (
            {"variants": [{"risk_parameters": {"max_open_positions": 1.5}}]},
            "variants[0].risk_parameters.max_open_positions",
        ),
        (
            {"variants": [{"risk_parameters": {"pyramiding": True}}]},
            "variants[0].risk_parameters.pyramiding",
        ),
        (
            {"variants": [{"strategy_parameters": {"extra": 1}}]},
            "variants[0].strategy_parameters.extra",
        ),
    ],
)
def test_invalid_import_has_same_per_field_errors_as_normal_post(
    request, changes, path
):
    client, db, _ = request.getfixturevalue("web_case")
    headers = token(client)
    value = imported() | changes
    for endpoint in ("/api/replay/config/validate", "/api/replay/runs"):
        r = client.post(endpoint, json=value, headers=headers)
        assert r.status_code == 400
        assert r.json["valid"] is False
        assert path in [e["field"] for e in r.json["errors"]]
        assert all(e["message"] for e in r.json["errors"])
    assert db.runs() == []


def test_errors_across_multiple_variants_and_both_unknown_ids():
    value = imported()
    value["variants"] = [
        dict(strategy="bad", exit_policy="bad"),
        dict(risk_parameters={"paper_fee_pct": "oops", "max_open_positions": "3"}),
    ]
    with pytest.raises(ConfigValidationError) as caught:
        validate(value)
    assert {e["field"] for e in caught.value.errors} == {
        "variants[0].strategy",
        "variants[0].exit_policy",
        "variants[1].risk_parameters.paper_fee_pct",
        "variants[1].risk_parameters.max_open_positions",
    }


def test_malformed_json_and_csrf_do_not_create_runs(request):
    client, db, _ = request.getfixturevalue("web_case")
    assert (
        client.post("/api/replay/config/validate", json=imported()).status_code == 403
    )
    headers = token(client)
    for body in ('{"start":', "null", "[]"):
        r = client.post(
            "/api/replay/config/validate",
            data=body,
            content_type="application/json",
            headers=headers,
        )
        assert r.status_code == 400 and r.json["errors"][0]["field"] == "$"
    assert db.runs() == []


def test_missing_dates_identified_independently():
    with pytest.raises(ConfigValidationError) as caught:
        validate({})
    assert {e["field"] for e in caught.value.errors} == {"start", "end"}
