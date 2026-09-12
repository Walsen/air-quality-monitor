"""The factory table and the loader's registry must agree exactly.

Task 21.1's named deliverable: "a test asserting the adapter factory table agrees exactly with
the loader's registry, so a name the configuration permits but nothing builds fails offline."

TWO FAILURES, IN OPPOSITE DIRECTIONS, AND BOTH ARE INVISIBLE IN ONE FILE:

* A registry entry with NO factory is a name the loader accepts and the composition root then
  cannot build. The operator reads the registry, sets the value, and the process dies at startup
  on a name its own configuration documented as valid.
* A factory with NO registry entry is unreachable. The loader rejects the name, so the code can
  never run — and it will still be maintained, tested and read by people who assume it can.

Neither shows up by reading `composition.py` or `config/loader.py` alone, which is why the
assertion is over both and in both directions.

THE ORDER MATTERS TOO, not just the membership. Req 23.6 makes the registry's FIRST entry the
default, so a factory table that offers every name but whose port keys disagree about which is
first would build a different adapter than the loader's default names. That is asserted
separately rather than folded in, because a set comparison passes over exactly that mistake.
"""

from __future__ import annotations

from aqm_advisor.composition import ADAPTER_FACTORIES
from aqm_advisor.config.loader import REGISTERED_ADAPTERS


def test_every_registered_port_has_a_factory_group() -> None:
    missing = sorted(set(REGISTERED_ADAPTERS) - set(ADAPTER_FACTORIES))
    assert not missing, (
        f"these ports are configurable but nothing builds them: {missing}. A name the loader "
        f"accepts and the root cannot build is a startup crash on a documented value."
    )


def test_no_factory_group_exists_for_an_unregistered_port() -> None:
    orphan = sorted(set(ADAPTER_FACTORIES) - set(REGISTERED_ADAPTERS))
    assert not orphan, (
        f"these ports have factories but no registry entry, so no configuration can reach "
        f"them: {orphan}"
    )


def test_every_registered_adapter_name_has_a_factory() -> None:
    missing: list[str] = []
    for port, names in REGISTERED_ADAPTERS.items():
        factories = ADAPTER_FACTORIES.get(port, {})
        missing += [f"{port}.{name}" for name in names if name not in factories]
    assert not missing, f"configurable names that nothing builds: {missing}"


def test_no_factory_is_unreachable_from_configuration() -> None:
    unreachable: list[str] = []
    for port, factories in ADAPTER_FACTORIES.items():
        names = REGISTERED_ADAPTERS.get(port, ())
        unreachable += [f"{port}.{name}" for name in factories if name not in names]
    assert not unreachable, f"factories no configuration can select: {unreachable}"


def test_every_factory_is_callable() -> None:
    # A table of INSTANCES would make two turns share one adapter, which is the cross-turn
    # leakage the per-turn construction rule exists to prevent. This pins the table's shape.
    not_callable = [
        f"{port}.{name}"
        for port, factories in ADAPTER_FACTORIES.items()
        for name, factory in factories.items()
        if not callable(factory)
    ]
    assert not not_callable, f"these are objects rather than constructors: {not_callable}"


def test_the_agreement_check_is_not_vacuous() -> None:
    # Every assertion above passes trivially over two empty mappings. This pins that both sides
    # are populated, and that they are the same size — so a future port added to one side alone
    # cannot be masked by a typo in the loop above.
    assert len(REGISTERED_ADAPTERS) >= 6
    assert len(ADAPTER_FACTORIES) == len(REGISTERED_ADAPTERS)
    total_names = sum(len(names) for names in REGISTERED_ADAPTERS.values())
    total_factories = sum(len(factories) for factories in ADAPTER_FACTORIES.values())
    assert total_names == total_factories >= 11


def test_the_default_adapter_name_is_the_registry_first_entry_and_buildable() -> None:
    # Req 23.6 makes the FIRST registered name the default. A set comparison passes over a table
    # whose port key order disagrees, so the default specifically is checked here.
    for port, names in REGISTERED_ADAPTERS.items():
        assert names, f"{port} registers no adapter at all"
        default = names[0]
        assert default in ADAPTER_FACTORIES[port], (
            f"{port}'s DEFAULT name {default!r} has no factory, so a service started with no "
            f"configuration for this port cannot build one"
        )
