import math
from dataclasses import dataclass
from threading import Lock

import httpx
import jwt
from sqlalchemy import select

from agent_runtime.executions import APIError


@dataclass(frozen=True)
class Principal:
    tenant: dict
    account_id: str


class Auth:
    def __init__(self, db, mw, clock):
        self.db, self.mw, self.clock = db, mw, clock
        self.keys = {}
        self.lock = Lock()

    def refresh_keys(self, tenant):
        try:
            keys = self.mw.jwks(tenant)["keys"]
            if not isinstance(keys, list):
                raise TypeError("Invalid JWKS")
            refreshed = {}
            for key in keys:
                if not isinstance(key, dict) or not isinstance(key.get("kty"), str):
                    raise TypeError("Invalid JWK")
                if key["kty"] == "RSA" and key.get("alg", "RS256") == "RS256":
                    if not all(
                        isinstance(key[field], str) and key[field] for field in ("kid", "n", "e")
                    ):
                        raise ValueError("Invalid RSA JWK")
                    refreshed[(tenant["id"], key["kid"])] = jwt.PyJWK.from_dict(key).key
        except (jwt.PyJWTError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise APIError(
                503, "AUTH_UNAVAILABLE", "Cannot retrieve valid verification keys"
            ) from exc
        # A malformed response must not partially populate the trusted key cache.
        self.keys.update(refreshed)

    def authenticate(self, conn, token):
        try:
            header = jwt.get_unverified_header(token)
            claims = jwt.decode(token, options={"verify_signature": False})
            issuer = claims.get("iss")
            if not isinstance(issuer, str) or header.get("alg") != "RS256":
                raise ValueError("Invalid issuer or algorithm")
            t = self.db.tenants
            tenant = conn.execute(select(t).where(t.c.mw_issuer == issuer)).mappings().first()
            if tenant is None:
                raise ValueError("Unknown issuer")
            kid = header["kid"]
            if not isinstance(kid, str):
                raise TypeError("Invalid key ID")
            cache_key = (tenant["id"], kid)
            with self.lock:
                if cache_key not in self.keys:
                    self.refresh_keys(tenant)
                key = self.keys[cache_key]
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=issuer,
                audience=f"{issuer}/api",
                options={
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                    "require": ["exp", "sub", "iss", "aud", "typ"],
                },
            )
            now = self.clock.now().timestamp()
            expiry = claims["exp"]
            if (
                not isinstance(expiry, (int, float))
                or isinstance(expiry, bool)
                or not math.isfinite(expiry)
                or now >= expiry
            ):
                raise ValueError("Expired or malformed expiry")
            for field in ("nbf", "iat"):
                if field in claims:
                    value = claims[field]
                    if (
                        not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        or value > now
                    ):
                        raise ValueError("Token not yet valid")
            if claims["typ"] != "human" or not isinstance(claims["sub"], str) or not claims["sub"]:
                raise ValueError("Human identity required")
            return Principal(dict(tenant), claims["sub"])
        except (jwt.PyJWTError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise APIError(401, "UNAUTHORIZED", "Invalid human token") from exc
