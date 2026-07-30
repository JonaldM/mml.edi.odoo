"""DEPLOYMENT-LOCAL legacy values for the 19.0.1.3.0 config extraction.

NOT PRODUCT CODE. This file exists so that the ONE deployment that has been
running these values as hardcoded code defaults keeps behaving identically
after the upgrade that removed those defaults.

Why it is a separate file
-------------------------
19.0.1.3.0 removed every account-specific literal from mml_edi's runtime paths
(VAN sender id, counterparty mailbox identities, GS1 company prefix,
counterparty GLN, notification from-address). Each is now read from
ir.config_parameter with NO code fallback, so a fresh install starts neutral
and fails closed until an operator configures it — see the module-level
comments in parsers/nimbrel_edifact.py, parsers/kestrelby_asn.py and
models/sscc_register.py.

The upgrade path still needs the old values, and they are inherently
account-specific. Keeping them in post-migration.py would simply have moved the
same literals from one shipped file to another. They live here instead, in a
single file that:

  * is excluded from the productised source tree
    (scripts/cadence_transform/rename_map.yaml `path_excludes`), so no other
    deployment ever receives another deployment's routing identities; and
  * is imported defensively by post-migration.py, which is a no-op seeder when
    the file is absent.

A tree without this file loses nothing: migrations only run when upgrading FROM
an older version, and only this deployment has an older version whose defaults
lived in code.

Adding a value here does NOT make it a default. post-migration.py seeds a key
only when it is currently unset, exactly once, on the 19.0.1.3.0 upgrade.
"""

#: ir.config_parameter key -> the value this deployment ran as a code default
#: before 19.0.1.3.0. Seeded only when the key is unset. See post-migration.py.
LEGACY_DEFAULTS = {
    # services/edi_service.py::_build_despatch_dict_kestrelby — our own sender
    # identity on the EDIS VAN, written to UNB S002 and NAD+SE of the DESADV.
    "mml_edi.sender_id": "MMLEDI",
    # parsers/kestrelby_asn.py — the counterparty's GS1 GLN, written to UNB
    # S003 and NAD+BY of the DESADV.
    "mml_edi.kestrelby_buyer_gln": "9469313000007",
    # parsers/nimbrel_edifact.py — the counterparty's SPS Commerce mailbox
    # identities. The test-portal mailbox is a DISTINCT identity, not the
    # production id with a flag.
    "mml_edi.default_unb_recipient_id": "ANIMATES",
    "mml_edi.default_unb_recipient_test_id": "TST1ANIMATES",
    # models/sscc_register.py — the GS1 company prefix SSCC-18 codes are minted
    # under. Allocated by GS1 to one company; never a product default.
    "mml_edi.gs1_company_prefix": "9419416",
    # models/edi_processor.py::_edi_mailer — from-address for EDI notification
    # mail, on this deployment's own mail domain.
    "mml_edi.notify_from": "MML EDI <noreply@mml.co.nz>",
}
