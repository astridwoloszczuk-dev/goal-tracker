#!/usr/bin/env python3
"""
garmin_token_setup.py
Run this ONCE locally to authenticate with Garmin (including MFA).
It saves the OAuth tokens and prints a JSON string to copy into
your GitHub repository secret named GARMIN_TOKENS.

Usage:
    pip install garminconnect
    python garmin_token_setup.py
"""

import getpass
import json
import os
import tempfile

try:
    from garminconnect import Garmin
except ImportError:
    print("ERROR: garminconnect not installed. Run: pip install garminconnect")
    exit(1)

email    = input("Garmin email: ")
password = getpass.getpass("Garmin password: ")

def prompt_mfa():
    return input("MFA code (check email or authenticator app): ")

print("\nLogging in to Garmin Connect...")
tmpdir = tempfile.mkdtemp()
try:
    client = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
    client.login(tokenstore=tmpdir)
    print("Login successful.\n")
except Exception as e:
    print(f"Login failed: {e}")
    exit(1)

token_files = {}
for fname in os.listdir(tmpdir):
    fpath = os.path.join(tmpdir, fname)
    if os.path.isfile(fpath):
        with open(fpath, "r", encoding="utf-8") as f:
            token_files[fname] = f.read()

token_json = json.dumps(token_files)

print("=" * 60)
print("Copy the following string as a GitHub Actions secret")
print("Secret name: GARMIN_TOKENS")
print("=" * 60)
print(token_json)
print("=" * 60)
print("\nDone. Tokens are valid for ~90 days.")
print("Re-run this script when the GitHub Action starts failing with auth errors.")
