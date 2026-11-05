import json
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

    PROFILE_FIELDS = {
        "goals",
        "skills",
        "preferences",
        "professional_background",
        "career_direction"
    }

    def __init__(self, max_messages=50):
        self.max_messages = max_messages
        init_db()

    def add_message(self, session_id, role, content):
        db: Session = SessionLocal()
        try:
            message = MemoryMessage(
                session_id=session_id,
                role=role,
                content=content
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
        db: Session = SessionLocal()
        try:
            messages = (
                db.query(MemoryMessage)
                .filter(MemoryMessage.session_id == session_id)
                .order_by(MemoryMessage.id.asc())
                .all()
            )

            return [
                {
                    "role": message.role,
                    "content": message.content,
                    "timestamp": message.timestamp.isoformat()
                }
                for message in messages
            ]
        finally:
            db.close()

    def clear_history(self, session_id):
        db: Session = SessionLocal()
        try:
            deleted = (
                db.query(MemoryMessage)
                .filter(MemoryMessage.session_id == session_id)
                .delete()
            )
            db.commit()
            return deleted > 0
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_profile(self, user_id):
        db: Session = SessionLocal()
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
        if not isinstance(updates, dict):
            raise ValueError("Profile updates must be a dictionary.")

        invalid_fields = set(updates) - self.PROFILE_FIELDS
        if invalid_fields:
            raise ValueError(
                f"Unsupported profile fields: {sorted(invalid_fields)}"
            )

        db: Session = SessionLocal()
        try:
            profile = (
                db.query(UserProfile)
                .filter(UserProfile.user_id == user_id)
                .first()
            )

            if profile is None:
                profile = UserProfile(
                    user_id=user_id,
                    profile_data="{}"
                )
                db.add(profile)
                current_data = {}
            else:
                current_data = json.loads(profile.profile_data)

            current_data.update(updates)
            profile.profile_data = json.dumps(current_data)
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
        db: Session = SessionLocal()
        try:
            deleted = (
                db.query(UserProfile)
                .filter(UserProfile.user_id == user_id)
                .delete()
            )
            db.commit()
            return deleted > 0
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


memory = NexusMemory()
