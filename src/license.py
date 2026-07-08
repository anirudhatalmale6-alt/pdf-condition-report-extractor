import os
import json
import logging
import hashlib
import platform
import socket
import uuid

import requests

from .config import APP_NAME, LICENSE_VALIDATE_URL, VERSION

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Activation storage
# ---------------------------------------------------------------------------
# After a successful activation the licence key + email are saved locally so the
# app can silently re-validate on every launch (device + subscription binding is
# enforced server-side via device_id). Only the key and email are stored - no
# "valid" flag is ever cached, so a machine can never self-authorise offline.

def get_data_dir():
    """Per-user, writable directory for ORBAS app data."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config"
        )
    d = os.path.join(base, APP_NAME)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.expanduser("~")
    return d


def _store_path():
    return os.path.join(get_data_dir(), "activation.json")


def save_activation(license_key, email):
    """Persist the activated key + email for silent re-validation next launch."""
    try:
        with open(_store_path(), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "license_key": (license_key or "").strip(),
                    "email": (email or "").strip(),
                    "device_id": get_device_id(),
                },
                f,
            )
        return True
    except Exception as e:
        logger.warning("Could not save activation: %s", e)
        return False


def load_activation():
    """Return the saved {license_key, email} dict, or None if not activated."""
    try:
        with open(_store_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("license_key"):
            return data
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("Could not read activation: %s", e)
    return None


def clear_activation():
    """Remove the stored activation (e.g. licence revoked / device changed)."""
    try:
        os.remove(_store_path())
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Could not clear activation: %s", e)


def get_device_id():
    """A stable, non-reversible identifier for this machine.

    Derived from the hardware MAC (uuid.getnode) plus the host/OS name and
    hashed, so the same machine always produces the same id but no personal
    information is exposed. Auto-generated - the user never types this.
    """
    try:
        raw = "{}-{}-{}".format(uuid.getnode(), platform.node(), platform.system())
    except Exception:
        raw = platform.node() or "unknown-device"
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:32]


def get_device_name():
    """Human-readable machine name, auto-detected."""
    try:
        return platform.node() or socket.gethostname() or "Unknown-PC"
    except Exception:
        return "Unknown-PC"


class LicenseValidator:
    def __init__(self, endpoint_url=None, timeout=DEFAULT_TIMEOUT):
        self.endpoint_url = endpoint_url or LICENSE_VALIDATE_URL
        self.timeout = timeout

    def build_payload(self, license_key, email=None):
        """The activation payload sent to the ORBAS endpoint: License Key, Email,
        Device ID, Device Name, Application Version. The licence type (Trial /
        Subscription) is determined server-side from the key and returned in the
        response - the app does not send it."""
        return {
            "license_key": (license_key or "").strip(),
            "email": (email or "").strip(),
            "device_id": get_device_id(),
            "device_name": get_device_name(),
            "app_version": VERSION,
        }

    def validate(self, license_key, email=None):
        if not license_key or not license_key.strip():
            return {"valid": False, "error": "License key is required"}

        payload = self.build_payload(license_key, email)

        # A named User-Agent is required: the ORBAS endpoint sits behind a WAF
        # that blocks the default python-requests UA with a 403, so every
        # activation must identify itself explicitly.
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ORBAS-Extractor/{}".format(VERSION),
        }

        try:
            response = requests.post(
                self.endpoint_url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )

            if response.status_code == 200:
                data = response.json()
                # Accept a couple of common truthy shapes so we stay compatible
                # with the final endpoint contract (valid / active / success).
                valid = bool(
                    data.get("valid", data.get("active", data.get("success", False)))
                )
                # Licence type (Trial / Subscription) is returned by the server.
                license_type = (
                    data.get("license_type")
                    or data.get("licence_type")
                    or data.get("type")
                    or ""
                )
                return {
                    "valid": valid,
                    "license_type": license_type,
                    "reason": data.get("reason", ""),
                    "message": data.get("message", ""),
                    "error": None if valid else (data.get("message") or data.get("error") or "Invalid product key."),
                }

            if response.status_code in (401, 403):
                return {"valid": False, "error": "Product key was not accepted. Please check the key and email."}

            return {
                "valid": False,
                "error": f"Server returned status {response.status_code}",
            }

        except requests.exceptions.ConnectionError:
            return {"valid": False, "error": "Cannot connect to license server. Check your internet connection."}
        except requests.exceptions.Timeout:
            return {"valid": False, "error": "License server timed out. Please try again."}
        except requests.exceptions.RequestException as e:
            return {"valid": False, "error": f"License validation error: {str(e)[:200]}"}
        except (json.JSONDecodeError, ValueError):
            return {"valid": False, "error": "Invalid response from license server."}


def validate_license(license_key, email=None, endpoint_url=None):
    validator = LicenseValidator(endpoint_url=endpoint_url)
    return validator.validate(license_key, email=email)
