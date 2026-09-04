META_PROVIDER_SCHEMA_VERSION = "meta.messaging.v2"
MAX_MESSAGING_ITEMS_PER_ENTRY = 100
MAX_MESSAGE_ATTACHMENTS = 10
PRIVATE_MEDIA_RETENTION_HOURS = 24

META_PAGE_WEBHOOK_FIELDS = frozenset(
    {
        "message_deliveries",
        "message_echoes",
        "message_edits",
        "message_reactions",
        "message_reads",
        "messages",
        "messaging_postbacks",
        "messaging_referrals",
    }
)
META_INSTAGRAM_WEBHOOK_FIELDS = frozenset(
    {
        "message_reactions",
        "messages",
        "messaging_postbacks",
        "messaging_referral",
        "messaging_seen",
    }
)

META_TRANSPORT_CONTRACTS = {
    "messenger_page": {
        "account_platform": "messenger",
        "asset_platform": "facebook",
        "object_type": "page",
        "transport": "page",
        "fields": META_PAGE_WEBHOOK_FIELDS,
    },
    "instagram_page_linked": {
        "account_platform": "instagram",
        "asset_platform": "instagram",
        "object_type": "instagram",
        "transport": "page_linked",
        "fields": META_INSTAGRAM_WEBHOOK_FIELDS,
    },
}


def transport_mode_for_asset(asset):
    """Return the one messaging mode represented by a canonical Meta asset."""

    if not asset:
        return ""
    observed = (asset.platform, asset.object_type, asset.transport)
    for mode, contract in META_TRANSPORT_CONTRACTS.items():
        expected = (
            contract["asset_platform"],
            contract["object_type"],
            contract["transport"],
        )
        if observed == expected:
            return mode
    return ""
