from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id            = Column(Integer, primary_key=True, index=True)
    email         = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    token         = Column(String, unique=True, index=True)
    credits       = Column(Integer, default=0)
    created_at    = Column(DateTime, default=datetime.utcnow)

class Payment(Base):
    __tablename__ = "payments"
    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id"))
    order_id    = Column(String, unique=True)
    payment_key = Column(String)
    amount      = Column(Integer)
    credits     = Column(Integer)
    package_id  = Column(String)
    created_at  = Column(DateTime, default=datetime.utcnow)

class AnalysisLog(Base):
    __tablename__ = "analysis_logs"
    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"))
    credits_used = Column(Integer)
    created_at   = Column(DateTime, default=datetime.utcnow)
