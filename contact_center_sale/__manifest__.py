{
    "name": "Contact Center Sale",
    "summary": "Customer conversations from quotations and sales orders",
    "version": "16.0.1.0.0",
    "category": "Sales/Sales",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/contact-center",
    "license": "AGPL-3",
    "depends": ["sale", "contact_center_ui"],
    "data": ["views/sale_order_views.xml"],
    "assets": {
        "web.assets_backend": [
            "contact_center_sale/static/src/js/sale_conversations.esm.js",
            "contact_center_sale/static/src/xml/sale_conversations.xml",
        ],
        "web.qunit_suite_tests": [
            "contact_center_sale/static/tests/sale_conversations_tests.esm.js",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
