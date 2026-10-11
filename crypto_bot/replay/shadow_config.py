"""Matched-entry job schema; delegates policy parameters to replay validation."""

import re

from crypto_bot.replay.config import at_field, invalid, object_fields, variant


def validate(value, source):
    object_fields(value, ("name", "anchor_run_id", "anchor_variant", "policies"), "$")
    run_id = value.get("anchor_run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        invalid("anchor_run_id", "Choose a completed replay run")
    anchor = at_field("anchor_run_id", lambda: source.get(run_id))
    if anchor["status"] != "COMPLETED":
        invalid("anchor_run_id", "Anchor must be COMPLETED; entries are immutable")
    index = value.get("anchor_variant", 0)
    if type(index) is not int or not 0 <= index < len(anchor["variants"]):
        invalid("anchor_variant", "Unknown anchor variant")
    policies = value.get("policies")
    if not isinstance(policies, list) or not 1 <= len(policies) <= 10:
        invalid("policies", "Choose 1..10 shadow policies")
    parsed, ids = [], set()
    for i, p in enumerate(policies):
        path = f"policies[{i}]"
        object_fields(p, ("id", "name", "exit_policy", "exit_parameters"), path)
        ident = p.get("id", f"policy_{i + 1}")
        if not isinstance(ident, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,40}", ident
        ):
            invalid(path + ".id", "Use 1..40 letters, digits, underscore or hyphen")
        if ident in ids:
            invalid(path + ".id", "Policy IDs must be unique")
        ids.add(ident)
        # The normal replay registry/schema remains the sole parameter validator.
        normalized = variant(
            dict(
                name=p.get("name", ident),
                exit_policy=p.get("exit_policy", "baseline_v1"),
                exit_parameters=p.get("exit_parameters", {}),
            ),
            i,
        )
        parsed.append(
            dict(
                id=ident,
                name=normalized["name"],
                exit_policy=normalized["exit_policy"],
                exit_parameters=normalized["exit_parameters"],
            )
        )
    name = value.get("name", "Shadow Exit Research")
    if not isinstance(name, str) or not 1 <= len(name) <= 80:
        invalid("name", "Use 1..80 characters")
    return dict(name=name, anchor_run_id=run_id, anchor_variant=index, policies=parsed)
