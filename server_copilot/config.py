from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SERVER_COPILOT_", env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8000
    docker_host: str = "tcp://docker-socket-proxy:2375"
    host_disks: str = ""
    # Directories whose text files (.md, .txt) read_text_file may return: name=/container/path,
    # comma separated, each bind-mounted read-only. Empty disables the tool.
    readable_dirs: str = ""
    # JSON file the host writes about its backups (bind-mount its folder read-only). Empty
    # disables get_backup_status, which then answers "unknown".
    backup_status_file: str = ""
    # JSON file the LAN scanner service writes and the network tools read. Empty disables the
    # tools, which then answer "unknown".
    network_scan_file: str = ""
    # Comma-separated private IPv4 CIDRs the scanner may probe (at most /22 each). Empty scans
    # the subnets of the host's own physical interfaces.
    network_scan_cidrs: str = ""
    network_scan_interval_seconds: int = Field(default=300, ge=60, le=86_400)
    network_scan_timeout_seconds: int = Field(default=120, ge=10, le=900)
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
