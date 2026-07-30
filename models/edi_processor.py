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
from datetime import date, datetime, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


def build_session_id() -> str:
    """Generate a short unique ID for correlating log messages within one poll run."""
    return secrets.token_hex(4)


# Default lead (days) beyond which an EDI delivery date marks the PO as an
# INDENT — a forward commitment placed months before the stock arrives.
# Overridable via ir.config_parameter 'mml_edi.indent_threshold_days'.
INDENT_THRESHOLD_DAYS = 28


def _is_indent_commitment(commitment, now, threshold_days=INDENT_THRESHOLD_DAYS):
    """True when a requested delivery date is far enough out to be an indent.

    Pure so the threshold maths is unit-testable: an order whose committed
    delivery sits more than ``threshold_days`` past ``now`` is a forward
    commitment, not a fulfil-now order.
    """
    if not commitment:
        return False
    return commitment > now + timedelta(days=threshold_days)


def _short_ship_split(ordered, available):
    """Split an ordered qty into ``(ship_qty, shortfall)`` given DC-available qty.

    Policy: never backorder. Ship what the fulfilment DC holds, acknowledge the
    rest as a shortfall in the ORDRSP. A negative availability (over-reservation)
    ships nothing rather than a negative qty.
    """
    ship = min(ordered, max(available, 0.0))
    return ship, ordered - ship


def _reclamp_line(ordered, available, current_qty, cap_to_current=False):
    """Pure re-clamp decision for one SO line at approve/ORDCHG time (SS-1/SS-5).

    Same math as the parse-time gate (_short_ship_split): clamp to
    min(ordered, available) with a floor of 0. Returns
    ``(new_qty, shortfall, changed)`` where ``changed`` is True when the line's
    current qty differs from the re-clamped qty (float-tolerant compare).

    ``cap_to_current=True`` (the Approve-with-Corrections path): the operator's
    current qty is a deliberate human decision — the re-clamp may only move
    the line DOWN from it (stock vanished), never restore it back up toward
    the customer's ordered qty. Basis becomes min(ordered, current_qty); the
    reported shortfall stays relative to the customer's ordered qty so the
    ORDRSP still tells the partner the truth about the original request.
    """
    basis = min(ordered, current_qty or 0.0) if cap_to_current else ordered
    new_qty, _base_short = _short_ship_split(basis, available)
    shortfall = max(0.0, (ordered or 0.0) - new_qty)
    changed = abs(new_qty - (current_qty or 0.0)) > 1e-6
    return new_qty, shortfall, changed


# Fixed class-id for the per-partner poll advisory lock (IDEM-1a). Both halves
# of pg_advisory_xact_lock(class, id) are signed int4; this constant must stay
# STABLE across releases — changing it would let two code versions poll the
# same partner concurrently. 0x45444950 == ASCII 'EDIP' (EDI Poll).
EDI_POLL_LOCK_CLASS = 0x45444950

# DB backstop unique index rejecting duplicate live EDI SOs (IDEM-1b). Created
# by sale.order.init() and migrations/19.0.1.0.3; referenced here to convert a
# losing create-race into the standard duplicate_skipped path.
SO_EDI_CLIENT_REF_INDEX = "sale_order_edi_client_ref_uniq"


def _is_duplicate_so_violation(exc) -> bool:
    """True when ``exc`` is the unique violation raised by the EDI client-ref
    backstop index — i.e. a concurrent transaction created (and committed) the
    same EDI SO first. Matched on the index name so no psycopg2 import is
    needed at module level (keeps the pure-pytest import path clean)."""
    return SO_EDI_CLIENT_REF_INDEX in str(exc)


def _commit_suppressed(env) -> bool:
    """True when the enclosing transaction belongs to a test harness and must
    never be really committed.

    Odoo 19 REMOVED Registry.in_test_mode() — calling it on a live registry
    raises AttributeError (caught by CI: it silently killed every ACK send).
    The registry hook is still consulted first so environments (and the pure
    test fakes) that provide one keep working; otherwise the ``--test-enable``
    config flag is the reliable signal. Undeterminable -> suppress (a
    maybe-test cursor must never be really committed; production always has a
    readable config, so durability there is unaffected)."""
    checker = getattr(getattr(env, "registry", None), "in_test_mode", None)
    if callable(checker):
        return bool(checker())
    try:
        from odoo.tools import config
        return bool(config.get("test_enable"))
    except Exception:
        return True


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
                failed_files = self.poll_trading_partner(partner)
            except Exception as exc:
                _logger.exception("[EDI] Poll failed for partner %s", partner.code)
                self._record_poll_failure(partner)
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
                continue
            if failed_files:
                # OPS-25: file-level failures COUNT TOWARD the breaker — never
                # reset it. Previously the poll "succeeded" (no exception), the
                # count was zeroed, and a poisoned file re-failed silently every
                # cycle forever. The rate-limited per-file alert is sent inside
                # poll_trading_partner.
                _logger.warning(
                    "[EDI] Poll for %s completed with %d failed file(s) — "
                    "counting toward the circuit breaker",
                    partner.code, failed_files,
                )
                self._record_poll_failure(partner)
            else:
                partner.write({"circuit_failure_count": 0, "circuit_open_since": False})

    def _record_poll_failure(self, partner):
        """Increment the partner's circuit-breaker failure count and open the
        circuit when the threshold is reached. Shared by the poll-exception
        path and the per-file failure path (OPS-25)."""
        new_count = partner.circuit_failure_count + 1
        vals = {"circuit_failure_count": new_count}
        if new_count >= partner.circuit_failure_threshold and not partner.circuit_open_since:
            vals["circuit_open_since"] = fields.Datetime.now()
            _logger.error(
                "[EDI] Circuit breaker TRIPPED for %s after %d consecutive failures",
                partner.code, new_count,
            )
        partner.write(vals)

    # ── Deferred-ACK retry safety net ─────────────────────────────────────

    @api.model
    def retry_pending_acks(self):
        """Re-queue ORDRSP/ACKs that should have been sent but weren't.

        A deferred ACK upload can fail (FTP down, transient error) and is logged
        ack_sent/error but never retried — Kestrelby then never gets a response.
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
            # C5: a cancellation review is a terminal, no-ACK record by design
            # — it must NEVER be offered to _queue_ack (Nimbrel gets a CONTRL
            # only). Checked first so a cancellation can never enter the
            # per-exchange dedup set and (via seen_exchanges) accidentally
            # suppress a legitimate sibling exchange's retry.
            if self._is_cancellation_review(review):
                continue

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

            # Skip if an ACK for this exact exchange already uploaded
            # successfully. The shared helper includes the ACK attempt
            # counter (IDEM-4): after a reset-after-sent the exchange moves
            # to a '_aN' filename, so the OLD attempt's success row must not
            # suppress the corrected re-send.
            filename = review._ack_exchange_filename()
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
        """Download and process all files from a trading partner's FTP inbox.

        Returns the number of files that FAILED this pass (0 = clean poll);
        run_scheduled_poll counts a non-zero return toward the circuit breaker
        so a poisoned file cannot re-fail silently forever (OPS-25).

        Concurrency (IDEM-1a): a per-partner Postgres advisory lock serializes
        concurrent polls of the SAME partner (cron tick vs 'Run Poll Now' vs
        two HTTP workers). The loser blocks until the winner's transaction
        ends and then sees its committed dedup rows. The partial unique index
        on EDI SOs (IDEM-1b, see sale_order.init) is the DB backstop for
        anything that still slips through.

        ORDERING INVARIANT per file (IDEM-2):
            process -> write dedup marker -> COMMIT -> FTP rename -> ACK/OOS.
        See the inline comment at the marker write.
        """
        from .edi_ftp import get_transport_handler
        from ..parsers.base_parser import EDIFTPError

        # Per-poll memo for the reservation-method config warning (SS-3):
        # a mutable set in context so the sanity check warns at most once
        # per poll per picking type.
        self = self.with_context(edi_reservation_warned=set())

        # IDEM-1a: serialize concurrent polls of this partner. Transaction-
        # scoped, so it is released automatically on commit/rollback — and
        # therefore RE-ACQUIRED after every per-file commit (_poll_commit).
        self._acquire_poll_lock(partner)

        sid = build_session_id()
        prefix = "[EDI:%s]" % sid
        _logger.info("%s Polling %s", prefix, partner.code)
        handler = get_transport_handler(partner)
        failed_files = 0

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
                            # Persist the stores that DID succeed (and their
                            # logs) — without a commit a worker death would roll
                            # them back, and the retry pass relies on their
                            # committed SO-ref dedup rows.
                            self._poll_commit(partner)
                            # OPS-25: a file re-failing every cycle must not stay
                            # silent (rate-limited).
                            self._alert_file_failure(
                                partner, filename,
                                "%d store-order(s) failed: %s" % (
                                    len(failures),
                                    "; ".join("%s: %s" % f for f in failures)),
                            )
                            failed_files += 1
                            continue

                        # ORDERING INVARIANT (IDEM-2) — do NOT reorder:
                        #   1. dedup marker (file_download/success) written
                        #   2. COMMIT — SOs/reviews/logs/marker become durable
                        #   3. FTP rename to '.processed'
                        #   4. ACK / OOS summary
                        # The DB must be durable BEFORE the rename: a worker
                        # death after the rename but before a commit would
                        # archive the file while rolling back its SOs — the PO
                        # silently lost forever. A death between 2 and 3 leaves
                        # the file in the inbox; the next poll dup-skips it by
                        # hash and just re-renames (existing self-heal). The
                        # ACK goes LAST so the partner can never hold an ORDRSP
                        # for uncommitted state; a death before 4 is covered by
                        # the retry_pending_acks cron.
                        self.env["edi.log"].log(
                            partner, "inbound", "file_download", "success",
                            "Downloaded: %s (%d bytes)" % (filename, len(content)),
                            filename=filename, file_hash=file_hash,
                        )

                        self._poll_commit(partner)

                        handler.move_to_processed(filename)

                        self._send_file_responses(partner, file_hash)

                    except Exception as exc:
                        _logger.exception(
                            "%s Error processing file %s for %s", prefix, filename, partner.code
                        )
                        self.env["edi.log"].log(
                            partner, "inbound", "error", "error",
                            "Error processing %s: %s" % (filename, str(exc)),
                            filename=filename, detail=str(exc),
                        )
                        self._alert_file_failure(partner, filename, str(exc))
                        failed_files += 1

        except EDIFTPError as exc:
            _logger.error("%s FTP connection failed for %s: %s", prefix, partner.code, exc)
            self.env["edi.log"].log(
                partner, "inbound", "ftp_connection", "error",
                "FTP connection failed: %s" % str(exc),
                detail=str(exc),
            )
            raise

        return failed_files

    def _acquire_poll_lock(self, partner):
        """Per-partner transaction-scoped advisory lock (IDEM-1a). Blocks until
        any concurrent poll of the same partner commits or rolls back."""
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (EDI_POLL_LOCK_CLASS, partner.id),
        )

    def _poll_commit(self, partner):
        """Durability point in the poll loop (IDEM-2).

        Commits the transaction so SOs/reviews/logs/dedup markers survive a
        worker death, then immediately re-takes the per-partner advisory lock
        (pg_advisory_xact_lock is transaction-scoped — the commit released it).
        Guarded so the test runner's enclosing transaction is never committed;
        under tests the lock also survives (nothing was committed).
        """
        if _commit_suppressed(self.env):
            return
        self.env.cr.commit()
        self._acquire_poll_lock(partner)

    def _alert_file_failure(self, partner, filename, detail):
        """Rate-limited email for a per-file processing failure (OPS-25).

        Without this, a poisoned file re-fails silently every poll: the poll
        itself 'succeeds', so neither the poll-level alert nor the breaker
        ever fires. Uses its own rate-limit bucket (mml_edi.file_failure) so
        it cannot suppress — or be suppressed by — the poll-failure alert.
        """
        self._send_cron_alert(
            'mml_edi.file_failure',
            'EDI file failed for %s: %s' % (partner.code, filename),
            detail,
        )

    def _send_file_responses(self, partner, file_hash):
        """Queue the per-PO ORDRSP + OOS summary for every PO of a file that
        just fully processed, then the mandatory per-interchange CONTRL.

        Runs AFTER the per-file commit + FTP rename (ordering invariant — see
        poll_trading_partner), so the partner can never receive an ACK for
        uncommitted state. Covers every PO in the file (not just the first);
        _queue_ack itself defers while sibling store-reviews are pending and
        is idempotent per exchange, and any miss here (e.g. worker death) is
        picked up by the retry_pending_acks cron.

        CONTRL ordering (documented per the C4 contract): emitted AFTER the
        ORDRSP queueing loop, independent of whether any ORDRSP actually sent
        (queue_ack legitimately defers while sibling stores are pending) —
        CONTRL acknowledges RECEIPT of the interchange at the syntax level
        and per the MIG is not contingent on business-level acceptance, so it
        must not wait on ORDRSP completion. C5: cancellation reviews are
        skipped by _queue_ack's caller here exactly like everywhere else
        (they hold state='rejected' with the CANCELLATION_MARKER and
        _queue_ack is simply never reached for them via this path either,
        since process_parsed_order never returns to a queueable state for a
        cancellation) — see _is_cancellation_review.
        """
        reviews = self.env["edi.order.review"].search([
            ("trading_partner_id", "=", partner.id),
            ("edi_file_hash", "=", file_hash),
        ], order="id")
        seen_pos = []
        for review in reviews:
            if self._is_cancellation_review(review):
                # C5: cancellations are CONTRL-only — never queue an ORDRSP,
                # and never let the ACK retry cron pick one up either (see
                # the matching guard in retry_pending_acks).
                continue
            po_number = review.customer_po_number
            if po_number in seen_pos:
                continue
            seen_pos.append(po_number)
            latest = reviews.filtered(
                lambda r, po=po_number: r.customer_po_number == po
            )[-1]
            latest._queue_ack()
            # Stock-shortfall (OOS) heads-up: one summary email per PO. OOS
            # lines do not block — this is informational, not a review request.
            try:
                self._send_oos_summary(partner, po_number)
            except Exception:
                _logger.warning(
                    "[EDI] OOS summary email failed for PO %s", po_number,
                    exc_info=True,
                )

        # AN-02: every inbound ORDERS interchange gets exactly one supplier
        # CONTRL, mandatory regardless of business outcome (accept/reject/
        # cancel) — emitted once per file/interchange, not per PO. hasattr-
        # guarded so partners whose parser has no CONTRL support (Kestrelby)
        # are a strict no-op; must never raise into the poll loop.
        try:
            self._emit_inbound_contrl(partner, file_hash)
        except Exception:
            _logger.warning(
                "[EDI] CONTRL emission failed for file_hash=%s (%s)",
                file_hash, partner.code, exc_info=True,
            )

    def _emit_inbound_contrl(self, partner, file_hash):
        """Emit + upload the mandatory CONTRL functional acknowledgement for
        one just-processed inbound interchange (C4 / AN-02).

        No-op (hasattr-guarded) for any parser without generate_contrl (e.g.
        Kestrelby) — preserves that partner's behaviour byte-for-byte. Failure
        here is logged (rate-limited alert) and NEVER raised into the poll
        loop: a missing/failed CONTRL must not turn a successfully processed
        ORDERS file into a failed poll (that would re-download and re-process
        it, defeating the file-hash dedup that already ran).
        """
        parser = partner.get_parser_instance()
        generate_contrl = getattr(parser, "generate_contrl", None)
        if not callable(generate_contrl):
            return

        review = self.env["edi.order.review"].search([
            ("trading_partner_id", "=", partner.id),
            ("edi_file_hash", "=", file_hash),
        ], order="id", limit=1)
        if not review:
            return
        raw_text = review.edi_raw_data
        if not raw_text:
            return

        filename = "CONTRL_%s_%s.edi" % (partner.code, file_hash[:12])
        Log = self.env["edi.log"]
        if Log.search_count([
            ("trading_partner_id", "=", partner.id),
            ("event_type", "=", "contrl_sent"),
            ("status", "=", "success"),
            ("filename", "=", filename),
        ]):
            # Already sent for this exact interchange (e.g. a retry re-running
            # _send_file_responses after a partial earlier failure).
            return

        try:
            contrl_bytes = generate_contrl(raw_text, partner)
        except Exception as exc:
            Log.log(
                partner, "outbound", "contrl_sent", "error",
                "CONTRL generation failed: %s" % str(exc),
                filename=filename, detail=str(exc),
            )
            self._send_cron_alert(
                'mml_edi.contrl_failure',
                'CONTRL generation failed for %s' % partner.code,
                str(exc),
            )
            return

        from .edi_ftp import get_transport_handler
        handler = get_transport_handler(partner)
        try:
            with handler.connection():
                handler.upload_file(filename, contrl_bytes)
        except Exception as exc:
            Log.log(
                partner, "outbound", "contrl_sent", "error",
                "CONTRL upload failed: %s" % str(exc),
                filename=filename, detail=str(exc),
            )
            self._send_cron_alert(
                'mml_edi.contrl_failure',
                'CONTRL upload failed for %s' % partner.code,
                str(exc),
            )
            return

        Log.log(
            partner, "outbound", "contrl_sent", "success",
            "CONTRL sent acknowledging inbound interchange: %s" % filename,
            filename=filename, review=review,
        )

    # ── File processing ───────────────────────────────────────────────────

    # EDI formats whose wire encoding is EDIFACT's UNOC level-C single-byte
    # charset (Latin-1 / ISO-8859-1), never UTF-8. Decoding these as UTF-8
    # with errors='replace' permanently corrupts any non-ASCII byte (0x80-0xFF)
    # into U+FFFD BEFORE the parser ever sees it — a Latin-1 round-trip cannot
    # recover the original character once replaced (verified missed finding,
    # AN gate review). iDOC/CSV partners (Kestrelby) are unaffected and keep
    # their existing utf-8 decode.
    _EDIFACT_FORMATS = frozenset({"edifact_d96a", "edifact_d01b"})

    def _decode_raw_text(self, content: bytes, partner) -> str:
        """Decode raw file bytes for storage/audit (edi.order.review.edi_raw_data)
        and CONTRL detection, using the partner's wire encoding.

        This is ONLY the fallback/storage decode — it must never overwrite a
        raw_data the parser itself already set from a decode it trusts more
        (see the loop in _process_file, which only assigns when raw_data is
        still empty).
        """
        encoding = "iso-8859-1" if partner.edi_format in self._EDIFACT_FORMATS else "utf-8"
        return content.decode(encoding, errors="replace")

    def _is_contrl_message(self, raw_text: str) -> bool:
        """True when an EDIFACT interchange's UNH identifies it as a CONTRL
        (functional acknowledgement) rather than a business message (ORDERS/
        ORDRSP/...). Cheap substring probe — full tokenizing is left to the
        partner's own parse_contrl so this stays format-agnostic and never
        raises on a malformed file (a false negative just falls through to
        the normal ORDERS parse, which then fails loudly and visibly instead
        of silently, per the code's own fail-closed convention)."""
        return "UNH+" in raw_text and ":CONTRL:" in raw_text.replace("+", ":")

    def _handle_inbound_contrl(
        self, raw_text: str, filename: str, file_hash: str, partner
    ) -> bool:
        """Route an inbound CONTRL to the parser's parse_contrl (AN gate
        review: 'Inbound CONTRL swallowed'). Returns True when this file WAS
        a CONTRL (whether or not it correlated) — the caller must stop and
        not fall through to parser.parse_file, which would silently emit
        'parsed 0 orders' for a CONTRL body with no BGM/LIN.

        A NEGATIVE CONTRL (action code other than '8' = interchange received)
        must create a blocking review, not vanish — Nimbrel uses only '8' by
        spec, but the wire is not trusted to enforce that.
        """
        parser = partner.get_parser_instance()
        parse_contrl = getattr(parser, "parse_contrl", None)
        if not callable(parse_contrl):
            # Parser has no CONTRL support (e.g. Kestrelby) — nothing to do;
            # the probe in _is_contrl_message is best-effort and format-
            # agnostic, so a partner without CONTRL support simply can't
            # receive one in practice, but guard anyway (hasattr contract).
            return False

        try:
            parsed = parse_contrl(raw_text)
        except Exception as exc:
            self.env["edi.log"].log(
                partner, "inbound", "error", "error",
                "Inbound CONTRL in %s failed to parse: %s" % (filename, str(exc)),
                filename=filename, file_hash=file_hash, detail=str(exc),
            )
            return True

        action = parsed.get("action")
        original_ref = parsed.get("original_ref")
        # Correlate against our sent exchanges via edi_log filename/control-ref
        # so a dropped/negative ack is visible rather than silently archived.
        matched = bool(original_ref) and bool(self.env["edi.log"].search_count([
            ("trading_partner_id", "=", partner.id),
            ("event_type", "=", "contrl_sent"),
            ("filename", "like", "%%%s%%" % original_ref),
        ]))

        if action != "8":
            # NEGATIVE CONTRL (rejection) — fail closed: this must block, not
            # vanish. No review/issue infrastructure is owned by this file for
            # a standalone interchange-level rejection, so a high-severity
            # edi.log row + the standard cron alert (rate-limited) is the
            # blocking signal until an operator-facing review type exists.
            self.env["edi.log"].log(
                partner, "inbound", "contrl_received", "error",
                "NEGATIVE CONTRL received in %s (action=%s, original_ref=%s, "
                "correlated=%s) — the interchange we sent was NOT accepted "
                "by the partner." % (filename, action, original_ref, matched),
                filename=filename, file_hash=file_hash,
            )
            self._send_cron_alert(
                'mml_edi.negative_contrl',
                'Negative CONTRL from %s' % partner.code,
                "CONTRL in %s rejected interchange %s (action=%s)" % (
                    filename, original_ref, action),
            )
            return True

        self.env["edi.log"].log(
            partner, "inbound", "contrl_received", "success",
            "CONTRL received in %s: interchange %s acknowledged (action=8, "
            "correlated=%s)" % (filename, original_ref, matched),
            filename=filename, file_hash=file_hash,
        )
        if not matched:
            # We cannot find a matching contrl_sent — informational only
            # (the sent-side filename format may predate this feature, or
            # this CONTRL acks a business message from a channel outside
            # our own sends); does not block, per the module's file-level
            # non-blocking convention for informational mismatches.
            _logger.info(
                "[EDI] CONTRL in %s (ref %s) has no matching contrl_sent log "
                "row — informational only", filename, original_ref,
            )
        return True

    def _validate_inbound_envelope(self, raw_text: str, partner) -> None:
        """Envelope validation + sender-mismatch check for inbound EDIFACT
        (AN gate review 'No inbound envelope validation').

        Fail-closed: raises EDIParseError on ANY violation so the file lands
        in the normal per-file failure path (left in the inbox, alerted,
        counted toward the circuit breaker) rather than silently producing a
        partial/wrong order. No-ops for non-EDIFACT partners and for any
        partner whose parser/trading-partner record doesn't yet expose the
        validator or the C1 sender-identity helpers (hasattr-guarded so this
        never regresses Kestrelby and degrades gracefully before Wave1-A's
        partner fields land).
        """
        if partner.edi_format not in self._EDIFACT_FORMATS:
            return
        try:
            from ..parsers import nimbrel_edifact as edifact_mod
        except Exception:
            # Shared EDIFACT helper module absent/broken — skip rather than
            # block every inbound EDIFACT file on an import error.
            return
        tokenize = getattr(edifact_mod, "tokenize", None)
        validate_interchange = getattr(edifact_mod, "validate_interchange", None)
        if not callable(tokenize) or not callable(validate_interchange):
            return

        from ..parsers.base_parser import EDIParseError

        try:
            _, segments = tokenize(raw_text)
            validate_interchange(segments)
        except Exception as exc:
            raise EDIParseError(
                "Inbound envelope validation failed: %s" % str(exc)) from exc

        get_unb_recipient = getattr(partner, "get_unb_recipient", None)
        if not callable(get_unb_recipient):
            # C1 helper not yet available on this DB (Wave1-A not landed /
            # older partner record) — skip the sender check rather than
            # block every inbound file on a missing capability.
            return
        unb = next((s for s in segments if getattr(s, "tag", None) == "UNB"), None)
        if unb is None:
            return
        sender_id, sender_qual = unb.comp(1, 0), unb.comp(1, 1)
        expected_id, expected_qual = get_unb_recipient()
        if (sender_id, sender_qual) != (expected_id, expected_qual):
            raise EDIParseError(
                "UNB sender %s:%s does not match expected counterparty %s:%s "
                "for environment=%s — refusing to process (fail-closed)" % (
                    sender_id, sender_qual, expected_id, expected_qual,
                    partner.environment,
                )
            )

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

        raw_text = self._decode_raw_text(content, partner)

        # AN-15: an inbound CONTRL (SPS/Nimbrel acking OUR outbound ORDRSP or
        # ORDERS) must be routed to parse_contrl, not silently swallowed as
        # "parsed 0 orders" by the ORDERS parser. Checked before parse_file so
        # a CONTRL body (no BGM/LIN) never reaches the ORDERS code path at all.
        if partner.edi_format in self._EDIFACT_FORMATS and self._is_contrl_message(raw_text):
            self._handle_inbound_contrl(raw_text, filename, file_hash, partner)
            return []

        # AN envelope validation: catches truncated/malformed interchanges and
        # sender/recipient mismatches (wrong mailbox, TST1NIMBREL vs NIMBREL)
        # BEFORE any order is parsed from them — fail-closed, not partial-accept.
        self._validate_inbound_envelope(raw_text, partner)

        parser = partner.get_parser_instance()
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
            # Only fill raw_data when the parser did not already set it — a
            # parser (e.g. Kestrelby) that decodes and stores its own raw_data
            # is trusted over this generic fallback; this only fires for
            # parsers (e.g. Nimbrel) that leave it to the processor.
            if not order.raw_data:
                order.raw_data = raw_text
            try:
                with self.env.cr.savepoint():
                    self.process_parsed_order(order, partner, filename, file_hash)
            except Exception as exc:
                if _is_duplicate_so_violation(exc):
                    # IDEM-1c: a concurrent transaction won the create race and
                    # committed first — the DB backstop index rejected our
                    # insert. The savepoint has already rolled this store back
                    # to a clean state, so convert the loss into the standard
                    # duplicate_skipped path instead of failing the file.
                    _logger.info(
                        "[EDI] Duplicate SO create for store '%s' of PO %s "
                        "blocked by DB unique index (%s) — created concurrently "
                        "by another worker; skipping",
                        order.store_code, order.po_number, filename,
                    )
                    self.env["edi.log"].log(
                        partner, "inbound", "duplicate_skipped", "warning",
                        "Duplicate PO — store '%s' of PO %s in %s was created "
                        "concurrently by another worker (DB unique index)" % (
                            order.store_code, order.po_number, filename),
                        filename=filename, file_hash=file_hash,
                    )
                    continue
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

        # NOTE: the per-PO ACK + OOS summary are deliberately NOT sent here.
        # They are dispatched by the poll loop via _send_file_responses AFTER
        # the per-file commit and FTP rename (ordering invariant IDEM-2), so
        # the partner can never hold an ORDRSP for uncommitted state.
        return failures

    @api.model
    def process_parsed_order(self, order, partner, filename: str, file_hash: str):
        """
        Process a single ParsedOrder through the full pipeline.
        Public — used by cron pipeline and tests.
        """
        client_ref = partner.render_client_ref(order.po_number, order.store_code)

        # C5: a cancellation (BGM 1225=1, Nimbrel) carries no order lines and
        # must NEVER be treated as a change order that mutates+re-confirms a
        # SO, and must NEVER queue an ORDRSP — only a CONTRL. Routed before
        # the change_order/new_order split so it can never fall through to
        # apply_change_order (which would try to diff lines against a
        # cancellation that has none).
        if order.document_type == "cancellation":
            self._process_cancellation(order, partner, client_ref, filename, file_hash)
        elif order.document_type == "change_order":
            self._process_change_order(order, partner, client_ref, filename, file_hash)
        else:
            self._process_new_order(order, partner, client_ref, filename, file_hash)

    # ── New order flow ────────────────────────────────────────────────────

    def _process_new_order(
        self, order, partner, client_ref: str, filename: str, file_hash: str
    ):
        """Full new order pipeline: dedup → SO → lines → issues → routing."""
        existing_so = self._find_existing_so(client_ref, partner)
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
        # Indent detection: a delivery date far in the future marks a FORWARD
        # COMMITMENT (e.g. Kestrelby indent POs placed months ahead of stock
        # arrival). Indents skip the OOS short-ship gate — the stock is
        # EXPECTED to be absent at import — and are held from the 3PL until
        # their release window (see stock_3pl_rohlig outbound gating).
        indent_days = int(self.env['ir.config_parameter'].sudo().get_param(
            'mml_edi.indent_threshold_days', str(INDENT_THRESHOLD_DAYS)))
        so_vals['x_is_indent'] = _is_indent_commitment(
            so_vals['commitment_date'], fields.Datetime.now(), indent_days)
        # Carry the clean customer PO number onto the SO. Per-store SOs otherwise
        # only have the suffixed client_order_ref (e.g. "4500180080_1080"); the
        # full Kestrelby PO must be visible per store for matching, packing slips
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
        # Route new orders to a fulfilment warehouse. Prefer the destination
        # island's DC (per-island model) via the ROQ resolver when available,
        # derived from the store's delivery address; then the trading partner's
        # configured fulfilment warehouse; else the EDI user's default (= AKL).
        # Called defensively so mml_edi does not hard-depend on mml_roq_forecast.
        if 'warehouse_id' in self.env['sale.order']._fields:
            target_wh_id = False
            wh_model = self.env['stock.warehouse']
            if hasattr(wh_model, '_resolve_island_dc'):
                dc = wh_model._resolve_island_dc(delivery_partner)
                if dc:
                    target_wh_id = dc.id
            if not target_wh_id and partner.warehouse_id:
                target_wh_id = partner.warehouse_id.id
            if target_wh_id:
                so_vals['warehouse_id'] = target_wh_id
        so = self.env["sale.order"].create(so_vals)
        if so_vals.get('x_is_indent'):
            so.message_post(body=_(
                'Indent order — forward commitment for delivery on %(date)s. '
                'Accepted in full (out-of-stock gate skipped by design); the '
                'delivery is held from the DC until its release window.',
                date=str(so_vals['commitment_date'])[:10]))

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
            # SS-3: reservations must actually exist after confirm — force +
            # verify them (free_qty for every subsequent order depends on it).
            self.verify_order_reservation(so, partner)
            review.write({"state": "auto_approved"})
            # ACK is sent once per PO after the whole file is processed,
            # committed and renamed (see poll loop -> _send_file_responses) —
            # Kestrelby expects a single per-PO ORDRSP.
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

    def _dc_available_qty(self, product, so):
        """Free-to-promise qty for a product at the order's fulfilment DC.

        Scoped to the order's island DC (never company-wide) so deprecated/untagged
        warehouses (e.g. retired Auckland's phantom stock) can't inflate availability.
        Returns 0.0 when the order has no warehouse. Degrades to warehouse-scoped
        free_qty when the ROQ island helper/tags are absent (mml_roq_forecast not
        installed, or the DC not yet tagged).
        """
        wh = so.warehouse_id if 'warehouse_id' in so._fields else False
        if not wh:
            return 0.0
        # Availability fields are computed + cached per transaction: inside a
        # long poll (or an approve that follows quant changes in the same
        # transaction) the cache can serve a figure computed BEFORE stock
        # moved, silently over- or under-promising. Always read fresh.
        product.invalidate_recordset(["free_qty", "qty_available", "virtual_available"])
        island = wh.roq_island if 'roq_island' in wh._fields else False
        if island and hasattr(product, '_fulfilment_free_qty'):
            return product._fulfilment_free_qty(island=island)
        # Fallback (DC not yet tagged): scope to the warehouse's stock location to
        # match _fulfilment_free_qty (WH/Stock only, not transit/input/output), and
        # NEVER the company-wide {} context.
        loc = wh.lot_stock_id
        return product.with_context(location=loc.id).free_qty if loc else 0.0

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

        # OOS policy (per edi.trading.partner): short_ship reduces the line to the
        # qty available at the fulfilment DC and ACKs the shortfall (so the WMS only
        # receives stock we can ship); backorder keeps the legacy accept-in-full
        # behaviour and warns. edi_ordered_qty always holds the original ordered qty.
        ordered = parsed_line.quantity
        policy = partner.oos_policy if 'oos_policy' in partner._fields else 'backorder'
        # Indent orders accept in full regardless of the partner's OOS policy:
        # the commitment is placed BEFORE the stock arrives by design, so an
        # availability gate at import time would zero every line. Availability
        # is enforced at release time instead.
        if getattr(so, 'x_is_indent', False):
            policy = 'backorder'
        # Short-ship math and availability are both in EA. If a line ever arrives in
        # a non-EA UoM (e.g. cartons), ordered and available aren't comparable —
        # accept in full rather than mis-ship (plan decision 5.5; all live Kestrelby
        # lines are EA today, this guards a future format change).
        line_uom = (getattr(parsed_line, 'uom', None) or '').strip().upper()
        uom_shippable = line_uom in ('', 'EA', 'EACH', 'PCE', 'PC', 'UNIT', 'UNITS')
        if policy == 'short_ship' and uom_shippable:
            ship_qty, shortfall = _short_ship_split(
                ordered, self._dc_available_qty(product, so))
        else:
            if policy == 'short_ship' and not uom_shippable:
                _logger.warning(
                    "[EDI] short-ship skipped for line %s (%s): non-EA UoM %r — "
                    "accepting in full to avoid mis-ship",
                    parsed_line.line_number,
                    product.default_code or product.name, line_uom)
            ship_qty, shortfall = ordered, 0.0

        sol = self.env["sale.order.line"].create({
            "order_id": so.id,
            "product_id": product.id,
            "product_uom_qty": ship_qty,
            "price_unit": parsed_line.unit_price,
            "edi_line_number": parsed_line.line_number,
            "edi_ordered_qty": ordered,
            "edi_qty_shortfall": shortfall,
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

        # Stock shortfall (non-blocking warning; edi_qty_shortfall also drives the
        # ORDRSP line action — short = qty-changed, fully OOS = rejected).
        if policy == 'short_ship':
            if shortfall > 0:
                # edi_qty_shortfall is already set at create; just raise the heads-up.
                self.env["edi.order.issue"].create({
                    "review_id": review.id,
                    "issue_type": "qty_shortfall",
                    "severity": "warning",
                    "description": "%s — ordered %.0f, shipping %.0f, short %.0f "
                                   "(acknowledged in ORDRSP)" % (
                        product.name, ordered, ship_qty, shortfall,
                    ),
                    "sale_order_line_id": sol.id,
                })
        else:
            # Legacy backorder: accept in full, warn if short. warehouse_id is added
            # by sale_stock; fall back to no warehouse context if absent.
            wh_ctx = {}
            if 'warehouse_id' in self.env['sale.order']._fields and so.warehouse_id:
                wh_ctx = {'warehouse': so.warehouse_id.id}
            qty_available = product.with_context(**wh_ctx).qty_available
            if qty_available < ordered:
                sol.edi_qty_shortfall = ordered - qty_available
                self.env["edi.order.issue"].create({
                    "review_id": review.id,
                    "issue_type": "qty_shortfall",
                    "severity": "warning",
                    "description": "%s — requested %.0f, available %.0f, shortfall %.0f" % (
                        product.name, ordered, qty_available, ordered - qty_available,
                    ),
                    "sale_order_line_id": sol.id,
                })

        # Price comparison (blocking if outside tolerance) — this IS the
        # authoritative GST guard. EDI prices are ex-GST (trade/wholesale net).
        # _get_pricelist_price returns the RAW pricelist value, so an ex-GST
        # pricelist compares cleanly while a genuinely GST-inclusive one shows a
        # ~15% discrepancy here and is flagged per line. edi.trading.partner only
        # surfaces a non-blocking advisory (pricelist_gst_warning) when products
        # carry an inc-GST tax — it no longer hard-blocks, because a product can
        # legitimately hold both taxes while being priced ex-GST (e.g. Nimbrel).
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
        existing_so = self._find_existing_so(client_ref, partner)
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

    # ── Cancellation flow (C5) ────────────────────────────────────────────

    # Sentinel prefix on edi.order.review.change_summary that marks a review
    # as a CANCELLATION (as opposed to an ordinary change-order or a manually
    # rejected new order that also ends with an SO in state='cancel'). This is
    # a module-owned marker rather than a new field/selection value on
    # edi.order.review (out of this file's scope) — retry_pending_acks and
    # _is_cancellation_review both match on it, so the two can never disagree.
    CANCELLATION_MARKER = "EDI-CANCELLATION:"

    def _is_cancellation_review(self, review) -> bool:
        """True when ``review`` was created by _process_cancellation — i.e. it
        must never be offered to _queue_ack (see CANCELLATION_MARKER)."""
        summary = getattr(review, "change_summary", None) or ""
        return summary.startswith(self.CANCELLATION_MARKER)

    def _process_cancellation(
        self, order, partner, client_ref: str, filename: str, file_hash: str
    ):
        """Route a cancellation (BGM 1225=1) — C5.

        Per the Testing Scenario Handbook (scenario 3B) a cancellation is
        acknowledged with CONTRL ONLY: the SO is cancelled, a review is
        created purely for audit trail (state='rejected' — no operator action
        needed), and NO ORDRSP is ever queued for it. The mandatory CONTRL is
        still emitted by the normal per-file path (_emit_inbound_contrl),
        which is keyed off the whole interchange, not the document type — a
        cancellation still received an interchange that must be syntactically
        acked.

        Two failure modes guarded against here (gate review AN-05/AN-12):
        - Cancel-before-original: the PO referenced no existing SO yet. There
          is nothing to cancel; log a warning and STOP — do not create a
          phantom review that could confuse a later ORDERS for the same PO.
        - Any accidental route into apply_change_order/ORDRSP: prevented
          structurally — this method never touches pending_changes JSON and
          never calls _queue_ack.
        """
        existing_so = self._find_existing_so(client_ref, partner)
        if not existing_so:
            self.env["edi.log"].log(
                partner, "inbound", "error", "warning",
                "Cancellation for PO '%s' but no matching SO found (ref: %s) "
                "— cancel arrived before (or without) the original order; "
                "nothing to cancel." % (order.po_number, client_ref),
                filename=filename,
            )
            return

        original_review = self.env["edi.order.review"].search([
            ("sale_order_id", "=", existing_so.id),
            ("document_type", "=", "new_order"),
        ], limit=1)

        already_cancelled = existing_so.state == "cancel"
        if not already_cancelled and existing_so.state in ("draft", "sent", "sale"):
            existing_so.action_cancel()

        change_summary = "%s PO %s cancelled by customer (BGM 1225=1)%s" % (
            self.CANCELLATION_MARKER, order.po_number,
            " — SO was already cancelled" if already_cancelled else "",
        )

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
            # Terminal state directly — no operator action is expected or
            # wanted for a cancellation; the state machine's action_approve/
            # action_reject paths (which both end by calling _queue_ack) are
            # deliberately never invoked for this review.
            "state": "rejected",
            "reviewed_date": fields.Datetime.now(),
        })

        # Chatter audit trail on both the review and the SO — a cancellation
        # is a customer-initiated, no-operator-input event, so the record of
        # WHY the SO is cancelled must live on the record itself, not just in
        # edi.log (which most users never open).
        review.message_post(body=_(
            "EDI cancellation received for PO %s. Sale order %s cancelled "
            "automatically. No ORDRSP is sent for a cancellation — only a "
            "CONTRL functional acknowledgement."
        ) % (order.po_number, existing_so.name))
        existing_so.message_post(body=_(
            "Cancelled via EDI: customer sent a cancellation (BGM 1225=1) "
            "for PO %s."
        ) % order.po_number)

        self.env["edi.log"].log(
            partner, "inbound", "order_created", "warning",
            "Cancellation processed: SO %s cancelled for PO %s" % (
                existing_so.name, order.po_number),
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
                # An approved ORDCHG redefines what the customer ORDERED: keep
                # edi_ordered_qty (the basis for the availability re-clamp
                # below and the ORDRSP shortfall) in sync with the new qty.
                so_line.write({
                    "product_uom_qty": line_change["new_qty"],
                    "edi_ordered_qty": line_change["new_qty"],
                })

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
                    # Zero the ordered basis too — otherwise the availability
                    # re-clamp below would resurrect the removed line.
                    so_line.write({
                        'product_uom_qty': 0,
                        'edi_ordered_qty': 0,
                        'edi_qty_shortfall': 0,
                    })
                    _logger.warning(
                        'EDI ORDCHG: SO %s is confirmed — zeroed qty on line %s '
                        '(action code 3). Manual review required.',
                        so.name, removed_line_num,
                    )

        # SS-5: an ORDCHG qty increase must not bypass the availability gate.
        # For short_ship partners re-run the re-clamp over the whole SO so a
        # raised qty is cut back to what the DC can actually supply — the
        # later ACK reads the live line qty + shortfall, so it confirms
        # reality. No-op ([]) for backorder partners.
        #
        # DRAFT ORDERS ONLY: a confirmed SO has already reserved its stock,
        # and free_qty nets that reservation OUT — reclamping would count the
        # order's own reservation against itself and clamp healthy, fully-
        # reserved lines toward zero (then ACK "cannot supply" stock reserved
        # for this very PO). Confirmed-SO changes stay human-gated: the
        # operator sees the raised qty on the review and Odoo's own
        # availability UI at re-reservation.
        reclamped = []
        if so.state == 'draft' and not getattr(so, 'x_is_indent', False):
            # Indents are never availability-clamped (see _process_order_line).
            reclamped = self.reclamp_order_lines(so, review.trading_partner_id)
        else:
            _logger.info(
                "[EDI] ORDCHG on confirmed SO %s — availability re-clamp "
                "skipped (own reservation would be double-counted); operator "
                "review is the gate.", so.name,
            )

        body = _("EDI change order approved: %s") % review.change_summary
        if reclamped:
            clamp_summary = "; ".join(
                "%s: %.0f -> %.0f (short %.0f)" % (
                    c["line"].product_id.display_name or c["line"].name,
                    c["old_qty"], c["new_qty"], c["shortfall"],
                ) for c in reclamped
            )
            body += _(" — availability re-clamp applied: %s") % clamp_summary
            # Audit trail on the review itself so the approver sees exactly
            # what diverged from the customer's requested quantities.
            review.message_post(
                body=_("Change-order quantities re-clamped to DC availability: "
                       "%s") % clamp_summary,
                subtype_xmlid="mail.mt_note",
            )
        so.message_post(
            body=body,
            subtype_xmlid="mail.mt_note",
        )

    # ── Availability re-clamp (shared helper — approve/ORDCHG consumers) ──

    @api.model
    def reclamp_order_lines(self, sale_order, partner, cap_to_current=False):
        """Re-run the availability gate on an EDI sale order at APPROVE time
        — SS-1/SS-5. CALLERS MUST ONLY PASS DRAFT ORDERS: a confirmed SO has
        reserved its own stock, which free_qty nets out, so reclamping it
        would count the order against itself.

        For ``partner.oos_policy == 'short_ship'``: every SO line with
        ``edi_ordered_qty > 0`` is re-clamped to
        ``min(edi_ordered_qty, DC-available)`` with a floor of 0 (the same
        math as the parse-time gate: _dc_available_qty + _short_ship_split),
        and ``edi_qty_shortfall`` is updated to match. Availability NEVER
        raises: a missing product/warehouse or a lookup error fails toward
        0.0 available (fail-closed — never promise stock we cannot see).

        ``cap_to_current=True`` (Approve-with-Corrections): operator qty
        reductions are deliberate — lines may only move DOWN from the
        operator's value, never be restored up toward the ordered qty
        (see _reclamp_line).

        For ``'backorder'`` partners: returns ``[]`` without touching lines.

        Returns ``[{'line': sol, 'old_qty': float, 'new_qty': float,
        'shortfall': float}]`` for CHANGED lines only, and writes ONE edi.log
        warning row when any line changed.
        """
        policy = partner.oos_policy if 'oos_policy' in partner._fields else 'backorder'
        if policy != 'short_ship':
            return []

        changes = []
        for sol in sale_order.order_line:
            ordered = sol.edi_ordered_qty or 0.0
            if ordered <= 0:
                continue
            try:
                available = (
                    self._dc_available_qty(sol.product_id, sale_order)
                    if sol.product_id else 0.0
                )
            except Exception:
                _logger.warning(
                    "[EDI] reclamp: availability lookup failed for %s line %s "
                    "— treating as 0.0 available (fail-closed)",
                    sale_order.name, sol.id, exc_info=True,
                )
                available = 0.0
            new_qty, shortfall, changed = _reclamp_line(
                ordered, available, sol.product_uom_qty,
                cap_to_current=cap_to_current)
            if not changed:
                # Keep the derived shortfall honest even when the qty itself
                # is unchanged (shortfall = ordered - qty).
                if sol.edi_qty_shortfall != shortfall:
                    sol.edi_qty_shortfall = shortfall
                continue
            old_qty = sol.product_uom_qty
            sol.write({
                "product_uom_qty": new_qty,
                "edi_qty_shortfall": shortfall,
            })
            changes.append({
                "line": sol,
                "old_qty": old_qty,
                "new_qty": new_qty,
                "shortfall": shortfall,
            })

        if changes:
            detail = "; ".join(
                "%s: %.0f -> %.0f (short %.0f)" % (
                    c["line"].product_id.display_name or c["line"].name,
                    c["old_qty"], c["new_qty"], c["shortfall"],
                ) for c in changes
            )
            self.env["edi.log"].log(
                partner, "internal", "info", "warning",
                "Availability re-clamped %d line(s) on %s at approve: %s" % (
                    len(changes), sale_order.name, detail),
                sale_order=sale_order,
            )
        return changes

    # ── Post-confirm reservation verify (SS-3) ────────────────────────────

    @api.model
    def verify_order_reservation(self, sale_order, partner):
        """Force + verify stock reservation on the outgoing picking(s) of a
        just-confirmed EDI SO (SS-3).

        ``action_assign()`` is idempotent (a no-op on already-reserved moves),
        so this is safe regardless of the picking type's reservation_method.
        Any move that still fails to reserve is listed in ONE edi.log warning
        — free_qty correctness for every subsequent order depends on the
        reservation actually existing. Never raises. Returns the moves that
        failed to reserve.
        """
        unreserved = self.env["stock.move"]
        try:
            pickings = sale_order.picking_ids.filtered(
                lambda p: p.picking_type_code == "outgoing"
                and p.state not in ("done", "cancel"))
            for picking in pickings:
                try:
                    picking.action_assign()
                except Exception:
                    _logger.warning(
                        "[EDI] action_assign failed on %s (SO %s)",
                        picking.name, sale_order.name, exc_info=True)
                unreserved |= picking.move_ids.filtered(
                    lambda m: m.state not in ("assigned", "done", "cancel")
                    and m.product_uom_qty > 0)
        except Exception:
            _logger.warning(
                "[EDI] reservation verify failed for SO %s",
                sale_order.name, exc_info=True)
            return unreserved

        if unreserved:
            detail = "; ".join(
                "%s: demand %.0f, state %s" % (
                    m.product_id.display_name, m.product_uom_qty, m.state)
                for m in unreserved)
            self.env["edi.log"].log(
                partner, "internal", "info", "warning",
                "%d move(s) failed to reserve after confirming %s: %s" % (
                    len(unreserved), sale_order.name, detail),
                sale_order=sale_order,
            )
        self._warn_reservation_method(sale_order, partner)
        return unreserved

    def _warn_reservation_method(self, sale_order, partner):
        """Config sanity check (SS-3): the DC outgoing picking type should
        reserve at confirm. With 'manual'/'by_date', free_qty never decrements
        on its own and every subsequent order is promised the same units.
        Warns at most once per poll per picking type via the mutable set
        placed in context by poll_trading_partner; outside a poll (tests,
        direct calls) it warns on every call."""
        wh = sale_order.warehouse_id if 'warehouse_id' in sale_order._fields else False
        picking_type = wh.out_type_id if wh else False
        if not picking_type or 'reservation_method' not in picking_type._fields:
            return
        if picking_type.reservation_method == 'at_confirm':
            return
        warned = self.env.context.get('edi_reservation_warned')
        if isinstance(warned, set):
            if picking_type.id in warned:
                return
            warned.add(picking_type.id)
        _logger.warning(
            "[EDI] Picking type '%s' has reservation_method=%r (expected "
            "'at_confirm') — confirmed EDI orders will not reserve stock "
            "automatically", picking_type.display_name,
            picking_type.reservation_method,
        )
        self.env["edi.log"].log(
            partner, "internal", "info", "warning",
            "Picking type '%s' has reservation_method '%s' (expected "
            "'at_confirm'): confirmed EDI orders will not reserve stock "
            "automatically and free_qty will over-promise." % (
                picking_type.display_name, picking_type.reservation_method),
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

    def _find_existing_so(self, client_ref: str, partner):
        """Find an existing EDI SO by client reference, scoped to the trading
        partner and company (IDEM major 3). An unrelated SO that happens to
        carry the same client_order_ref (e.g. a manually-entered ref colliding
        with Nimbrel's bare '{po_number}' template) must never suppress a real
        order as duplicate_skipped. Returns record or None."""
        return self.env["sale.order"].search([
            ("client_order_ref", "=", client_ref),
            ("edi_trading_partner_id", "=", partner.id),
            ("company_id", "=", self.env.company.id),
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

    def _edi_mailer(self):
        """(from_addr, mail_server) for outbound EDI notifications.

        Office365 denies 'send as' for the company/notifications address
        (SendAsDenied), so route EDI alerts and OOS summaries through the
        noreply mailbox, which is the permitted relay. The from-address is
        ACCOUNT-SPECIFIC (it carries the deploying company's own mail domain)
        and lives in ir.config_parameter ``mml_edi.notify_from`` with NO
        hardcoded fallback — an upgrade seeds the deployment's existing value
        (migrations/19.0.1.3.0/post-migration.py). When unset, fall back to the
        company's own email and finally to Odoo's own default from-address, so
        a fresh install never mails out under a foreign domain.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        from_addr = (
            ICP.get_param('mml_edi.notify_from')
            or self.env.company.email
            or ICP.get_param('mail.default.from')
            or ''
        )
        server = self.env['ir.mail_server'].sudo().search(
            [('name', 'ilike', 'noreply')], limit=1)
        return from_addr, server

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
                from_addr, server = self._edi_mailer()
                ev = {
                    'email_to': ','.join(recipients.mapped('email')),
                    'email_from': from_addr,
                }
                if server:
                    ev['mail_server_id'] = server.id
                template.send_mail(review.id, force_send=True, email_values=ev)
        except Exception as exc:
            _logger.warning("[EDI] Failed to send review alert: %s", exc)

    def _send_oos_summary(self, partner, po_number):
        """Email a per-PO summary of stock-shortfall (OOS) lines after import.

        OOS lines are non-blocking warnings; the body reflects the partner's
        oos_policy — short-shipped + ACKed in the ORDRSP (short_ship) or accepted
        in full + backordered (backorder). A heads-up for operations, not an
        approval request — sent to the partner's Alert Email Recipients. Fires once
        per file import (re-polls of the same file are dedup-skipped before here).
        """
        recipients = partner.alert_email_ids.filtered('email')
        if not recipients:
            return
        reviews = self.env["edi.order.review"].search([
            ("trading_partner_id", "=", partner.id),
            ("customer_po_number", "=", po_number),
        ])
        shortfalls = self.env["edi.order.issue"].search([
            ("review_id", "in", reviews.ids),
            ("issue_type", "=", "qty_shortfall"),
        ], order="id")
        if not shortfalls:
            return

        cap = 200
        rows = "".join(
            '<tr><td style="padding:4px 10px;border:1px solid #ddd;">%s</td>'
            '<td style="padding:4px 10px;border:1px solid #ddd;">%s</td></tr>'
            % (html.escape(iss.review_id.store_code or ""),
               html.escape(iss.description or ""))
            for iss in shortfalls[:cap]
        )
        more = len(shortfalls) - cap
        extra = ('<p style="color:#888;">… and %d more line(s).</p>' % more
                 if more > 0 else "")
        if partner.oos_policy == 'short_ship':
            policy_msg = ('The available qty was short-shipped and the shortfall '
                          'acknowledged to the customer in the ORDRSP — no backorder '
                          'is created. No action is required unless you want to '
                          'expedite stock.')
        else:
            policy_msg = ('The order was accepted in full and acknowledged to the '
                          'customer; the short quantity will backorder. No action is '
                          'required unless you want to expedite stock.')
        body = (
            '<div style="font-family:Arial,sans-serif;font-size:14px;color:#333;max-width:680px;">'
            '<h2 style="color:#e67e22;border-bottom:2px solid #e67e22;padding-bottom:6px;">'
            'Stock shortfall summary — PO %s</h2>'
            '<p>%d line(s) on this auto-approved order are short on available stock. '
            '%s</p>'
            '<table style="border-collapse:collapse;margin:12px 0;">'
            '<thead><tr>'
            '<th style="padding:4px 10px;border:1px solid #ddd;text-align:left;background:#f7f7f7;">Store</th>'
            '<th style="padding:4px 10px;border:1px solid #ddd;text-align:left;background:#f7f7f7;">Shortfall</th>'
            '</tr></thead><tbody>%s</tbody></table>%s'
            '<p style="color:#999;font-size:12px;">Automated summary from the MML EDI system.</p>'
            '</div>'
        ) % (html.escape(str(po_number)), len(shortfalls), policy_msg, rows, extra)

        from_addr, server = self._edi_mailer()
        vals = {
            "subject": "EDI stock shortfall: PO %s (%d line(s) short)" % (
                po_number, len(shortfalls)),
            "body_html": body,
            "email_from": from_addr,
            "email_to": ",".join(recipients.mapped("email")),
        }
        if server:
            vals["mail_server_id"] = server.id
        self.env["mail.mail"].sudo().create(vals).send()
