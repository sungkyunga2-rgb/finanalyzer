from sqlalchemy import Column, Integer, BigInteger, String, DateTime, ForeignKey, Text
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
    company_name  = Column(String, default="")
    rep_name      = Column(String, default="")
    nickname      = Column(String, default="")
    phone         = Column(String, default="")
    business_number = Column(String, default="")
    terms_agreed_at = Column(DateTime, nullable=True)
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

class RefundRequest(Base):
    __tablename__ = "refund_requests"
    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"))
    reason       = Column(String, default="")
    status       = Column(String, default="pending")  # pending / processed / rejected
    admin_note   = Column(String, default="")
    created_at   = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)

class CommunityPost(Base):
    __tablename__ = "community_posts"
    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"))
    title      = Column(String, nullable=False)
    content    = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)

class CommunityComment(Base):
    __tablename__ = "community_comments"
    id         = Column(Integer, primary_key=True, index=True)
    post_id    = Column(Integer, ForeignKey("community_posts.id"))
    user_id    = Column(Integer, ForeignKey("users.id"))
    content    = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)


class AnalysisHistory(Base):
    """분석 이력 — 사용자가 지난 분석 결과를 다시 보고 리포트를 출력할 수 있도록 저장"""
    __tablename__ = "analysis_histories"
    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id"), index=True)
    company_name    = Column(String, default="")
    rep_name        = Column(String, default="")
    business_number = Column(String, default="")
    source_type     = Column(String, default="image")   # image(재무제표 이미지 분석) / manual(직접 입력)
    revenue         = Column(BigInteger, nullable=True)  # 매출액 (목록에 표시)
    data_json       = Column(Text, default="")           # 분석 결과 전체 JSON (리포트 재출력용)
    created_at      = Column(DateTime, default=datetime.utcnow)
    printed_at      = Column(DateTime, nullable=True)    # PDF(리포트) 출력 완료 시각 — null이면 아직 출력 전
