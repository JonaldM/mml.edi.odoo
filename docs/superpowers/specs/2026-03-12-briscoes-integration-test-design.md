# Briscoes EDI Integration Test Design

**Status:** Approved for implementation

## Goal

Full integration test of `mml_edi` against the live `mml_dev` Odoo 19 instance on Hetzner (port 8090), covering all Briscoes EDI message types and all ORDRSP response scenarios. Confirms read/write to real Odoo DB before Briscoes go-live.

## Infrastructure

- **Hetzner server:** `root@100.94.135.90` (Tailscale) / `46.62.148.99` (public)
- **Odoo 19 dev instance:** `http://46.62.148.99:8090`, DB: `mml_dev`
- **Addons on server:** `/home/deploy/odoo-dev/addons/mml_edi/`
- **DB credentials:** user `odoo`, password `devpass123`

## Briscoes Message Types to Cover

| Type | BGM code | Direction | File in fixtures |
|------|----------|-----------|-----------------|
| ORDERS | 220 | Inbound | `briscoes_orders_4500038166.edi` |
| ORDCHG | 230 | Inbound | `briscoes_ordchg_4500038166.edi` |
| ORDRSP — Supplied In Full | 231 purpose 29 | Outbound | generated |
| ORDRSP — Short Supplied | 231 purpose 4 | Outbound | generated |
| ORDRSP — Cancelled/Deleted | 231 purpose 27 | Outbound | generated |
| ORDRSP — Price/Date Changed | 231 purpose 4 | Outbound | generated |
| ORDRSP — Incorrect Items | 231 purpose 4 | Outbound | generated |

## Test Data Strategy

The sample EDIFACT files use barcodes `9414844375629`, `9414844375636`, `9414844375674`.
These do not exist in `mml_dev` products, so integration tests create them in `setUp()` and rely on `TransactionCase` auto-rollback for cleanup.

Products created in `setUp()`:
- Product A: barcode `9414844375629`, list_price 5.50, internal ref `375629`
- Product B: barcode `9414844375636`, list_price 0.55, internal ref `375636`
- Product C: barcode `9414844375674`, list_price 9.50, internal ref `375674`

Trading partner: `code=BRISCOES_TEST`, parser `BriscoesParser`, `product_match_field=barcode`, `order_split_mode=per_store`, `price_tolerance_pct=100.0` (avoids price blocking during testing), `auto_confirm_clean=False`.

## Architecture

```
test_briscoes_integration.py
├── EDIBriscoesSetup (mixin, extends EDITestSetup)
│   └── setUp(): creates 3 products + Briscoes trading partner
├── TestBriscoesOrdersIntegration  (TransactionCase)
│   ├── test_orders_creates_sale_order()
│   ├── test_orders_line_count_matches_per_store()
│   └── test_orders_product_lookup_by_barcode()
├── TestBriscoesOrdchgIntegration  (TransactionCase)
│   ├── test_ordchg_routes_to_pending_review()
│   └── test_ordchg_apply_updates_so_lines()
└── TestBriscoesOrdrspIntegration  (TransactionCase)
    ├── test_ordrsp_supplied_in_full()
    ├── test_ordrsp_short_supplied()
    ├── test_ordrsp_cancelled()
    ├── test_ordrsp_price_date_changed()
    └── test_ordrsp_incorrect_items()
```

## Test Execution

Pure-Python (local, fast):
```bash
cd E:/ClaudeCode/projects/mml.odoo/mml.odoo.apps/mml_edi
pytest -q
```

Odoo integration (on Hetzner):
```bash
ssh root@100.94.135.90 "docker exec mml-dev-odoo odoo --test-enable \
  -d mml_dev --db_host=db --db_user=odoo --db_password=devpass123 \
  --test-tags /mml_edi --no-http --stop-after-init -u mml_edi 2>&1 | tail -50"
```

## Sync Mechanism

```bash
ssh root@100.94.135.90 "rsync not available" # use tar+ssh
# Package mml_edi locally → upload → extract on Hetzner → update module
```

## Success Criteria

- All pure-Python pytest tests pass (green)
- All `TestBriscoesOrdersIntegration` tests pass: SO created with correct partner, lines, review state
- All `TestBriscoesOrdchgIntegration` tests pass: change order routed to review, apply mutates SO
- All `TestBriscoesOrdrspIntegration` tests pass: ORDRSP bytes have correct BGM purpose code, correct LIN action codes per scenario
- Any bugs found are fixed and committed
