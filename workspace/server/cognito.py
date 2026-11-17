"""Verify the journal app's Cognito ID token (RS256) with the standard library: the workspace shares the journal's sign-in."""
import base64
import hashlib
import json
import threading
import time
import urllib.request

_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")   # PKCS#1 v1.5 prefix for SHA-256


def _b64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _int(s):
    return int.from_bytes(_b64(s), "big")


class Verifier:
    def __init__(self, region, pool_id, client_id, fetch=None):
        self.iss = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"
        self.client_id = client_id
        self.keys, self.loaded, self.lock = {}, 0, threading.Lock()
        self.fetch = fetch or (lambda url: json.loads(urllib.request.urlopen(url, timeout=10).read()))

    def _key(self, kid):
        with self.lock:
            if kid not in self.keys and time.time() - self.loaded > 60:
                self.keys = {k["kid"]: (_int(k["n"]), _int(k["e"])) for k in self.fetch(self.iss + "/.well-known/jwks.json")["keys"] if k.get("kty") == "RSA"}
                self.loaded = time.time()
            return self.keys.get(kid)

    def verify(self, token):
        """The token's claims, or None if it isn't a valid, unexpired ID token for this app."""
        try:
            h64, p64, s64 = token.split(".")
            head, claims = json.loads(_b64(h64)), json.loads(_b64(p64))
            if head.get("alg") != "RS256":
                return None
            key = self._key(head.get("kid"))
            if not key:
                return None
            n, e = key
            k = (n.bit_length() + 7) // 8
            em = pow(int.from_bytes(_b64(s64), "big"), e, n).to_bytes(k, "big")
            t = _DIGEST_INFO + hashlib.sha256(f"{h64}.{p64}".encode()).digest()
            if em != b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t:
                return None
            if claims.get("iss") != self.iss or claims.get("aud") != self.client_id or claims.get("token_use") != "id":
                return None
            if claims.get("exp", 0) < time.time() - 30:
                return None
            return claims
        except Exception:
            return None
