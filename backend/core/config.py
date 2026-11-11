import os


class Settings:
    def __init__(self):
        self.cors_origins = os.getenv("CORS_ORIGINS", "*").split(",")


settings = Settings()
