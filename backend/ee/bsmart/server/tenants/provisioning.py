import asyncio
import logging
import uuid

import aiohttp  # Async HTTP client
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from bsmart.auth.users import exceptions
from bsmart.configs.app_configs import CONTROL_PLANE_API_BASE_URL
from bsmart.db.engine import get_session_with_tenant
from bsmart.db.engine import get_sqlalchemy_engine
from bsmart.db.llm import update_default_provider
from bsmart.db.llm import upsert_cloud_embedding_provider
from bsmart.db.llm import upsert_llm_provider
from bsmart.db.models import IndexModelStatus
from bsmart.db.models import SearchSettings
from bsmart.db.models import UserTenantMapping
from bsmart.llm.llm_provider_options import ANTHROPIC_MODEL_NAMES
from bsmart.llm.llm_provider_options import ANTHROPIC_PROVIDER_NAME
from bsmart.llm.llm_provider_options import OPEN_AI_MODEL_NAMES
from bsmart.llm.llm_provider_options import OPENAI_PROVIDER_NAME
from bsmart.server.manage.embedding.models import CloudEmbeddingProviderCreationRequest
from bsmart.server.manage.llm.models import LLMProviderUpsertRequest
from bsmart.setup import setup_bsmart
from ee.bsmart.configs.app_configs import ANTHROPIC_DEFAULT_API_KEY
from ee.bsmart.configs.app_configs import COHERE_DEFAULT_API_KEY
from ee.bsmart.configs.app_configs import OPENAI_DEFAULT_API_KEY
from ee.bsmart.server.tenants.access import generate_data_plane_token
from ee.bsmart.server.tenants.models import TenantCreationPayload
from ee.bsmart.server.tenants.schema_management import create_schema_if_not_exists
from ee.bsmart.server.tenants.schema_management import drop_schema
from ee.bsmart.server.tenants.schema_management import run_alembic_migrations
from ee.bsmart.server.tenants.user_mapping import add_users_to_tenant
from ee.bsmart.server.tenants.user_mapping import get_tenant_id_for_email
from ee.bsmart.server.tenants.user_mapping import user_owns_a_tenant
from shared_configs.configs import MULTI_TENANT
from shared_configs.configs import POSTGRES_DEFAULT_SCHEMA
from shared_configs.configs import TENANT_ID_PREFIX
from shared_configs.contextvars import CURRENT_TENANT_ID_CONTEXTVAR
from shared_configs.enums import EmbeddingProvider

logger = logging.getLogger(__name__)


async def get_or_create_tenant_id(
    email: str, referral_source: str | None = None
) -> str:
    """Get existing tenant ID for an email or create a new tenant if none exists."""
    if not MULTI_TENANT:
        return POSTGRES_DEFAULT_SCHEMA

    try:
        tenant_id = get_tenant_id_for_email(email)
    except exceptions.UserNotExists:
        # If tenant does not exist and in Multi tenant mode, provision a new tenant
        try:
            tenant_id = await create_tenant(email, referral_source)
        except Exception as e:
            logger.error(f"Tenant provisioning failed: {e}")
            raise HTTPException(status_code=500, detail="Failed to provision tenant.")

    if not tenant_id:
        raise HTTPException(
            status_code=401, detail="User does not belong to an organization"
        )

    return tenant_id


async def create_tenant(email: str, referral_source: str | None = None) -> str:
    tenant_id = TENANT_ID_PREFIX + str(uuid.uuid4())
    try:
        # Provision tenant on data plane
        await provision_tenant(tenant_id, email)
        # Notify control plane
        await notify_control_plane(tenant_id, email, referral_source)
    except Exception as e:
        logger.error(f"Tenant provisioning failed: {e}")
        await rollback_tenant_provisioning(tenant_id)
        raise HTTPException(status_code=500, detail="Failed to provision tenant.")
    return tenant_id


async def provision_tenant(tenant_id: str, email: str) -> None:
    if not MULTI_TENANT:
        raise HTTPException(status_code=403, detail="Multi-tenancy is not enabled")

    if user_owns_a_tenant(email):
        raise HTTPException(
            status_code=409, detail="User already belongs to an organization"
        )

    logger.info(f"Provisioning tenant: {tenant_id}")
    token = None

    try:
        if not create_schema_if_not_exists(tenant_id):
            logger.info(f"Created schema for tenant {tenant_id}")
        else:
            logger.info(f"Schema already exists for tenant {tenant_id}")

        token = CURRENT_TENANT_ID_CONTEXTVAR.set(tenant_id)

        # Await the Alembic migrations
        await asyncio.to_thread(run_alembic_migrations, tenant_id)

        with get_session_with_tenant(tenant_id) as db_session:
            configure_default_api_keys(db_session)

            current_search_settings = (
                db_session.query(SearchSettings)
                .filter_by(status=IndexModelStatus.FUTURE)
                .first()
            )
            cohere_enabled = (
                current_search_settings is not None
                and current_search_settings.provider_type == EmbeddingProvider.COHERE
            )
            setup_bsmart(db_session, tenant_id, cohere_enabled=cohere_enabled)

        add_users_to_tenant([email], tenant_id)

    except Exception as e:
        logger.exception(f"Failed to create tenant {tenant_id}")
        raise HTTPException(
            status_code=500, detail=f"Failed to create tenant: {str(e)}"
        )
    finally:
        if token is not None:
            CURRENT_TENANT_ID_CONTEXTVAR.reset(token)


async def notify_control_plane(
    tenant_id: str, email: str, referral_source: str | None = None
) -> None:
    logger.info("Fetching billing information")
    token = generate_data_plane_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = TenantCreationPayload(
        tenant_id=tenant_id, email=email, referral_source=referral_source
    )

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{CONTROL_PLANE_API_BASE_URL}/tenants/create",
            headers=headers,
            json=payload.model_dump(),
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                logger.error(f"Control plane tenant creation failed: {error_text}")
                raise Exception(
                    f"Failed to create tenant on control plane: {error_text}"
                )


async def rollback_tenant_provisioning(tenant_id: str) -> None:
    # Logic to rollback tenant provisioning on data plane
    logger.info(f"Rolling back tenant provisioning for tenant_id: {tenant_id}")
    try:
        # Drop the tenant's schema to rollback provisioning
        drop_schema(tenant_id)
        # Remove tenant mapping
        with Session(get_sqlalchemy_engine()) as db_session:
            db_session.query(UserTenantMapping).filter(
                UserTenantMapping.tenant_id == tenant_id
            ).delete()
            db_session.commit()
    except Exception as e:
        logger.error(f"Failed to rollback tenant provisioning: {e}")


def configure_default_api_keys(db_session: Session) -> None:
    if OPENAI_DEFAULT_API_KEY:
        open_provider = LLMProviderUpsertRequest(
            name="OpenAI",
            provider=OPENAI_PROVIDER_NAME,
            api_key=OPENAI_DEFAULT_API_KEY,
            default_model_name="gpt-4",
            fast_default_model_name="gpt-4o-mini",
            model_names=OPEN_AI_MODEL_NAMES,
        )
        try:
            full_provider = upsert_llm_provider(open_provider, db_session)
            update_default_provider(full_provider.id, db_session)
        except Exception as e:
            logger.error(f"Failed to configure OpenAI provider: {e}")
    else:
        logger.error(
            "OPENAI_DEFAULT_API_KEY not set, skipping OpenAI provider configuration"
        )

    if ANTHROPIC_DEFAULT_API_KEY:
        anthropic_provider = LLMProviderUpsertRequest(
            name="Anthropic",
            provider=ANTHROPIC_PROVIDER_NAME,
            api_key=ANTHROPIC_DEFAULT_API_KEY,
            default_model_name="claude-3-5-sonnet-20241022",
            fast_default_model_name="claude-3-5-sonnet-20241022",
            model_names=ANTHROPIC_MODEL_NAMES,
        )
        try:
            full_provider = upsert_llm_provider(anthropic_provider, db_session)
            update_default_provider(full_provider.id, db_session)
        except Exception as e:
            logger.error(f"Failed to configure Anthropic provider: {e}")
    else:
        logger.error(
            "ANTHROPIC_DEFAULT_API_KEY not set, skipping Anthropic provider configuration"
        )

    if COHERE_DEFAULT_API_KEY:
        cloud_embedding_provider = CloudEmbeddingProviderCreationRequest(
            provider_type=EmbeddingProvider.COHERE,
            api_key=COHERE_DEFAULT_API_KEY,
        )

        try:
            logger.info("Attempting to upsert Cohere cloud embedding provider")
            upsert_cloud_embedding_provider(db_session, cloud_embedding_provider)
            logger.info("Successfully upserted Cohere cloud embedding provider")

            logger.info("Updating search settings with Cohere embedding model details")
            query = (
                select(SearchSettings)
                .where(SearchSettings.status == IndexModelStatus.FUTURE)
                .order_by(SearchSettings.id.desc())
            )
            result = db_session.execute(query)
            current_search_settings = result.scalars().first()

            if current_search_settings:
                current_search_settings.model_name = (
                    "embed-english-v3.0"  # Cohere's latest model as of now
                )
                current_search_settings.model_dim = (
                    1024  # Cohere's embed-english-v3.0 dimension
                )
                current_search_settings.provider_type = EmbeddingProvider.COHERE
                current_search_settings.index_name = (
                    "bsmart_chunk_cohere_embed_english_v3_0"
                )
                current_search_settings.query_prefix = ""
                current_search_settings.passage_prefix = ""
                db_session.commit()
            else:
                raise RuntimeError(
                    "No search settings specified, DB is not in a valid state"
                )
            logger.info("Fetching updated search settings to verify changes")
            updated_query = (
                select(SearchSettings)
                .where(SearchSettings.status == IndexModelStatus.PRESENT)
                .order_by(SearchSettings.id.desc())
            )
            updated_result = db_session.execute(updated_query)
            updated_result.scalars().first()

        except Exception:
            logger.exception("Failed to configure Cohere embedding provider")
    else:
        logger.info(
            "COHERE_DEFAULT_API_KEY not set, skipping Cohere embedding provider configuration"
        )
