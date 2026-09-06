from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import keyring
from platformdirs import user_config_dir

APP_NAME = "AvitoRawExport"
KEYRING_SERVICE = "avito-raw-export"


@dataclass(slots=True)
class Profile:
    name: str
    client_id: str


class ProfileStore:
    def __init__(self) -> None:
        self.root = Path(user_config_dir(APP_NAME))
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "profiles.json"

    def list(self) -> list[Profile]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return [
                Profile(**row)
                for row in payload
                if row.get("name") and row.get("client_id")
            ]
        except (ValueError, TypeError, AttributeError) as exc:
            raise RuntimeError(
                "Profile configuration is damaged; restore it before saving"
            ) from exc

    def save(self, profile: Profile, client_secret: str) -> None:
        if (
            not profile.name.strip()
            or not profile.client_id.strip()
            or not client_secret.strip()
        ):
            raise ValueError("Profile credentials must not be empty")
        profiles = {p.name: p for p in self.list()}
        keyring.set_password(KEYRING_SERVICE, profile.name, client_secret)
        profiles[profile.name] = profile
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                [asdict(p) for p in profiles.values()], ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def delete(self, name: str) -> None:
        remaining = [p for p in self.list() if p.name != name]
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps([asdict(p) for p in remaining], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
        try:
            keyring.delete_password(KEYRING_SERVICE, name)
        except Exception:
            pass

    def secret(self, name: str) -> str | None:
        return keyring.get_password(KEYRING_SERVICE, name)
