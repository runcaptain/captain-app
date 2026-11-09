"""OIDC resource-server verification. No enrollment or trust from token claims."""
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException
import jwt


@dataclass(frozen=True)
class OIDCSettings:
    issuer: str
    audience: str
    jwks_url: str

    def __post_init__(self):
        for value in (self.issuer, self.jwks_url):
            parsed = urlsplit(value)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError('OIDC issuer and JWKS must be explicit HTTPS URLs')
        if not self.audience.strip():
            raise ValueError('OIDC audience is required')


class OIDCVerifier:
    def __init__(self, settings):
        self.settings = settings
        self.jwks = jwt.PyJWKClient(settings.jwks_url, timeout=5, lifespan=300)

    def verify(self, token):
        try:
            # Fixed asymmetric algorithm; token headers never select trust URLs or algorithms.
            header = jwt.get_unverified_header(token)
            if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str):
                raise jwt.InvalidTokenError('unsupported signing header')
            key = self.jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=['RS256'],
                                issuer=self.settings.issuer, audience=self.settings.audience,
                                options={'require': ['iss', 'aud', 'sub', 'exp', 'iat']})
            if not isinstance(claims['sub'], str) or not claims['sub'] or len(claims['sub']) > 500:
                raise jwt.InvalidTokenError('invalid subject')
            return self.settings.issuer, claims['sub']
        except (jwt.PyJWTError, ValueError, TypeError):
            raise HTTPException(401, 'unauthenticated') from None
