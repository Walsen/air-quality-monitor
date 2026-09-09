"""In-memory adapters: the offline suite runs entirely against these.

Re-exported here so a caller imports from the package rather than reaching into a
module, which keeps the internal layout free to change.
"""

from aqm_ingestion.adapters.memory.adapters import (
    InMemoryForecastClient,
    InMemoryMeteorologyProvider,
    InMemoryProfileStore,
    InMemoryRawArchive,
    InMemoryReadingsStore,
    InMemorySensorRegistryStore,
    LocalAuthenticator,
    ScriptedFeedClient,
    ScriptedMqttTransport,
)

__all__ = [
    "InMemoryForecastClient",
    "InMemoryMeteorologyProvider",
    "InMemoryProfileStore",
    "InMemoryRawArchive",
    "InMemoryReadingsStore",
    "InMemorySensorRegistryStore",
    "LocalAuthenticator",
    "ScriptedFeedClient",
    "ScriptedMqttTransport",
]
