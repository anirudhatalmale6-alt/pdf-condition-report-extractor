import os
import re
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


# ---------------------------------------------------------------------------
# Subscription enforcement
# ---------------------------------------------------------------------------
# Rule (from client): a SUBSCRIPTION licence may only be used while the
# subscription is active - a valid key alone is NOT enough. A TRIAL licence is
# exempt from this check (it is governed by its own trial validity, not by an
# active subscription).

_INACTIVE_SUB_REASONS = {
    "SUBSCRIPTION_INACTIVE", "SUBSCRIPTION_EXPIRED", "INACTIVE_SUBSCRIPTION",
    "NO_ACTIVE_SUBSCRIPTION", "SUBSCRIPTION_NOT_ACTIVE", "SUBSCRIPTION_CANCELLED",
}


def is_trial(license_type):
    # Server sends free-form types like "TRIAL" / "FREE TRIAL"; match on token.
    return "trial" in str(license_type or "").strip().lower()


def is_subscription(license_type):
    # Server sends "PAID SUBSCRIPTION", "SUBSCRIPTION", "PREMIUM" etc. - match on
    # any subscription-ish token (but a trial is never a subscription).
    s = str(license_type or "").strip().lower()
    if is_trial(s):
        return False
    return any(tok in s for tok in ("subscription", "paid", "premium"))


def _to_int(v, default=None):
    """Coerce a value that may be a string ("3") or int (0) to int."""
    try:
        if v is None or v == "":
            return default
        return int(str(v).strip())
    except (ValueError, TypeError):
        return default


def _to_bool_flag(v):
    """Interpret a Yes/No/true/1 style flag. Returns None if not present."""
    if v is None:
        return None
    return str(v).strip().lower() in ("yes", "true", "1", "y", "active", "validated")


def device_usage_text(result):
    """A short 'Device X of Y' string from a validation result, or '' if unknown."""
    md = result.get("max_devices")
    ad = result.get("activated_devices")
    if isinstance(md, int) and isinstance(ad, int) and md > 0:
        return "Device {} of {}".format(ad, md)
    return ""


_ACTIVE_SUB_VALUES = {"active", "current", "valid", "trialing"}
_INACTIVE_SUB_VALUES = {
    "inactive", "expired", "cancelled", "canceled", "past_due",
    "suspended", "none", "lapsed", "unpaid",
}


def subscription_is_active(data):
    """Best-effort read of subscription-plan status from a validation response.

    The ORBAS server reports a subscription plan status of "Active" / "Inactive".
    Returns True/False when the server states it, or None when the response says
    nothing about subscription status (in which case we defer to the server's
    overall `valid` verdict).
    """
    # Explicit boolean flags.
    for k in ("subscription_active", "subscriptionActive",
              "is_subscription_active", "active_subscription"):
        if k in data and isinstance(data[k], bool):
            return data[k]

    # Named string status fields (values like "Active" / "Inactive" / "in-active").
    # Normalise by stripping non-letters so "in-active", "in_active", "in active"
    # all collapse to "inactive".
    def _norm(v):
        return re.sub(r"[^a-z]", "", str(v).lower())

    for k in ("subscription_status", "subscription_plan_status", "plan_status",
              "subscriptionStatus", "subscriptionPlanStatus", "subscription", "status"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            s = _norm(v)
            if s in _ACTIVE_SUB_VALUES:
                return True
            if s in _INACTIVE_SUB_VALUES:
                return False

    # Fallback: any key that mentions subscription/plan + status.
    for k, v in data.items():
        kl = str(k).lower()
        if isinstance(v, str) and "status" in kl and ("subscription" in kl or "plan" in kl):
            s = _norm(v)
            if s in _ACTIVE_SUB_VALUES:
                return True
            if s in _INACTIVE_SUB_VALUES:
                return False

    reason = str(data.get("reason") or "").strip().upper()
    if reason in _INACTIVE_SUB_REASONS:
        return False
    return None


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

    def _interpret(self, data):
        """Turn a 200 response body into the app's normalised licence result.

        Kept separate from the network call so it can be unit-tested against the
        exact JSON the ORBAS endpoint returns.
        """
        # Accept a couple of common truthy shapes so we stay compatible with the
        # final endpoint contract (valid / active / success).
        valid = bool(
            data.get("valid", data.get("active", data.get("success", False)))
        )
        # The success response nests licence details under a "license" object
        # (license_type, status, subscription_status, expires_at, max_devices,
        # activated_devices, is_validated). Flatten it so lookups work either way.
        lic = data.get("license") if isinstance(data.get("license"), dict) else {}
        fields = {**lic, **{k: v for k, v in data.items() if k != "license"}}
        # Licence type (Trial / Subscription) is returned by the server.
        license_type = (
            fields.get("license_type")
            or fields.get("licence_type")
            or fields.get("type")
            or ""
        )
        max_devices = _to_int(fields.get("max_devices"))
        activated_devices = _to_int(fields.get("activated_devices"))
        is_validated = _to_bool_flag(fields.get("is_validated"))
        result = {
            "valid": valid,
            "license_type": license_type,
            "status": fields.get("status", ""),
            "subscription_status": fields.get("subscription_status", ""),
            "expires_at": fields.get("expires_at", ""),
            "max_devices": max_devices,
            "activated_devices": activated_devices,
            "is_validated": is_validated,
            "reason": data.get("reason", ""),
            "message": data.get("message", ""),
            "error": None if valid else (data.get("message") or data.get("error") or "Invalid product key."),
        }
        if not valid:
            return result

        # Key is valid. For a SUBSCRIPTION licence, additionally require an active
        # subscription - a valid key is not enough. TRIAL is exempt.
        if is_subscription(license_type):
            active = subscription_is_active(fields)
            if active is False:
                result["valid"] = False
                # Server returned valid:true here, so its `message` is a success
                # line - use our own rejection text instead.
                result["error"] = (
                    "Your subscription is not active. Please renew your subscription to use the extractor."
                )
                return result

        # Device-limit enforcement. The server is the source of truth (it either
        # registers this device or refuses). This is a clear, friendly message
        # for the case where the server did NOT validate this device because the
        # plan's device allowance is already fully used on other machines.
        if is_validated is False and max_devices is not None and \
                activated_devices is not None and activated_devices >= max_devices:
            result["valid"] = False
            result["reason"] = result.get("reason") or "DEVICE_LIMIT_REACHED"
            result["error"] = (
                "You have reached the maximum number of registered devices allowed "
                "under your subscription ({0} of {0} in use). Please remove an "
                "existing device, upgrade your subscription, or contact the ORBAS "
                "team.".format(max_devices)
            )
        return result

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
                return self._interpret(response.json())

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
