"""
Multi-user login store. Passwords are hashed with werkzeug's
generate_password_hash (same algorithm the original script used for its
one admin account) and kept in users.json, not in .env — so adding a
user for a family member or employee doesn't mean editing secrets you
also use for email/Twilio.

The ADMIN_USERNAME / ADMIN_PASSWORD in .env still work as a bootstrap
account even after users.json exists, so you can't lock yourself out by
deleting the wrong user.
"""
import os
import json
import threading
from werkzeug.security import generate_password_hash, check_password_hash

import config as cfg

USERS_PATH = os.path.join(cfg.BASE_DIR, "users.json")
_lock = threading.Lock()


def _load():
    with _lock:
        if not os.path.exists(USERS_PATH):
            return {}
        with open(USERS_PATH, "r") as f:
            return json.load(f)


def _save(users):
    with _lock:
        with open(USERS_PATH, "w") as f:
            json.dump(users, f, indent=2)


def list_usernames():
    names = list(_load().keys())
    if cfg.ADMIN_USERNAME not in names:
        names.append(f"{cfg.ADMIN_USERNAME} (from .env)")
    return names


def add_user(username, password):
    if not username or not password:
        raise ValueError("Username and password are both required.")
    users = _load()
    users[username] = generate_password_hash(password)
    _save(users)


def remove_user(username):
    users = _load()
    if username in users:
        del users[username]
        _save(users)


def verify(username, password):
    """Checks users.json first, then falls back to the .env bootstrap
    admin account. Returns the username on success, None on failure."""
    users = _load()
    if username in users and check_password_hash(users[username], password):
        return username
    if username == cfg.ADMIN_USERNAME and password == cfg.ADMIN_PASSWORD:
        return username
    return None
