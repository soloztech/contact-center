{
    "name": "Contact Center Meta",
    "summary": "Meta Messenger and Instagram provider foundation for Contact Center",
    "version": "16.0.1.0.0",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/contact-center",
    "license": "AGPL-3",
    "category": "Productivity/Discuss",
    "depends": ["contact_center_base", "meta_api_base", "meta_webhook_base"],
    "data": [
        "data/media_locator_cron.xml",
        "views/meta_config_views.xml",
        "views/menus.xml",
    ],
    "installable": True,
    "application": False,
}
