from pydantic import BaseModel

from bsmart.one_shot_answer.models import ThreadMessage


class SlackMessageInfo(BaseModel):
    thread_messages: list[ThreadMessage]
    channel_to_respond: str
    msg_to_respond: str | None
    thread_to_respond: str | None
    sender: str | None
    email: str | None
    bypass_filters: bool  # User has tagged @BsmartBot
    is_bot_msg: bool  # User is using /BsmartBot
    is_bot_dm: bool  # User is direct messaging to BsmartBot
