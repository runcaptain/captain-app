"""Create, read in another process and delete one synthetic OS-keychain item."""
import json
import secrets
import subprocess
import sys
import uuid
import keyring

service = 'captain-team-synthetic-check-' + uuid.uuid4().hex
account = 'synthetic-user'
secret = secrets.token_urlsafe(40)
backend = keyring.get_keyring()
allowed = ('keyring.backends.macOS', 'keyring.backends.Windows', 'keyring.backends.SecretService')
if type(backend).__module__ not in allowed:
    raise SystemExit('A supported OS keychain backend is required; no plaintext fallback')
try:
    keyring.set_password(service, account, secret)
    assert keyring.get_password(service, account) == secret
    child = subprocess.run([sys.executable, '-c',
        'import json,sys,keyring; s,a,v=json.load(sys.stdin); assert keyring.get_password(s,a)==v'],
        input=json.dumps([service, account, secret]), text=True, capture_output=True)
    assert child.returncode == 0, 'Separate-process keychain read failed'
    print('PASS: real OS keychain write and separate-process read; no plaintext credential file')
finally:
    try:
        keyring.delete_password(service, account)
    except keyring.errors.PasswordDeleteError:
        pass
    assert keyring.get_password(service, account) is None
    print('CLEANUP VERIFIED: owned synthetic keychain item removed')
