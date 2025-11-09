"""SQLAlchemy models for the memo processing platform.

The following entities are represented:

* **Dealer**: a company or individual uploading memos. Dealers have a
  unique ID, name, GSTIN (optional), phone for Twilio integration and a
  storage folder path.
* **User**: admin accounts for accessing the dashboard. Uses
  Flask-Login for authentication.
* **Memos**: records each uploaded memo file and its processing
  results, including extracted fields, status, duplicate flags,
  confidence scores, risk score, and report locations.
* **MemosEmbedding**: persists the vector embeddings associated with a
  memo for duplicate detection.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional, Any
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, Numeric, String, Text, LargeBinary, JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from . import db

JSONType = JSONB(astext_type=Text()).with_variant(JSON, "sqlite").with_variant(JSON, "mysql").with_variant(JSON, "mssql")


def generate_uuid() -> str:
    return str(uuid.uuid4())


class Dealer(db.Model):
    __tablename__ = "dealers"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    gstin = Column(String(15), unique=True, nullable=True)
    phone = Column(String(20), unique=True, nullable=True)
    folder_path = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    memos = relationship("Memos", back_populates="dealer")

    def __repr__(self) -> str:
        return f"<Dealer{self.id} {self.name}>"


class Memos(db.Model):
    __tablename__ = "memos"
    id = Column(Integer, primary_key=True)
    dealer_id = Column(Integer, ForeignKey("dealers.id"), nullable=True)
    original_filename = Column(String(255), nullable=False)
    mime_type = Column(String(100), nullable=False)
    storage_path = Column(String(255), nullable=False)
    checksum = Column(String(64), nullable=False)
    status = Column(String(50), default="queued")
    extracted_fields = Column(JSONType, nullable=True)
    confidence_scores = Column(JSONType, nullable=True)
    ai_md_path = Column(String(255), nullable=True)
    ai_pdf_path = Column(String(255), nullable=True)
    duplicate_flag = Column(Boolean, default=False)
    duplicate_of_id = Column(Integer, ForeignKey("memos.id"), nullable=True)
    gst_verify_status = Column(String(20), nullable=True)
    risk_score = Column(Numeric, nullable=True)
    anomaly_summary = Column(JSONType, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)

    dealer = relationship("Dealer", back_populates="memos")
    duplicate_of = relationship("Memos", remote_side=[id])
    embedding = relationship("MemosEmbedding", uselist=False, back_populates="memo")

    def __repr__(self) -> str:
        return f"<Memos {self.id} {self.original_filename} {self.status}>"


class MemosEmbedding(db.Model):
    __tablename__ = "memos_embeddings"
    id = Column(Integer, primary_key=True)
    memo_id = Column(Integer, ForeignKey("memos.id"), nullable=False)
    vector = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    memo = relationship("Memos", back_populates="embedding")

    def __repr__(self) -> str:
        return f"<MemosEmbedding {self.memo_id}>"