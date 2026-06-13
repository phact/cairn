"""
A bounded Python "request flow" — an OAuth-middleware-style sample. Pure stdlib,
deterministic, no I/O beyond the in-process call tree, so it's a clean capture
target.

Shape mirrors a typical request flow on purpose (so the ranker faces the same
problems):
  handle -> strip_spoofed -> validate_jwt -> {decode_header, decode_payload,
  verify} -> inject_identity -> forward
with branches (valid / expired / missing), nested values (dict claims, list
headers), helpers hit many times (frequency), and fan-out joints.
"""

import base64
import json


# --- a tiny JWT-ish helper set (no crypto; structure is what matters) --------
def b64url_decode(seg):
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def decode_header(token):
    header_b64 = token.split(".")[0]
    return json.loads(b64url_decode(header_b64))


def decode_payload(token):
    payload_b64 = token.split(".")[1]
    return json.loads(b64url_decode(payload_b64))


def verify_signature(token, key):
    # structural stand-in: a "valid" token's 3rd segment equals "sig-" + key
    sig = token.split(".")[2]
    return sig == "sig-" + key


EXPECTED_ALG = "EdDSA"


def validate_jwt(token, key, now):
    header = decode_header(token)
    if header.get("alg") != EXPECTED_ALG:          # branch: bad alg
        return None
    payload = decode_payload(token)
    if payload.get("exp", 0) < now:                # branch: expired
        return None
    if not verify_signature(token, key):           # branch: bad sig
        return None
    return payload                                  # the claims


# --- the request pipeline ----------------------------------------------------
SPOOFABLE = {"x-user", "x-auth-user"}


def strip_spoofed(headers):
    return [(k, v) for (k, v) in headers if k.lower() not in SPOOFABLE]


def inject_identity(headers, claims):
    headers.append(("x-auth-user", claims["sub"]))
    headers.append(("x-auth-email", claims["email"]))
    return headers


def forward(req):
    # the "upstream": echo a 200 with the headers the upstream would see
    return {"status": 200, "echoed_headers": dict(req["headers"])}


def handle(req, key, now):
    req["headers"] = strip_spoofed(req["headers"])
    token = dict(req["headers"]).get("authorization", "").removeprefix("Bearer ")
    claims = validate_jwt(token, key, now)
    if claims is None:                              # branch: unauthorized
        return {"status": 401, "headers": {"auth-required": "true"}}
    req["headers"] = inject_identity(req["headers"], claims)
    return forward(req)


def mint(alg, exp, sub, key):
    header = {"alg": alg, "kid": "test-key-1", "typ": "JWT"}
    payload = {"iss": "https://oauth.example.test", "aud": "this-box",
               "sub": sub, "email": sub + "@example.com", "exp": exp}

    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return seg(header) + "." + seg(payload) + ".sig-" + key


def scenario_valid_jwt():
    key = "k-2024"
    now = 1_000_000
    token = mint(EXPECTED_ALG, now + 600, "gh:42", key)
    req = {"method": "GET", "path": "/probe",
           "headers": [("host", "echo.example.test"),
                       ("x-auth-user", "SPOOFED"),
                       ("authorization", "Bearer " + token)]}
    return handle(req, key, now)


if __name__ == "__main__":
    print(scenario_valid_jwt())
