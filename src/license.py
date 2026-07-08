import json
import logging
import hashlib
import platform
import socket
import uuid

import requests

from .config import LICENSE_VALIDATE_URL, PRODUCT_CODE, VERSION

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15


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
        """The activation payload sent to the ORBAS endpoint. Field names mirror
        the licence-activation form (License Key, Email, Product Code, Device ID,
        Device Name, Application Version)."""
        return {
            "license_key": (license_key or "").strip(),
            "email": (email or "").strip(),
            "product_code": PRODUCT_CODE,
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
                return {
                    "valid": valid,
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
