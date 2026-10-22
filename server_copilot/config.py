from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SERVER_COPILOT_", env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8000
    docker_host: str = "tcp://docker-socket-proxy:2375"
    host_disks: str = ""
    auth_token: SecretStr | None = None
    auth_disabled: bool = False
    # Public https origin of this server (https://server-copilot.example.com, no path).
    public_url: str | None = None
    # AuthKit domain of the WorkOS project (https://<app>.authkit.app). Together with
    # ``public_url`` it turns OAuth sign-in on for /mcp.
    authkit_domain: str | None = None
    # Comma-separated verified emails allowed to sign in. Empty allows nobody.
    oauth_allowed_emails: str = ""

    @property
    def oauth_requested(self) -> bool:
        return bool(self.authkit_domain or self.public_url)

    def is_oauth_email_allowed(self, email: str) -> bool:
        allowed = {e.strip().lower() for e in self.oauth_allowed_emails.split(",") if e.strip()}
        return email.strip().lower() in allowed


def get_settings() -> Settings:
    return Settings()
