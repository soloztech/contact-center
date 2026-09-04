{
    "name": "Contact Center WuzAPI",
    "summary": "WuzAPI provider adapter for Contact Center",
    "version": "16.0.1.29.2",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/contact-center",
    "license": "AGPL-3",
    "category": "Productivity/Discuss",
    "depends": ["contact_center_base"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/contact_center_wuzapi_security.xml",
        "security/ir.model.access.csv",
        "data/webhook_event_data.xml",
        "data/queue_job.xml",
        "views/wuzapi_config_views.xml",
        "views/onboarding_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "contact_center_wuzapi/static/src/js/onboarding_qr_field.esm.js",
            "contact_center_wuzapi/static/src/xml/onboarding_qr_field.xml",
            "contact_center_wuzapi/static/src/scss/onboarding.scss",
        ],
    },
    "installable": True,
    "application": False,
}
