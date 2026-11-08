from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.memory import NexusMemory


def build_memory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return NexusMemory(
        max_messages=5, history_limit=3,
        session_factory=factory, initialize=False
    )


def test_memory_returns_recent_messages_in_chronological_order():
    memory = build_memory()
    for index in range(5):
        memory.add_message("session-a", "user", f"message-{index}")
    history = memory.get_history("session-a")
    assert [item["content"] for item in history] == [
        "message-2", "message-3", "message-4"
    ]


def test_memory_retention_caps_total_messages_per_session():
    memory = build_memory()
    for index in range(8):
        memory.add_message("session-a", "user", f"message-{index}")
    history = memory.get_history("session-a")
    assert len(history) == 3
    assert history[-1]["content"] == "message-7"


def test_profile_updates_merge_nested_skills_and_preferences():
    memory = build_memory()
    memory.update_profile("user-a", {
        "skills": {"python": "beginner", "sql": "intermediate"},
        "preferences": {"tone": "direct"}
    })
    updated = memory.update_profile("user-a", {
        "skills": {"python": "intermediate"},
        "preferences": {"format": "short"}
    })
    assert updated["profile"]["skills"] == {
        "python": "intermediate", "sql": "intermediate"
    }
    assert updated["profile"]["preferences"] == {
        "tone": "direct", "format": "short"
    }


def test_memory_rejects_invalid_session_and_message():
    memory = build_memory()
    try:
        memory.get_history("")
    except ValueError:
        pass
    else:
        raise AssertionError("Empty session id should be rejected")

    try:
        memory.add_message("session-a", "system", " ")
    except ValueError:
        pass
    else:
        raise AssertionError("Empty message content should be rejected")
