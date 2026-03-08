# mml.edi/utils/credential_store.py
"""Fernet-based symmetric encryption for EDI trading partner credential fields.

Master key is stored in ir.config_parameter under key 'mml_edi.fernet_master_key'.
On first use, a new key is generated and stored. The same key must persist across
restarts.

Encrypted values are stored as base64-encoded Fernet tokens prefixed with 'enc:'
to distinguish them from legacy plaintext values.

Usage:
    from mml_edi.utils.credential_store import encrypt_credential, decrypt_credential

    ciphertext = encrypt_credential(plaintext, env)
    plaintext  = decrypt_credential(ciphertext, env)
"""
import logging

_logger = logging.getLogger(__name__)

_PARAM_KEY = 'mml_edi.fernet_master_key'
_PREFIX = 'enc:'


def _get_or_create_key(env) -> bytes:
    """Return the Fernet master key bytes, generating and persisting one if absent.

    The key is stored in ir.config_parameter as a base64 string (the native
    format returned by Fernet.generate_key()). .sudo() is used so the key can
    be read/written regardless of the current user's access rights.
    """
    from cryptography.fernet import Fernet

    IrParam = env['ir.config_parameter'].sudo()
    stored = IrParam.get_param(_PARAM_KEY)
    if stored:
        return stored.encode()

    new_key = Fernet.generate_key()  # bytes, already base64-encoded
    IrParam.set_param(_PARAM_KEY, new_key.decode())
    _logger.warning(
        'mml_edi: generated new Fernet credential encryption key. '
        'Back up ir.config_parameter key %r to prevent credential loss.',
        _PARAM_KEY,
    )
    return new_key


def encrypt_credential(plaintext: str, env) -> str:
    """Encrypt *plaintext* and return an 'enc:'-prefixed Fernet token string.

    - If *plaintext* is falsy, it is returned unchanged.
    - If *plaintext* already starts with 'enc:', it is returned unchanged (idempotent).
    - On any Fernet error, a UserError is raised to prevent silent data loss.
    """
    if not plaintext:
        return plaintext
    if plaintext.startswith(_PREFIX):
        return plaintext

    try:
        from cryptography.fernet import Fernet
        key = _get_or_create_key(env)
        f = Fernet(key)
        token = f.encrypt(plaintext.encode('utf-8'))
        return _PREFIX + token.decode()
    except Exception as exc:
        _logger.error('encrypt_credential: Fernet encryption failed: %s', exc)
        from odoo.exceptions import UserError
        raise UserError(
            'Credential encryption failed. Ensure the "cryptography" package is installed '
            'and the encryption key in ir.config_parameter is valid. '
            'Error: %s' % str(exc)[:200]
        )


def decrypt_credential(ciphertext: str, env) -> str:
    """Decrypt an 'enc:'-prefixed Fernet token and return the plaintext.

    - If *ciphertext* is falsy or does not start with 'enc:', it is returned
      unchanged (handles legacy plaintext stored before encryption was enabled).
    - On any Fernet error (bad key, corrupted token), an empty string is returned
      and the error is logged — never raises to the caller.
    """
    if not ciphertext:
        return ciphertext
    if not ciphertext.startswith(_PREFIX):
        _logger.warning(
            'decrypt_credential: FTP password is stored as plaintext — '
            're-save the trading partner record to encrypt it.'
        )
        return ciphertext

    try:
        from cryptography.fernet import Fernet
        key = _get_or_create_key(env)
        f = Fernet(key)
        token = ciphertext[len(_PREFIX):].encode()
        return f.decrypt(token).decode('utf-8')
    except Exception as exc:
        _logger.error(
            'decrypt_credential: failed to decrypt FTP password — key may have changed. '
            'Re-enter the FTP password on the trading partner record. Error: %s', exc
        )
        return ''
