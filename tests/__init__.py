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
        test_approve_reclamp,
        test_briscoes_asn,
        test_briscoes_integration,
        test_cascade_lookup,
        test_deduplication,
        test_idempotency_db_backstop,
        test_ordchg_availability_gate,
        test_po_change_workflow,
        test_price_discrepancy,
        test_processor,
        test_reclamp_order_lines,
        test_reservation_verify,
        test_review_workflow,
        test_short_ship_policy,
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
            test_asn_barcode_validation,
            test_briscoes_edifact_parser,
            test_briscoes_encoding,
            test_briscoes_idoc_multi_po_ack,
            test_briscoes_ordrsp,
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
            test_migration_dup_precheck,
            test_poll_ordering_invariant,
            test_pricelist_gst_constraint,
            test_reclamp_math,
            test_short_ship,
        )
    except (ImportError, ModuleNotFoundError):
        pass
