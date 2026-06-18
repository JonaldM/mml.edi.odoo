# mml.edi/models/edi_processor.py
"""
Customer-agnostic EDI processing engine.

Entry point for cron: edi.processor.run_scheduled_poll()
Entry point for manual: trading_partner.action_run_poll_now() → poll_trading_partner()
Public API for tests: process_parsed_order(), apply_change_order()

AbstractModel — no database table.
"""
import base64
import hashlib
import html
import json
import logging
import secrets
from datetime import date, datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


def build_session_id() -> str:
    """Generate a short unique ID for correlating log messages within one poll run."""
    return secrets.token_hex(4)


def _escape_ilike(value: str) -> str:
    """Escape LIKE/ILIKE wildcards so an '=ilike' acts as an exact (but
    case-insensitive) match.

    A product code containing '_' or '%' (both legal in default_code /
    supplierinfo codes) would otherwise be treated as a wildcard by '=ilike'
    and match the WRONG product. PostgreSQL LIKE uses backslash as the default
    escape char, so escape the backslash first, then '%' and '_'.
    """
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


class EDIProcessor(models.AbstractModel):
    _name = "edi.processor"
    _description = "EDI Processing Engine"

    # ── Cron entry point ──────────────────────────────────────────────────

    @api.model
    def run_scheduled_poll(self):
        """Called by ir.cron. Polls all active trading partners."""
        from .edi_trading_partner import circuit_is_open
        partners = self.env["edi.trading.partner"].search([("active", "=", True)])
        _logger.info("[EDI] Scheduled poll: %d active partner(s)", len(partners))
        for partner in partners:
            if circuit_is_open(partner):
                _logger.info(
                    "[EDI] Circuit breaker OPEN for %s — skipping poll "
                    "(failures=%d, open_since=%s, cooldown=%dmin)",
                    partner.code,
                    partner.circuit_failure_count,
                    partner.circuit_open_since,
                    partner.circuit_cooldown_minutes,
                )
                continue
            try:
                self.poll_trading_partner(partner)
                partner.write({"circuit_failure_count": 0, "circuit_open_since": False})
            except Exception as exc:
                _logger.exception("[EDI] Poll failed for partner %s", partner.code)
                new_count = partner.circuit_failure_count + 1
                vals = {"circuit_failure_count": new_count}
                if new_count >= partner.circuit_failure_threshold and not partner.circuit_open_since:
                    vals["circuit_open_since"] = fields.Datetime.now()
                    _logger.error(
                        "[EDI] Circuit breaker TRIPPED for %s after %d consecutive failures",
                        partner.code, new_count,
                    )
                partner.write(vals)
                self.env["edi.log"].log(
                    partner, "inbound", "error", "error",
                    "Scheduled poll failed: %s" % str(exc),
                    detail=str(exc),
                )
                self._send_cron_alert(
                    'mml_edi',
                    'EDI poll failed for %s' % partner.code,
                    str(exc),
                )

    # ── Deferred-ACK retry safety net ─────────────────────────────────────

    @api.model
    def retry_pending_acks(self):
        """Re-queue ORDRSP/ACKs that should have been sent but weren't.

        A deferred ACK upload can fail (FTP down, transient error) and is logged
        ack_sent/error but never retried — Briscoes then never gets a response.
        This cron finds POs whose store-reviews are ALL resolved (none still in
        pending_review) yet have no successful ack_sent log row, and re-queues the
        ACK. Idempotent: _queue_ack() defers while any sibling is pending and is
        keyed per-exchange (see fix #5 / edi_file_hash), so re-running is safe.
        Called by ir.cron (cron_edi_retry_acks).
        """
        Review = self.env["edi.order.review"]
        Log = self.env["edi.log"]

        # Candidate exchanges: resolved reviews (left pending_review) for active
        # partners. One ACK is sent per (partner, PO, inbound-file) exchange, so
        # group by that triple — mirrors _queue_ack's per-exchange idempotency key.
        resolved = Review.search([
            ("state", "in", ("approved", "rejected", "auto_approved")),
            ("trading_partner_id.active", "=", True),
        ], order="id")

        seen_exchanges = set()
        requeued = 0
        for review in resolved:
            partner = review.trading_partner_id
            po = review.customer_po_number
            exchange = (partner.id, po, review.edi_file_hash or review.id)
            if exchange in seen_exchanges:
                continue
            seen_exchanges.add(exchange)

            # Skip if this PO still has any store-review in manual review — the
            # ACK is legitimately deferred and not yet due.
            if Review.search_count([
                ("trading_partner_id", "=", partner.id),
                ("customer_po_number", "=", po),
                ("state", "=", "pending_review"),
            ]):
                continue

            # Skip if an ACK for this exact exchange already uploaded successfully.
            exchange_key = (review.edi_file_hash or str(review.id))[:8]
            filename = "ACK_%s_%s_%s.edi" % (partner.code, po, exchange_key)
            if Log.search_count([
                ("trading_partner_id", "=", partner.id),
                ("event_type", "=", "ack_sent"),
                ("status", "=", "success"),
                ("filename", "=", filename),
            ]):
                continue

            # There is at least one prior failed/absent ACK for a fully-resolved
            # exchange — re-queue it (no-op-safe if it now succeeds or is deferred).
            try:
                review._queue_ack()
                requeued += 1
            except Exception:
                _logger.exception(
                    "[EDI] retry_pending_acks: re-queue failed for PO %s (%s)",
                    po, partner.code,
                )

        if requeued:
            _logger.info("[EDI] retry_pending_acks: re-queued %d ACK(s)", requeued)

    # ── Per-partner poll ──────────────────────────────────────────────────

    @api.model
    def poll_trading_partner(self, partner):
        """Download and process all files from a trading partner's FTP inbox."""
        from .edi_ftp import EDIFTPHandler
        from ..parsers.base_parser import EDIFTPError

        sid = build_session_id()
        prefix = "[EDI:%s]" % sid
        _logger.info("%s Polling %s", prefix, partner.code)
        handler = EDIFTPHandler(partner)

        try:
            with handler.connection():
                files = handler.list_files()
                _logger.info("%s %s: %d file(s) in inbox", prefix, partner.code, len(files))

                for filename in files:
                    try:
                        content = handler.download_file(filename)
                        file_hash = hashlib.sha256(content).hexdigest()

                        # Dedup BEFORE writing any success row: a genuinely
                        # re-sent file (same hash) carries a file_download/success
                        # row from an earlier poll, so it is skipped here. The
                        # current file has no such row yet — we only write it
                        # AFTER _process_file succeeds (below), so a file is never
                        # flagged as a duplicate of itself within its own poll.
                        if self._is_file_duplicate(file_hash, partner):
                            self.env["edi.log"].log(
                                partner, "inbound", "duplicate_skipped", "warning",
                                "Duplicate file skipped (hash already processed): %s" % filename,
                                filename=filename, file_hash=file_hash,
                            )
                            handler.move_to_processed(filename)
                            continue

                        # _process_file isolates each store-order in its own
                        # savepoint and returns the list of stores that failed.
                        # We do NOT wrap the whole call in a rollback savepoint —
                        # stores that succeeded must persist so a retry can skip
                        # them via SO-ref dedup.
                        failures = self._process_file(content, file_hash, filename, partner)

                        if failures:
                            # Leave the file in the inbox for retry and withhold
                            # the dedup marker (file_download/success). On the next
                            # poll the same file is re-downloaded; succeeded stores
                            # are skipped by SO-ref dedup and only the failed ones
                            # are re-attempted.
                            self.env["edi.log"].log(
                                partner, "inbound", "error", "error",
                                "%d store-order(s) failed in %s — file left for retry"
                                % (len(failures), filename),
                                filename=filename, file_hash=file_hash,
                            )
                            continue

                        # Mark the file processed (the dedup marker) only after
                        # EVERY store-order succeeded.
                        self.env["edi.log"].log(
                            partner, "inbound", "file_download", "success",
                            "Downloaded: %s (%d bytes)" % (filename, len(content)),
                            filename=filename, file_hash=file_hash,
                        )

                        handler.move_to_processed(filename)

                    except Exception as exc:
                        _logger.exception(
                            "%s Error processing file %s for %s", prefix, filename, partner.code
                        )
                        self.env["edi.log"].log(
                            partner, "inbound", "error", "error",
                            "Error processing %s: %s" % (filename, str(exc)),
                            filename=filename, detail=str(exc),
                        )

        except EDIFTPError as exc:
            _logger.error("%s FTP connection failed for %s: %s", prefix, partner.code, exc)
            self.env["edi.log"].log(
                partner, "inbound", "ftp_connection", "error",
                "FTP connection failed: %s" % str(exc),
                detail=str(exc),
            )
            raise

    # ── File processing ───────────────────────────────────────────────────

    def _process_file(self, content: bytes, file_hash: str, filename: str, partner) -> list:
        """Parse a downloaded file and dispatch each ParsedOrder.

        Returns a list of (store_code, error) tuples for store-orders that failed
        — empty when the whole file processed cleanly. The poll path uses this to
        decide whether to write the dedup marker / move the file to processed.
        """
        if self._is_file_duplicate(file_hash, partner):
            self.env["edi.log"].log(
                partner, "inbound", "duplicate_skipped", "warning",
                "Duplicate file skipped (hash already processed): %s" % filename,
                filename=filename, file_hash=file_hash,
            )
            return []

        parser = partner.get_parser_instance()
        raw_text = content.decode("utf-8", errors="replace")
        parsed_orders = parser.parse_file(content, partner)

        self.env["edi.log"].log(
            partner, "inbound", "file_parse", "success",
            "Parsed %d order(s) from %s" % (len(parsed_orders), filename),
            filename=filename, file_hash=file_hash,
        )

        # Process each store-order in its own savepoint AND its own try/except so
        # one bad store cannot abort the rest of the PO (which would silently lose
        # every remaining store). Each store that succeeds commits independently;
        # failures are collected and RETURNED to the caller, which then leaves the
        # whole file in the inbox for retry (and withholds the dedup marker). On
        # retry the already-created stores are skipped by SO-ref dedup, so only the
        # previously-failed store(s) are re-attempted — no duplicate SOs.
        failures = []
        for order in parsed_orders:
            order.raw_data = raw_text
            try:
                with self.env.cr.savepoint():
                    self.process_parsed_order(order, partner, filename, file_hash)
            except Exception as exc:
                _logger.exception(
                    "[EDI] Failed to process store '%s' of PO %s from %s",
                    order.store_code, order.po_number, filename,
                )
                self.env["edi.log"].log(
                    partner, "inbound", "error", "error",
                    "Failed to process store '%s' of PO %s in %s: %s" % (
                        order.store_code, order.po_number, filename, str(exc)),
                    filename=filename, file_hash=file_hash, detail=str(exc),
                )
                failures.append((order.store_code, str(exc)))

        # Per-PO ACK: every store of this PO has now been processed. Send a single
        # ORDRSP if the order auto-approved cleanly; this is a no-op (deferred)
        # while any store-review is still in manual review, and idempotent so it
        # uploads at most once per PO. (Briscoes expects one ORDRSP per PO.)
        # Skip the ACK when any store failed — the PO is not yet fully processed,
        # and the deferred-ACK retry cron (see retry_pending_acks) will pick it up
        # once the file is retried and all stores have resolved.
        if parsed_orders and not failures:
            po_number = parsed_orders[0].po_number
            any_review = self.env["edi.order.review"].search([
                ("trading_partner_id", "=", partner.id),
                ("customer_po_number", "=", po_number),
            ], order="id desc", limit=1)
            if any_review:
                any_review._queue_ack()

        return failures

    @api.model
    def process_parsed_order(self, order, partner, filename: str, file_hash: str):
        """
        Process a single ParsedOrder through the full pipeline.
        Public — used by cron pipeline and tests.
        """
        client_ref = partner.render_client_ref(order.po_number, order.store_code)

        if order.document_type == "change_order":
            self._process_change_order(order, partner, client_ref, filename, file_hash)
        else:
            self._process_new_order(order, partner, client_ref, filename, file_hash)

    # ── New order flow ────────────────────────────────────────────────────

    def _process_new_order(
        self, order, partner, client_ref: str, filename: str, file_hash: str
    ):
        """Full new order pipeline: dedup → SO → lines → issues → routing."""
        existing_so = self._find_existing_so(client_ref)
        if existing_so:
            if existing_so.state == 'cancel':
                _logger.info(
                    'EDI: found cancelled SO %s for client_ref=%s — '
                    'creating new SO (re-order after cancellation)',
                    existing_so.name, client_ref,
                )
                # Fall through to create new SO
            else:
                # Valid existing SO — skip
                self.env["edi.log"].log(
                    partner, "inbound", "duplicate_skipped", "warning",
                    "Duplicate PO — SO %s already exists (state: %s)" % (
                        existing_so.name, existing_so.state),
                    filename=filename, file_hash=file_hash, sale_order=existing_so,
                )
                return

        delivery_partner, store_unknown = self._resolve_delivery_partner(partner, order)

        so_vals = {
            "partner_id": delivery_partner.id,
            "partner_invoice_id": partner.partner_id.id,
            "client_order_ref": client_ref,
            "commitment_date": (
                fields.Datetime.to_datetime(str(order.requested_delivery_date))
                if order.requested_delivery_date else False
            ),
            "edi_trading_partner_id": partner.id,
            "company_id": self.env.company.id,
        }
        # Carry the clean customer PO number onto the SO. Per-store SOs otherwise
        # only have the suffixed client_order_ref (e.g. "4500180080_1080"); the
        # full Briscoes PO must be visible per store for matching, packing slips
        # and downstream Enabling reports. Set both the standard field and the
        # legacy Studio field when present (guarded so other DBs don't break).
        _so_fields = self.env['sale.order']._fields
        if 'customer_po_number' in _so_fields:
            so_vals['customer_po_number'] = order.po_number
        if 'x_edi_ponumber' in _so_fields:
            so_vals['x_edi_ponumber'] = order.po_number
        # pricelist_id is only available when sale_management is installed (Odoo 17+)
        if (
            'pricelist_id' in self.env['sale.order']._fields
            and partner.pricelist_id
        ):
            so_vals['pricelist_id'] = partner.pricelist_id.id
        so = self.env["sale.order"].create(so_vals)

        blocking_issues = []

        # Create review before lines so issues can reference it
        review = self.env["edi.order.review"].create({
            "trading_partner_id": partner.id,
            "customer_po_number": order.po_number,
            "store_code": order.store_code,
            "sale_order_id": so.id,
            "edi_file_hash": file_hash,
            "edi_filename": filename,
            "edi_raw_data": order.raw_data,
            "document_type": "new_order",
        })
        so.edi_review_id = review.id

        # Unknown store (per_store mode, store child not found): the SO fell back
        # to the head-office/parent partner. Raise a BLOCKING unknown_store issue
        # so the order lands in review for an operator to fix the delivery address
        # instead of silently auto-confirming to the wrong (parent) address.
        if store_unknown:
            self.env["edi.order.issue"].create({
                "review_id": review.id,
                "issue_type": "unknown_store",
                "severity": "blocking",
                "description": (
                    "Store code '%s' not found as a child contact (ref) of %s. "
                    "Delivery defaulted to the parent partner — set the correct "
                    "store contact before confirming." % (
                        order.store_code, partner.partner_id.name,
                    )
                ),
            })
            blocking_issues.append({"type": "unknown_store"})

        for parsed_line in order.lines:
            line_blocking = self._process_order_line(parsed_line, so, partner, review)
            blocking_issues.extend(line_blocking)

        # Route
        if not blocking_issues and partner.auto_confirm_clean:
            so.action_confirm()
            review.write({"state": "auto_approved"})
            # ACK is sent once per PO after the whole file is processed
            # (see _process_file) — Briscoes expects a single per-PO ORDRSP.
            self.env["edi.log"].log(
                partner, "inbound", "order_created", "success",
                "Auto-approved: SO %s from %s" % (so.name, filename),
                filename=filename, sale_order=so, review=review,
            )
        else:
            review.write({"state": "pending_review"})
            self.env["edi.log"].log(
                partner, "inbound", "order_created",
                "warning" if blocking_issues else "success",
                "Pending review: SO %s — %d blocking issue(s)" % (
                    so.name, len(blocking_issues)),
                filename=filename, sale_order=so, review=review,
            )
            if partner.alert_on_issues and blocking_issues:
                self._send_review_alert(partner, review)

        # Emit billable event — fires whether auto-approved or pending review
        self.env['mml.event'].emit(
            'edi.order.processed',
            quantity=len(so.order_line),
            billable_unit='edi_order_line',
            res_model='sale.order',
            res_id=so.id,
            source_module='mml_edi',
            payload={'partner': partner.name, 'order_ref': so.name},
        )

    # ── Order line processing ─────────────────────────────────────────────

    def _process_order_line(
        self, parsed_line, so, partner, review
    ) -> list[dict]:
        """
        Process one ParsedOrderLine. Creates SO line and edi.order.issue records.
        Returns list of blocking issue dicts (empty = no blockers on this line).
        """
        blocking = []

        product, matched_by = self._find_product(parsed_line, partner)
        if not product:
            self.env["edi.order.issue"].create({
                "review_id": review.id,
                "issue_type": "product_not_found",
                "severity": "blocking",
                "description": "Product not found: code '%s' (%s) — %s" % (
                    parsed_line.product_code,
                    partner.product_match_field,
                    parsed_line.description,
                ),
                "edi_line_data": json.dumps({
                    "product_code": parsed_line.product_code,
                    "description": parsed_line.description,
                    "quantity": parsed_line.quantity,
                    "unit_price": parsed_line.unit_price,
                }),
            })
            blocking.append({"type": "product_not_found"})
            return blocking

        sol = self.env["sale.order.line"].create({
            "order_id": so.id,
            "product_id": product.id,
            "product_uom_qty": parsed_line.quantity,
            "price_unit": parsed_line.unit_price,
            "edi_line_number": parsed_line.line_number,
            "edi_price": parsed_line.unit_price,
            "edi_matched_by": matched_by,
        })

        # Create fallback warning issue if product was not found on primary strategy
        primary_field = partner.product_match_field
        if matched_by and matched_by != primary_field:
            self.env["edi.order.issue"].create({
                "review_id": review.id,
                "issue_type": "product_matched_by_fallback",
                "severity": "warning",
                "description": (
                    "Product matched by fallback '%s' — primary code '%s' not found "
                    "via '%s'. Consider adding the barcode/code to the product record." % (
                        matched_by, parsed_line.product_code, primary_field,
                    )
                ),
                "sale_order_line_id": sol.id,
            })

        # Stock check (warning, non-blocking)
        # warehouse_id is added by sale_stock; fall back to no warehouse context if absent
        wh_ctx = {}
        if 'warehouse_id' in self.env['sale.order']._fields and so.warehouse_id:
            wh_ctx = {'warehouse': so.warehouse_id.id}
        qty_available = product.with_context(**wh_ctx).qty_available
        if qty_available < parsed_line.quantity:
            shortfall = parsed_line.quantity - qty_available
            sol.edi_qty_shortfall = shortfall
            self.env["edi.order.issue"].create({
                "review_id": review.id,
                "issue_type": "qty_shortfall",
                "severity": "warning",
                "description": "%s — requested %.0f, available %.0f, shortfall %.0f" % (
                    product.name, parsed_line.quantity, qty_available, shortfall,
                ),
                "sale_order_line_id": sol.id,
            })

        # Price comparison (blocking if outside tolerance).
        # NOTE: EDI prices from Briscoes are ex-GST (trade/wholesale net prices).
        # GST-inclusive pricelists would cause a systematic ~15% discrepancy on
        # every line and are now rejected at write-time by
        # edi.trading.partner._validate_pricelist_gst (api.constrains). The
        # debug log below is retained as a belt-and-braces diagnostic for
        # operators reading server logs.
        system_price = self._get_pricelist_price(product, parsed_line.quantity, partner)
        if system_price is not None:
            _logger.debug(
                '[EDI] Price check for %s: EDI=%.4f system=%.4f '
                '(EDI prices are ex-GST; ensure pricelist is also ex-GST)',
                product.name, parsed_line.unit_price, system_price,
            )
            sol.edi_system_price = system_price
            tolerance = partner.price_tolerance_pct / 100.0
            if system_price > 0:
                diff_pct = abs(parsed_line.unit_price - system_price) / system_price
            else:
                diff_pct = 1.0 if parsed_line.unit_price != 0 else 0.0

            if diff_pct > tolerance:
                self.env["edi.order.issue"].create({
                    "review_id": review.id,
                    "issue_type": "price_discrepancy",
                    "severity": "blocking",
                    "description": (
                        "Price discrepancy on %s (code: %s): "
                        "EDI=%.4f, System=%.4f, Diff=%.2f%%" % (
                            product.name, parsed_line.product_code,
                            parsed_line.unit_price, system_price, diff_pct * 100,
                        )
                    ),
                    "edi_price": parsed_line.unit_price,
                    "system_price": system_price,
                    "sale_order_line_id": sol.id,
                })
                blocking.append({"type": "price_discrepancy"})

        return blocking

    # ── Change order flow ─────────────────────────────────────────────────

    def _process_change_order(
        self, order, partner, client_ref: str, filename: str, file_hash: str
    ):
        """Route a change order to pending review with a diff summary."""
        existing_so = self._find_existing_so(client_ref)
        if not existing_so:
            self.env["edi.log"].log(
                partner, "inbound", "error", "warning",
                "Change order for PO '%s' but no matching SO found (ref: %s)" % (
                    order.po_number, client_ref),
                filename=filename,
            )
            return

        original_review = self.env["edi.order.review"].search([
            ("sale_order_id", "=", existing_so.id),
            ("document_type", "=", "new_order"),
        ], limit=1)

        change_summary = self._compute_change_summary(existing_so, order)

        review = self.env["edi.order.review"].create({
            "trading_partner_id": partner.id,
            "customer_po_number": order.po_number,
            "store_code": order.store_code,
            "sale_order_id": existing_so.id,
            "edi_file_hash": file_hash,
            "edi_filename": filename,
            "edi_raw_data": order.raw_data,
            "document_type": "change_order",
            "original_review_id": original_review.id if original_review else False,
            "change_summary": change_summary,
            "state": "pending_review",
        })

        # Store pending changes as a timestamped JSON attachment for apply_change_order().
        # Each change order gets its own attachment so history is preserved when
        # multiple change orders arrive before any is approved.
        ts = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        self.env["ir.attachment"].create({
            "name": f"pending_changes_{ts}.json",
            "res_model": "edi.order.review",
            "res_id": review.id,
            "datas": self._encode_pending_changes(order, existing_so),
            "mimetype": "application/json",
            "description": f"Change order received {ts}",
        })

        self.env["edi.log"].log(
            partner, "inbound", "order_created", "warning",
            "Change order routed to review: SO %s — %s" % (
                existing_so.name, change_summary),
            filename=filename, sale_order=existing_so, review=review,
        )

        if partner.alert_on_issues:
            self._send_review_alert(partner, review)

    @api.model
    def apply_change_order(self, review):
        """
        Apply a change order's pending changes to its linked SO.
        Called by edi.order.review.action_approve() for change_order type.
        """
        if review.document_type != "change_order":
            raise UserError(_("apply_change_order called on non-change-order review"))

        so = review.sale_order_id
        if not so:
            raise UserError(_("No SO linked to this change order review"))

        attachment = self.env["ir.attachment"].search([
            ("res_model", "=", "edi.order.review"),
            ("res_id", "=", review.id),
            ("name", "like", "pending_changes_"),
        ], order="create_date desc", limit=1)

        if not attachment:
            _logger.warning(
                "[EDI] No pending_changes.json found for review %s", review.name
            )
            return

        changes = json.loads(base64.b64decode(attachment.datas).decode())

        if changes.get("new_delivery_date"):
            new_date = date.fromisoformat(changes["new_delivery_date"])
            so.commitment_date = fields.Datetime.to_datetime(str(new_date))

        for line_change in changes.get("line_changes", []):
            so_line = self.env["sale.order.line"].search([
                ("order_id", "=", so.id),
                ("edi_line_number", "=", line_change["line_number"]),
            ], limit=1)
            if so_line:
                so_line.product_uom_qty = line_change["new_qty"]

        # Handle removed lines (ORDCHG action code 3)
        for removed_line_num in changes.get('removed_lines', []):
            so_line = self.env['sale.order.line'].search([
                ('order_id', '=', so.id),
                ('edi_line_number', '=', removed_line_num),
            ], limit=1)
            if so_line:
                if so.state == 'draft':
                    so_line.unlink()
                else:
                    so_line.write({'product_uom_qty': 0})
                    _logger.warning(
                        'EDI ORDCHG: SO %s is confirmed — zeroed qty on line %s '
                        '(action code 3). Manual review required.',
                        so.name, removed_line_num,
                    )

        so.message_post(
            body=_("EDI change order approved: %s") % review.change_summary,
            subtype_xmlid="mail.mt_note",
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    def _is_file_duplicate(self, file_hash: str, partner) -> bool:
        """Check if this file hash was already successfully processed."""
        return bool(self.env["edi.log"].search([
            ("trading_partner_id", "=", partner.id),
            ("file_hash", "=", file_hash),
            ("event_type", "=", "file_download"),
            ("status", "=", "success"),
        ], limit=1))

    def _find_existing_so(self, client_ref: str):
        """Find an existing SO by client reference. Returns record or None."""
        return self.env["sale.order"].search([
            ("client_order_ref", "=", client_ref),
        ], limit=1) or None

    def _resolve_delivery_partner(self, partner, order):
        """
        Resolve delivery partner.
        Per-store: look up child contact by ref=store_code.
        Single: use trading_partner.partner_id directly.

        Returns (partner_record, store_unknown): store_unknown is True only when
        per_store mode was requested with a store_code that did not resolve to a
        child contact — the caller then raises a blocking unknown_store issue so
        the order is reviewed instead of auto-confirmed to the parent address.
        """
        if partner.order_split_mode == "per_store" and order.store_code:
            store_partner = self.env["res.partner"].search([
                ("parent_id", "=", partner.partner_id.id),
                ("ref", "=", order.store_code),
            ], limit=1)
            if store_partner:
                return store_partner, False
            _logger.warning(
                "[EDI] Store code '%s' not found as child of partner %s",
                order.store_code, partner.partner_id.name,
            )
            return partner.partner_id, True
        return partner.partner_id, False

    def _find_product(self, parsed_line, partner):
        """
        Look up product using cascade strategy:
        1. Try partner.product_match_field with parsed_line.product_code (primary)
        2. If miss: try barcode with product_code (if not already barcode mode)
        3. If miss: try default_code with parsed_line.vendor_code
        4. If miss: try product.supplierinfo.product_code with parsed_line.buyer_article_no

        Returns: (product_record | None, matched_by: str | None)
        matched_by is the strategy name that succeeded, or None if not found.
        """
        strategies = []

        # Strategy 1: configured primary field
        primary_field = partner.product_match_field
        strategies.append((primary_field, parsed_line.product_code))

        # Strategy 2: barcode fallback (if primary wasn't already barcode)
        if primary_field != "barcode":
            strategies.append(("barcode", parsed_line.product_code))

        # Strategy 3: internal reference via vendor_code
        if parsed_line.vendor_code:
            strategies.append(("default_code", parsed_line.vendor_code))

        # Strategy 4: supplier code via buyer_article_no
        if parsed_line.buyer_article_no:
            strategies.append(("supplier_sku", parsed_line.buyer_article_no))

        for strategy, code in strategies:
            if not code:
                continue
            product = self._lookup_by_strategy(strategy, code)
            if product:
                return product, strategy

        return None, None

    def _lookup_by_strategy(self, strategy: str, code: str):
        """Single-strategy product lookup. Returns product.product record or None."""
        if strategy == "barcode":
            return self.env["product.product"].search(
                [("barcode", "=", code)], limit=1
            ) or None
        elif strategy == "default_code":
            # Case-insensitive exact match: retail EDI codes arrive in mixed case
            # (e.g. iDOC 'HESPG' vs Odoo 'hespg'); the legacy .NET handler matched
            # case-insensitively. '=ilike' with wildcards escaped = exact,
            # case-insensitive (so a code with '_'/'%' can't match the wrong product).
            return self.env["product.product"].search(
                [("default_code", "=ilike", _escape_ilike(code))], limit=1
            ) or None
        elif strategy == "supplier_sku":
            info = self.env["product.supplierinfo"].search(
                [("product_code", "=ilike", _escape_ilike(code))], limit=1
            )
            if not info:
                return None
            return info.product_id or info.product_tmpl_id.product_variant_ids[:1] or None
        return None

    def _get_pricelist_price(
        self, product, quantity: float, partner
    ) -> float | None:
        """Get pricelist price. Returns None if no pricelist configured.

        Odoo 17+: _get_product_price(product, quantity) — partner arg was removed.
        Falls back to product.list_price on any exception.
        """
        if not partner.pricelist_id:
            return None
        try:
            return partner.pricelist_id._get_product_price(product, quantity)
        except Exception:
            # Further fallback: try with the old 3-arg signature for older Odoo versions
            try:
                return partner.pricelist_id._get_product_price(
                    product, quantity, partner.partner_id
                )
            except Exception as exc:
                # A pricelist IS configured but the lookup failed. Fall back to
                # the product's list price (as the docstring promises) rather than
                # returning None — None would make the caller SKIP the price-sanity
                # check entirely, defeating the purpose of having one.
                _logger.warning(
                    "[EDI] Pricelist price lookup failed for %s: %s — "
                    "falling back to list_price for the price check",
                    product.name, exc,
                )
                return product.list_price

    def _compute_change_summary(self, existing_so, order) -> str:
        """Generate human-readable summary of what changed."""
        parts = []
        if order.requested_delivery_date:
            current = existing_so.commitment_date
            current_date = current.date() if current else None
            if current_date != order.requested_delivery_date:
                parts.append("Delivery: %s → %s" % (
                    current_date, order.requested_delivery_date))

        existing_qtys = {
            line.edi_line_number: line.product_uom_qty
            for line in existing_so.order_line
        }
        for parsed_line in order.lines:
            existing_qty = existing_qtys.get(parsed_line.line_number)
            if existing_qty is None:
                parts.append("New line %d: %s ×%.0f" % (
                    parsed_line.line_number, parsed_line.description,
                    parsed_line.quantity))
            elif existing_qty != parsed_line.quantity:
                parts.append("Line %d qty: %.0f → %.0f" % (
                    parsed_line.line_number, existing_qty, parsed_line.quantity))

        removed = set(existing_qtys.keys()) - {l.line_number for l in order.lines}
        for ln in removed:
            parts.append("Line %d removed" % ln)

        return "; ".join(parts) if parts else "No changes detected"

    def _encode_pending_changes(self, order, existing_so) -> str:
        """Encode change order data as base64 JSON for ir.attachment.datas."""
        existing_line_nums = {
            line.edi_line_number for line in existing_so.order_line
        }
        incoming_line_nums = {l.line_number for l in order.lines}
        removed_line_nums = sorted(existing_line_nums - incoming_line_nums)

        changes = {
            "new_delivery_date": (
                order.requested_delivery_date.isoformat()
                if order.requested_delivery_date else None
            ),
            "line_changes": [
                {"line_number": l.line_number, "new_qty": l.quantity}
                for l in order.lines
            ],
            "removed_lines": removed_line_nums,
        }
        return base64.b64encode(json.dumps(changes).encode()).decode()

    _ALERT_COOLDOWN_SECONDS = 3600  # 1 alert per hour per module

    def _send_cron_alert(self, module_name: str, subject: str, body: str) -> None:
        """Send an email alert when a scheduled action fails.

        Rate-limited to one alert per hour per module to prevent alert storms.
        Timestamp stored in ir.config_parameter under mml_edi.last_alert.<module>.
        """
        from datetime import datetime, timezone

        alert_email = self.env['ir.config_parameter'].sudo().get_param(
            'mml.cron_alert_email', False
        )
        if not alert_email:
            return

        # Rate limiting: suppress if an alert was sent within the cooldown window
        param_key = 'mml_edi.last_alert.%s' % module_name
        ICP = self.env['ir.config_parameter'].sudo()
        last_alert_str = ICP.get_param(param_key, '')
        if last_alert_str:
            try:
                last_alert = datetime.fromisoformat(last_alert_str)
                if last_alert.tzinfo is None:
                    last_alert = last_alert.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - last_alert).total_seconds()
                if elapsed < self._ALERT_COOLDOWN_SECONDS:
                    _logger.debug(
                        'EDI alert suppressed for %s (%.0fs ago, cooldown %ds)',
                        module_name, elapsed, self._ALERT_COOLDOWN_SECONDS,
                    )
                    return
            except (ValueError, TypeError):
                pass  # Malformed stored value — send the alert

        try:
            self.env['mail.mail'].sudo().create({
                'subject': '[MML ALERT] %s: %s' % (module_name, subject),
                'body_html': '<pre>%s</pre>' % html.escape(body),
                'email_to': alert_email,
            }).send()
            # Record timestamp only after a successful send
            ICP.set_param(param_key, datetime.now(timezone.utc).isoformat())
        except Exception:
            _logger.exception('Failed to send cron alert email for %s', module_name)

    def _send_review_alert(self, partner, review):
        """Send alert email to configured recipients.

        The mail template's email_to is intentionally blank — recipients come
        from the trading partner's Alert Email Recipients, injected here so a
        partner can be re-pointed without editing the template.
        """
        recipients = partner.alert_email_ids.filtered('email')
        if not recipients:
            return
        try:
            template = self.env.ref(
                "mml_edi.mail_template_edi_review_alert",
                raise_if_not_found=False,
            )
            if template:
                template.send_mail(
                    review.id, force_send=True,
                    email_values={'email_to': ','.join(recipients.mapped('email'))},
                )
        except Exception as exc:
            _logger.warning("[EDI] Failed to send review alert: %s", exc)
