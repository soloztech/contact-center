"""Process-local capabilities for the conversation lifecycle ledger.

Every value here is a Python object. RPC and JSON context values can only carry
strings, numbers and containers, so a caller outside the application service can
neither choose the recorded source of a lifecycle event nor offer a creation as
causal evidence.
"""

LIFECYCLE_SOURCE_CONTEXT_KEY = "contact_center_lifecycle_source"
LIFECYCLE_CREATIONS_CONTEXT_KEY = "contact_center_lifecycle_creations"
LIFECYCLE_EVENT_TOKEN = object()


class LifecycleSource:
    """Origin label of the writes performed under one context."""

    __slots__ = ("value", "automatic")

    def __init__(self, value, automatic):
        self.value = value
        self.automatic = automatic

    def __repr__(self):
        return "<LifecycleSource %s>" % self.value


LIFECYCLE_SOURCE_AUTOMATIC_INBOUND = LifecycleSource("automatic_inbound", True)
LIFECYCLE_SOURCE_ACCESS_CHANGE = LifecycleSource("access_change", False)
_KNOWN_SOURCES = (LIFECYCLE_SOURCE_AUTOMATIC_INBOUND, LIFECYCLE_SOURCE_ACCESS_CHANGE)


def lifecycle_source(context):
    """Return the trusted source object of a context, or ``None`` (manual)."""

    value = context.get(LIFECYCLE_SOURCE_CONTEXT_KEY)
    return next((source for source in _KNOWN_SOURCES if value is source), None)


class LifecycleCreations:
    """Conversations created while one inbound message was being processed.

    ``_post_inbound`` passes a fresh collector to the channel resolution only;
    the ``created`` events recorded under it are the causal evidence the new
    message binding may carry. Nothing outside that call can add to it.
    """

    __slots__ = ("_events",)

    def __init__(self):
        self._events = {}

    def add(self, event):
        self._events[event.channel_id.id] = event

    def event_of(self, channel):
        return self._events.get(channel.id)
