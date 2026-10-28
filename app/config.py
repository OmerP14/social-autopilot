"""Ortak ayarlar (.env) ve firma profilleri (clients/<slug>/client.yaml)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CLIENTS_DIR = Path(os.getenv("CLIENTS_DIR") or ROOT / "clients")
OUTPUT_DIR = ROOT / "output"
DATA_DIR = ROOT / "data"
TEMPLATES_DIR = ROOT / "templates"


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str = env("ANTHROPIC_API_KEY")
    claude_model: str = env("CLAUDE_MODEL", "claude-opus-5-5")
    openai_api_key: str = env("OPENAI_API_KEY")
    unsplash_access_key: str = env("UNSPLASH_ACCESS_KEY")
    pexels_api_key: str = env("PEXELS_API_KEY")
    pixabay_api_key: str = env("PIXABAY_API_KEY")
    ai_backgrounds_enabled: bool = env("AI_BACKGROUNDS_ENABLED", "false").lower() == "true"
    telegram_bot_token: str = env("TELEGRAM_BOT_TOKEN")
    telegram_admin_id: str = env("TELEGRAM_ADMIN_ID")
    meta_app_id: str = env("META_APP_ID")
    meta_app_secret: str = env("META_APP_SECRET")
    public_media_base_url: str = env("PUBLIC_MEDIA_BASE_URL")
    timezone: str = env("TIMEZONE", "Europe/Istanbul")
    db_path: Path = DATA_DIR / "autopilot.db"


@dataclass
class Client:
    slug: str
    data: dict = field(repr=False)

    @property
    def dir(self) -> Path:
        return CLIENTS_DIR / self.slug

    @property
    def photos_dir(self) -> Path:
        return self.dir / "photos"

    @property
    def videos_dir(self) -> Path:
        return self.dir / "videos"

    @property
    def output_dir(self) -> Path:
        return OUTPUT_DIR / self.slug

    def __getitem__(self, key: str):
        return self.data[key]


def load_client(slug: str) -> Client:
    with open(CLIENTS_DIR / slug / "client.yaml", encoding="utf-8") as f:
        return Client(slug=slug, data=yaml.safe_load(f))


def load_clients(active_only: bool = True) -> list[Client]:
    clients = [load_client(p.parent.name) for p in sorted(CLIENTS_DIR.glob("*/client.yaml"))]
    return [c for c in clients if c.data.get("active", True) or not active_only]


settings = Settings()


if __name__ == "__main__":
    for c in load_clients():
        print(f"{c.slug}: {c['business']['name']} (@{c['instagram_handle']})")
    print("Eksik anahtarlar:", [k for k, v in vars(settings).items() if v == ""] or "yok")
