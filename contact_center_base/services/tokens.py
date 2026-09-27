"""Process-local capabilities for Contact Center application services."""

CONTACT_CENTER_MEMBERSHIP_TOKEN = object()
CONTACT_CENTER_POST_TOKEN = object()
CONTACT_CENTER_ATTRIBUTION_TOKEN = object()
CONTACT_CENTER_PRODUCTIVITY_TOKEN = object()

CONTACT_CENTER_DELETION_TOKEN = object()
# Marks the agent-side bulk read (L08): no read receipt to the customer.
CONTACT_CENTER_BULK_READ_TOKEN = object()
