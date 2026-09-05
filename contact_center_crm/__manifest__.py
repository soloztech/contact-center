{
    "name": "Contact Center CRM",
    "summary": "Explicit CRM bridge for Contact Center cases and pipelines",
    "version": "16.0.1.0.0",
    "category": "Sales/CRM",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/contact-center",
    "license": "AGPL-3",
    "depends": ["contact_center_kanban", "crm"],
    "data": [
        "security/ir.model.access.csv",
        "security/contact_center_crm_security.xml",
        "views/contact_center_crm_views.xml",
        "views/crm_lead_views.xml",
        "views/menus.xml",
    ],
    "installable": True,
    "application": False,
}
