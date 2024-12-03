from datetime import timedelta
from typing import Any

from bsmart.configs.constants import BsmartCeleryPriority


tasks_to_schedule = [
    {
        "name": "check-for-vespa-sync",
        "task": "check_for_vespa_sync_task",
        "schedule": timedelta(seconds=20),
        "options": {"priority": BsmartCeleryPriority.HIGH},
    },
    {
        "name": "check-for-connector-deletion",
        "task": "check_for_connector_deletion_task",
        "schedule": timedelta(seconds=20),
        "options": {"priority": BsmartCeleryPriority.HIGH},
    },
    {
        "name": "check-for-indexing",
        "task": "check_for_indexing",
        "schedule": timedelta(seconds=15),
        "options": {"priority": BsmartCeleryPriority.HIGH},
    },
    {
        "name": "check-for-prune",
        "task": "check_for_pruning",
        "schedule": timedelta(seconds=15),
        "options": {"priority": BsmartCeleryPriority.HIGH},
    },
    {
        "name": "kombu-message-cleanup",
        "task": "kombu_message_cleanup_task",
        "schedule": timedelta(seconds=3600),
        "options": {"priority": BsmartCeleryPriority.LOWEST},
    },
    {
        "name": "monitor-vespa-sync",
        "task": "monitor_vespa_sync",
        "schedule": timedelta(seconds=5),
        "options": {"priority": BsmartCeleryPriority.HIGH},
    },
    {
        "name": "check-for-doc-permissions-sync",
        "task": "check_for_doc_permissions_sync",
        "schedule": timedelta(seconds=30),
        "options": {"priority": BsmartCeleryPriority.HIGH},
    },
    {
        "name": "check-for-external-group-sync",
        "task": "check_for_external_group_sync",
        "schedule": timedelta(seconds=20),
        "options": {"priority": BsmartCeleryPriority.HIGH},
    },
]


def get_tasks_to_schedule() -> list[dict[str, Any]]:
    return tasks_to_schedule
