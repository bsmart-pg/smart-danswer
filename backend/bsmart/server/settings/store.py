from typing import cast

from bsmart.configs.constants import KV_SETTINGS_KEY
from bsmart.key_value_store.factory import get_kv_store
from bsmart.key_value_store.interface import KvKeyNotFoundError
from bsmart.server.settings.models import Settings


def load_settings() -> Settings:
    dynamic_config_store = get_kv_store()
    try:
        settings = Settings(**cast(dict, dynamic_config_store.load(KV_SETTINGS_KEY)))
    except KvKeyNotFoundError:
        settings = Settings()
        dynamic_config_store.store(KV_SETTINGS_KEY, settings.model_dump())

    return settings


def store_settings(settings: Settings) -> None:
    get_kv_store().store(KV_SETTINGS_KEY, settings.model_dump())
