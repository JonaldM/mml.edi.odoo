# feat(edi): enforce ex-GST pricelist constraint on trading partners

## Summary

Convert the previously informational GST-inclusive pricelist check on
`edi.trading.partner` into a hard `@api.constrains`, making the
misconfiguration impossible by construction.

## Why

EDI prices from Briscoes (and other retail trading partners) are quoted
ex-GST. If the assigned `product.pricelist` resolves to GST-inclusive
prices, every line shows a systematic ~15% discrepancy. The processor
then creates blocking `price_discrepancy` issues on otherwise-clean
orders, sending them to manual review for no real reason. This is the
top-ranked false-positive source flagged in the 2026-04-27
production-readiness audit.

Before this change, `edi_processor.py` documented the requirement in a
comment but did not enforce it. A misconfigured pricelist could ship to
production without warning.

## What changed

- `models/edi_trading_partner.py`: new
  `@api.constrains('pricelist_id')` method `_validate_pricelist_gst`.
  It iterates `pricelist_id.item_ids`, follows each item's
  `product_id` (or `product_tmpl_id` for template-level items) to its
  `taxes_id`, and raises `ValidationError` if any tax has
  `price_include=True`. Empty / missing pricelists are accepted (price
  comparison is opt-in).
- `models/edi_processor.py`: comment block above the price-comparison
  call updated to reference the new constraint. The diagnostic
  `_logger.debug` was kept as belt-and-braces — it is unreachable on
  normally-configured systems but remains useful if the constraint is
  ever bypassed (raw SQL, migration, etc.). No functional change to the
  processor flow.
- `tests/test_pricelist_gst_constraint.py`: 6 new pure-Python tests
  (using the existing conftest stubs):
  - `test_no_pricelist_is_accepted`
  - `test_ex_gst_pricelist_is_accepted`
  - `test_gst_inclusive_pricelist_is_rejected` (asserts the message
    names the pricelist and mentions 15% / discrepancy)
  - `test_mixed_taxes_with_one_inclusive_is_rejected`
  - `test_pricelist_with_template_only_item_is_checked`
  - `test_pricelist_with_no_items_is_accepted`

No new model, no new ACL entries — the constraint piggy-backs on the
existing `edi.trading.partner` access rules.

## Decision: warning treatment

The pre-existing `_logger.debug` line in `edi_processor.py` was
**retained** as a defensive diagnostic. The constraint makes
GST-inclusive impossible through the ORM, but operators reading server
logs still benefit from the explicit "EDI prices are ex-GST" hint when
debugging future issues. The comment block above it was rewritten to
point at the new constraint as the source of truth.

## Test results

```
$ pytest -m "not odoo_integration" -q
......................................ssssssssssssssssss..............ss [ 47%]
ss................sssss......................sssssss..ssssss......ssssss [ 94%]
sssssssss                                                                [100%]
98 passed, 55 skipped in 0.20s
```

Baseline before this change: 92 passed, 55 skipped.
After this change: 98 passed, 55 skipped. (+6 new tests, 0 regressions.)

The 55 skipped tests are `odoo_integration` tests that require a live
Odoo database — out of scope for this change.

## Test plan

- [x] New unit tests cover accept/reject paths and the empty-pricelist edge case
- [x] Full pytest suite green
- [ ] Manual: in a staging Odoo instance, attempt to assign a
  GST-inclusive pricelist to an `edi.trading.partner` and confirm the
  ValidationError surfaces in the form view
- [ ] Manual: confirm existing trading partners with ex-GST pricelists
  are unaffected on save

## Compare & PR

https://github.com/JonaldM/mml.edi.odoo/compare/master...claude-sprint/edi-gst-constraint?expand=1
