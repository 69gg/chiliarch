"""Shared, model-visible guidance for live collaboration."""

INBOX_POLLING_GUIDANCE = (
    "Proactively call read_messages(after=null) at task start, periodically during long work, "
    "between meaningful steps, while awaiting collaborators, and before finishing. "
    "The daemon receives messages but does not inject them into your model context or wake an idle "
    "interactive session. Use the current task scope for managed work. Process each returned page, "
    "then call acknowledge_messages with its cursor and the same task scope; reading alone does "
    "not advance the cursor. For peer messages separately report read/processed using "
    "acknowledge_peer_message and the payload's message_id. Read means seen; processed means "
    "actually handled. Continue paging when a page is full. Back off after empty reads instead of "
    "busy-polling; resume useful work between checks and do not block resources needed by peers. "
)
