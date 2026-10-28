import re

MASK = "********"

_SENSITIVE_NAME = re.compile(
    r"pass|secret|token|key|cred|auth|private|dsn|cookie|session|salt|signature|cert",
    re.IGNORECASE,
)
_URL_CREDENTIALS = re.compile(r"(?<=://)([^/\s:@]+):([^/\s@]+)@")


def is_sensitive(name: str) -> bool:
    return _SENSITIVE_NAME.search(name) is not None


def mask_url_credentials(text: str) -> str:
    return _URL_CREDENTIALS.sub(rf"\1:{MASK}@", text)


def mask_value(name: str, value: str) -> str:
    if is_sensitive(name):
        return MASK
    return mask_url_credentials(value)
