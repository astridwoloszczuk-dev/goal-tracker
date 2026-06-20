#!/usr/bin/env python3
"""
garmin_token_setup.py
Run this ONCE on James to authenticate with Garmin (including MFA).
Saves OAuth tokens to ~/.garth — exactly where garmin_sync.py reads them.
Garmin stops flagging logins from James's stable residential IP, so the
tokens persist and auto-refresh (no more re-auth-every-few-days like CI).

Usage (on James):
    ~/Code/goal-tracker/.venv/bin/python ~/Code/goal-tracker/garmin_token_setup.py
"""

import getpass
import os

try:
    from garminconnect import Garmin
except ImportError:
    print("ERROR: garminconnect not installed in this venv.")
    exit(1)

GARTH_DIR = os.path.expanduser("~/.garth")
os.makedirs(GARTH_DIR, exist_ok=True)

email    = input("Garmin email: ")
password = getpass.getpass("Garmin password: ")

def prompt_mfa():
    return input("MFA code (check email or authenticator app): ")

print("\nLogging in to Garmin Connect...")
try:
    client = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
    client.login(tokenstore=GARTH_DIR)
    print("Login successful.\n")
except Exception as e:
    print(f"Login failed: {e}")
    exit(1)

# Sanity check: confirm the sync can read tokens back
try:
    check = Garmin()
    check.login(tokenstore=GARTH_DIR)
    print(f"✓ Tokens saved to {GARTH_DIR} and verified — garmin_sync.py will use them.")
    print("  Valid ~90 days; James's residential IP keeps them refreshing.")
except Exception as e:
    print(f"WARNING: tokens saved but re-read check failed: {e}")
