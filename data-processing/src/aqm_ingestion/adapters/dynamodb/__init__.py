"""DynamoDB adapters for the readings, registry, and profile stores.

Cloud adapters only — nothing here is imported by ``domain/``, which the task-3.4 architecture
check enforces. Exercised by the shared port contract suite alongside the in-memory adapters
(Requirements 16.8, 28.10), so an adapter swap cannot change domain behaviour.
"""

from aqm_ingestion.adapters.dynamodb.adapters import (
    DynamoDbProfileStore,
    DynamoDbReadingsStore,
    DynamoDbSensorRegistryStore,
)

__all__ = [
    "DynamoDbProfileStore",
    "DynamoDbReadingsStore",
    "DynamoDbSensorRegistryStore",
]
