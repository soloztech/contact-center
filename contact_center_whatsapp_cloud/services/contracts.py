"""Immutable WhatsApp Cloud contracts shared by the adapter and its consumer."""

import datetime

WHATSAPP_CLOUD_ADAPTER_KEY = "whatsapp_cloud"
WHATSAPP_CLOUD_PROVIDER_SCHEMA_VERSION = "whatsapp_cloud.v1"
WHATSAPP_CLOUD_PAYLOAD_SCHEMA_VERSION = "contact_center.whatsapp_cloud.v1"
WHATSAPP_CLOUD_CONSUMER_KEY = "contact_center.whatsapp_cloud"
WHATSAPP_OBJECT_TYPE = "whatsapp_business_account"
WHATSAPP_OWNER_KIND = "whatsapp_business_account"
WHATSAPP_WEBHOOK_FIELD = "messages"
WHATSAPP_ACCOUNT_PLATFORM = "whatsapp"
WHATSAPP_ASSET_CONTRACT = {
    "platform": "whatsapp",
    "object_type": WHATSAPP_OBJECT_TYPE,
    "transport": "whatsapp_cloud",
}
WHATSAPP_COLLECTIONS = ("messages", "statuses", "errors")
DEFAULT_GRAPH_VERSION = "v26.0"

PHONE_NAMESPACE = "whatsapp.pn"
BSUID_NAMESPACE = "whatsapp.bsuid"
PHONE_SUFFIX = "@s.whatsapp.net"

RESPONSE_WINDOW = datetime.timedelta(hours=24)
MARK_READ_WINDOW = datetime.timedelta(days=30)
REFERRAL_LINK_RETENTION = datetime.timedelta(days=8)
MAX_FUTURE_CLOCK_SKEW = datetime.timedelta(minutes=5)

WHATSAPP_SYSTEM_EVENT_TYPE = "whatsapp_cloud.system"
WHATSAPP_SYSTEM_CONTENT_TYPE = "whatsapp.system"
WHATSAPP_SYSTEM_EXTENSION = "whatsapp_system"

CLIENT_MESSAGE_ID_PREFIX = "wac:"
