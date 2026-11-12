# SPDX-License-Identifier: GPL-2.0-or-later
"""API token in GNOME Keyring, using the same item as the top bar extension.

These calls block on D-Bus, so call them from a worker thread.
"""

from ..const import SECRET_SCHEMA
from .config import normalize_site

_schema = None


def _secret():
    import gi
    gi.require_version('Secret', '1')
    from gi.repository import Secret
    return Secret


def _get_schema():
    global _schema
    if _schema is None:
        Secret = _secret()
        _schema = Secret.Schema.new(SECRET_SCHEMA, Secret.SchemaFlags.NONE, {
            'site': Secret.SchemaAttributeType.STRING,
            'email': Secret.SchemaAttributeType.STRING,
        })
    return _schema


def _attrs(site, email):
    return {'site': normalize_site(site), 'email': (email or '').strip()}


def lookup_token(site, email):
    return _secret().password_lookup_sync(_get_schema(), _attrs(site, email), None)


def store_token(site, email, token):
    attrs = _attrs(site, email)
    Secret = _secret()
    return Secret.password_store_sync(_get_schema(), attrs, Secret.COLLECTION_DEFAULT,
                                      f'Jira API token ({attrs["email"]} @ {attrs["site"]})', token, None)
