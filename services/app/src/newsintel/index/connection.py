"""Pixeltable connection setup.

Pixeltable reads its configuration from environment variables when first used, so we translate our
Settings into those variables *before* importing pixeltable anywhere in the process:

* PIXELTABLE_DB_CONNECT_STR → shared Postgres (pgvector) instead of the embedded per-user server (ADR-003)
* PIXELTABLE_HOME           → local file cache only; disposable
* AWS_ENDPOINT_URL_S3 + keys → lets Pixeltable read `s3://` media from MinIO (ADR-002, tested)
"""

import os
import threading

from newsintel.config import Settings

_lock = threading.Lock()
_configured = False


def configure_pixeltable(settings: Settings) -> None:
    global _configured
    with _lock:
        if _configured:
            return
        os.environ["PIXELTABLE_DB_CONNECT_STR"] = settings.pixeltable_db_url
        os.environ["PIXELTABLE_HOME"] = settings.pixeltable_home
        os.environ.setdefault("PIXELTABLE_VERBOSITY", "0")
        os.environ["AWS_ENDPOINT_URL_S3"] = settings.s3_endpoint_url
        os.environ["AWS_ACCESS_KEY_ID"] = settings.s3_access_key
        os.environ["AWS_SECRET_ACCESS_KEY"] = settings.s3_secret_key.get_secret_value()
        os.environ["AWS_DEFAULT_REGION"] = settings.s3_region
        import pixeltable as pxt

        pxt.init()
        _configured = True
