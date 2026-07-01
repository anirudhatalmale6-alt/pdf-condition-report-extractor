import json
import logging
import requests

from .config import LICENSE_VALIDATE_URL

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15


class LicenseValidator:
    def __init__(self, endpoint_url=None, timeout=DEFAULT_TIMEOUT):
        self.endpoint_url = endpoint_url or LICENSE_VALIDATE_URL
        self.timeout = timeout

    def validate(self, license_key):
        if not license_key or not license_key.strip():
            return {"valid": False, "error": "License key is required"}

        try:
            response = requests.post(
                self.endpoint_url,
                json={"license_key": license_key.strip()},
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )

            if response.status_code == 200:
                data = response.json()
                return {
                    "valid": data.get("valid", False),
                    "message": data.get("message", ""),
                    "error": None,
                }

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


def validate_license(license_key, endpoint_url=None):
    validator = LicenseValidator(endpoint_url=endpoint_url)
    return validator.validate(license_key)
