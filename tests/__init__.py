try:
    from . import (
        test_briscoes_integration,
        test_cascade_lookup,
        test_deduplication,
        test_po_change_workflow,
        test_price_discrepancy,
        test_processor,
        test_review_workflow,
    )
except (ImportError, ModuleNotFoundError):
    # Odoo is not available in the pure-Python test environment.
    # These modules are collected directly by pytest and do not need to be
    # imported here — this block only exists for Odoo's own test runner.
    pass
