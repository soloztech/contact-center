{
    "name": "Contact Center Kanban",
    "summary": "Optional service cases and pipelines for Contact Center",
    "version": "16.0.1.0.0",
    "category": "Productivity/Discuss",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/contact-center",
    "license": "AGPL-3",
    "depends": ["contact_center_base"],
    "data": [
        "security/contact_center_kanban_security.xml",
        "security/ir.model.access.csv",
        "views/pipeline_views.xml",
        "views/productivity_views.xml",
    ],
    "installable": True,
    "application": False,
    "post_init_hook": "post_init_hook",
}
