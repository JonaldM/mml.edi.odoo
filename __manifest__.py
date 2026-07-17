{
    "name": "MML EDI",
    "version": "19.0.1.0.1",
    "summary": "Electronic Data Interchange for retail partners (Briscoes Group and others)",
    "description": """
        Customer-agnostic EDI module for Odoo 19.
        Replaces the legacy .NET Windows service handling Briscoes Group purchase orders.
        Phase 1: Briscoes parser stub + full processing engine, review dashboard, FTP handler.
        Phase 2: Real EDIFACT D96A parsing (awaiting partner technical spec).
    """,
    "author": "MML Consumer Products Ltd",
    "website": "https://www.mmlconsumerproducts.co.nz",
    "category": "Operations",
    "license": "LGPL-3",
    "depends": ["mml_base", "sale", "account", "stock", "mail"],
    "data": [
        # Security — groups first, then access rules
        "security/edi_security.xml",
        "security/ir.model.access.csv",
        # Sequences and cron (noupdate=1 inside)
        "data/ir_sequence.xml",
        "data/ir_cron.xml",
        # Wizard views (must come before main views that reference wizard actions)
        "wizards/edi_bulk_action_views.xml",
        "wizards/edi_seed_stores_views.xml",
        # Views
        "views/edi_trading_partner_views.xml",
        "views/edi_order_review_views.xml",
        "views/edi_order_issue_views.xml",
        "views/edi_log_views.xml",
        "views/sale_order_views.xml",
        "views/stock_location_views.xml",
        # Client-action screens (OWL) — actions defined before the menus below
        "views/edi_dashboard_action.xml",
        # Menus (after all view actions are defined)
        "views/menuitems.xml",
        # Review-queue OWL client actions + nav menu (parent defined in menuitems)
        "views/edi_review_queue_action.xml",
        # Seed data (noupdate=1 inside)
        "data/edi_trading_partner_briscoes.xml",
        # Templates
        "data/mail_template.xml",
    ],
    "assets": {
        "web.assets_backend": [
            # Shared theme token layer (this task owns it for the UI-refresh
            # program) + shape vocabulary — listed first so per-screen SCSS
            # composes it.
            "mml_edi/static/src/scss/edi_shared.scss",
            "mml_edi/static/src/utils/edi_format.js",
            "mml_edi/static/src/utils/use_partner_filter.js",
            "mml_edi/static/src/utils/use_partner_filter.xml",
            # EDI operations dashboard (client action `edi_dashboard`).
            "mml_edi/static/src/scss/edi_dashboard.scss",
            "mml_edi/static/src/js/edi_dashboard.js",
            "mml_edi/static/src/xml/edi_dashboard.xml",
            # EDI review-queue screens (client actions `edi_review_queue`,
            # `edi_review_detail`) — master-detail triage + single-review page.
            "mml_edi/static/src/utils/review_format.js",
            "mml_edi/static/src/scss/edi_review_queue.scss",
            "mml_edi/static/src/js/edi_review_queue.js",
            "mml_edi/static/src/js/edi_review_detail.js",
            "mml_edi/static/src/xml/edi_review_queue.xml",
            "mml_edi/static/src/xml/edi_review_detail.xml",
        ],
        # Dark-mode token overrides — same bundle key web_enterprise uses for
        # its own *.dark.scss companions, so Odoo loads it only under the dark
        # colour scheme (no custom toggle).
        "web.assets_web_dark": [
            "mml_edi/static/src/scss/edi_shared.dark.scss",
        ],
        # Hoot unit tests for the pure formatting helpers.
        "web.assets_unit_tests": [
            "mml_edi/static/tests/**/*",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
    "web_icon": "mml_edi,static/description/icon.png",
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
}
