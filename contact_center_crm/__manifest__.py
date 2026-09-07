{
    "name": "Contact Center CRM",
    "summary": "Customer opportunities, quotations, orders and invoices in the inbox",
    "version": "16.0.1.0.0",
    "category": "Sales/CRM",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/contact-center",
    "license": "AGPL-3",
    "depends": ["contact_center_ui", "crm"],
    "data": [
        "security/ir.model.access.csv",
        "security/contact_center_crm_security.xml",
        "views/crm_lead_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "contact_center_crm/static/src/js/crm_panel.esm.js",
            "contact_center_crm/static/src/js/contact_center_app_crm.esm.js",
            "contact_center_crm/static/src/xml/*.xml",
            "contact_center_crm/static/src/scss/crm_panel.scss",
        ],
        "web.qunit_suite_tests": [
            "contact_center_crm/static/tests/crm_panel_tests.esm.js",
        ],
    },
    "installable": True,
    "application": False,
}
