"""Real local HTTPS JWKS retrieval and rotation with ephemeral synthetic keys."""
import datetime
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import HTTPException

from captain_search.collaboration.identity import OIDCSettings, OIDCVerifier


def test_https_jwks_retrieval_rotation_and_unknown_key(tmp_path, monkeypatch):
    tls_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(tls_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
            .sign(tls_key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'tls.crt', tmp_path / 'tls.key'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(tls_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    signing_keys = [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]
    state = {'keys': [], 'requests':0}
    def publish(index):
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_keys[index].public_key()))
        jwk.update(kid=f'key-{index}', use='sig', alg='RS256')
        state['keys'] = [jwk]
    publish(0)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state['requests'] += 1
            body = json.dumps({'keys':state['keys']}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path,key_path)
    server.socket = context.wrap_socket(server.socket,server_side=True)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        origin = f'https://localhost:{server.server_port}'
        verifier = OIDCVerifier(OIDCSettings(origin,'team-api',origin+'/jwks'))
        verifier.jwks = jwt.PyJWKClient(origin+'/jwks', timeout=5,
                                      ssl_context=ssl.create_default_context(cafile=str(cert_path)))
        def token(index, kid=None):
            return jwt.encode({'iss':origin,'aud':'team-api','sub':'subject','iat':int(time.time()),'exp':int(time.time())+60},
                              signing_keys[index],algorithm='RS256',headers={'kid':kid or f'key-{index}'})
        assert verifier.jwks.fetch_data()['keys']
        assert verifier.verify(token(0)) == (origin,'subject')
        publish(1)
        # Recent PyJWT versions intentionally throttle unknown-key refreshes.
        # Advance only the JWKS client's clock past that cooldown.
        if hasattr(verifier.jwks, 'cooldown_duration'):
            from types import SimpleNamespace
            real_monotonic = time.monotonic
            monkeypatch.setattr(jwt.jwks_client, 'time', SimpleNamespace(
                monotonic=lambda: real_monotonic() + verifier.jwks.cooldown_duration + 1))
        assert verifier.verify(token(1)) == (origin,'subject')
        assert state['requests'] >= 2
        with pytest.raises(HTTPException) as error:
            verifier.verify(token(0,kid='unknown'))
        assert error.value.status_code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
