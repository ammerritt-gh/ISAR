"""Pure compilation of frozen QoI distinction policy evidence."""
from __future__ import annotations

from typing import Any, Mapping

from isar.eig.scorer import ScalarQoIDiscretization
from isar.schema.eig_policy import (
    MAX_QOI_POLICIES,
    MAX_RUNTIME_VARIANTS,
    QoIDistinctionPolicy,
    QoIDistinctionPolicySet,
    QoIDistinctionVariant,
)


POLICY_COMPILER_REVISION = "isar.offline_eig_policy_compiler/o36.5"
CURRENT_OBJECTIVE_SCOPE_REVISION = "isar.current_objective_comparison_scope/1"


def canonical_scan_window_eig_policy_authoring() -> dict[str, Any]:
    """The card authoring a campaign gets when it enables scan-window EIG without declaring one.

    Scan-window EIG used to require every campaign to hand-author this exact table in
    ``card.policy.toml``; an ordinary campaign that switched the mode on simply refused with
    ``scan_window_eig_authoritative_profile_invalid``, so EIG was reachable only from a
    purpose-built "O41 campaign". There is one profile the restricted selector accepts, so making
    a campaign retype it was ceremony, not choice — this is that one profile, and declaring the
    table in the card still wins over it.

    The identity fields are read from :mod:`isar.eig.o41_authority` rather than restated, because a
    default that drifted from the validator would fail the very campaigns it exists to enable.
    """
    from isar.eig.o41_authority import (
        PRIMARY_PARAMETERS, PRIMARY_POLICY_ID, PRIMARY_POLICY_KIND, PRIMARY_QOI,
        PRIMARY_VARIANT_ID,
    )
    return {
        "policies": [{
            "policy_id": PRIMARY_POLICY_ID,
            "policy_kind": PRIMARY_POLICY_KIND,
            "source_qoi": dict(PRIMARY_QOI),
            "scope": {
                "bind_to_current_campaign_configuration": True,
                "annotation": {
                    "campaign_name": "default scan-window EIG policy",
                    "experiment_description": (
                        "excitation energy at the restricted 0.5 meV absolute partition"),
                    "date": "2026-08-05",
                },
            },
            "provenance": {
                "kind": "explicit_user_analysis/1",
                "source_id": "O41",
                "rationale": "Reviewed Slice 8 V3 positive authority envelope",
            },
            "variants": [{
                "variant_id": PRIMARY_VARIANT_ID,
                "parameters": {**PRIMARY_PARAMETERS,
                               "offsets": list(PRIMARY_PARAMETERS["offsets"])},
            }],
        }],
    }


def _identity_value(value):
    """Order-stable, hashable form of an authoring value for identity comparison."""
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _identity_value(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_identity_value(item) for item in value)
    return value


def policy_identity(policy_set: QoIDistinctionPolicySet | None):
    """Binding-free identity of a compiled policy set — everything except which campaign it is for.

    Used to tell "this objective carries the canonical default" from "this objective carries a
    hand-authored policy" without comparing fingerprints, which legitimately differ between two
    compilations of the same policy against different objective scopes.

    Only the scope *binding* (``kind`` and the campaign ``fingerprint``) is excluded. The scope's
    annotation and the policy's provenance are included deliberately: they are the scientist's
    attribution of why this partition is being measured, and treating a policy that differs only
    there as the default would silently rewrite that attribution during a rebind.
    """
    if policy_set is None:
        return None
    return tuple(
        (policy.policy_id, policy.policy_kind,
         _identity_value(policy.source_qoi),
         _identity_value(dict(policy.scope).get("annotation")),
         _identity_value(policy.provenance),
         tuple((variant.variant_id, _identity_value(variant.parameters))
               for variant in policy.variants))
        for policy in policy_set.policies)


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{name} fields are invalid")


def _valid_fp(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def compile_qoi_distinction_policy(
    policy: QoIDistinctionPolicy,
    variant: QoIDistinctionVariant,
) -> ScalarQoIDiscretization:
    """Compile one supported scalar variant; preserve unknown versioned kinds as unsupported."""
    if not isinstance(policy, QoIDistinctionPolicy) or variant not in policy.variants:
        raise TypeError("policy and variant must be authenticated records")
    if policy.policy_kind != "isar.scalar_absolute_partitions/o36.5":
        raise ValueError("qoi_policy_unsupported")
    parameters = variant.parameters
    _exact_keys(parameters, {"origin", "bin_width", "offsets"}, "scalar policy parameters")
    return ScalarQoIDiscretization(
        qoi_id=policy.source_qoi["id"],
        qoi_unit=policy.source_qoi["unit"],
        origin=parameters["origin"],
        bin_width=parameters["bin_width"],
        offsets=tuple(parameters["offsets"]),
    )


def compile_card_eig_policy_set(
    authoring_value: Mapping[str, Any] | None,
    objective_scope: Mapping[str, str],
    *,
    max_runtime_variants: int = MAX_RUNTIME_VARIANTS,
) -> QoIDistinctionPolicySet | None:
    """Materialize authoring-only campaign bindings into an authenticated policy set.

    Card authoring deliberately has no evidence contracts or fingerprints.  The only live scope
    syntax is ``bind_to_current_campaign_configuration = true``; compilation replaces it with the
    exact objective-scope fingerprint before any frozen objective is emitted.
    """
    if authoring_value in (None, {}):
        return None
    if not isinstance(max_runtime_variants, int) or isinstance(max_runtime_variants, bool):
        raise ValueError("max_runtime_variants must be an integer")
    if not 1 <= max_runtime_variants <= MAX_RUNTIME_VARIANTS:
        raise ValueError(f"max_runtime_variants must be between 1 and {MAX_RUNTIME_VARIANTS}")
    _exact_keys(objective_scope, {"producer_revision", "fingerprint"}, "objective scope")
    if (
        objective_scope["producer_revision"] != CURRENT_OBJECTIVE_SCOPE_REVISION
        or not _valid_fp(objective_scope["fingerprint"])
    ):
        raise ValueError("objective scope is invalid")
    _exact_keys(authoring_value, {"policies"}, "card scan-window EIG policy set")
    raw_policies = authoring_value["policies"]
    if not isinstance(raw_policies, list):
        raise ValueError("card scan-window EIG policies must be an array of tables")
    if len(raw_policies) > MAX_QOI_POLICIES:
        raise ValueError(f"at most {MAX_QOI_POLICIES} QoI distinction policies are allowed")

    policies: list[QoIDistinctionPolicy] = []
    for raw in raw_policies:
        _exact_keys(
            raw,
            {"policy_id", "policy_kind", "source_qoi", "scope", "provenance", "variants"},
            "card QoI distinction policy",
        )
        scope = raw["scope"]
        _exact_keys(
            scope,
            {"bind_to_current_campaign_configuration", "annotation"},
            "card QoI distinction policy scope",
        )
        if scope["bind_to_current_campaign_configuration"] is not True:
            raise ValueError("live QoI policy scope must bind to current campaign configuration")
        variants_raw = raw["variants"]
        if not isinstance(variants_raw, list) or not variants_raw:
            raise ValueError("card QoI distinction policy variants must be a non-empty array")
        if len(variants_raw) > max_runtime_variants:
            raise ValueError(
                f"policy variants exceed max_runtime_variants={max_runtime_variants}")
        variants: list[QoIDistinctionVariant] = []
        for variant_raw in variants_raw:
            _exact_keys(variant_raw, {"variant_id", "parameters"}, "card policy variant")
            variants.append(QoIDistinctionVariant(
                variant_id=variant_raw["variant_id"],
                parameters=variant_raw["parameters"],
            ))
        variants.sort(key=lambda item: item.variant_id)
        policy = QoIDistinctionPolicy(
            policy_id=raw["policy_id"],
            policy_kind=raw["policy_kind"],
            source_qoi=raw["source_qoi"],
            scope={
                "kind": "campaign_configuration",
                "fingerprint": objective_scope["fingerprint"],
                "annotation": scope["annotation"],
            },
            provenance=raw["provenance"],
            variants=tuple(variants),
        )
        if policy.policy_kind == "isar.scalar_absolute_partitions/o36.5":
            for variant in policy.variants:
                compile_qoi_distinction_policy(policy, variant)
        policies.append(policy)
    policies.sort(key=lambda item: item.policy_id)
    return QoIDistinctionPolicySet(tuple(policies))


def validate_card_eig_policy_authoring(authoring_value: Mapping[str, Any] | None) -> None:
    """Validate inline card syntax independently of any real campaign identity."""
    compile_card_eig_policy_set(
        authoring_value,
        {"producer_revision": CURRENT_OBJECTIVE_SCOPE_REVISION, "fingerprint": "0" * 64},
    )


__all__ = [
    "POLICY_COMPILER_REVISION", "CURRENT_OBJECTIVE_SCOPE_REVISION",
    "compile_qoi_distinction_policy", "compile_card_eig_policy_set",
    "validate_card_eig_policy_authoring",
]
