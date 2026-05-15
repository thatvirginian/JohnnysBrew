# -*- coding: utf-8 -*-

import os
import json
import time
import requests
import shutil
from src.utils import get_env

# Absolute base directory ensures token file is always in project folder
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, "token_store.json")


class ToastAPI:
    def __init__(self):
        # Load configuration from environment
        self.base_url = get_env("TOAST_API_HOST")
        self.client_id = get_env("TOAST_CLIENT_ID")
        self.client_secret = get_env("TOAST_CLIENT_SECRET")

        self.token = None
        self.token_expiry = 0

        # Load cached token if available
        self._load_token_from_file()


    def _load_token_from_file(self):
        """Load cached token from local JSON file if available."""
        if os.path.exists(TOKEN_FILE):
            try:
                with open(TOKEN_FILE, "r") as f:
                    data = json.load(f)
                    self.token = data.get("access_token")
                    self.token_expiry = data.get("expires_at", 0)
            except Exception as e:
                print(f"Warning: failed to read token file: {e}")

    def _save_token_to_file(self):
        """Save the current token and expiry time to JSON file safely."""
        dir_path = os.path.dirname(TOKEN_FILE)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

        tmp_file = TOKEN_FILE + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump({
                "access_token": self.token,
                "expires_at": self.token_expiry,
                "created_at": time.time(),
                "client_id": self.client_id
            }, f, indent=2)
        # Atomic replacement
        shutil.move(tmp_file, TOKEN_FILE)

    def _authenticate(self):
        """Fetch access token from Toast."""
        print("Authenticating with Toast API...")

        url = f"{self.base_url}/authentication/v1/authentication/login"
        payload = {
            "clientId": self.client_id,
            "clientSecret": self.client_secret,
            "userAccessType": "TOAST_MACHINE_CLIENT"
        }

        headers = {"Content-Type": "application/json"}
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()

        data = response.json()
        token_data = data.get("token", {})
        self.token = token_data.get("accessToken")
        expires_in = token_data.get("expiresIn", 3600)

        if not self.token:
            raise Exception("No access token returned from Toast API.")

        # Save expiry time with a 1-minute buffer
        self.token_expiry = time.time() + expires_in - 60
        self._save_token_to_file()

        print("Token saved and ready to use.")
        return self.token

    def get_token(self):
        """Return a valid token, authenticating only if expired."""
        if not self.token or time.time() >= self.token_expiry:
            return self._authenticate()
        return self.token

    def get_headers(self):
        """Return authorization headers for API requests."""
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "Content-Type": "application/json"
        }
