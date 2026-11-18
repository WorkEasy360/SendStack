"""
A WorkEasy360 Accounts stand-in for SendStack's tests. Not used at runtime.

It is a real HTTP server on 127.0.0.1 (a thread in the test process), so the
connector talks to it exactly as it talks to WorkEasy360 -- discovery, the
key set, the token endpoint and the entitlement endpoint over HTTP -- and a
second SendStack process (the two-instance test) can reach it too.

It checks what the platform checks:

* ``private_key_jwt``: the assertion is signed by the client's registered
  key, ``iss = sub = client id``, ``aud`` is **the token endpoint** (not the
  issuer), it lives at most 60 s, and each ``jti`` is accepted once -- on the
  token endpoint and the entitlement endpoint alike;
* PKCE: ``S256`` only, and ``BASE64URL(SHA256(code_verifier))`` must equal the
  challenge sent to the authorization endpoint;
* codes are single use, bound to the client and the redirect URI.

Its signing key and the product's client key are generated when it starts;
nothing here is a secret anyone else holds.
"""

import base64
import hashlib
import json
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

BACKCHANNEL_LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


def _new_key():
    return ec.generate_private_key(ec.SECP256R1())


def _pem(private_key) -> str:
    return private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode("ascii")


def _jwk(public_key, kid) -> dict:
    entry = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(public_key))
    entry.update({"kid": kid, "alg": "ES256", "use": "sig"})
    return entry


class FakeWorkEasy360:
    product_code = "workeasy_sendstack"
    client_id = "sendstack-test-client"
    redirect_uri = "https://sendstack.test/api/v1/auth/workeasy360/callback/"
    post_logout_redirect_uri = "https://sendstack.test/login"

    def __init__(self):
        self.signing_key = _new_key()
        self.kid = f"w360-{secrets.token_hex(4)}"
        # Same kid, different key: only the signature tells them apart.
        self.foreign_key = _new_key()
        self.client_key = _new_key()
        self.client_kid = f"cred-{secrets.token_hex(4)}"
        self.codes = {}
        self.assertion_ids = set()
        self.lock = threading.Lock()
        self.seen = {"token": [], "entitlement": []}
        #: (status, body) for the entitlement endpoint, per (organization, subject).
        self.entitlement = self.allow
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.issuer = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # --- what the product is configured with -------------------------------------

    @property
    def token_endpoint(self):
        return f"{self.issuer}/api/v1/integrations/oauth/token"

    @property
    def client_private_key_pem(self):
        return _pem(self.client_key)

    def connector_settings(self, **extra):
        """The WORKEASY360_SSO block a test product runs with (on top of settings.py's)."""
        from django.conf import settings

        values = dict(settings.WORKEASY360_SSO)
        values.update({
            "ISSUER": self.issuer,
            "CLIENT_ID": self.client_id,
            "REDIRECT_URI": self.redirect_uri,
            "POST_LOGOUT_REDIRECT_URI": self.post_logout_redirect_uri,
            "PRIVATE_KEY": self.client_private_key_pem,
            "KEY_ID": self.client_kid,
            "ALLOW_INSECURE_HTTP": True,
        })
        values.update(extra)
        return values

    def environment(self):
        """The same configuration as environment variables, for a second process."""
        return {
            "WORKEASY360_ISSUER": self.issuer,
            "WORKEASY360_CLIENT_ID": self.client_id,
            "WORKEASY360_REDIRECT_URI": self.redirect_uri,
            "WORKEASY360_POST_LOGOUT_REDIRECT_URI": self.post_logout_redirect_uri,
            "WORKEASY360_PRIVATE_KEY": self.client_private_key_pem.replace("\n", "\\n"),
            "WORKEASY360_KEY_ID": self.client_kid,
            "WORKEASY360_ALLOW_INSECURE_HTTP": "true",
            "WORKEASY360_PRODUCT_CODE": self.product_code,
        }

    # --- people and answers -------------------------------------------------------

    @staticmethod
    def person(**overrides):
        person = {
            "sub": str(uuid.uuid4()),
            "email": f"person-{secrets.token_hex(3)}@example.test",
            "email_verified": True,
            "name": "Asha Rao",
            "organization_id": str(uuid.uuid4()),
            "organization_name": "Rao Traders",
            "sid": f"sid-{secrets.token_hex(6)}",
        }
        person.update(overrides)
        return person

    def allow(self, organization_id, subject):
        return 200, {
            "decision": "allow", "reason": "entitled", "product_code": self.product_code,
            "organization_id": organization_id, "write_allowed": True, "revalidate_after": 900,
        }

    def deny(self, reason="not_entitled"):
        def answer(organization_id, subject):
            return 200, {
                "decision": "deny", "reason": reason, "product_code": self.product_code,
                "organization_id": organization_id, "write_allowed": False, "revalidate_after": 900,
            }
        return answer

    # --- the authorization endpoint (the browser's side, done in-process) ------------

    def authorize(self, authorization_url, person, *, tamper=None, error=""):
        """What WorkEasy360 sends the browser back with, for an authorization request.

        Checks the request as the platform does and returns the callback URL
        (``redirect_uri?code=..&state=..``, or ``?error=..``).
        """
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorization_url).query).items()}
        assert authorization_url.startswith(f"{self.issuer}/api/v1/integrations/oauth/authorize?"), authorization_url
        assert query.get("client_id") == self.client_id, query
        assert query.get("redirect_uri") == self.redirect_uri, query
        assert query.get("response_type") == "code", query
        assert query.get("code_challenge_method") == "S256", query
        assert len(query.get("code_challenge", "")) >= 43, query
        assert "openid" in query.get("scope", "").split(), query
        assert query.get("state") and query.get("nonce"), query
        back = {"state": query["state"]}
        if error:
            back["error"] = error
            return f"{self.redirect_uri}?{urlencode(back)}"
        code = secrets.token_urlsafe(32)
        organization_id = query.get("organization") or person.get("organization_id")
        with self.lock:
            self.codes[code] = {
                "redirect_uri": query["redirect_uri"],
                "challenge": query["code_challenge"],
                "nonce": query["nonce"],
                "person": dict(person, organization_id=organization_id),
                "tamper": dict(tamper or {}),
                "issued_at": time.time(),
            }
        back["code"] = code
        return f"{self.redirect_uri}?{urlencode(back)}"

    # --- tokens ------------------------------------------------------------------------

    def id_token(self, pending):
        person, tamper = pending["person"], pending["tamper"]
        now = int(time.time())
        claims = {
            "iss": tamper.get("issuer", self.issuer),
            "sub": person["sub"],
            "aud": tamper.get("audience", self.client_id),
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "jti": str(uuid.uuid4()),
            "typ": tamper.get("typ", "id_token"),
            "nonce": tamper.get("nonce", pending["nonce"]),
            "product_code": tamper.get("product_code", self.product_code),
            "email": person["email"],
            "email_verified": person["email_verified"],
            "name": person["name"],
            "aal": 1,
            "auth_time": now,
        }
        if person.get("organization_id"):
            claims["organization_id"] = person["organization_id"]
        if person.get("organization_name"):
            claims["organization_name"] = person["organization_name"]
        if person.get("sid"):
            claims["sid"] = person["sid"]
        if "organization_id" in tamper:
            claims["organization_id"] = tamper["organization_id"]
        for name in tamper.get("drop", ()):
            claims.pop(name, None)
        key = self.foreign_key if tamper.get("foreign_key") else self.signing_key
        return jwt.encode(claims, key, algorithm="ES256", headers={"kid": self.kid, "typ": "JWT"})

    def logout_token(self, *, sub=None, sid=None, issuer=None, audience=None, typ="logout+jwt",
                     event=True, nonce=None, foreign_key=False, jti=None, issued_at=None, lifetime=120):
        now = int(issued_at if issued_at is not None else time.time())
        claims = {
            "iss": issuer or self.issuer,
            "aud": audience or self.client_id,
            "iat": now,
            "exp": now + lifetime,
            "jti": jti or str(uuid.uuid4()),
            "typ": "logout_token",
        }
        if event:
            claims["events"] = {BACKCHANNEL_LOGOUT_EVENT: {}}
        if sub:
            claims["sub"] = sub
        if sid:
            claims["sid"] = sid
        if nonce:
            claims["nonce"] = nonce
        key = self.foreign_key if foreign_key else self.signing_key
        return jwt.encode(claims, key, algorithm="ES256", headers={"kid": self.kid, "typ": typ})

    # --- client authentication, as the platform does it --------------------------------

    def _authenticate_client(self, form):
        if form.get("client_assertion_type") != ASSERTION_TYPE:
            return None
        assertion = form.get("client_assertion", "")
        try:
            claims = jwt.decode(
                assertion, self.client_key.public_key(), algorithms=["ES256"],
                audience=self.token_endpoint, issuer=self.client_id,
                options={"require": ["iss", "sub", "aud", "exp", "iat", "jti"]},
            )
            header = jwt.get_unverified_header(assertion)
        except jwt.PyJWTError:
            return None
        if claims["sub"] != self.client_id or header.get("kid") != self.client_kid:
            return None
        if claims["exp"] - claims["iat"] > 60:
            return None
        with self.lock:
            if claims["jti"] in self.assertion_ids:
                return None
            self.assertion_ids.add(claims["jti"])
        return claims

    def _token(self, form):
        assertion = self._authenticate_client(form)
        if assertion is None:
            return 401, {"error": "invalid_client"}
        self.seen["token"].append({"form": form, "assertion": assertion})
        if form.get("grant_type") != "authorization_code":
            return 400, {"error": "unsupported_grant_type"}
        with self.lock:
            pending = self.codes.pop(form.get("code", ""), None)
        if pending is None or pending["redirect_uri"] != form.get("redirect_uri"):
            return 400, {"error": "invalid_grant"}
        if time.time() - pending["issued_at"] > 60:
            return 400, {"error": "invalid_grant"}
        verifier = form.get("code_verifier", "")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
        if challenge != pending["challenge"]:
            return 400, {"error": "invalid_grant"}
        return 200, {
            "access_token": secrets.token_urlsafe(16), "token_type": "Bearer", "expires_in": 900,
            "scope": "openid email profile organization", "id_token": self.id_token(pending),
        }

    def _entitlement(self, form):
        assertion = self._authenticate_client(form)
        if assertion is None:
            return 401, {"error": "invalid_client"}
        self.seen["entitlement"].append({"form": form, "assertion": assertion})
        return self.entitlement(form.get("organization_id", ""), form.get("subject") or None)

    def discovery(self):
        base = self.issuer
        return {
            "issuer": base,
            "authorization_endpoint": f"{base}/api/v1/integrations/oauth/authorize",
            "token_endpoint": self.token_endpoint,
            "jwks_uri": f"{base}/.well-known/jwks.json",
            "end_session_endpoint": f"{base}/api/v1/integrations/oauth/logout",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "id_token_signing_alg_values_supported": ["ES256", "RS256"],
            "token_endpoint_auth_methods_supported": ["private_key_jwt"],
            "subject_types_supported": ["public"],
            "scopes_supported": ["openid", "profile", "email", "organization"],
            "backchannel_logout_supported": True,
            "backchannel_logout_session_supported": True,
            "workeasy360_entitlement_endpoint": f"{base}/api/v1/integrations/entitlements/verify",
            "workeasy360_entitlement_revalidate_after": 900,
        }

    def _handler(self):
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet test output
                return

            def _send(self, status, body):
                payload = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/.well-known/openid-configuration":
                    return self._send(200, provider.discovery())
                if path == "/.well-known/jwks.json":
                    return self._send(200, {"keys": [_jwk(provider.signing_key.public_key(), provider.kid)]})
                return self._send(404, {"error": "not_found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                form = {k: v[0] for k, v in parse_qs(raw).items()}
                path = urlsplit(self.path).path
                if path == "/api/v1/integrations/oauth/token":
                    return self._send(*provider._token(form))
                if path == "/api/v1/integrations/entitlements/verify":
                    return self._send(*provider._entitlement(form))
                return self._send(404, {"error": "not_found"})

        return Handler
