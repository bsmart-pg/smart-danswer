import functools
from collections.abc import Callable
from typing import Any
from typing import cast
from typing import Optional
from typing import TypeVar

from retry import retry
from slack_sdk import WebClient
from slack_sdk.models.blocks import SectionBlock

from bsmart.configs.app_configs import DISABLE_GENERATIVE_AI
from bsmart.configs.bsmartbot_configs import BSMART_BOT_ANSWER_GENERATION_TIMEOUT
from bsmart.configs.bsmartbot_configs import BSMART_BOT_DISABLE_COT
from bsmart.configs.bsmartbot_configs import BSMART_BOT_DISABLE_DOCS_ONLY_ANSWER
from bsmart.configs.bsmartbot_configs import BSMART_BOT_DISPLAY_ERROR_MSGS
from bsmart.configs.bsmartbot_configs import BSMART_BOT_NUM_RETRIES
from bsmart.configs.bsmartbot_configs import BSMART_BOT_TARGET_CHUNK_PERCENTAGE
from bsmart.configs.bsmartbot_configs import BSMART_BOT_USE_QUOTES
from bsmart.configs.bsmartbot_configs import BSMART_FOLLOWUP_EMOJI
from bsmart.configs.bsmartbot_configs import BSMART_REACT_EMOJI
from bsmart.configs.bsmartbot_configs import ENABLE_BSMARTBOT_REFLEXION
from bsmart.context.search.enums import OptionalSearchSetting
from bsmart.context.search.models import BaseFilters
from bsmart.context.search.models import RerankingDetails
from bsmart.context.search.models import RetrievalDetails
from bsmart.bsmartbot.slack.blocks import build_slack_response_blocks
from bsmart.bsmartbot.slack.handlers.utils import send_team_member_message
from bsmart.bsmartbot.slack.models import SlackMessageInfo
from bsmart.bsmartbot.slack.utils import respond_in_thread
from bsmart.bsmartbot.slack.utils import SlackRateLimiter
from bsmart.bsmartbot.slack.utils import update_emote_react
from bsmart.db.engine import get_session_with_tenant
from bsmart.db.models import Persona
from bsmart.db.models import SlackBotResponseType
from bsmart.db.models import SlackChannelConfig
from bsmart.db.persona import fetch_persona_by_id
from bsmart.db.search_settings import get_current_search_settings
from bsmart.db.users import get_user_by_email
from bsmart.llm.answering.prompts.citations_prompt import (
    compute_max_document_tokens_for_persona,
)
from bsmart.llm.factory import get_llms_for_persona
from bsmart.llm.utils import check_number_of_tokens
from bsmart.llm.utils import get_max_input_tokens
from bsmart.one_shot_answer.answer_question import get_search_answer
from bsmart.one_shot_answer.models import DirectQARequest
from bsmart.one_shot_answer.models import OneShotQAResponse
from bsmart.utils.logger import BsmartLoggingAdapter


srl = SlackRateLimiter()

RT = TypeVar("RT")  # return type


def rate_limits(
    client: WebClient, channel: str, thread_ts: Optional[str]
) -> Callable[[Callable[..., RT]], Callable[..., RT]]:
    def decorator(func: Callable[..., RT]) -> Callable[..., RT]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> RT:
            if not srl.is_available():
                func_randid, position = srl.init_waiter()
                srl.notify(client, channel, position, thread_ts)
                while not srl.is_available():
                    srl.waiter(func_randid)
            srl.acquire_slot()
            return func(*args, **kwargs)

        return wrapper

    return decorator


def handle_regular_answer(
    message_info: SlackMessageInfo,
    slack_channel_config: SlackChannelConfig | None,
    receiver_ids: list[str] | None,
    client: WebClient,
    channel: str,
    logger: BsmartLoggingAdapter,
    feedback_reminder_id: str | None,
    tenant_id: str | None,
    num_retries: int = BSMART_BOT_NUM_RETRIES,
    answer_generation_timeout: int = BSMART_BOT_ANSWER_GENERATION_TIMEOUT,
    thread_context_percent: float = BSMART_BOT_TARGET_CHUNK_PERCENTAGE,
    should_respond_with_error_msgs: bool = BSMART_BOT_DISPLAY_ERROR_MSGS,
    disable_docs_only_answer: bool = BSMART_BOT_DISABLE_DOCS_ONLY_ANSWER,
    disable_cot: bool = BSMART_BOT_DISABLE_COT,
    reflexion: bool = ENABLE_BSMARTBOT_REFLEXION,
) -> bool:
    channel_conf = slack_channel_config.channel_config if slack_channel_config else None

    messages = message_info.thread_messages
    message_ts_to_respond_to = message_info.msg_to_respond
    is_bot_msg = message_info.is_bot_msg
    user = None
    if message_info.is_bot_dm:
        if message_info.email:
            with get_session_with_tenant(tenant_id) as db_session:
                user = get_user_by_email(message_info.email, db_session)

    document_set_names: list[str] | None = None
    persona = slack_channel_config.persona if slack_channel_config else None
    prompt = None
    if persona:
        document_set_names = [
            document_set.name for document_set in persona.document_sets
        ]
        prompt = persona.prompts[0] if persona.prompts else None

    should_respond_even_with_no_docs = persona.num_chunks == 0 if persona else False

    bypass_acl = False
    if (
        slack_channel_config
        and slack_channel_config.persona
        and slack_channel_config.persona.document_sets
    ):
        # For Slack channels, use the full document set, admin will be warned when configuring it
        # with non-public document sets
        bypass_acl = True

    # figure out if we want to use citations or quotes
    use_citations = (
        not BSMART_BOT_USE_QUOTES
        if slack_channel_config is None
        else slack_channel_config.response_type == SlackBotResponseType.CITATIONS
    )

    if not message_ts_to_respond_to and not is_bot_msg:
        # if the message is not "/bsmart" command, then it should have a message ts to respond to
        raise RuntimeError(
            "No message timestamp to respond to in `handle_message`. This should never happen."
        )

    @retry(
        tries=num_retries,
        delay=0.25,
        backoff=2,
    )
    @rate_limits(client=client, channel=channel, thread_ts=message_ts_to_respond_to)
    def _get_answer(new_message_request: DirectQARequest) -> OneShotQAResponse | None:
        max_document_tokens: int | None = None
        max_history_tokens: int | None = None

        with get_session_with_tenant(tenant_id) as db_session:
            if len(new_message_request.messages) > 1:
                if new_message_request.persona_config:
                    raise RuntimeError("Slack bot does not support persona config")
                elif new_message_request.persona_id is not None:
                    persona = cast(
                        Persona,
                        fetch_persona_by_id(
                            db_session,
                            new_message_request.persona_id,
                            user=None,
                            get_editable=False,
                        ),
                    )
                else:
                    raise RuntimeError(
                        "No persona id provided, this should never happen."
                    )

                llm, _ = get_llms_for_persona(persona)

                # In cases of threads, split the available tokens between docs and thread context
                input_tokens = get_max_input_tokens(
                    model_name=llm.config.model_name,
                    model_provider=llm.config.model_provider,
                )
                max_history_tokens = int(input_tokens * thread_context_percent)

                remaining_tokens = input_tokens - max_history_tokens

                query_text = new_message_request.messages[0].message
                if persona:
                    max_document_tokens = compute_max_document_tokens_for_persona(
                        persona=persona,
                        actual_user_input=query_text,
                        max_llm_token_override=remaining_tokens,
                    )
                else:
                    max_document_tokens = (
                        remaining_tokens
                        - 512  # Needs to be more than any of the QA prompts
                        - check_number_of_tokens(query_text)
                    )

            if DISABLE_GENERATIVE_AI:
                return None

            # This also handles creating the query event in postgres
            answer = get_search_answer(
                query_req=new_message_request,
                user=user,
                max_document_tokens=max_document_tokens,
                max_history_tokens=max_history_tokens,
                db_session=db_session,
                answer_generation_timeout=answer_generation_timeout,
                enable_reflexion=reflexion,
                bypass_acl=bypass_acl,
                use_citations=use_citations,
                bsmartbot_flow=True,
            )

            if not answer.error_msg:
                return answer
            else:
                raise RuntimeError(answer.error_msg)

    try:
        # By leaving time_cutoff and favor_recent as None, and setting enable_auto_detect_filters
        # it allows the slack flow to extract out filters from the user query
        filters = BaseFilters(
            source_type=None,
            document_set=document_set_names,
            time_cutoff=None,
        )

        # Default True because no other ways to apply filters in Slack (no nice UI)
        # Commenting this out because this is only available to the slackbot for now
        # later we plan to implement this at the persona level where this will get
        # commented back in
        # auto_detect_filters = (
        #     persona.llm_filter_extraction if persona is not None else True
        # )
        auto_detect_filters = (
            slack_channel_config.enable_auto_filters
            if slack_channel_config is not None
            else False
        )
        retrieval_details = RetrievalDetails(
            run_search=OptionalSearchSetting.ALWAYS,
            real_time=False,
            filters=filters,
            enable_auto_detect_filters=auto_detect_filters,
        )

        # Always apply reranking settings if it exists, this is the non-streaming flow
        with get_session_with_tenant(tenant_id) as db_session:
            saved_search_settings = get_current_search_settings(db_session)

        # This includes throwing out answer via reflexion
        answer = _get_answer(
            DirectQARequest(
                messages=messages,
                multilingual_query_expansion=saved_search_settings.multilingual_expansion
                if saved_search_settings
                else None,
                prompt_id=prompt.id if prompt else None,
                persona_id=persona.id if persona is not None else 0,
                retrieval_options=retrieval_details,
                chain_of_thought=not disable_cot,
                rerank_settings=RerankingDetails.from_db_model(saved_search_settings)
                if saved_search_settings
                else None,
            )
        )
    except Exception as e:
        logger.exception(
            f"Unable to process message - did not successfully answer "
            f"in {num_retries} attempts"
        )
        # Optionally, respond in thread with the error message, Used primarily
        # for debugging purposes
        if should_respond_with_error_msgs:
            respond_in_thread(
                client=client,
                channel=channel,
                receiver_ids=None,
                text=f"Encountered exception when trying to answer: \n\n```{e}```",
                thread_ts=message_ts_to_respond_to,
            )

        # In case of failures, don't keep the reaction there permanently
        update_emote_react(
            emoji=BSMART_REACT_EMOJI,
            channel=message_info.channel_to_respond,
            message_ts=message_info.msg_to_respond,
            remove=True,
            client=client,
        )

        return True

    # Edge case handling, for tracking down the Slack usage issue
    if answer is None:
        assert DISABLE_GENERATIVE_AI is True
        try:
            respond_in_thread(
                client=client,
                channel=channel,
                receiver_ids=receiver_ids,
                text="Hello! Bsmart has some results for you!",
                blocks=[
                    SectionBlock(
                        text="Bsmart is down for maintenance.\nWe're working hard on recharging the AI!"
                    )
                ],
                thread_ts=message_ts_to_respond_to,
                # don't unfurl, since otherwise we will have 5+ previews which makes the message very long
                unfurl=False,
            )

            # For DM (ephemeral message), we need to create a thread via a normal message so the user can see
            # the ephemeral message. This also will give the user a notification which ephemeral message does not.
            if receiver_ids:
                respond_in_thread(
                    client=client,
                    channel=channel,
                    text=(
                        "👋 Hi, we've just gathered and forwarded the relevant "
                        + "information to the team. They'll get back to you shortly!"
                    ),
                    thread_ts=message_ts_to_respond_to,
                )

            return False

        except Exception:
            logger.exception(
                f"Unable to process message - could not respond in slack in {num_retries} attempts"
            )
            return True

    # Got an answer at this point, can remove reaction and give results
    update_emote_react(
        emoji=BSMART_REACT_EMOJI,
        channel=message_info.channel_to_respond,
        message_ts=message_info.msg_to_respond,
        remove=True,
        client=client,
    )

    if answer.answer_valid is False:
        logger.notice(
            "Answer was evaluated to be invalid, throwing it away without responding."
        )
        update_emote_react(
            emoji=BSMART_FOLLOWUP_EMOJI,
            channel=message_info.channel_to_respond,
            message_ts=message_info.msg_to_respond,
            remove=False,
            client=client,
        )

        if answer.answer:
            logger.debug(answer.answer)
        return True

    retrieval_info = answer.docs
    if not retrieval_info:
        # This should not happen, even with no docs retrieved, there is still info returned
        raise RuntimeError("Failed to retrieve docs, cannot answer question.")

    top_docs = retrieval_info.top_documents
    if not top_docs and not should_respond_even_with_no_docs:
        logger.error(
            f"Unable to answer question: '{answer.rephrase}' - no documents found"
        )
        # Optionally, respond in thread with the error message
        # Used primarily for debugging purposes
        if should_respond_with_error_msgs:
            respond_in_thread(
                client=client,
                channel=channel,
                receiver_ids=None,
                text="Found no documents when trying to answer. Did you index any documents?",
                thread_ts=message_ts_to_respond_to,
            )
        return True

    if not answer.answer and disable_docs_only_answer:
        logger.notice(
            "Unable to find answer - not responding since the "
            "`BSMART_BOT_DISABLE_DOCS_ONLY_ANSWER` env variable is set"
        )
        return True

    only_respond_with_citations_or_quotes = (
        channel_conf
        and "well_answered_postfilter" in channel_conf.get("answer_filters", [])
    )
    has_citations_or_quotes = bool(answer.citations or answer.quotes)
    if (
        only_respond_with_citations_or_quotes
        and not has_citations_or_quotes
        and not message_info.bypass_filters
    ):
        logger.error(
            f"Unable to find citations or quotes to answer: '{answer.rephrase}' - not answering!"
        )
        # Optionally, respond in thread with the error message
        # Used primarily for debugging purposes
        if should_respond_with_error_msgs:
            respond_in_thread(
                client=client,
                channel=channel,
                receiver_ids=None,
                text="Found no citations or quotes when trying to answer.",
                thread_ts=message_ts_to_respond_to,
            )
        return True

    all_blocks = build_slack_response_blocks(
        tenant_id=tenant_id,
        message_info=message_info,
        answer=answer,
        persona=persona,
        channel_conf=channel_conf,
        use_citations=use_citations,
        feedback_reminder_id=feedback_reminder_id,
    )

    try:
        respond_in_thread(
            client=client,
            channel=channel,
            receiver_ids=receiver_ids,
            text="Hello! Bsmart has some results for you!",
            blocks=all_blocks,
            thread_ts=message_ts_to_respond_to,
            # don't unfurl, since otherwise we will have 5+ previews which makes the message very long
            unfurl=False,
        )

        # For DM (ephemeral message), we need to create a thread via a normal message so the user can see
        # the ephemeral message. This also will give the user a notification which ephemeral message does not.
        # if there is no message_ts_to_respond_to, and we have made it this far, then this is a /bsmart message
        # so we shouldn't send_team_member_message
        if receiver_ids and message_ts_to_respond_to is not None:
            send_team_member_message(
                client=client,
                channel=channel,
                thread_ts=message_ts_to_respond_to,
            )

        return False

    except Exception:
        logger.exception(
            f"Unable to process message - could not respond in slack in {num_retries} attempts"
        )
        return True