"""Whole-run exports remain immutable, deterministic and independent of selection."""

import hashlib
import io
import json
import sqlite3
import zipfile

import pytest
from test_replay_companion_isolation import controlled, execute  # noqa: F401
from test_replay_integration import web_case  # noqa: F401

from crypto_bot.replay.config import validate
from crypto_bot.replay.engine import ReplayEngine
from crypto_bot.replay.exporter import bundle, payload
from crypto_bot.replay.models import ReplayStore
from crypto_bot.replay.research_exporter import (
    ReadOnlyReplayStore,
    research_bundle,
    variant_labels,
)


@pytest.fixture
def research_case(request):
    client, writable, production = request.getfixturevalue("web_case")
    request.getfixturevalue("controlled")
    store, rid = execute(writable.path.parent, list("ABCDE"))
    return client, store, rid


def opened(blob):
    archive = zipfile.ZipFile(io.BytesIO(blob))
    assert archive.testzip() is None
    return archive


def test_complete_bundle_all_five_variants_provenance_and_determinism(research_case):
    client, store, rid = research_case
    readonly = ReadOnlyReplayStore(store.path)
    blob = research_bundle(readonly, rid)
    assert blob == research_bundle(readonly, rid)
    response = client.get(f"/api/replay/runs/{rid}/research-bundle.zip?variant=4")
    assert response.status_code == 200 and response.data == blob
    assert response.mimetype == "application/zip"
    assert rid in response.headers["Content-Disposition"]
    z = opened(blob)
    run = store.get(rid)
    manifest = json.loads(z.read("manifest.json"))
    summary = json.loads(z.read("run-summary.json"))
    validation = json.loads(z.read("export-validation.json"))
    assert manifest["run_id"] == summary["run_id"] == validation["run_id"] == rid
    assert manifest["config_hash"] == run["metadata"]["config_hash"]
    assert manifest["market_context_version"] == "ISOLATED_TOPN_V1"
    assert validation["read_only_database_access"]["database_writes"] is False
    assert summary["winner_selected"] is False and len(summary["comparisons"]) == 10
    frozen = json.loads(z.read("validated-config.json"))
    public = json.loads(z.read("config.json"))
    assert frozen == run["config"]
    assert "market_context_version" not in public
    # This adversarial fixture changes internal scanner sampling after validation.
    # Public parameters remain importable; the frozen snapshot records that input.
    assert validate(public)["variants"] == frozen["variants"]
    assert manifest["exact_git_sha"] == run["metadata"]["git_commit"]
    for name, record in manifest["files"].items():
        assert hashlib.sha256(z.read(name)).hexdigest() == record["sha256"]
        assert len(z.read(name)) == record["bytes"]
    for i, label in enumerate("ABCDE"):
        full = json.loads(z.read(label + ".analysis.json"))
        compact = json.loads(z.read(label + ".compact.json"))
        trades = json.loads(z.read(label + ".trades.json"))
        assert full == payload(readonly, rid, i)
        assert compact == payload(readonly, rid, i, True)
        assert trades["variant"] == i and trades["variant_label"] == label
        assert trades["trades"] and all(
            t["variant"] == i and t["run_id"] == rid for t in trades["trades"]
        )
        assert summary["variants"][i]["config"] == run["variants"][i]["config"]
        assert full["variant"] == run["variants"][i]["config"]
        assert summary["variants"][i]["closed"] == len(full["trades"])


def test_export_routes_never_write_migrate_or_start_replay(research_case, monkeypatch):
    client, store, rid = research_case
    before = store.path.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("Export attempted a write/migration/replay")

    monkeypatch.setattr(ReplayStore, "__init__", forbidden)
    monkeypatch.setattr(ReplayStore, "create", forbidden)
    monkeypatch.setattr(ReplayEngine, "run", forbidden)
    real_connect = sqlite3.connect
    calls = []

    def guarded(database, *args, **kwargs):
        # Replay export must open only mode=ro; production heartbeat may use ORM.
        if str(store.path) in str(database):
            assert "mode=ro" in str(database) and kwargs.get("uri")
            calls.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded)
    for suffix in [
        "research-bundle.zip",
        "export.json?variant=4",
        "export.compact.json?variant=4",
        "export.csv?variant=4",
        "export.zip?variant=4",
    ]:
        assert client.get(f"/api/replay/runs/{rid}/{suffix}").status_code == 200
    assert calls and store.path.read_bytes() == before


def test_existing_variant_links_and_bundle_still_work(research_case):
    client, store, rid = research_case
    page = client.get(f"/replay/runs/{rid}?variant=4").get_data(as_text=True)
    assert "DOWNLOAD VARIANT BUNDLE" in page and "DOWNLOAD FULL BUNDLE" not in page
    assert (
        page.index("Run Progress")
        < page.index("RESEARCH EXPORT")
        < page.index("Variant / Results")
    )
    for suffix in ["json", "compact.json", "csv", "zip"]:
        assert f"export.{suffix}?variant=4" in page
        assert (
            client.get(f"/api/replay/runs/{rid}/export.{suffix}?variant=4").status_code
            == 200
        )
    z = opened(client.get(f"/api/replay/runs/{rid}/export.zip?variant=4").data)
    assert json.loads(z.read("analysis.json")) == payload(store, rid, 4)
    assert set(z.namelist()) == set(opened(bundle(store, rid, 4)).namelist())


@pytest.mark.parametrize(
    "status",
    ["QUEUED", "DOWNLOADING", "WARMUP", "RUNNING", "PAUSED", "FAILED", "CANCELLED"],
)
def test_non_completed_runs_have_no_ready_research_bundle(research_case, status):
    client, store, rid = research_case
    store.update(rid, status=status)
    assert client.get(f"/api/replay/runs/{rid}/research-bundle.zip").status_code == 409
    page = client.get(f"/replay/runs/{rid}").get_data(as_text=True)
    anchor = page.split('id="replay-research-download"')[1].split("</a>")[0]
    assert 'aria-disabled="true"' in anchor and "href=" not in anchor
    assert "Available when this run is COMPLETED." in page


def test_unknown_run_and_corrupted_config_rejected(research_case):
    client, store, rid = research_case
    assert client.get("/api/replay/runs/unknown/research-bundle.zip").status_code == 404
    with store.connection() as db:
        meta = json.loads(
            db.execute(
                "select metadata from replay_runs where id=?", (rid,)
            ).fetchone()[0]
        )
        meta["config_hash"] = "wrong"
        db.execute(
            "update replay_runs set metadata=? where id=?", (json.dumps(meta), rid)
        )
    assert client.get(f"/api/replay/runs/{rid}/research-bundle.zip").status_code == 409


def test_export_does_not_initialize_missing_replay_storage(
    request, tmp_path, monkeypatch
):
    client, _, _ = request.getfixturevalue("web_case")
    root = tmp_path / "missing-export-storage"
    monkeypatch.setenv("REPLAY_DATA_DIR", str(root))
    assert client.get("/api/replay/runs/unknown/research-bundle.zip").status_code == 404
    assert client.get("/api/replay/runs/unknown/export.zip").status_code == 404
    assert not root.exists()


def test_entry_validation_bundle_afgh_uses_only_requested_run(request):
    from test_institutional_entry_v1 import small_run

    client, writable, _ = request.getfixturevalue("web_case")
    request.getfixturevalue("controlled")
    store, rid = small_run(writable.path.parent, list("AFGH"))
    _, foreign = execute(writable.path.parent, list("ABCDE"))
    before = store.path.read_bytes()
    response = client.get(f"/api/replay/runs/{rid}/research-bundle.zip?variant=3")
    assert response.status_code == 200
    z = opened(response.data)
    expected = {
        "manifest.json",
        "run-summary.json",
        "config.json",
        "validated-config.json",
        "export-validation.json",
        "run-metadata.json",
        "comparison.json",
    } | {
        f"{label}.{kind}.json"
        for label in "AFGH"
        for kind in ("analysis", "compact", "trades")
    }
    assert set(z.namelist()) == expected
    for name in z.namelist():
        assert foreign not in z.read(name).decode()
    for i, label in enumerate("AFGH"):
        full = json.loads(z.read(label + ".analysis.json"))
        compact = json.loads(z.read(label + ".compact.json"))
        trades = json.loads(z.read(label + ".trades.json"))
        assert full == payload(ReadOnlyReplayStore(store.path), rid, i)
        assert compact == payload(ReadOnlyReplayStore(store.path), rid, i, True)
        assert trades["run_id"] == rid and trades["variant"] == i
        assert trades["variant_label"] == label
    summary = json.loads(z.read("run-summary.json"))
    assert [v["variant_label"] for v in summary["variants"]] == list("AFGH")
    assert set(summary["comparisons"]) == {
        "A_vs_F",
        "A_vs_G",
        "A_vs_H",
        "F_vs_G",
        "F_vs_H",
        "G_vs_H",
    }
    assert store.path.read_bytes() == before and len(store.runs()) == 2


@pytest.mark.parametrize(
    "names", [["F", "F"], ["Unnamed", "F"], ["../A", "G"], ["Alpha", "Gamma"]]
)
def test_ambiguous_labels_fall_back_to_safe_unique_indices(names):
    assert variant_labels([{"config": {"name": n}} for n in names]) == ["A", "B"]


def test_explicit_study_labels_are_retained():
    assert variant_labels(
        [
            {"config": {"name": n}}
            for n in ["A - Control", "F - Delta", "G — ATR", "H – Range"]
        ]
    ) == list("AFGH")
