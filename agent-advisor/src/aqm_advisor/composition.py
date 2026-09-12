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

from collections.abc import Callable, Mapping, Sequence
from typing import Final

from strands import Agent
from strands.models.model import Model

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
from aqm_advisor.agent.advisory import AdvisoryTurnPipeline
from aqm_advisor.agent.audit import AuditWriter
from aqm_advisor.agent.generation import ModelGeneration
from aqm_advisor.agent.tools import RetrievalRecorder, build_retrieval_tools
from aqm_advisor.agent.verification import VerificationHook, VerificationLedger
from aqm_advisor.domain.idempotency import TurnIdentity
from aqm_advisor.domain.models import AdvisoryRequest
from aqm_advisor.domain.redflag import RedFlagRule
from aqm_advisor.observability.logging import EventLogger
from aqm_advisor.ports.clock import Clock, FixedClock, SystemClock
from aqm_advisor.ports.protocols import (
    AdviceAuditStore,
    GuardrailChecker,
    ServingClient,
    ServingClientError,
)

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

__all__ = ["ADAPTER_FACTORIES", "IdentityUnavailableError", "build_pipeline_factory"]


class IdentityUnavailableError(RuntimeError):
    """Raised when no lawful source of the pseudonymous user identity is configured.

    THIS IS A SPEC GAP MADE OPERATIONAL, not a missing feature. Req 20.2 requires the audit
    record to carry the pseudonymous user identity. Req 5.6 forbids this service from decoding,
    parsing or validating the credential — "a component that parses a token is a component that
    can log a claim". The AgentCore SDK forwards only `Authorization`, the session id, the
    request id and caller-supplied custom headers, so no verified claim reaches this process.
    And Service 2 only VALIDATES a `userId` it is given; it never returns one.

    So there is no compliant source today, and the identity is an INJECTED resolver rather than
    something this module invents. Raising here keeps two things true: the container builds and
    the composition root is complete, while a turn that would write an unattributable audit
    record fails loudly instead of writing a wrong subject `forget_user` could never erase.
    """


def build_pipeline_factory(
    *,
    identity_for: Callable[[AdvisoryRequest], TurnIdentity],
    serving_client: ServingClient,
    guardrail: GuardrailChecker,
    audit_store: AdviceAuditStore,
    model: Model,
    clock: Clock,
    logger: EventLogger,
    red_flag_rules: Sequence[RedFlagRule],
    forbidden_patterns: Sequence[str],
    emergency_guidance: str,
    system_prompt: str,
) -> Callable[[AdvisoryRequest], AdvisoryTurnPipeline]:
    """Return a factory that builds ONE pipeline, and one agent, per turn.

    EVERYTHING PER-TURN IS BUILT INSIDE THE CLOSURE, and that is the whole point. The recorder,
    the verification ledger, the five tools and the `Agent` itself are constructed per call: the
    tools close over this turn's credential, and a reused recorder would leak
    `retrieved_site_code` across turns AND users. Nothing mutable is shared between turns, which
    is what makes the resulting `run_turn` safe for the concurrent invocations Req 32.4b
    describes.

    The process-wide collaborators — the client, the guardrail, the store, the model, the clock
    — are bound once and are stateless with respect to a turn.
    """

    def make_pipeline(request: AdvisoryRequest) -> AdvisoryTurnPipeline:
        identity = identity_for(request)
        recorder = RetrievalRecorder()
        ledger = VerificationLedger()
        credential = request.credential.get_secret_value()

        tools = build_retrieval_tools(
            client=serving_client,
            credential=credential,
            recorder=recorder,
            clock=clock,
            identity=identity,
        )

        # The agent is per turn because its TOOLS are. A process-wide agent would need the
        # credential as a call parameter, which is exactly what Req 5.2 forbids.
        agent = Agent(
            model=model,
            tools=list(tools),
            system_prompt=system_prompt,
            hooks=[VerificationHook(ledger, None)],
            callback_handler=None,
        )

        def invoke() -> ModelGeneration:
            return agent.structured_output(ModelGeneration, request.utterance)

        return AdvisoryTurnPipeline(
            recorder=recorder,
            ledger=ledger,
            invoke=invoke,
            audit_writer=AuditWriter(store=audit_store, logger=logger),
            clock=clock,
            identity=identity,
            red_flag_rules=red_flag_rules,
            emergency_guidance=emergency_guidance,
            forbidden_patterns=forbidden_patterns,
            guardrail=guardrail,
            retrieve_snapshot=lambda: _snapshot(serving_client, credential),
        )

    return make_pipeline


def _snapshot(client: ServingClient, credential: str) -> object | None:
    """Fetch the snapshot for the envelope and the basis, degrading rather than raising.

    Req 21.1 wants a degraded answer naming the kind, not a failed turn, so a client error
    becomes `None` here and the pipeline flags the response. No tool call is recorded: this is
    the service's own fetch, and Req 35.4's trajectory is what the MODEL called.
    """
    try:
        return client.air_quality(credential)
    except ServingClientError:
        return None
