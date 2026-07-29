try:
    import odoo as _odoo
    _STUB = getattr(_odoo, "_stubbed", False)
except ImportError:
    _STUB = True

# Odoo-safe: TransactionCase subclasses, no module-level pytest import,
# no module-scope manual model loading.  Imported so odoo-bin --test-enable
# can discover and run them.  Wrapped in try/except because this __init__.py
# is executed before the conftest stubs are in place when pytest first
# imports the package.
try:
    from . import (
        test_ack_reset_reapprove,
        test_ack_send_claim,
        test_nimbrel_identity_odoo,
        test_nimbrel_invoice_odoo,
        test_nimbrel_ordrsp_live_odoo,
        test_nimbrel_pipeline_odoo,
        test_approve_reclamp,
        test_kestrelby_asn,
        test_kestrelby_integration,
        test_cascade_lookup,
        test_deduplication,
        test_edi_dashboard,
        test_edi_partner_health,
        test_edi_processing_log,
        test_edi_trading_partner_settings,
        test_edi_seed_stores_odoo,
        test_edi_service_nimbrel_odoo,
        test_idempotency_db_backstop,
        test_ordchg_availability_gate,
        test_po_change_workflow,
        test_price_discrepancy,
        test_processor,
        test_reclamp_order_lines,
        test_reservation_verify,
        test_review_workflow,
        test_edi_review_queue,
        test_edi_wall,
        test_edi_mobile_triage,
        test_short_ship_policy,
        test_sscc_register_odoo,
    )
except (ImportError, ModuleNotFoundError):
    # conftest stubs not yet in place — pytest will collect these directly.
    pass

if _STUB:
    # Pure-Python only: module-level `import pytest`, or function-only tests
    # (no TestCase).  Odoo's loader would crash on these; pytest collects them
    # directly via conftest, so they do not need to be imported here.
    try:
        from . import (
            test_ack_exchange_filename,
            test_nimbrel_contrl_generate,
            test_nimbrel_identity,
            test_nimbrel_invoice_service,
            test_nimbrel_ordrsp_live,
            test_nimbrel_pipeline,
            test_nimbrel_store_master,
            test_asn_barcode_validation,
            test_kestrelby_edifact_parser,
            test_kestrelby_encoding,
            test_kestrelby_idoc_multi_po_ack,
            test_kestrelby_ordrsp,
            test_circuit_breaker,
            test_client_ref_template,
            test_correlation_logging,
            test_credential_encryption,
            test_cron_alert_escaping,
            test_cron_alert_rate_limit,
            test_duplicate_so_guard,
            test_edi_service,
            test_file_failure_breaker,
            test_ftp_handler,
            test_gs1_sscc,
            test_localdir_handler,
            test_migration_dup_precheck,
            test_poll_ordering_invariant,
            test_pricelist_gst_constraint,
            test_wall_format,
            test_mobile_triage_format,
            test_reclamp_math,
            test_short_ship,
            test_sscc_register_constraint_matching,
        )
    except (ImportError, ModuleNotFoundError):
        pass
