from typing import Optional
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


# 9. 배치 잡 실행 기록 (jobs/ 패키지, `python -m jobs.run <job>`)
class JobRun(SQLModel, table=True):
    __tablename__ = "job_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'skipped', 'abandoned')",
            name="ck_job_runs_status",
        ),
        Index("ix_job_runs_job_started_at", "job", text("started_at DESC")),
    )

    id: Optional[int] = Field(default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True))
    job: str = Field(sa_column=Column(String(64), nullable=False))
    started_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    )
    finished_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    status: str = Field(sa_column=Column(String(16), nullable=False))
    stats: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    error: Optional[str] = Field(default=None, sa_column=Column(Text))
    git_sha: Optional[str] = Field(default=None, sa_column=Column(String(40)))
