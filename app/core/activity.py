from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Session

from app.core.database import Base, SessionLocal, init_db


class AgentActivity(Base):
    __tablename__ = "agent_activity"

    id = Column(Integer, primary_key=True, index=True)
    agent_name = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, index=True)
    duration_ms = Column(Integer, nullable=False)
    error_message = Column(Text, nullable=True)
    timestamp = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )


class ActivityLogger:

    def __init__(self):
        init_db()

    def record(self, agent_name, status, duration_ms, error_message=None):
        db: Session = SessionLocal()

        try:
            activity = AgentActivity(
                agent_name=agent_name,
                status=status,
                duration_ms=duration_ms,
                error_message=error_message
            )

            db.add(activity)
            db.commit()
            db.refresh(activity)

            return {
                "id": activity.id,
                "agent_name": activity.agent_name,
                "status": activity.status,
                "duration_ms": activity.duration_ms,
                "timestamp": activity.timestamp.isoformat()
            }

        except Exception:
            db.rollback()
            raise

        finally:
            db.close()

    def get_recent(self, limit=50):
        db: Session = SessionLocal()

        try:
            activities = (
                db.query(AgentActivity)
                .order_by(AgentActivity.id.desc())
                .limit(max(1, min(limit, 200)))
                .all()
            )

            return [
                {
                    "id": item.id,
                    "agent_name": item.agent_name,
                    "status": item.status,
                    "duration_ms": item.duration_ms,
                    "error_message": item.error_message,
                    "timestamp": item.timestamp.isoformat()
                }
                for item in activities
            ]

        finally:
            db.close()



    def get_summary(self):
        db: Session = SessionLocal()
        try:
            rows = (
                db.query(
                    AgentActivity.agent_name,
                    AgentActivity.status,
                    func.count(AgentActivity.id),
                    func.avg(AgentActivity.duration_ms)
                )
                .group_by(AgentActivity.agent_name, AgentActivity.status)
                .all()
            )

            agents = {}
            for name, status, count, average in rows:
                item = agents.setdefault(name, {
                    "agent_name": name,
                    "total_executions": 0,
                    "successful": 0,
                    "failed": 0,
                    "rejected": 0,
                    "average_duration_ms": 0,
                    "_duration_total": 0
                })

                item["total_executions"] += count
                item["_duration_total"] += (average or 0) * count

                if status == "success":
                    item["successful"] += count
                elif status == "failed":
                    item["failed"] += count
                elif status == "rejected":
                    item["rejected"] += count

            result = []
            for item in agents.values():
                total = item["total_executions"]
                item["average_duration_ms"] = round(
                    item["_duration_total"] / total
                ) if total else 0
                del item["_duration_total"]
                result.append(item)

            return {
                "agents": sorted(result, key=lambda x: x["agent_name"]),
                "total_executions": sum(x["total_executions"] for x in result)
            }
        finally:
            db.close()

activity_logger = ActivityLogger()
