import json
import time
import logging
import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 2


class CloudSync:
    def __init__(self, endpoint_url, api_key=None, timeout=DEFAULT_TIMEOUT,
                 max_retries=DEFAULT_MAX_RETRIES, retry_delay=DEFAULT_RETRY_DELAY):
        self.endpoint_url = endpoint_url
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    def sync(self, data, on_progress=None):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = json.dumps(data, ensure_ascii=False)

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                if on_progress:
                    on_progress(f"Upload attempt {attempt}/{self.max_retries}...")

                response = requests.post(
                    self.endpoint_url,
                    data=payload.encode("utf-8"),
                    headers=headers,
                    timeout=self.timeout,
                )

                if response.status_code in (200, 201, 202):
                    logger.info(f"Cloud sync successful (status {response.status_code})")
                    return {
                        "success": True,
                        "status_code": response.status_code,
                        "response": response.text,
                        "attempts": attempt,
                    }

                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning(f"Cloud sync attempt {attempt} failed: {last_error}")

            except requests.exceptions.Timeout:
                last_error = f"Request timed out after {self.timeout}s"
                logger.warning(f"Cloud sync attempt {attempt}: {last_error}")
            except requests.exceptions.ConnectionError as e:
                last_error = f"Connection error: {str(e)[:200]}"
                logger.warning(f"Cloud sync attempt {attempt}: {last_error}")
            except requests.exceptions.RequestException as e:
                last_error = f"Request error: {str(e)[:200]}"
                logger.warning(f"Cloud sync attempt {attempt}: {last_error}")

            if attempt < self.max_retries:
                delay = self.retry_delay * (2 ** (attempt - 1))
                if on_progress:
                    on_progress(f"Retrying in {delay}s...")
                time.sleep(delay)

        logger.error(f"Cloud sync failed after {self.max_retries} attempts: {last_error}")
        return {
            "success": False,
            "error": last_error,
            "attempts": self.max_retries,
        }
