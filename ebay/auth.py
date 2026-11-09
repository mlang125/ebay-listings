"""eBay OAuth: one-time browser login, then refresh-token -> access-token on each run."""

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, keychain


def _cert_account():
    return config.ENV + "-cert-id"


def _refresh_account():
    return config.ENV + "-refresh-token"


def _basic_auth_header():
    cert_id = keychain.get(_cert_account())
    if not cert_id:
        raise SystemExit(
            "No Cert ID in the Keychain for %s. Store it with:\n"
            "  security add-generic-password -s %s -a %s -w\n"
            "(it will prompt for the value)" % (config.ENV, keychain.SERVICE, _cert_account())
        )
    raw = "%s:%s" % (config.app()["client_id"], cert_id)
    return "Basic " + base64.b64encode(raw.encode()).decode()


def _token_request(form):
    req = urllib.request.Request(
        config.app()["api_base"] + "/identity/v1/oauth2/token",
        data=urllib.parse.urlencode(form).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": _basic_auth_header(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise SystemExit("Token request failed (%s): %s" % (e.code, e.read().decode()))


def consent_url():
    params = {
        "client_id": config.app()["client_id"],
        "redirect_uri": config.app()["ru_name"],
        "response_type": "code",
        "scope": " ".join(config.SCOPES),
    }
    return config.app()["auth_url"] + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def login(pasted):
    """Exchange the code from the redirect URL (or the bare code) for tokens."""
    code = pasted.strip()
    if "code=" in code:
        query = urllib.parse.urlparse(code).query
        code = urllib.parse.parse_qs(query)["code"][0]
    tokens = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.app()["ru_name"],
        }
    )
    keychain.put(_refresh_account(), tokens["refresh_token"])
    days = tokens.get("refresh_token_expires_in", 0) // 86400
    return days


_cache = {"token": None, "expires": 0}


def access_token():
    if _cache["token"] and time.time() < _cache["expires"] - 60:
        return _cache["token"]
    refresh = keychain.get(_refresh_account())
    if not refresh:
        raise SystemExit("Not logged in for %s. Run: python3 cli.py login" % config.ENV)
    tokens = _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "scope": " ".join(config.SCOPES),
        }
    )
    _cache["token"] = tokens["access_token"]
    _cache["expires"] = time.time() + tokens["expires_in"]
    return _cache["token"]
