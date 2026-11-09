"""Read and write secrets in the macOS login Keychain via the `security` CLI."""

import subprocess

SERVICE = "ebay-listings"


def get(account):
    result = subprocess.run(
        ["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def put(account, secret):
    # -U updates the item if it already exists.
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", SERVICE, "-a", account, "-w", secret],
        check=True,
        capture_output=True,
    )
