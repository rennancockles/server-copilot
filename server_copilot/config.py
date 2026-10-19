from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SERVER_COPILOT_", env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8000
    docker_host: str = "tcp://docker-socket-proxy:2375"


def get_settings() -> Settings:
    return Settings()
