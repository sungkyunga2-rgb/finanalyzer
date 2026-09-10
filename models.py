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
    cancelled_amount = Column(Integer, default=0)        # 환불(취소)된 누적 금액
    cancelled_at     = Column(DateTime, nullable=True)   # 마지막 취소 시각

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
    order_id         = Column(String, default="")   # 실제 취소한 결제건
    refunded_amount  = Column(Integer, default=0)   # 실제 환불한 금액
    credits_deducted = Column(Integer, default=0)   # 회수한 크레딧

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
    stability_grade = Column(String, default="")         # 사업 안정성 종합등급 (예: "A (우수)")
    stability_score = Column(Integer, nullable=True)     # 취득 점수
    stability_max   = Column(Integer, nullable=True)     # 만점 (산정불가 항목이 있으면 100 미만)
    data_json       = Column(Text, default="")           # 분석 결과 전체 JSON (리포트 재출력용)
    created_at      = Column(DateTime, default=datetime.utcnow)
    printed_at      = Column(DateTime, nullable=True)    # PDF(리포트) 출력 완료 시각 — null이면 아직 출력 전


class PromoCode(Base):
    """프로모션(할인) 코드 — 관리자 화면에서 생성·수정"""
    __tablename__ = "promo_codes"
    id               = Column(Integer, primary_key=True, index=True)
    code             = Column(String, unique=True, index=True, nullable=False)  # 항상 대문자로 저장
    discount_percent = Column(Integer, default=0)      # 할인율 (1~100)
    packages         = Column(String, default="")      # "" 이면 전체 이용권, 아니면 "single,mega" 형태
    valid_from       = Column(DateTime, nullable=True) # 비우면 즉시 시작
    valid_until      = Column(DateTime, nullable=True) # 비우면 무기한
    max_uses         = Column(Integer, nullable=True)  # 전체 사용 가능 횟수 (비우면 무제한)
    used_count       = Column(Integer, default=0)
    once_per_user    = Column(Integer, default=1)      # 1이면 한 계정당 1회만
    enabled          = Column(Integer, default=1)      # 0이면 사용 중단
    memo             = Column(String, default="")
    created_at       = Column(DateTime, default=datetime.utcnow)


class PromoUse(Base):
    """프로모션 코드 사용 내역"""
    __tablename__ = "promo_uses"
    id               = Column(Integer, primary_key=True, index=True)
    promo_id         = Column(Integer, ForeignKey("promo_codes.id"), index=True)
    user_id          = Column(Integer, ForeignKey("users.id"), index=True)
    code             = Column(String, default="")
    package_id       = Column(String, default="")
    order_id         = Column(String, default="")
    discount_percent = Column(Integer, default=0)
    original_price   = Column(Integer, default=0)
    paid_price       = Column(Integer, default=0)
    created_at       = Column(DateTime, default=datetime.utcnow)
