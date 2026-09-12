"""The adapter factory table: one place where a configured NAME becomes an object.

Requirement 23.6 says an adapter is selected by name from a registry and an unregistered name
is rejected. `config/loader.py` owns the registry — the names configuration may use, and which
is the default. This module owns the other half: what each of those names BUILDS.

THE TWO HALVES CAN DISAGREE, WHICH IS THE FAILURE THIS MODULE'S TEST EXISTS TO CATCH. A
registry entry with no factory is a name the loader happily accepts and the composition root
then cannot build — a startup crash for a value the configuration documented as valid. A
factory with no registry entry is dead code that no configuration can reach. Neither is
visible by reading one file, so `tests/unit/test_composition.py` asserts the two agree
EXACTLY, in both directions.

WHY A TABLE RATHER THAN A FUNCTION WITH BRANCHES. Req 23.6's stated reason is that "adding an
adapter is a registry entry rather than a new branch". A dict keeps that true: adding an
adapter is one line here and one in the registry, and the agreement test fails until both
exist. An if/elif chain would let the two drift silently, because nothing structural connects
a missing branch to the name that needed it.

WHAT IS NOT HERE. No factory takes configuration and reads it itself; each takes
already-validated values from `AdvisorConfig`. Nothing in this module constructs a Strands
`Agent` or an AgentCore app either — Req 32.2 keeps the AgentCore dependency at the deployment
boundary, and an import here would put it one `import` away from the domain.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from aqm_advisor.adapters.audit.dynamodb import DynamoDbAdviceAuditStore
from aqm_advisor.adapters.guardrail.bedrock import ApplyGuardrailChecker
from aqm_advisor.adapters.local import (
    InMemoryAdviceAuditStore,
    LocalGuardrailChecker,
    RecordingAssociationTrigger,
    ScriptedServingClient,
)
from aqm_advisor.adapters.model.bedrock import build_bedrock_model
from aqm_advisor.adapters.model.scripted import ScriptedModel
from aqm_advisor.adapters.serving.http import HttpServingClient
from aqm_advisor.ports.clock import FixedClock, SystemClock

ADAPTER_FACTORIES: Final[Mapping[str, Mapping[str, object]]] = {
    "serving_client": {
        "scripted": ScriptedServingClient,
        "http": HttpServingClient,
    },
    "guardrail_checker": {
        "local": LocalGuardrailChecker,
        "bedrock": ApplyGuardrailChecker,
    },
    "advice_audit_store": {
        "memory": InMemoryAdviceAuditStore,
        "dynamodb": DynamoDbAdviceAuditStore,
    },
    "association_trigger": {
        # The only port with no production factory, and deliberately so — task 18 established
        # that this service does not host the trigger, because Service 2 drives the derivation
        # from its own schedule and exposes nothing to call. The registry says the same thing.
        "recording": RecordingAssociationTrigger,
    },
    "model": {
        "scripted": ScriptedModel,
        "bedrock": build_bedrock_model,
    },
    "clock": {
        "system": SystemClock,
        "fixed": FixedClock,
    },
}
"""Every configured adapter name and what it builds.

A CALLABLE, not an instance. Several of these need per-turn or per-process arguments the
composition root supplies — a credential, a table name, a fixed instant — so the table maps a
name to the thing that CONSTRUCTS the adapter and never to a shared object. A table of
instances would also make two turns share one `RecordingAssociationTrigger`, which is exactly
the cross-turn leakage the per-turn tool construction rule exists to prevent.
"""

__all__ = ["ADAPTER_FACTORIES"]
