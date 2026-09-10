"""The ports: protocols the domain depends on, never concrete implementations.

Every boundary that gets swapped in this service — the readings store, the raw
archive, the sensor registry, the profile store, the transport that delivers
records, the external forecast and pollen feeds, the authenticator, and the clock
— is declared here as a Protocol. The domain imports only from this package, so a
local in-memory adapter and a cloud adapter are interchangeable and the whole
suite runs with no AWS credentials and no network beyond localhost.

Each port carries ONE shared behavioural test suite, run against every adapter
that claims to implement it, so an adapter cannot quietly diverge.
"""
