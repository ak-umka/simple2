import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    keitaro_url: str = os.getenv("KEITARO_URL", "").rstrip("/")
    keitaro_api_key: str = os.getenv("KEITARO_API_KEY", "")
    campaign_prefix: str = os.getenv("CAMPAIGN_PREFIX", "test_")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./app.db")


settings = Settings()
