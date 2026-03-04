# mml.edi/models/edi_processor.py
"""
Customer-agnostic EDI processing engine.

Entry point for cron: edi.processor.run_scheduled_poll()
Entry point for manual: trading_partner.action_run_poll_now() → poll_trading_partner()
Public API for tests: process_parsed_order(), apply_change_order()

AbstractModel — no database table.
"""
from __future__ import annotations
import base64
import hashlib
import json
import logging
from datetime import date

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class EDIProcessor(models.AbstractModel):
    _name = "edi.processor"
    _description = "EDI Processing Engine"

    # ── Cron entry point ──────────────────────────────────────────────────

    @api.model
    def run_scheduled_poll(self):
        """Called by ir.cron. Polls all active trading partners."""
        partners = self.env["edi.trading.partner"].search([("active", "=", True)])
        _logger.info("[EDI] Scheduled poll: %d active partner(s)", len(partners))
        for partner in partners:
            try:
                self.poll_trading_partner(partner)
            except Exception as exc:
                _logger.exception("[EDI] Poll failed for partner %s", partner.code)
                self.env["edi.log"].log(
                    partner, "inbound", "error", "error",
                    "Scheduled poll failed: %s" % str(exc),
                    detail=str(exc),
                )

    # ── Per-partner poll ──────────────────────────────────────────────────

    @api.model
    def poll_trading_partner(self, partner):
        """Download and process all files from a trading partner's FTP inbox."""
        from .edi_ftp import EDIFTPHandler
        from ..parsers.base_parser import EDIFTPError

        _logger.info("[EDI] Polling %s", partner.code)
        handler = EDIFTPHandler(partner)

        try:
            with handler.connection():
                files = handler.list_files()
                _logger.info("[EDI] %s: %d file(s) in inbox", partner.code, len(files))

                for filename in files:
                    try:
                        content = handler.download_file(filename)
                        file_hash = hashlib.sha256(content).hexdigest()

                        self.env["edi.log"].log(
                            partner, "inbound", "file_download", "success",
                            "Downloaded: %s (%d bytes)" % (filename, len(content)),
                            filename=filename, file_hash=file_hash,
                        )

                        with self.env.cr.savepoint():
                            self._process_file(content, file_hash, filename, partner)

                        handler.move_to_processed(filename)

                    except Exception as exc:
                        _logger.exception(
                            "[EDI] Error processing file %s for %s", filename, partner.code
                        )
                        self.env["edi.log"].log(
                            partner, "inbound", "error", "error",
                            "Error processing %s: %s" % (filename, str(exc)),
                            filename=filename, detail=str(exc),
                        )

        except EDIFTPError as exc:
            self.env["edi.log"].log(
                partner, "inbound", "ftp_connection", "error",
                "FTP connection failed: %s" % str(exc),
                detail=str(exc),
            )
            raise

    # ── File processing ───────────────────────────────────────────────────

    def _process_file(self, content: bytes, file_hash: str, filename: str, partner):
        """Parse a downloaded file and dispatch each ParsedOrder."""
        if self._is_file_duplicate(file_hash, partner):
            self.env["edi.log"].log(
                partner, "inbound", "duplicate_skipped", "warning",
                "Duplicate file skipped (hash already processed): %s" % filename,
                filename=filename, file_hash=file_hash,
            )
            return

        parser = partner.get_parser_instance()
        raw_text = content.decode("utf-8", errors="replace")
        parsed_orders = parser.parse_file(content, partner)

        self.env["edi.log"].log(
            partner, "inbound", "file_parse", "success",
            "Parsed %d order(s) from %s" % (len(parsed_orders), filename),
            filename=filename, file_hash=file_hash,
        )

        for order in parsed_orders:
            order.raw_data = raw_text
            with self.env.cr.savepoint():
                self.process_parsed_order(order, partner, filename, file_hash)

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
        if existing_so and existing_so.state in ("draft", "sent", "sale", "done"):
            self.env["edi.log"].log(
                partner, "inbound", "duplicate_skipped", "warning",
                "Duplicate PO — SO %s already exists (state: %s)" % (
                    existing_so.name, existing_so.state),
                filename=filename, file_hash=file_hash, sale_order=existing_so,
            )
            return

        delivery_partner = self._resolve_delivery_partner(partner, order)

        so = self.env["sale.order"].create({
            "partner_id": delivery_partner.id,
            "partner_invoice_id": partner.partner_id.id,
            "pricelist_id": partner.pricelist_id.id if partner.pricelist_id else False,
            "client_order_ref": client_ref,
            "commitment_date": (
                fields.Datetime.to_datetime(str(order.requested_delivery_date))
                if order.requested_delivery_date else False
            ),
            "edi_trading_partner_id": partner.id,
            "company_id": self.env.company.id,
        })

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

        for parsed_line in order.lines:
            line_blocking = self._process_order_line(parsed_line, so, partner, review)
            blocking_issues.extend(line_blocking)

        # Route
        if not blocking_issues and partner.auto_confirm_clean:
            so.action_confirm()
            review.write({"state": "auto_approved"})
            review._queue_ack()
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
        qty_available = product.with_context(
            warehouse=so.warehouse_id.id
        ).qty_available
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

        # Price comparison (blocking if outside tolerance)
        system_price = self._get_pricelist_price(product, parsed_line.quantity, partner)
        if system_price is not None:
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

        # Store pending changes as a JSON attachment for apply_change_order()
        self.env["ir.attachment"].create({
            "name": "pending_changes.json",
            "res_model": "edi.order.review",
            "res_id": review.id,
            "datas": self._encode_pending_changes(order, existing_so),
            "mimetype": "application/json",
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
            ("name", "=", "pending_changes.json"),
        ], limit=1)

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
        """
        if partner.order_split_mode == "per_store" and order.store_code:
            store_partner = self.env["res.partner"].search([
                ("parent_id", "=", partner.partner_id.id),
                ("ref", "=", order.store_code),
            ], limit=1)
            if store_partner:
                return store_partner
            _logger.warning(
                "[EDI] Store code '%s' not found as child of partner %s",
                order.store_code, partner.partner_id.name,
            )
        return partner.partner_id

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
            return self.env["product.product"].search(
                [("default_code", "=", code)], limit=1
            ) or None
        elif strategy == "supplier_sku":
            info = self.env["product.supplierinfo"].search(
                [("product_code", "=", code)], limit=1
            )
            if not info:
                return None
            return info.product_id or info.product_tmpl_id.product_variant_ids[:1] or None
        return None

    def _get_pricelist_price(
        self, product, quantity: float, partner
    ) -> float | None:
        """
        Get pricelist price using Odoo 15 API.

        Odoo 15: pricelist.price_get(prod_id, qty, partner_id) → {pricelist_id: price}
        Returns None if no pricelist configured.
        """
        if not partner.pricelist_id:
            return None
        try:
            pricelist = partner.pricelist_id
            price_dict = pricelist.price_get(product.id, quantity, partner.partner_id.id)
            return price_dict.get(pricelist.id)
        except Exception as exc:
            _logger.warning(
                "[EDI] Pricelist price lookup failed for %s: %s", product.name, exc
            )
            return None

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
        changes = {
            "new_delivery_date": (
                order.requested_delivery_date.isoformat()
                if order.requested_delivery_date else None
            ),
            "line_changes": [
                {"line_number": l.line_number, "new_qty": l.quantity}
                for l in order.lines
            ],
        }
        return base64.b64encode(json.dumps(changes).encode()).decode()

    def _send_review_alert(self, partner, review):
        """Send alert email to configured recipients."""
        if not partner.alert_email_ids:
            return
        try:
            template = self.env.ref(
                "mml_edi.mail_template_edi_review_alert",
                raise_if_not_found=False,
            )
            if template:
                template.send_mail(review.id, force_send=True)
        except Exception as exc:
            _logger.warning("[EDI] Failed to send review alert: %s", exc)
