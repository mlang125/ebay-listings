"""Minimal JSON client for eBay's REST APIs."""

import json
import urllib.error
import urllib.request

from . import auth, config


class EbayError(Exception):
    def __init__(self, status, body):
        super().__init__("eBay API error %s: %s" % (status, body))
        self.status = status
        self.body = body


class ReadOnly(EbayError):
    def __init__(self, what):
        Exception.__init__(self, "EBAY_READ_ONLY is set in .env, so %s was not sent to eBay." % what)
        self.status = None
        self.body = None


def check_writable(what):
    if config.READ_ONLY:
        raise ReadOnly(what)


def call(method, path, body=None, headers=None):
    if method != "GET":
        check_writable("%s %s" % (method, path.split("?")[0]))
    all_headers = {
        "Authorization": "Bearer " + auth.access_token(),
        "Accept": "application/json",
        "Content-Language": "en-US",
        "X-EBAY-C-MARKETPLACE-ID": config.MARKETPLACE_ID,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        all_headers["Content-Type"] = "application/json"
    all_headers.update(headers or {})
    req = urllib.request.Request(
        config.app()["api_base"] + path, data=data, headers=all_headers, method=method
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise EbayError(e.code, e.read().decode())
