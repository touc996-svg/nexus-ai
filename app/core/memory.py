
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


class NexusMemory:

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


memory = NexusMemory()
