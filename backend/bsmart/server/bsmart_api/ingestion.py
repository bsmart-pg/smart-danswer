from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from bsmart.auth.users import api_key_dep
from bsmart.configs.constants import DocumentSource
from bsmart.connectors.models import Document
from bsmart.connectors.models import IndexAttemptMetadata
from bsmart.db.connector_credential_pair import get_connector_credential_pair_from_id
from bsmart.db.document import get_documents_by_cc_pair
from bsmart.db.document import get_ingestion_documents
from bsmart.db.engine import get_current_tenant_id
from bsmart.db.engine import get_session
from bsmart.db.models import User
from bsmart.db.search_settings import get_current_search_settings
from bsmart.db.search_settings import get_secondary_search_settings
from bsmart.document_index.document_index_utils import get_both_index_names
from bsmart.document_index.factory import get_default_document_index
from bsmart.indexing.embedder import DefaultIndexingEmbedder
from bsmart.indexing.indexing_pipeline import build_indexing_pipeline
from bsmart.server.bsmart_api.models import DocMinimalInfo
from bsmart.server.bsmart_api.models import IngestionDocument
from bsmart.server.bsmart_api.models import IngestionResult
from bsmart.utils.logger import setup_logger

logger = setup_logger()

# not using /api to avoid confusion with nginx api path routing
router = APIRouter(prefix="/bsmart-api")


@router.get("/connector-docs/{cc_pair_id}")
def get_docs_by_connector_credential_pair(
    cc_pair_id: int,
    _: User | None = Depends(api_key_dep),
    db_session: Session = Depends(get_session),
) -> list[DocMinimalInfo]:
    db_docs = get_documents_by_cc_pair(cc_pair_id=cc_pair_id, db_session=db_session)
    return [
        DocMinimalInfo(
            document_id=doc.id,
            semantic_id=doc.semantic_id,
            link=doc.link,
        )
        for doc in db_docs
    ]


@router.get("/ingestion")
def get_ingestion_docs(
    _: User | None = Depends(api_key_dep),
    db_session: Session = Depends(get_session),
) -> list[DocMinimalInfo]:
    db_docs = get_ingestion_documents(db_session)
    return [
        DocMinimalInfo(
            document_id=doc.id,
            semantic_id=doc.semantic_id,
            link=doc.link,
        )
        for doc in db_docs
    ]


@router.post("/ingestion")
def upsert_ingestion_doc(
    doc_info: IngestionDocument,
    _: User | None = Depends(api_key_dep),
    db_session: Session = Depends(get_session),
    tenant_id: str = Depends(get_current_tenant_id),
) -> IngestionResult:
    doc_info.document.from_ingestion_api = True

    document = Document.from_base(doc_info.document)

    # TODO once the frontend is updated with this enum, remove this logic
    if document.source == DocumentSource.INGESTION_API:
        document.source = DocumentSource.FILE

    cc_pair = get_connector_credential_pair_from_id(
        cc_pair_id=doc_info.cc_pair_id or 0, db_session=db_session
    )
    if cc_pair is None:
        raise HTTPException(
            status_code=400, detail="Connector-Credential Pair specified does not exist"
        )

    # Need to index for both the primary and secondary index if possible
    curr_ind_name, sec_ind_name = get_both_index_names(db_session)
    curr_doc_index = get_default_document_index(
        primary_index_name=curr_ind_name, secondary_index_name=None
    )

    search_settings = get_current_search_settings(db_session)

    index_embedding_model = DefaultIndexingEmbedder.from_db_search_settings(
        search_settings=search_settings
    )

    indexing_pipeline = build_indexing_pipeline(
        embedder=index_embedding_model,
        document_index=curr_doc_index,
        ignore_time_skip=True,
        db_session=db_session,
        tenant_id=tenant_id,
    )

    new_doc, __chunk_count = indexing_pipeline(
        document_batch=[document],
        index_attempt_metadata=IndexAttemptMetadata(
            connector_id=cc_pair.connector_id,
            credential_id=cc_pair.credential_id,
        ),
    )

    # If there's a secondary index being built, index the doc but don't use it for return here
    if sec_ind_name:
        sec_doc_index = get_default_document_index(
            primary_index_name=curr_ind_name, secondary_index_name=None
        )

        sec_search_settings = get_secondary_search_settings(db_session)

        if sec_search_settings is None:
            # Should not ever happen
            raise RuntimeError(
                "Secondary index exists but no search settings configured"
            )

        new_index_embedding_model = DefaultIndexingEmbedder.from_db_search_settings(
            search_settings=sec_search_settings
        )

        sec_ind_pipeline = build_indexing_pipeline(
            embedder=new_index_embedding_model,
            document_index=sec_doc_index,
            ignore_time_skip=True,
            db_session=db_session,
            tenant_id=tenant_id,
        )

        sec_ind_pipeline(
            document_batch=[document],
            index_attempt_metadata=IndexAttemptMetadata(
                connector_id=cc_pair.connector_id,
                credential_id=cc_pair.credential_id,
            ),
        )

    return IngestionResult(document_id=document.id, already_existed=not bool(new_doc))