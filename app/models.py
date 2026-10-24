from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import String, Text, DateTime, ForeignKey, Integer, Float, Boolean, JSON, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


def uid():
    return str(uuid4())


def now():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Document(Base):
    __tablename__ = 'documents'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default='uploading')
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index('ix_doc_owner_sha', 'owner_id', 'sha256'),)


class Page(Base):
    __tablename__ = 'pages'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(ForeignKey('documents.id', ondelete='CASCADE'), index=True)
    number: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint('document_id', 'number'),)


class Chunk(Base):
    __tablename__ = 'chunks'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(ForeignKey('documents.id', ondelete='CASCADE'), index=True)
    position: Mapped[int] = mapped_column(Integer)
    pages: Mapped[list] = mapped_column(JSON)
    text: Mapped[str] = mapped_column(Text)
    vector_id: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint('document_id', 'vector_id'),)


class Draft(Base):
    __tablename__ = 'drafts'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    prompt: Mapped[str] = mapped_column(Text)
    controls: Mapped[dict] = mapped_column(JSON, default=dict)
    document_ids: Mapped[list] = mapped_column(JSON)
    configuration: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    conflicts: Mapped[list] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(30), default='created')
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DraftQuestion(Base):
    __tablename__ = 'draft_questions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    draft_id: Mapped[str] = mapped_column(ForeignKey('drafts.id', ondelete='CASCADE'), index=True)
    slot: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    replaced_from: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint('draft_id', 'slot', 'revision'),)


class Assessment(Base):
    __tablename__ = 'assessments'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    draft_id: Mapped[str] = mapped_column(ForeignKey('drafts.id'))
    start_key: Mapped[str] = mapped_column(String(100))
    prompt: Mapped[str] = mapped_column(Text)
    configuration: Mapped[dict] = mapped_column(JSON)
    initial_snapshot: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default='active')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint('owner_id', 'start_key'),)


class SessionQuestion(Base):
    __tablename__ = 'session_questions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    session_id: Mapped[str] = mapped_column(ForeignKey('assessments.id', ondelete='CASCADE'), index=True)
    slot: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    replaced_from: Mapped[str | None] = mapped_column(String(36), nullable=True)
    __table_args__ = (UniqueConstraint('session_id', 'slot', 'revision'),)


class Response(Base):
    __tablename__ = 'responses'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    session_id: Mapped[str] = mapped_column(ForeignKey('assessments.id', ondelete='CASCADE'), index=True)
    question_revision_id: Mapped[str] = mapped_column(ForeignKey('session_questions.id'))
    slot: Mapped[int] = mapped_column(Integer)
    answer: Mapped[str] = mapped_column(Text)
    marks: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_marks: Mapped[float] = mapped_column(Float)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[str] = mapped_column(String(20), default='pending')
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint('session_id', 'slot'),)


class Adaptation(Base):
    __tablename__ = 'adaptations'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    session_id: Mapped[str] = mapped_column(ForeignKey('assessments.id', ondelete='CASCADE'), index=True)
    trigger_response_id: Mapped[str] = mapped_column(ForeignKey('responses.id'))
    previous_question_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    replacement_question_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    old_difficulty: Mapped[str | None] = mapped_column(String(15), nullable=True)
    new_difficulty: Mapped[str | None] = mapped_column(String(15), nullable=True)
    decision: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint('trigger_response_id'),)
