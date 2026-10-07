from datetime import datetime

import httpx

from agent_runtime.executions import APIError


class MockWorkday:
    def __init__(self, client=None):
        self.client = client or httpx.Client(trust_env=False, timeout=5)

    def jwks(self, tenant):
        response = self.client.get(
            tenant["mw_base_url"] + "/.well-known/jwks.json", headers={"Host": tenant["mw_host"]}
        )
        response.raise_for_status()
        return response.json()

    def grants(self, tenant, token):
        try:
            response = self.client.get(
                tenant["mw_base_url"] + "/api/v1/delegation-grants",
                headers={"Host": tenant["mw_host"], "Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            grants = response.json()
            if not isinstance(grants, list):
                raise TypeError("Invalid grant list")
            parsed = []
            for g in grants:
                if not all(
                    isinstance(g[k], str) and g[k] for k in ("id", "client_id", "expires_at")
                ):
                    raise ValueError("Invalid grant identity")
                if not isinstance(g["scopes"], list) or not all(
                    isinstance(s, str) for s in g["scopes"]
                ):
                    raise ValueError("Invalid scopes")
                expiry = datetime.fromisoformat(g["expires_at"])
                if expiry.tzinfo is None:
                    raise ValueError("Invalid expiry")
                revoked = g["revoked_at"]
                if revoked is not None:
                    if not isinstance(revoked, str):
                        raise ValueError("Invalid revocation")
                    datetime.fromisoformat(revoked)
                parsed.append({**g, "expires_at": expiry})
            return parsed
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise APIError(503, "GRANT_CHECK_UNAVAILABLE", "Cannot validate grant") from exc
