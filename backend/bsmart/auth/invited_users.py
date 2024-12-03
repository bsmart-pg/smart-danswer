from typing import cast

from bsmart.configs.constants import KV_USER_STORE_KEY
from bsmart.key_value_store.factory import get_kv_store
from bsmart.key_value_store.interface import KvKeyNotFoundError
from bsmart.utils.special_types import JSON_ro


def get_invited_users() -> list[str]:
    try:
        store = get_kv_store()

        return cast(list, store.load(KV_USER_STORE_KEY))
    except KvKeyNotFoundError:
        return list()


def write_invited_users(emails: list[str]) -> int:
    store = get_kv_store()
    store.store(KV_USER_STORE_KEY, cast(JSON_ro, emails))
    return len(emails)
