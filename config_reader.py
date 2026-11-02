from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    tg_bot_token: SecretStr
    vk_api_token: SecretStr
    # Через что ходить в Telegram. Оба параметра необязательные:
    # tg_proxy   — свой прокси, например socks5://127.0.0.1:10808 или http://user:pass@host:port
    # tg_api_url — свой адрес Bot API (зеркало/воркер), например https://my-worker.workers.dev
    tg_proxy: str | None = None
    tg_api_url: str | None = None
    # Telegram id админов через запятую — только им бот разрешает создавать посты
    admin_ids: str = ''

    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8')

config = Settings()
