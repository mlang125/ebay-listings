"""Upload listing photos to eBay Picture Services via the Media API."""

import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid

from . import auth, config


def _media_base():
    # The Media API lives on the apim host, not the usual api host.
    return config.app()["api_base"].replace("://api.", "://apim.") + "/commerce/media/v1_beta"


def upload_image(path):
    """Upload one image file; returns its eBay-hosted URL."""
    boundary = uuid.uuid4().hex
    filename = os.path.basename(path)
    with open(path, "rb") as f:
        data = f.read()
    body = (
        ("--%s\r\n" % boundary).encode()
        + ('Content-Disposition: form-data; name="image"; filename="%s"\r\n' % filename).encode()
        + ("Content-Type: %s\r\n\r\n" % (mimetypes.guess_type(filename)[0] or "image/jpeg")).encode()
        + data
        + ("\r\n--%s--\r\n" % boundary).encode()
    )
    req = urllib.request.Request(
        _media_base() + "/image/create_image_from_file",
        data=body,
        headers={
            "Authorization": "Bearer " + auth.access_token(),
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            location = resp.headers.get("Location")
    except urllib.error.HTTPError as e:
        raise RuntimeError("Image upload failed for %s (%s): %s" % (filename, e.code, e.read().decode()))
    if raw:
        info = json.loads(raw)
        if info.get("imageUrl"):
            return info["imageUrl"]
    # Some responses only return a Location header; fetch the image record for its URL.
    req = urllib.request.Request(location, headers={"Authorization": "Bearer " + auth.access_token(), "Accept": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)["imageUrl"]
