#!/usr/bin/env python3
"""List the credentials opencode has stored for itself (`opencode auth login`).

Usage: _opencode_stored_credentials.py OPENCODE_DATA_DIR

Prints one tab-separated line per stored credential:

    <integration>  <method>  <aws profile, or empty>

e.g. "amazon-bedrock\taws-profile\tbedrock-sso". Secrets are never printed: only
the integration, the login method and, for an AWS-profile login, the profile
name are read out of each entry.

Why this matters: a stored account takes precedence over the provider settings
in opencode's config files. A stored Amazon Bedrock account therefore overrides
the `profile` in the config `mqbedrock --setup-opencode` writes — opencode then
signs with whatever profile the account names (typically the SSO one, which
fails once its hour-long login token expires) and never reads the static
profile mqbedrock keeps refreshed. Both mqbedrock and mqyolo use this to say so,
since nothing in opencode's own output points at the stored account.

Two on-disk formats are read, both best-effort:
  - opencode.db (2.x): the `credential` table, whose `value` column is JSON like
    {"type": "external", "methodID": "aws-profile", "metadata": {"profile": "..."}}.
    Opened read-only, so a running opencode server is never disturbed.
  - auth.json (1.x): an object keyed by provider id, e.g. {"anthropic": {"type": "api", ...}}.
These are opencode internals, not a documented interface: anything missing or
unrecognised is skipped silently rather than reported, so a future format
change degrades to "no warning", never to a failing mqbedrock or mqyolo.
"""

import json
import os
import sqlite3
import sys
import urllib.parse


def _entry(integration, value):
    if not isinstance(value, dict):
        return None
    method = value.get("methodID") or value.get("type") or ""
    metadata = value.get("metadata")
    profile = metadata.get("profile") if isinstance(metadata, dict) else None
    if not isinstance(profile, str):
        profile = ""
    return (str(integration), str(method), profile.strip())


def from_db(path):
    if not os.path.isfile(path):
        return []
    uri = "file:{}?mode=ro".format(urllib.parse.quote(path))
    try:
        con = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            rows = con.execute(
                "SELECT COALESCE(integration_id, connector_id), value FROM credential"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    out = []
    for integration, value in rows:
        if not integration:
            continue
        try:
            entry = _entry(integration, json.loads(value))
        except (TypeError, ValueError):
            continue
        if entry:
            out.append(entry)
    return out


def from_auth_json(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return [e for e in (_entry(k, v) for k, v in data.items()) if e]


def main(argv):
    if len(argv) != 2:
        sys.stderr.write(__doc__.split("\n\n")[1] + "\n")
        return 2
    data_dir = argv[1]
    entries = from_db(os.path.join(data_dir, "opencode.db"))
    entries += from_auth_json(os.path.join(data_dir, "auth.json"))
    for entry in entries:
        print("\t".join(field.replace("\t", " ").replace("\n", " ") for field in entry))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
