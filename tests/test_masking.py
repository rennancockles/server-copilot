import pytest

from server_copilot.masking import MASK, is_sensitive, mask_value

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "name",
    ["DB_PASSWORD", "api_token", "SECRET_KEY", "AWS_ACCESS_KEY_ID", "AUTH_HEADER", "MYSQL_PASS"],
)
def test_sensitive_names(name: str) -> None:
    assert is_sensitive(name)
    assert mask_value(name, "hunter2") == MASK


@pytest.mark.parametrize("name", ["PATH", "TZ", "LANG", "PORT", "HOSTNAME"])
def test_plain_names_are_kept(name: str) -> None:
    assert not is_sensitive(name)
    assert mask_value(name, "value") == "value"


def test_url_credentials_are_masked() -> None:
    value = mask_value("DATABASE_URL", "postgres://admin:s3cr3t@db:5432/app")
    assert value == f"postgres://admin:{MASK}@db:5432/app"


def test_url_without_credentials_is_kept() -> None:
    assert mask_value("UPSTREAM", "http://app:8000/path") == "http://app:8000/path"
