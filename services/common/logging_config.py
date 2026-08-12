"""Uniform logging setup.

Every service emits records in the same shape so that logs from the whole compose stack
can be read — and later shipped — as one stream.
"""

import logging

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def configure_logging(service_name: str) -> logging.Logger:
    """Initialise root logging and return the logger this service should use."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    return logging.getLogger(service_name)
