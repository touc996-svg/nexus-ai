import json
import os
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.orm import Session

from app.core.database import Base, SessionLocal, init_db


class MemoryMessage(Base):
    __tablename__ = "memory_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, index=True, nullable=False)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, unique=True, index=True, nullable=False)
    profile_data = Column(Text, nullable=False, default="{}")
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False
    )


class NexusMemory:
    MAX_PROFILE_JSON_CHARS = 20_000
    MAX_PROFILE_LIST_ITEMS = 100
    MAX_PROFILE_TEXT_CHARS = 4_000
    PROFILE_FIELDS = {
        "goals", "skills", "preferences",
        "professional_background", "career_direction"
    }

    def __init__(
        self,
        max_messages: int = 50,
        history_limit: int | None = None,
        session_factory=None,
        initialize: bool = True
    ):
        self.max_messages = max(1, int(max_messages))
        self.history_limit = max(
            1,
            int(history_limit or os.getenv("NEXUS_MEMORY_HISTORY_LIMIT", "16"))
        )
        self.session_factory = session_factory or SessionLocal
        if initialize:
            init_db()

    @staticmethod
    def _validate_identifier(value, field_name):
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 128
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError(f"{field_name} must contain 1 to 128 printable characters")

    @classmethod
    def _validate_session_id(cls, session_id):
        cls._validate_identifier(session_id, "session_id")

    @classmethod
    def _validate_user_id(cls, user_id):
        cls._validate_identifier(user_id, "user_id")

    @classmethod
    def _validate_profile_updates(cls, updates):
        if not isinstance(updates, dict):
            raise ValueError("Profile updates must be a dictionary.")
        invalid_fields = set(updates) - cls.PROFILE_FIELDS
        if invalid_fields:
            raise ValueError(f"Unsupported profile fields: {sorted(invalid_fields)}")
        for field, value in updates.items():
            if field == "goals":
                if not isinstance(value, list) or len(value) > cls.MAX_PROFILE_LIST_ITEMS:
                    raise ValueError("goals must be a list with at most 100 items")
                if any(not isinstance(item, str) or len(item) > 500 for item in value):
                    raise ValueError("each goal must be text of at most 500 characters")
            elif field in {"skills", "preferences"}:
                if not isinstance(value, (dict, list, str)):
                    raise ValueError(f"{field} must contain JSON-compatible profile data")
                if isinstance(value, str) and len(value) > cls.MAX_PROFILE_TEXT_CHARS:
                    raise ValueError(f"{field} exceeds the profile text limit")
            elif field in {"professional_background", "career_direction"}:
                if not isinstance(value, str) or len(value) > cls.MAX_PROFILE_TEXT_CHARS:
                    raise ValueError(f"{field} must be text of at most 4000 characters")
        try:
            encoded = json.dumps(updates, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("Profile data must be valid JSON values") from exc
        if len(encoded) > cls.MAX_PROFILE_JSON_CHARS:
            raise ValueError("Profile data exceeds the 20000-character limit")

    @staticmethod
    def _merge_profile(current: dict, updates: dict) -> dict:
        merged = dict(current)
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = NexusMemory._merge_profile(merged[key], value)
            else:
                merged[key] = value
        return merged

    def add_message(self, session_id, role, content):
        self._validate_session_id(session_id)
        if role not in {"user", "assistant", "system"}:
            raise ValueError("role must be user, assistant, or system")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content cannot be empty")
        if len(content) > 12000:
            raise ValueError("content exceeds the 12000 character limit")

        db: Session = self.session_factory()
        try:
            message = MemoryMessage(
                session_id=session_id, role=role, content=content
            )
            db.add(message)
            db.commit()
            db.refresh(message)
            old_messages = (
                db.query(MemoryMessage)
                .filter(MemoryMessage.session_id == session_id)
                .order_by(MemoryMessage.id.desc())
                .offset(self.max_messages)
                .all()
            )
            for old_message in old_messages:
                db.delete(old_message)
            if old_messages:
                db.commit()
            return {
                "role": message.role,
                "content": message.content,
                "timestamp": message.timestamp.isoformat()
            }
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_history(self, session_id):
        self._validate_session_id(session_id)
        db: Session = self.session_factory()
        try:
            messages = (
                db.query(MemoryMessage)
                .filter(MemoryMessage.session_id == session_id)
                .order_by(MemoryMessage.id.desc())
                .limit(self.history_limit)
                .all()
            )
            messages.reverse()
            return [
                {
                    "role": item.role,
                    "content": item.content,
                    "timestamp": item.timestamp.isoformat()
                }
                for item in messages
            ]
        finally:
            db.close()

    def clear_history(self, session_id):
        self._validate_session_id(session_id)
        db: Session = self.session_factory()
        try:
            deleted = (
                db.query(MemoryMessage)
                .filter(MemoryMessage.session_id == session_id)
                .delete(synchronize_session=False)
            )
            db.commit()
            return deleted > 0
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_profile(self, user_id):
        self._validate_user_id(user_id)
        db: Session = self.session_factory()
        try:
            profile = (
                db.query(UserProfile)
                .filter(UserProfile.user_id == user_id)
                .first()
            )
            if profile is None:
                return None
            return {
                "user_id": profile.user_id,
                "profile": json.loads(profile.profile_data),
                "created_at": profile.created_at.isoformat(),
                "updated_at": profile.updated_at.isoformat()
            }
        finally:
            db.close()

    def update_profile(self, user_id, updates):
        self._validate_user_id(user_id)
        self._validate_profile_updates(updates)

        db: Session = self.session_factory()
        try:
            profile = (
                db.query(UserProfile)
                .filter(UserProfile.user_id == user_id)
                .first()
            )
            if profile is None:
                profile = UserProfile(user_id=user_id, profile_data="{}")
                db.add(profile)
                current_data = {}
            else:
                current_data = json.loads(profile.profile_data)
            current_data = self._merge_profile(current_data, updates)
            self._validate_profile_updates(current_data)
            profile.profile_data = json.dumps(current_data, ensure_ascii=False, allow_nan=False)
            profile.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(profile)
            return {
                "user_id": profile.user_id,
                "profile": json.loads(profile.profile_data),
                "created_at": profile.created_at.isoformat(),
                "updated_at": profile.updated_at.isoformat()
            }
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def delete_profile(self, user_id):
        self._validate_user_id(user_id)
        db: Session = self.session_factory()
        try:
            deleted = (
                db.query(UserProfile)
                .filter(UserProfile.user_id == user_id)
                .delete(synchronize_session=False)
            )
            db.commit()
            return deleted > 0
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def health_check(self) -> dict:
        db: Session = self.session_factory()
        try:
            db.query(MemoryMessage.id).limit(1).all()
            return {
                "status": "online",
                "storage": "sql",
                "history_limit": self.history_limit,
                "max_messages_per_session": self.max_messages
            }
        except Exception as exc:
            return {
                "status": "offline",
                "storage": "sql",
                "error_type": type(exc).__name__
            }
        finally:
            db.close()


memory = NexusMemory()
