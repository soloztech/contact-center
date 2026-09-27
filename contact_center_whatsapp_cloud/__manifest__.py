{
    "name": "Contact Center WhatsApp Cloud",
    "summary": "WhatsApp Business Platform (Cloud API) adapter for Contact Center",
    "version": "16.0.1.0.0",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/contact-center",
    "license": "AGPL-3",
    "category": "Productivity/Discuss",
    "depends": ["contact_center_base", "meta_api_base", "meta_webhook_base"],
    "data": [
        "security/ir.model.access.csv",
        "security/delivery_failure_security.xml",
        "views/whatsapp_cloud_config_views.xml",
        "views/delivery_failure_views.xml",
    ],
    "installable": True,
    "application": False,
}
