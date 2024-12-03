from typing import Any
from typing import Type

from sqlalchemy.orm import Session

from bsmart.configs.constants import DocumentSource
from bsmart.configs.constants import DocumentSourceRequiringTenantContext
from bsmart.connectors.asana.connector import AsanaConnector
from bsmart.connectors.axero.connector import AxeroConnector
from bsmart.connectors.blob.connector import BlobStorageConnector
from bsmart.connectors.bookstack.connector import BookstackConnector
from bsmart.connectors.clickup.connector import ClickupConnector
from bsmart.connectors.confluence.connector import ConfluenceConnector
from bsmart.connectors.bsmart_jira.connector import JiraConnector
from bsmart.connectors.discourse.connector import DiscourseConnector
from bsmart.connectors.document360.connector import Document360Connector
from bsmart.connectors.dropbox.connector import DropboxConnector
from bsmart.connectors.file.connector import LocalFileConnector
from bsmart.connectors.fireflies.connector import FirefliesConnector
from bsmart.connectors.freshdesk.connector import FreshdeskConnector
from bsmart.connectors.github.connector import GithubConnector
from bsmart.connectors.gitlab.connector import GitlabConnector
from bsmart.connectors.gmail.connector import GmailConnector
from bsmart.connectors.gong.connector import GongConnector
from bsmart.connectors.google_drive.connector import GoogleDriveConnector
from bsmart.connectors.google_site.connector import GoogleSitesConnector
from bsmart.connectors.guru.connector import GuruConnector
from bsmart.connectors.hubspot.connector import HubSpotConnector
from bsmart.connectors.interfaces import BaseConnector
from bsmart.connectors.interfaces import EventConnector
from bsmart.connectors.interfaces import LoadConnector
from bsmart.connectors.interfaces import PollConnector
from bsmart.connectors.linear.connector import LinearConnector
from bsmart.connectors.loopio.connector import LoopioConnector
from bsmart.connectors.mediawiki.wiki import MediaWikiConnector
from bsmart.connectors.models import InputType
from bsmart.connectors.notion.connector import NotionConnector
from bsmart.connectors.productboard.connector import ProductboardConnector
from bsmart.connectors.salesforce.connector import SalesforceConnector
from bsmart.connectors.sharepoint.connector import SharepointConnector
from bsmart.connectors.slab.connector import SlabConnector
from bsmart.connectors.slack.connector import SlackPollConnector
from bsmart.connectors.slack.load_connector import SlackLoadConnector
from bsmart.connectors.teams.connector import TeamsConnector
from bsmart.connectors.web.connector import WebConnector
from bsmart.connectors.wikipedia.connector import WikipediaConnector
from bsmart.connectors.xenforo.connector import XenforoConnector
from bsmart.connectors.zendesk.connector import ZendeskConnector
from bsmart.connectors.zulip.connector import ZulipConnector
from bsmart.db.credentials import backend_update_credential_json
from bsmart.db.models import Credential


class ConnectorMissingException(Exception):
    pass


def identify_connector_class(
    source: DocumentSource,
    input_type: InputType | None = None,
) -> Type[BaseConnector]:
    connector_map = {
        DocumentSource.WEB: WebConnector,
        DocumentSource.FILE: LocalFileConnector,
        DocumentSource.SLACK: {
            InputType.LOAD_STATE: SlackLoadConnector,
            InputType.POLL: SlackPollConnector,
            InputType.SLIM_RETRIEVAL: SlackPollConnector,
        },
        DocumentSource.GITHUB: GithubConnector,
        DocumentSource.GMAIL: GmailConnector,
        DocumentSource.GITLAB: GitlabConnector,
        DocumentSource.GOOGLE_DRIVE: GoogleDriveConnector,
        DocumentSource.BOOKSTACK: BookstackConnector,
        DocumentSource.CONFLUENCE: ConfluenceConnector,
        DocumentSource.JIRA: JiraConnector,
        DocumentSource.PRODUCTBOARD: ProductboardConnector,
        DocumentSource.SLAB: SlabConnector,
        DocumentSource.NOTION: NotionConnector,
        DocumentSource.ZULIP: ZulipConnector,
        DocumentSource.GURU: GuruConnector,
        DocumentSource.LINEAR: LinearConnector,
        DocumentSource.HUBSPOT: HubSpotConnector,
        DocumentSource.DOCUMENT360: Document360Connector,
        DocumentSource.GONG: GongConnector,
        DocumentSource.GOOGLE_SITES: GoogleSitesConnector,
        DocumentSource.ZENDESK: ZendeskConnector,
        DocumentSource.LOOPIO: LoopioConnector,
        DocumentSource.DROPBOX: DropboxConnector,
        DocumentSource.SHAREPOINT: SharepointConnector,
        DocumentSource.TEAMS: TeamsConnector,
        DocumentSource.SALESFORCE: SalesforceConnector,
        DocumentSource.DISCOURSE: DiscourseConnector,
        DocumentSource.AXERO: AxeroConnector,
        DocumentSource.CLICKUP: ClickupConnector,
        DocumentSource.MEDIAWIKI: MediaWikiConnector,
        DocumentSource.WIKIPEDIA: WikipediaConnector,
        DocumentSource.ASANA: AsanaConnector,
        DocumentSource.S3: BlobStorageConnector,
        DocumentSource.R2: BlobStorageConnector,
        DocumentSource.GOOGLE_CLOUD_STORAGE: BlobStorageConnector,
        DocumentSource.OCI_STORAGE: BlobStorageConnector,
        DocumentSource.XENFORO: XenforoConnector,
        DocumentSource.FRESHDESK: FreshdeskConnector,
        DocumentSource.FIREFLIES: FirefliesConnector,
    }
    connector_by_source = connector_map.get(source, {})

    if isinstance(connector_by_source, dict):
        if input_type is None:
            # If not specified, default to most exhaustive update
            connector = connector_by_source.get(InputType.LOAD_STATE)
        else:
            connector = connector_by_source.get(input_type)
    else:
        connector = connector_by_source
    if connector is None:
        raise ConnectorMissingException(f"Connector not found for source={source}")

    if any(
        [
            input_type == InputType.LOAD_STATE
            and not issubclass(connector, LoadConnector),
            input_type == InputType.POLL and not issubclass(connector, PollConnector),
            input_type == InputType.EVENT and not issubclass(connector, EventConnector),
        ]
    ):
        raise ConnectorMissingException(
            f"Connector for source={source} does not accept input_type={input_type}"
        )
    return connector


def instantiate_connector(
    db_session: Session,
    source: DocumentSource,
    input_type: InputType,
    connector_specific_config: dict[str, Any],
    credential: Credential,
    tenant_id: str | None = None,
) -> BaseConnector:
    connector_class = identify_connector_class(source, input_type)

    if source in DocumentSourceRequiringTenantContext:
        connector_specific_config["tenant_id"] = tenant_id

    connector = connector_class(**connector_specific_config)
    new_credentials = connector.load_credentials(credential.credential_json)

    if new_credentials is not None:
        backend_update_credential_json(credential, new_credentials, db_session)

    return connector
