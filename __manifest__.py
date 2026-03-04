{
    "name": "MML EDI",
    "version": "15.0.1.0.0",
    "summary": "Electronic Data Interchange for retail partners (Briscoes Group and others)",
    "description": """
        Customer-agnostic EDI module for Odoo 15.
        Replaces the legacy .NET Windows service handling Briscoes Group purchase orders.
        Phase 1: Briscoes parser stub + full processing engine, review dashboard, FTP handler.
        Phase 2: Real EDIFACT D96A parsing (awaiting partner technical spec).
    """,
    "author": "MML Consumer Products Ltd",
    "website": "https://github.com/JonaldM/mml.edi.odoo",
    "category": "Operations",
    "license": "LGPL-3",
    "depends": ["base", "sale", "account", "stock", "mail"],
    "data": [
        # Security — groups first, then access rules
        "security/edi_security.xml",
        "security/ir.model.access.csv",
        # Sequences and cron (noupdate=1 inside)
        "data/ir_sequence.xml",
        "data/ir_cron.xml",
        # Views
        "views/edi_trading_partner_views.xml",
        "views/edi_order_review_views.xml",
        "views/edi_order_issue_views.xml",
        "views/edi_log_views.xml",
        "views/sale_order_views.xml",
        # Wizard views
        "wizards/edi_bulk_action_views.xml",
        "wizards/edi_seed_stores_views.xml",
        # Menus (after all view actions are defined)
        "views/menuitems.xml",
        # Seed data (noupdate=1 inside)
        "data/edi_trading_partner_briscoes.xml",
        # Templates
        "data/mail_template.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
}
