from pydantic import PostgresDsn
from pydantic_settings import BaseSettings
from decouple import config


class Settings(BaseSettings):
    SECRET_KEY: str = config('SECRET_KEY')
    # DATABASE_URL: PostgresDsn = config('DATABASE_URL')
    MOMENTO_API_KEY: str = config('MOMENTO_API_KEY')
    MOMENTO_TTL_SECONDS: int = config('MOMENTO_TTL_SECONDS', cast=int, default=600)
    GEMINI_API_KEY: str = config('GEMINI_API_KEY')
    
settings = Settings()  # type: ignore    