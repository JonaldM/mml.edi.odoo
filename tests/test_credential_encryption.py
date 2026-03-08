"""Tests for EDI credential encryption utility."""
import importlib.util
import os
import sys
import types


def _load_credential_store():
    """Load credential_store directly from file path, bypassing package registration."""
    utils_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', 'utils')
    )
    # Register mml_edi.utils as a namespace package if not already present
    if 'mml_edi.utils' not in sys.modules:
        mml_edi = sys.modules.get('mml_edi')
        utils_mod = types.ModuleType('mml_edi.utils')
        utils_mod.__package__ = 'mml_edi.utils'
        utils_mod.__path__ = [utils_dir]
        sys.modules['mml_edi.utils'] = utils_mod
        if mml_edi is not None:
            mml_edi.utils = utils_mod

    mod_name = 'mml_edi.utils.credential_store'
    if mod_name in sys.modules:
        return sys.modules[mod_name]

    file_path = os.path.join(utils_dir, 'credential_store.py')
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = 'mml_edi.utils'
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_encrypt_decrypt_roundtrip():
    cs = _load_credential_store()

    from cryptography.fernet import Fernet
    test_key = Fernet.generate_key().decode()

    mock_icp = types.SimpleNamespace(
        get_param=lambda key, default=False: test_key,
        set_param=lambda key, val: None,
    )
    mock_env = {'ir.config_parameter': types.SimpleNamespace(sudo=lambda: mock_icp)}

    plaintext = 's3cr3t_ftp_p@ssword'
    encrypted = cs.encrypt_credential(plaintext, mock_env)

    assert encrypted.startswith('enc:'), "Encrypted value must start with 'enc:'"
    assert plaintext not in encrypted

    decrypted = cs.decrypt_credential(encrypted, mock_env)
    assert decrypted == plaintext


def test_legacy_plaintext_passthrough():
    cs = _load_credential_store()

    result = cs.decrypt_credential('legacy_plain', None)
    assert result == 'legacy_plain'


def test_encrypt_idempotent():
    cs = _load_credential_store()

    from cryptography.fernet import Fernet
    test_key = Fernet.generate_key().decode()

    mock_icp = types.SimpleNamespace(
        get_param=lambda key, default=False: test_key,
        set_param=lambda key, val: None,
    )
    mock_env = {'ir.config_parameter': types.SimpleNamespace(sudo=lambda: mock_icp)}

    plaintext = 'my_password'
    encrypted_once = cs.encrypt_credential(plaintext, mock_env)
    encrypted_twice = cs.encrypt_credential(encrypted_once, mock_env)

    assert encrypted_once == encrypted_twice, "encrypt_credential must be idempotent for 'enc:' values"


def test_encrypt_empty_passthrough():
    cs = _load_credential_store()
    assert cs.encrypt_credential('', None) == ''
    assert cs.encrypt_credential(None, None) is None
