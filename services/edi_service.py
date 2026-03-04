import logging

_logger = logging.getLogger(__name__)


class EDIService:
    """Public API for mml_edi. Instantiated directly — mml.registry not available on Odoo 15."""

    def __init__(self, env):
        self.env = env

    def on_3pl_despatch_confirmed(self, event) -> None:
        """
        Trigger ASN send when Mainfreight confirms despatch.
        Stub — implement when 3PL↔EDI bridge is built (mml_freight_3pl).
        """
        pass
