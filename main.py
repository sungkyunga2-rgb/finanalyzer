import asyncio
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import httpx
import os
import uuid
from datetime import datetime, timedelta
from database import get_db, engine, SessionLocal
import models
from sqlalchemy.orm import Session

models.Base.metadata.create_all(bind=engine)

# ── 자동 마이그레이션: 기존 테이블에 없는 컬럼 자동 추가 ──
def run_auto_migration():
    from sqlalchemy import text, inspect
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    def add_missing_columns(table_name, required_columns):
        if table_name not in tables:
            return
        existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        with engine.connect() as conn:
            for col_name, col_def in required_columns.items():
                if col_name not in existing_columns:
                    try:
                        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}"))
                        conn.commit()
                    except Exception as e:
                        print(f"마이그레이션 오류 ({table_name}.{col_name}): {e}")

    add_missing_columns("users", {
        "company_name": "VARCHAR DEFAULT ''",
        "rep_name": "VARCHAR DEFAULT ''",
        "phone": "VARCHAR DEFAULT ''",
        "business_number": "VARCHAR DEFAULT ''",
        "terms_agreed_at": "TIMESTAMP",
        "nickname": "VARCHAR DEFAULT ''",
    })
    add_missing_columns("teams", {"plan_expires_at": "TIMESTAMP"})
    # 커뮤니티 글/댓글 수정 기능을 위한 updated_at 컬럼 추가
    add_missing_columns("community_posts", {"updated_at": "TIMESTAMP"})
    add_missing_columns("community_comments", {"updated_at": "TIMESTAMP"})
    # 분석 이력 테이블 (신규 테이블은 create_all이 만들지만, 이미 있던 경우를 대비해 컬럼도 확인)
    add_missing_columns("analysis_histories", {
        "company_name": "VARCHAR DEFAULT ''",
        "rep_name": "VARCHAR DEFAULT ''",
        "business_number": "VARCHAR DEFAULT ''",
        "source_type": "VARCHAR DEFAULT 'image'",
        "revenue": "BIGINT",
        "stability_grade": "VARCHAR DEFAULT ''",
        "stability_score": "INTEGER",
        "stability_max": "INTEGER",
        "data_json": "TEXT",
        "created_at": "TIMESTAMP",
        "printed_at": "TIMESTAMP",
    })
    # 프로모션(할인) 코드 테이블
    add_missing_columns("promo_codes", {
        "discount_percent": "INTEGER DEFAULT 0",
        "packages": "VARCHAR DEFAULT ''",
        "valid_from": "TIMESTAMP",
        "valid_until": "TIMESTAMP",
        "max_uses": "INTEGER",
        "used_count": "INTEGER DEFAULT 0",
        "once_per_user": "INTEGER DEFAULT 1",
        "enabled": "INTEGER DEFAULT 1",
        "memo": "VARCHAR DEFAULT ''",
        "created_at": "TIMESTAMP",
    })
    add_missing_columns("payments", {
        "cancelled_amount": "INTEGER DEFAULT 0",
        "cancelled_at": "TIMESTAMP",
    })
    add_missing_columns("refund_requests", {
        "order_id": "VARCHAR DEFAULT ''",
        "refunded_amount": "INTEGER DEFAULT 0",
        "credits_deducted": "INTEGER DEFAULT 0",
    })
    add_missing_columns("site_visits", {
        "visitor_id": "VARCHAR DEFAULT ''",
        "path": "VARCHAR DEFAULT ''",
        "source": "VARCHAR DEFAULT ''",
        "referrer": "VARCHAR DEFAULT ''",
        "promo_code": "VARCHAR DEFAULT ''",
        "device": "VARCHAR DEFAULT ''",
        "is_member": "INTEGER DEFAULT 0",
        "created_at": "TIMESTAMP",
    })
    add_missing_columns("promo_uses", {
        "promo_id": "INTEGER",
        "user_id": "INTEGER",
        "code": "VARCHAR DEFAULT ''",
        "package_id": "VARCHAR DEFAULT ''",
        "order_id": "VARCHAR DEFAULT ''",
        "discount_percent": "INTEGER DEFAULT 0",
        "original_price": "INTEGER DEFAULT 0",
        "paid_price": "INTEGER DEFAULT 0",
        "created_at": "TIMESTAMP",
    })

run_auto_migration()

app = FastAPI(title="FinAnalyzer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

PORTONE_SECRET_KEY = os.getenv("PORTONE_SECRET_KEY", "")   # 포트원 콘솔 > API 키
PORTONE_STORE_ID   = os.getenv("PORTONE_STORE_ID", "store-9354e198-29ea-4866-91dc-ddecebe8661e")   # 결제 취소 API에 필요 (프론트의 값과 동일해야 함)
PORTONE_API_BASE   = "https://api.portone.io"
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")       # Google AI Studio > API 키
ADMIN_PASSWORD     = os.getenv("ADMIN_PASSWORD", "")       # 관리자 페이지 접근 비밀번호 (Render 환경변수에 설정)
SENDER_EMAIL       = "cngpartners123@gmail.com"             # 임시비밀번호 발송용 발신 계정 (Brevo에 발신자로 등록 필요)
BREVO_API_KEY      = os.getenv("BREVO_API_KEY", "")         # Brevo(구 Sendinblue) API 키 (Render 환경변수에 설정)
COST_PER_ANALYSIS = 10  # 분석 1회당 차감 크레딧
# 무료 분석: 구독이 없어도 분석은 할 수 있고, 결과는 일부만 보여준다(예상 한도부터는 구독 시 공개).
# AI 비용이 드는 만큼 구독 없는 회원은 월 무료 분석 횟수를 둔다.
FREE_ANALYSES_PER_MONTH = 30

CREDIT_PACKAGES = {
    "single":   {"price": 13900, "credits": 10,  "label": "1건"},
    "standard": {"price": 59075, "credits": 50,  "label": "5건 (15% 할인)"},
    "mega":     {"price": 97300, "credits": 100, "label": "10건 (30% 할인)"},
}

# 구독 요금제 — 월 분석 건수는 제한하지 않고 '사용자 수(seats)'로만 구분한다.
# price = 월 구독료(원). 이 금액 그대로 카드에 청구된다.
# list_price = 정가(할인 표시용). hidden = 새로 가입은 받지 않는 이전 요금제(기존 팀·관리자 부여용으로만 유지)
SUBSCRIPTION_PLANS = {
    "starter":  {"label": "피나 구독", "seats": 1,  "price": 59000, "list_price": 330000},
    "standard": {"label": "스탠다드",  "seats": 3,  "price": 550000, "hidden": True},
    "pro":      {"label": "프로",      "seats": 10, "price": 1100000, "hidden": True},
}

def _public_plans():
    return [{"id": k, "label": v["label"], "seats": v["seats"], "price": v["price"], "list_price": v.get("list_price")}
            for k, v in SUBSCRIPTION_PLANS.items() if not v.get("hidden")]
TEAM_INVITE_VALID_DAYS = 14   # 초대 유효기간 (지나면 수락 불가)
TEAM_MAX_PENDING_INVITES = 20 # 수락 대기 초대 상한 (초대 남발 방지용 — 사용자 수 한도와는 별개)


def _email_eq(value):
    """이메일 비교 — 대소문자·앞뒤 공백 차이는 같은 주소로 본다 (예: Hong@Naver.com = hong@naver.com)"""
    from sqlalchemy import func
    return func.lower(func.trim(models.User.email)) == str(value or "").strip().lower()


# ── 모델 ─────────────────────────────────────────────
class UserCreate(BaseModel):
    email: str
    password: str
    company_name: str = ""
    rep_name: str = ""
    nickname: str = ""
    phone: str = ""
    business_number: str = ""
    terms_agreed: bool = False

class UserLogin(BaseModel):
    email: str
    password: str

class PaymentRequest(BaseModel):
    payment_id: str   # 포트원 V2의 paymentId (프론트에서 전달)
    package_id: str
    amount: int
    promo_code: Optional[str] = None

class ImageItem(BaseModel):
    data: str
    mime: str = "image/jpeg"

class AnalysisRequest(BaseModel):
    image_base64: str = ""       # 하위호환: 단일 이미지
    image_mime: str = "image/jpeg"
    images: list[ImageItem] = [] # 다중 이미지 (최대 10장)

# ── 간단한 토큰 인증 (실제 운영 시 JWT 사용 권장) ──
def get_current_user(authorization: str = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    token = authorization.split(" ")[1]
    user = db.query(models.User).filter(models.User.token == token).first()
    if not user:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")
    return user


# ── 라우터 ───────────────────────────────────────────

# ══════════════════════════════════════════════════════════════
# 프로모션(할인) 코드
#   ⚠️ 할인 금액은 반드시 서버에서 다시 계산합니다.
#      프론트가 보낸 금액을 그대로 믿으면 결제금액을 조작당할 수 있습니다.
# ══════════════════════════════════════════════════════════════
def apply_discount(price: int, discount_percent: int) -> int:
    """할인가 계산 — 10원 단위로 올림 처리해 금액이 지저분해지지 않게 함"""
    pct = max(0, min(100, int(discount_percent or 0)))
    discounted = price * (100 - pct) / 100
    # 10원 단위 반올림 (최소 100원 — 포트원 최소 결제금액 고려)
    discounted = int(round(discounted / 10.0) * 10)
    return max(100, discounted)


def check_promo_basic(db: Session, code: str, user: "models.User"):
    """이용권과 무관한 공통 조건(존재·사용중·기간·횟수·1인1회)만 검증"""
    normalized = str(code or "").strip().upper()
    if not normalized:
        return None

    promo = db.query(models.PromoCode).filter(models.PromoCode.code == normalized).first()
    if not promo:
        raise HTTPException(status_code=400, detail="존재하지 않는 코드입니다. 코드를 다시 확인해주세요.")
    if not promo.enabled:
        raise HTTPException(status_code=400, detail="현재 사용할 수 없는 코드입니다.")

    now = datetime.utcnow()
    if promo.valid_from and now < promo.valid_from:
        raise HTTPException(status_code=400, detail="아직 사용 기간이 시작되지 않은 코드입니다.")
    if promo.valid_until and now > promo.valid_until:
        raise HTTPException(status_code=400, detail="사용 기간이 지난 코드입니다.")

    if promo.max_uses is not None and (promo.used_count or 0) >= promo.max_uses:
        raise HTTPException(status_code=400, detail="사용 횟수가 모두 소진된 코드입니다.")

    if promo.once_per_user:
        already = db.query(models.PromoUse).filter(
            models.PromoUse.promo_id == promo.id,
            models.PromoUse.user_id == user.id,
        ).first()
        if already:
            raise HTTPException(status_code=400, detail="이미 사용하신 코드입니다. (한 계정당 1회)")

    return promo


def promo_allowed_packages(promo) -> list:
    """이 코드가 적용되는 이용권 목록"""
    if not promo or not promo.packages:
        return list(CREDIT_PACKAGES.keys())
    allowed = [x.strip() for x in promo.packages.split(",") if x.strip() in CREDIT_PACKAGES]
    return allowed or list(CREDIT_PACKAGES.keys())


def resolve_promo(db: Session, code: str, package_id: str, user: "models.User"):
    """코드를 검증하고 (PromoCode, 할인가)를 반환. 문제가 있으면 HTTPException."""
    if not str(code or "").strip():
        return None, None

    pkg = CREDIT_PACKAGES.get(package_id)
    if not pkg:
        raise HTTPException(status_code=400, detail="유효하지 않은 패키지입니다.")

    promo = check_promo_basic(db, code, user)
    if not promo:
        return None, None

    allowed = promo_allowed_packages(promo)
    if package_id not in allowed:
        labels = ", ".join(CREDIT_PACKAGES[a]["label"] for a in allowed)
        raise HTTPException(status_code=400, detail=f"이 코드는 {labels} 이용권에만 사용할 수 있습니다.")

    return promo, apply_discount(pkg["price"], promo.discount_percent)


class PromoCheckBody(BaseModel):
    code: str
    package_id: Optional[str] = None

@app.post("/promo/check")
def check_promo(
    body: PromoCheckBody,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """결제 전 코드 확인 — 할인가를 미리 보여주기 위한 용도.
    package_id를 주면 그 이용권만, 안 주면 적용 가능한 전체 이용권의 할인가를 반환."""
    if not str(body.code or "").strip():
        raise HTTPException(status_code=400, detail="코드를 입력해주세요.")

    promo = check_promo_basic(db, body.code, user)
    allowed = promo_allowed_packages(promo)

    if body.package_id:
        promo2, price = resolve_promo(db, body.code, body.package_id, user)
        pkg = CREDIT_PACKAGES[body.package_id]
        return {
            "code": promo.code,
            "discount_percent": promo.discount_percent,
            "packages": allowed,
            "original_price": pkg["price"],
            "discounted_price": price,
            "saved": pkg["price"] - price,
        }

    prices = {
        pid: {
            "original_price": pkg["price"],
            "discounted_price": apply_discount(pkg["price"], promo.discount_percent),
        }
        for pid, pkg in CREDIT_PACKAGES.items() if pid in allowed
    }
    return {
        "code": promo.code,
        "discount_percent": promo.discount_percent,
        "packages": allowed,
        "prices": prices,
    }


@app.get("/")
def root():
    return {"status": "ok", "service": "FinAnalyzer API"}

@app.get("/packages")
def list_packages():
    return CREDIT_PACKAGES

# 회원가입
@app.post("/auth/register")
def register(body: UserCreate, db: Session = Depends(get_db)):
    import re
    if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", body.email):
        raise HTTPException(status_code=400, detail="올바른 이메일 형식이 아닙니다.")
    if not body.terms_agreed:
        raise HTTPException(status_code=400, detail="이용약관 및 개인정보처리방침에 동의해주세요.")
    if not body.nickname.strip():
        raise HTTPException(status_code=400, detail="별명을 입력해주세요.")
    existing = db.query(models.User).filter(_email_eq(body.email)).first()
    if existing:
        raise HTTPException(status_code=400, detail="이미 사용 중인 이메일입니다.")
    import hashlib
    pw_hash = hashlib.sha256(body.password.encode()).hexdigest()
    token = str(uuid.uuid4())
    user = models.User(
        email=body.email, password_hash=pw_hash, token=token, credits=0,
        company_name=body.company_name, rep_name=body.rep_name, nickname=body.nickname.strip(), phone=body.phone,
        business_number=body.business_number, terms_agreed_at=datetime.utcnow()
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {
        "token": token, "email": user.email, "credits": user.credits,
        "company_name": user.company_name, "rep_name": user.rep_name, "nickname": user.nickname,
        "business_number": user.business_number
    }

# 로그인
@app.post("/auth/login")
def login(body: UserLogin, db: Session = Depends(get_db)):
    import hashlib
    pw_hash = hashlib.sha256(body.password.encode()).hexdigest()
    user = db.query(models.User).filter(
        _email_eq(body.email),
        models.User.password_hash == pw_hash
    ).first()
    if not user:
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 틀렸습니다.")
    return {
        "token": user.token, "email": user.email, "credits": user.credits,
        "company_name": user.company_name, "rep_name": user.rep_name, "nickname": user.nickname,
        "business_number": user.business_number,
        "subscribed": user_has_subscription_access(db, user),
    }

# 내 크레딧 조회
@app.get("/me")
def me(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"email": user.email, "credits": user.credits, "subscribed": user_has_subscription_access(db, user)}

# 비밀번호 재확인 후 상세 정보 조회
class PasswordVerify(BaseModel):
    password: str

@app.post("/auth/verify")
def verify_password(
    body: PasswordVerify,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    import hashlib
    pw_hash = hashlib.sha256(body.password.encode()).hexdigest()
    if pw_hash != user.password_hash:
        raise HTTPException(status_code=401, detail="비밀번호가 일치하지 않습니다.")
    return {
        "email": user.email,
        "company_name": user.company_name,
        "rep_name": user.rep_name,
        "nickname": user.nickname,
        "phone": user.phone,
        "business_number": user.business_number,
        "credits": user.credits
    }

# 사업자등록번호 수정
class BusinessNumberUpdate(BaseModel):
    business_number: str

@app.post("/auth/update-business-number")
def update_business_number(
    body: BusinessNumberUpdate,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    biz = body.business_number.strip()
    if not biz:
        raise HTTPException(status_code=400, detail="사업자등록번호를 입력해주세요.")
    user.business_number = biz
    db.commit()
    return {"business_number": user.business_number}

# 별명 수정 (커뮤니티에서 사용되는 이름)
class NicknameUpdate(BaseModel):
    nickname: str

@app.post("/auth/update-nickname")
def update_nickname(
    body: NicknameUpdate,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    nickname = body.nickname.strip()
    if not nickname:
        raise HTTPException(status_code=400, detail="별명을 입력해주세요.")
    user.nickname = nickname
    db.commit()
    return {"nickname": user.nickname}

# 비밀번호 변경
class PasswordChange(BaseModel):
    current_password: str
    new_password: str

@app.post("/auth/change-password")
def change_password(
    body: PasswordChange,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    import hashlib
    current_hash = hashlib.sha256(body.current_password.encode()).hexdigest()
    if current_hash != user.password_hash:
        raise HTTPException(status_code=401, detail="현재 비밀번호가 일치하지 않습니다.")
    if len(body.new_password) < 4:
        raise HTTPException(status_code=400, detail="새 비밀번호는 4자 이상이어야 합니다.")
    user.password_hash = hashlib.sha256(body.new_password.encode()).hexdigest()
    db.commit()
    return {"message": "비밀번호가 변경되었습니다."}

# 본인 결제/분석 내역 조회
@app.get("/auth/my-history")
def my_history(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    payments = db.query(models.Payment).filter(models.Payment.user_id == user.id).order_by(models.Payment.created_at.desc()).limit(50).all()
    logs = db.query(models.AnalysisLog).filter(models.AnalysisLog.user_id == user.id).order_by(models.AnalysisLog.created_at.desc()).limit(50).all()
    return {
        "payments": [
            {"order_id": p.order_id, "amount": p.amount, "credits": p.credits, "package_id": p.package_id,
             "created_at": p.created_at.isoformat() if p.created_at else None}
            for p in payments
        ],
        "recent_analyses": [
            {"credits_used": l.credits_used, "created_at": l.created_at.isoformat() if l.created_at else None}
            for l in logs
        ],
    }

# 회원 탈퇴
class WithdrawBody(BaseModel):
    password: str

@app.post("/auth/withdraw")
def withdraw(
    body: WithdrawBody,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    import hashlib
    pw_hash = hashlib.sha256(body.password.encode()).hexdigest()
    if pw_hash != user.password_hash:
        raise HTTPException(status_code=401, detail="비밀번호가 일치하지 않습니다.")
    live_sub = db.query(models.Subscription).filter(
        models.Subscription.user_id == user.id, models.Subscription.status.in_(("active", "past_due"))
    ).first()
    if live_sub:
        raise HTTPException(status_code=400, detail="구독 중에는 탈퇴할 수 없습니다. '구독' 메뉴에서 먼저 해지한 뒤 탈퇴해주세요.")
    db.query(models.Payment).filter(models.Payment.user_id == user.id).delete()
    db.query(models.AnalysisLog).filter(models.AnalysisLog.user_id == user.id).delete()
    db.query(models.AnalysisHistory).filter(models.AnalysisHistory.user_id == user.id).delete()
    db.query(models.PromoUse).filter(models.PromoUse.user_id == user.id).delete()
    cleanup_user_subscription(db, user)   # 예약 결제 취소 + 빌링키 삭제
    cleanup_user_team(db, user)   # 팀 소속/초대 정리 (대표가 탈퇴하면 팀 해산)
    db.delete(user)
    db.commit()
    return {"message": "회원 탈퇴가 완료되었습니다."}

# 환불 신청
class RefundRequestBody(BaseModel):
    reason: str = ""

def notify_admin(subject: str, text: str, reply_to: str = ""):
    """관리자 이메일로 알림 발송 (실패해도 본래 작업은 계속 진행)"""
    if not BREVO_API_KEY:
        print(f"관리자 알림 미발송(BREVO_API_KEY 없음): {subject}")
        return False
    try:
        payload = {
            "sender": {"name": "FinAnalyzer 알림", "email": SENDER_EMAIL},
            "to": [{"email": SENDER_EMAIL}],
            "subject": subject,
            "textContent": text,
        }
        if reply_to:
            payload["replyTo"] = {"email": reply_to}
        resp = httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json", "Accept": "application/json"},
            json=payload, timeout=15,
        )
        if resp.status_code not in (200, 201):
            print(f"관리자 알림 발송 실패 (status={resp.status_code}): {resp.text}")
            return False
        return True
    except Exception as e:
        print(f"관리자 알림 발송 중 예외: {e}")
        return False


@app.post("/refund/request")
def request_refund(
    body: RefundRequestBody,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    r = models.RefundRequest(user_id=user.id, reason=body.reason.strip())
    db.add(r)
    db.commit()
    db.refresh(r)

    # 관리자에게 즉시 알림 (놓치지 않도록)
    recent = (db.query(models.Payment)
                .filter(models.Payment.user_id == user.id)
                .order_by(models.Payment.created_at.desc()).limit(5).all())
    pay_lines = "\n".join(
        f"  - {p.created_at.strftime('%Y-%m-%d') if p.created_at else '-'} · {p.order_id} · "
        f"{(p.amount or 0):,}원 · {(CREDIT_PACKAGES.get(p.package_id) or {}).get('label', p.package_id)}"
        + (f" · 이미 {(p.cancelled_amount or 0):,}원 취소됨" if (p.cancelled_amount or 0) else "")
        for p in recent
    ) or "  - (결제 내역 없음)"

    notify_admin(
        subject=f"[FinAnalyzer] 환불 신청 접수 — {user.email}",
        text=f"""환불 신청이 접수되었습니다.

신청자   : {user.email}
회사명   : {user.company_name or '-'}
대표자   : {user.rep_name or '-'}
연락처   : {user.phone or '-'}
잔여횟수 : {user.credits // COST_PER_ANALYSIS}건 ({user.credits} 크레딧)

신청 사유:
{r.reason or '(미입력)'}

최근 결제 내역:
{pay_lines}

접수 시각 : {r.created_at.isoformat() if r.created_at else '-'} (UTC)

※ 관리자 화면에서 환불 처리를 진행해주세요. 상태만 바꾸면 실제 환불은 되지 않습니다.
""",
        reply_to=user.email,
    )
    return {"message": "환불 신청이 접수되었습니다. 영업일 기준 며칠 내로 처리될 예정입니다."}

@app.get("/refund/my-requests")
def my_refund_requests(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    reqs = db.query(models.RefundRequest).filter(models.RefundRequest.user_id == user.id).order_by(models.RefundRequest.created_at.desc()).all()
    status_kr = {"pending": "처리 대기", "processed": "환불 완료", "rejected": "반려"}
    return {
        "requests": [
            {
                "reason": r.reason,
                "status": status_kr.get(r.status, r.status),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in reqs
        ]
    }

# 결제 검증 + 크레딧 지급 (포트원 V2)
@app.post("/payments/confirm")
async def confirm_payment(
    body: PaymentRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    pkg = CREDIT_PACKAGES.get(body.package_id)
    if not pkg:
        raise HTTPException(status_code=400, detail="유효하지 않은 패키지입니다.")

    # ── 프로모션 코드가 있으면 서버에서 할인가를 다시 계산 (프론트 금액은 신뢰하지 않음) ──
    promo, expected_price = resolve_promo(db, body.promo_code, body.package_id, user)
    if not promo:
        expected_price = pkg["price"]

    # ── 중복 결제 방지: 이미 처리된 payment_id인지 확인 ──
    existing = db.query(models.Payment).filter(models.Payment.order_id == body.payment_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="이미 처리된 결제입니다.")

    # ── 포트원 V2 API로 결제 내역 조회 (서버사이드 검증) ──
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{PORTONE_API_BASE}/payments/{body.payment_id}",
            headers={
                "Authorization": f"PortOne {PORTONE_SECRET_KEY}",
                "Content-Type": "application/json",
            }
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail="포트원 결제 조회 실패")

    payment_data = resp.json()

    # ── 검증: 상태, 금액, 통화 ──
    if payment_data.get("status") != "PAID":
        raise HTTPException(status_code=400, detail=f"결제 미완료 상태: {payment_data.get('status')}")

    paid_amount = payment_data.get("amount", {}).get("total", 0)
    if paid_amount != expected_price:
        # 금액 불일치 → 거부 (보안)
        raise HTTPException(status_code=400, detail=f"결제 금액 불일치 (요청: {expected_price}원, 실제: {paid_amount}원)")

    # ── 크레딧 지급 ──
    credits_to_add = pkg["credits"]
    user.credits += credits_to_add
    payment_record = models.Payment(
        user_id=user.id,
        order_id=body.payment_id,
        payment_key=payment_data.get("pgTxId", ""),
        amount=paid_amount,
        credits=credits_to_add,
        package_id=body.package_id,
    )
    db.add(payment_record)

    # ── 프로모션 코드 사용 기록 ──
    if promo:
        promo.used_count = (promo.used_count or 0) + 1
        db.add(models.PromoUse(
            promo_id=promo.id,
            user_id=user.id,
            code=promo.code,
            package_id=body.package_id,
            order_id=body.payment_id,
            discount_percent=promo.discount_percent,
            original_price=pkg["price"],
            paid_price=paid_amount,
        ))

    db.commit()
    db.refresh(user)

    return {
        "success": True,
        "credits_added": credits_to_add,
        "total_credits": user.credits,
        "promo_code": promo.code if promo else None,
        "discount_percent": promo.discount_percent if promo else 0,
    }

# 재무제표 분석
@app.post("/analyze")
async def analyze(
    body: AnalysisRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return await _analyze_financial(body, user, db, consume=True)

# 원클릭 분석 '서류로 채우기' — 재무제표에서 숫자만 읽어 입력칸을 채운다 (분석 횟수는 원클릭 분석 때 1회만)
@app.post("/extract/financial")
async def extract_financial(
    body: AnalysisRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return await _analyze_financial(body, user, db, consume=False)

def _fin_biz_info(data, today=None):
    """재무제표에서 읽은 업체 정보 → 원클릭 입력값 (지역·업태/종목 2개까지·대표자 만 나이). 생년월일 원문은 돌려주지 않는다"""
    def clean(v):
        v = ("" if v is None else str(v)).strip().strip('"').strip()
        return "" if v.lower() in ("", "null", "none", "n/a", "-", "없음", "미상", "불명") else v
    addr = clean(data.get("address"))
    if addr:
        loc = _sido(addr)
        if loc:
            data["location"] = loc
    bt, bi = clean(data.get("business_type")), clean(data.get("business_item"))
    if bt or bi:
        i1, t1, i2, t2 = _pair_biz(bt, bi)
        for k, v in (("industry", i1), ("item", t1), ("industry2", i2), ("item2", t2)):
            if v:
                data[k] = v
    b = _re2.sub(r"\D", "", clean(data.pop("birth_date", None)))
    today = today or (datetime.utcnow() + timedelta(hours=9))
    if len(b) == 6:                                   # YYMMDD → 올해보다 뒤면 1900년대
        yy = int(b[:2]); b = (("19" if yy > today.year % 100 else "20") + b)
    if len(b) == 8:
        try:
            y, m, d = int(b[:4]), int(b[4:6]), int(b[6:8])
            if 1900 < y <= today.year and 1 <= m <= 12 and 1 <= d <= 31:
                age = today.year - y - ((today.month, today.day) < (m, d))
                if 17 <= age <= 100:
                    data["rep_age"] = age
        except Exception:
            pass
    for k in ("address", "business_type", "business_item"):
        data.pop(k, None)
    return data


async def _analyze_financial(body, user, db, consume: bool):
    from fastapi.concurrency import run_in_threadpool
    subscribed = await run_in_threadpool(user_has_subscription_access, db, user)
    if consume and not subscribed:
        await run_in_threadpool(_check_free_quota, db, user)

    prompt = """이 문서는 한국 기업/개인사업자의 재무제표입니다(이미지 또는 PDF, 1장 이상 첨부될 수 있음). 여러 장이 첨부된 경우 표지·재무상태표·손익계산서 등 서로 다른 페이지일 수 있으니, 페이지 번호(예: "1/5", "2/5")를 참고해 순서대로 모든 이미지를 함께 분석한 뒤 아래 항목들을 추출하세요. 절대로 짐작하지 말고, 각 숫자가 어느 페이지의 어느 항목(계정과목/코드)에서 나온 것인지 반드시 확인하고 추출하세요.

━━━ 인식 절차 (반드시 2단계로 진행) ━━━
1단계: 표/양식의 각 행에서 "계정과목명(라벨 문구)"과 "금액"을 짝지어 먼저 읽으세요. 이때 라벨 문구는 한 글자씩 정확히 확인하세요 (예: "매출액"과 "매출총이익", "자본금"과 "자본총계", "영업이익"과 "영업외수익"처럼 이름이 비슷한 항목을 혼동하지 않도록 주의).
2단계: 1단계에서 읽은 라벨-금액 짝을 바탕으로 아래 추출 항목에 매핑하세요. 매핑하기 전에 라벨 문구가 정확히 일치하는지 한 번 더 재확인하세요.
이미지의 글씨가 작거나 스캔 화질이 낮아 흐릿한 경우, 숫자의 자릿수(0의 개수)와 콤마 위치, 마이너스(-) 부호 유무를 특히 신중하게 재확인하세요. 여전히 확신이 서지 않는 항목은 null로 두거나 comment에 "OO 항목은 화질 문제로 불확실함"과 같이 명시하세요.

━━━ 국세청 홈택스 "표준재무제표증명" 양식인 경우 참고 (코드번호는 문서마다 조금씩 다를 수 있으니 반드시 계정과목명으로도 재확인) ━━━
[표준재무상태표]
- 유동부채(계) → current_liabilities
- 비유동부채(계) → noncurrent_liabilities
- 부채총계(Ⅰ+Ⅱ) → current_liabilities + noncurrent_liabilities 합과 반드시 일치해야 함
- 자본금 (부채총계 아래, 보통 "Ⅲ.자본금" 항목) → capital_stock ※ 개인사업자는 마이너스(-)로 표기되는 경우가 흔함. 부호를 반드시 그대로 반영할 것
- 당기순이익 (보통 "Ⅳ.당기순이익") → 아래 손익계산서의 최종 당기순손익과 반드시 같은 값이어야 함
- 자본총계(Ⅲ+Ⅳ) → total_equity ※ "자본금 + 당기순이익"과 반드시 일치해야 함. 절대 다른 항목(예: 영업외수익 등)을 여기 넣지 말 것
- 부채및자본총계(Ⅰ+Ⅱ+Ⅲ+Ⅳ) → 유동자산+비유동자산(자산총계)과 반드시 일치해야 함
- 유동자산(Ⅰ) → current_assets, 비유동자산(Ⅱ) → noncurrent_assets
- 유동부채 하위 "단기차입금"(있으면 "유동성장기부채"도 더함) → short_borrowings, 비유동부채 하위 "장기차입금" → long_borrowings (세부 항목 "기타장기차입금" 등이 같이 있으면 상위 합계 행 하나만. 없으면 null)

[표준손익계산서]
- Ⅰ.매출액 → revenue
- Ⅴ.영업손익(Ⅲ-Ⅳ) → operating_income
- 영업외비용 항목 중 "1.이자비용" → interest_expense (연간 금액 그대로. "영업외비용" 총액이 아니라 그 하위의 "이자비용" 세부 항목만 가져올 것)
- Ⅷ.당기순손익(Ⅴ+Ⅵ-Ⅶ) 또는 문서 맨 마지막 최종 순이익 항목 → net_income ※ 영업외수익(Ⅵ) 등 중간 항목과 절대 혼동하지 말 것. 반드시 "당기순손익/당기순이익"이라는 이름이 붙은 최종 항목만 사용

⚠️ 절대 혼동하면 안 되는 항목들 (실제로 자주 발생하는 오류):
- "매입채무"(재무상태표, 유동부채 하위 항목)는 자본금이 아닙니다. 자본금은 반드시 부채총계 아래, 별도의 "Ⅲ.자본금" 행에서만 가져오세요.
- "통신비", "여비교통비", "광고선전비", "운반비", "지급수수료", "세금과공과", "소모품비" 등은 판매비및관리비의 세부 항목일 뿐, 당기순이익도 이자비용도 아닙니다. 이자비용은 오직 "영업외비용" 섹션 하위의 "1.이자비용" 행에서만, 당기순이익은 오직 손익계산서 맨 마지막 "당기순손익/당기순이익" 행에서만 가져오세요.
- 숫자가 비슷한 자릿수라고 해서 근처에 있는 다른 계정과목의 금액을 가져오면 안 됩니다. 반드시 라벨(계정과목명) 전체를 읽고 정확히 일치하는 행에서만 값을 가져오세요.

━━━ 추출 후 자체 검증 (반드시 수행) ━━━
1. current_assets + noncurrent_assets ≈ current_liabilities + noncurrent_liabilities + total_equity (자산총계 = 부채총계 + 자본총계)
2. total_equity ≈ capital_stock + net_income (자본총계 = 자본금 + 당기순이익)
위 두 식이 맞지 않으면, 각 숫자를 다시 원본에서 확인하고 올바른 값으로 정정하세요. 그래도 확신이 없으면 comment에 어떤 항목이 불확실한지 명시하세요.

추출 항목: company_name(회사명, 문자열), rep_name(대표자명, 문자열), business_number(사업자등록번호, 문자열, 000-00-00000 형식), address(사업장 소재지), business_type(업태), business_item(종목), birth_date(대표자 생년월일 YYYY-MM-DD),
revenue(매출액), current_assets(유동자산), noncurrent_assets(비유동자산), current_liabilities(유동부채),
noncurrent_liabilities(비유동부채), capital_stock(자본금), total_equity(자본총계),
operating_income(영업이익), interest_expense(이자비용), net_income(당기순이익), short_borrowings(단기차입금), long_borrowings(장기차입금)

대표자명과 사업자등록번호는 재무제표 표지, 법인/개인 정보란, 사업자등록증 첨부 등에서 찾을 수 있습니다. 문서에 없으면 null로 두세요.

━━━ 업체 정보 (표준재무제표증명 상단 납세자 정보란 등) ━━━
- address: "사업장 소재지"(또는 "사업장소재지", "본점소재지", "주소") 주소 전체. 사업장 소재지가 있으면 그것을 우선.
- business_type, business_item: "업태", "종목". 여러 개면 쉼표(,)로 구분해 문서 순서대로 모두 적고, 업태와 종목은 같은 순서로 짝을 맞추세요.
- birth_date: 대표자의 "생년월일"을 "YYYY-MM-DD"로. 생년월일 칸이 없고 주민등록번호만 있으면 앞 6자리(YYMMDD)만 보고 생년월일로 바꾸세요. 주민등록번호 뒷자리는 절대 읽거나 적지 마세요. 법인등록번호는 생년월일이 아닙니다. 없으면 null.

아래 JSON 형식으로만 응답하세요. 없는 항목은 null:
{"company_name":문자열또는null,"rep_name":문자열또는null,"business_number":문자열또는null,"address":문자열또는null,"business_type":문자열또는null,"business_item":문자열또는null,"birth_date":문자열또는null,"revenue":숫자또는null,"current_assets":숫자또는null,"noncurrent_assets":숫자또는null,"current_liabilities":숫자또는null,
"noncurrent_liabilities":숫자또는null,"capital_stock":숫자또는null,"total_equity":숫자또는null,
"operating_income":숫자또는null,"interest_expense":숫자또는null,"net_income":숫자또는null,"short_borrowings":숫자또는null,"long_borrowings":숫자또는null,
"comment":"인식 관련 메모 1~2문장. 자체 검증에서 불일치가 있었다면 반드시 언급"}"""

    # 다중 이미지(최대 10장) 지원. images가 있으면 우선 사용, 없으면 단일 image_base64로 하위호환 처리
    images_payload = body.images if body.images else (
        [ImageItem(data=body.image_base64, mime=body.image_mime)] if body.image_base64 else []
    )
    if not images_payload:
        raise HTTPException(status_code=400, detail="분석할 이미지가 없습니다.")
    if len(images_payload) > 10:
        raise HTTPException(status_code=400, detail="이미지는 최대 10장까지 첨부할 수 있습니다.")

    image_parts = [
        {"inline_data": {"mime_type": img.mime or "image/jpeg", "data": img.data}}
        for img in images_payload
    ]

    # PDF/이미지 모두 지원
    import json, re

    # 폴백 모델 순서 (정확도 우선 1차 → 속도우선 폴백 → 최신 GA 모델)
    GEMINI_MODELS = [
        "gemini-2.5-flash",        # 1차: 표준 Flash — 정확도 우선
        "gemini-2.5-flash-lite",   # 2차: 1차 과부하/오류 시 가볍고 빠른 폴백
        "gemini-3.7-flash",        # 3차: 최신 GA 모델 (앞 두 모델 전체 장애 시 대비)
    ]

    async def call_gemini(client: httpx.AsyncClient, prompt_text: str):
        """이미지 + 프롬프트로 Gemini를 호출하고, 모델 폴백을 거쳐 파싱된 JSON(dict)을 반환. 실패 시 (None, 에러메시지)."""
        import asyncio
        last_err = None
        for model in GEMINI_MODELS:
            # 일시적 과부하(503/429)에 대비해 같은 모델로 최대 2회 시도 (1회 재시도, 0.8초 대기)
            for attempt in range(2):
                try:
                    resp = await client.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}",
                        headers={"Content-Type": "application/json"},
                        json={
                            "contents": [{
                                "parts": [
                                    *image_parts,
                                    {"text": prompt_text}
                                ]
                            }],
                            "generationConfig": {"temperature": 0, "maxOutputTokens": 8000}
                        }
                    )
                    if resp.status_code in (503, 429):
                        last_err = resp.text
                        if attempt == 0:
                            await asyncio.sleep(0.8)
                            continue  # 같은 모델로 한 번 더 시도
                        break  # 재시도까지 실패 → 다음 모델로
                    if resp.status_code != 200:
                        last_err = resp.text
                        break  # 다음 모델로
                    try:
                        candidate_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                        candidate_clean = re.sub(r"```json|```", "", candidate_text).strip()
                        start = candidate_clean.find("{")
                        end = candidate_clean.rfind("}")
                        if start == -1 or end == -1:
                            raise ValueError("JSON 객체를 찾을 수 없음")
                        return json.loads(candidate_clean[start:end + 1]), None
                    except Exception as parse_err:
                        last_err = f"JSON 파싱 실패: {parse_err}"
                        break  # 다음 모델로
                except Exception as e:
                    last_err = str(e)
                    break  # 다음 모델로
        return None, last_err

    def find_mismatches(d: dict) -> list[str]:
        """추출된 수치들의 산술 정합성을 검증. 문제가 있으면 한국어 설명 리스트를 반환."""
        issues = []
        TOL = 1000  # 원 단위 허용 오차 (반올림 등 감안)

        ca, nca = d.get("current_assets"), d.get("noncurrent_assets")
        cl, ncl = d.get("current_liabilities"), d.get("noncurrent_liabilities")
        cap, te, ni = d.get("capital_stock"), d.get("total_equity"), d.get("net_income")

        if all(v is not None for v in [ca, nca, cl, ncl, te]):
            assets = ca + nca
            liab_equity = cl + ncl + te
            if abs(assets - liab_equity) > TOL:
                issues.append(
                    f"자산총계({assets:,.0f})가 부채총계+자본총계({liab_equity:,.0f})와 일치하지 않습니다. "
                    f"유동자산/비유동자산/유동부채/비유동부채/자본총계 중 잘못 읽은 값이 있을 수 있습니다."
                )
        if all(v is not None for v in [cap, te, ni]):
            expected_equity = cap + ni
            if abs(expected_equity - te) > TOL:
                issues.append(
                    f"자본총계({te:,.0f})가 자본금+당기순이익({expected_equity:,.0f})과 일치하지 않습니다. "
                    f"자본금, 당기순이익, 자본총계 중 잘못 읽은 값이 있을 수 있습니다 (판매비/관리비 세부항목이나 "
                    f"매입채무 등 다른 항목의 숫자를 착각했을 가능성이 높습니다)."
                )
        return issues

    async with httpx.AsyncClient(timeout=90) as client:
        data, last_error = await call_gemini(client, prompt)

        if data is not None:
            mismatches = find_mismatches(data)
            if mismatches:
                # 산술 검증 실패 → 구체적인 불일치 내용을 알려주고 동일 이미지로 재확인 요청
                correction_prompt = prompt + f"""

━━━ 재확인 요청 ━━━
방금 아래와 같이 추출했으나, 값들 사이의 산술 검증에 실패했습니다:
{json.dumps(data, ensure_ascii=False)}

발견된 불일치:
- """ + "\n- ".join(mismatches) + """

원본 이미지를 다시 꼼꼼히 확인해서, 특히 자본금/자본총계/당기순이익/이자비용 항목을 판매비및관리비 세부항목(통신비, 여비교통비, 광고선전비, 운반비, 지급수수료 등)이나 부채 세부항목(매입채무 등)과 절대 혼동하지 말고 정확한 위치에서 다시 읽어 전체 항목을 다시 추출하세요. 같은 JSON 형식으로만 응답하세요."""

                corrected_data, correction_err = await call_gemini(client, correction_prompt)
                if corrected_data is not None:
                    still_wrong = find_mismatches(corrected_data)
                    if still_wrong:
                        note = " / ".join(still_wrong)
                        corrected_data["comment"] = (corrected_data.get("comment") or "") + \
                            f" ⚠️ 재확인 후에도 일부 수치가 서로 맞지 않아 오인식 가능성이 있습니다: {note}"
                    data = corrected_data
                else:
                    # 재확인 실패 시 원래 데이터에 경고만 덧붙여 사용
                    note = " / ".join(mismatches)
                    data["comment"] = (data.get("comment") or "") + f" ⚠️ 일부 수치 정합성 검증에 실패했습니다: {note}"

    if data is None:
        raise HTTPException(status_code=500, detail=f"AI 분석 서버가 일시적으로 불안정합니다. 잠시 후 다시 시도해주세요. ({last_error[:150] if last_error else ''})")

    if isinstance(data, dict):
        _fin_biz_info(data)
    # 구독 → 전체 공개 / 예전 충전분이 있으면 1건 차감 후 전체 공개 / 둘 다 없으면 무료(일부 공개)
    if not consume:
        return {"data": data}
    used, full = _consume_analysis(db, user, subscribed)
    return {"data": data, "credits_used": used, "remaining_credits": user.credits, "subscribed": subscribed, "full": full}

# 직접 입력(수동) 분석 — AI 호출 없이 프론트에서 계산하지만, 크레딧 차감은 서버에서 검증
# ══════════════════════════════════════════════════════════════
# 원클릭 분석 — 예상 한도 계산 (서버 전용: 기관별 기준은 외부에 노출하지 않는다)
#  · 기관별 한도 = 기준 매출 × 25% ~ 30% (청년 대표 25% ~ 50%, 미소금융은 1천만 ~ 2천만 원)
#  · 순서에 따라 중복 지원: 소진공 → (재단 · 신용보증기금 · 기술보증기금 중 1곳) → 미소
#  · 재단 · 신보 · 기보는 셋 중 한 곳만 — 그중 한 곳에 기대출이 있으면 나머지 두 곳은 불가
#  · 기대출(어느 기관 정책자금이든, 기관을 모르는 대출이든)은 미소금융을 뺀 모든 기관 한도에서 차감
#  · 소상공인이 아니면(상시근로자 5인 이상, 제조·건설·운수·광업 10인 이상) 소진공 제외
#  · 업력 1년 미만 + (제조 · 콘텐츠 제작 · 특허) → 매출이 없어도 기보 5천만 ~ 1억
#  · 중진공 최대 5억(운전자금 연 5억, 수출기업·최근 1년 10명 이상 고용 증가 10억) / 신보·기보 최대 10억 / 소진공·재단 최대 2억 (2억 이상이 한 금액이면 1억 ~ 2억 범위)
#  · 1천만 원 미만은 한도 없음 (미소금융만 500만 원)
#  · 총 예상한도 = 기준 매출 × 25% ~ 30%(청년 50%) × 신용 조정 − 기대출 합계 (최대 10억)
#  · 소진공 신용취약소상공인자금: 소상공인 + 업력 3개월 이상 + 개인신용평점 839 이하 → 최대 3천만 원 (소진공 줄을 이 금액으로)
#  · 법인은 미소금융 불가. 미소금융은 KCB 700 이하 또는 NICE 749 이하
#  · 신용점수(NICE, 없으면 KCB): 소진공·중진공 805 / 재단 805(755 이상 0.9) / 신보·기보 700 — 미달 50점 이내 0.8, 100점 이내 0.6, 그 이하 0.4
#  · 신보·기보 1천만 원 단위 내림, 9천만 원대는 8천만 원
#  · 중진공·신보·기보는 매출 3억 이상, 가능하면 최소 1억 / 기보는 제조·기술·지식서비스·콘텐츠·공연예술
#  · 수출업체는 소진공·중진공·기보·신보 모두 가능 (신보 제한업종은 제외)
#  · 신보 제한업종 제외 / 유의업종 70% / 재단 우선취급업종이면 신보 제외(매출 5억 이상이면 가능)
#  · 이미 대출이 있는 기관은 조건과 관계없이 남은 한도를 보여준다
#  · 부채비율(부채총계÷자본총계) 제한 — 창업년월이 있을 때만 업력 예외를 따지고, 없으면 업력 예외 없이 계산
#    - 소진공: 업력 7년 이상이면 부채비율 700% 초과 또는 총차입금(기대출) > 매출 → 신청 불가
#    - 중진공: 업력 7년 이상이면 업종별 제한부채비율(2026 융자계획 별표5) 초과 → 신청 불가
#              (간편장부대상 개인사업자·협동조합은 제외 / 업태로 중분류 → 없으면 대분류 → 기타 500%)
#    - 지역신보재단: 총차입금(기대출) > 매출 → 신청 불가
#    - 신보·기보: 부채비율 550% 초과 → 신청 불가 (업력 무관)
#  · 개인대출(구분 '개인')은 한도에서 빼지 않음 / 카드론·현금서비스·대부업이 합쳐서 3건 이상이면 신청 어려움(전체)
#  · 재무제표 차입금이 있으면 기관 미상 기대출 대신 '단기차입금 전액 + 장기차입금(담보로 봄) 30%'를 차감
#    (최근 1년 고용 유지·증가면 장기 25%). 정책기관 기대출은 그 기관에서 전액 차감하고, 차입금 차감분에서는 그만큼 뺌
#  · 대출 후 부채비율 상한: 신보·기보 550%, 소진공 700%·중진공 업종별(업력 7년 이상만) → 기관 한도 ≤ 자본×기준 − 현재 부채
#  · 완전자본잠식: 소진공(업력 7년 이상)·재단 불가, 중진공은 업력 5년 초과 + 당기 적자(모르면 적자로 봄)면 불가, 신보·기보 불가
#  · 신용점수(NICE, 없으면 KCB) 700 미만이면 한도는 줄이고(신용 조정), 종합 평가에 '금융거래가 어려울 수 있음' 안내
#  · 청년 대표(만 39세 이하) 50%는 소진공·재단·신보만 — 중진공·기보는 30%
#    - 2~3년 연속 조건(연속 적자·이자보상배수 등)은 1년치 재무로 판단할 수 없어 보지 않는다. 신보·기보는 공개 기준 없음
# ══════════════════════════════════════════════════════════════
import math as _math
import re as _re
from datetime import datetime as _dt, timedelta as _td

HT_LIMIT_RULE = {
    "minRatio": 0.25, "maxRatio": 0.30, "youthMaxRatio": 0.50, "youthAge": 39,
    "miso": {"min": 10000000, "max": 20000000},
    "sojinMax": 200000000, "jaedanMax": 200000000, "fundMax": 1000000000,
    "minAmount": 10000000, "misoMinAmount": 5000000,
    "credit": {"sojin": {"full": 805, "min": 805}, "jungjin": {"full": 805, "min": 805}, "jaedan": {"full": 805, "min": 755},
               "kodit": {"full": 700, "min": 700}, "kibo": {"full": 700, "min": 700}},
    "fundMinSales": 300000000, "fundMinAmount": 100000000, "koditPriorityException": 500000000,
    "koditCautionFactor": 0.7, "misoKcb": 700, "misoNice": 749,
    "weakMax": 30000000, "weakScore": 839, "unit": 1000000,
    "totalMax": 1000000000,
    "jungjinMax": 500000000, "jungjinExportMax": 1000000000,
    "collateralRate": 0.30, "collateralRateEmp": 0.25,      # 장기차입금(담보로 봄)은 30%만 차감 (최근 1년 고용 유지·증가면 25%)
}


_HT_RISKY_LOAN = _re.compile(r"카드|현금\s*서비스|대부|러시앤캐시|산와|리드코프")


def _ht_loan_amt(l, inp=None):
    return _ht_num(l.get("amount")) or 0


def _ht_fin_borrow(inp):
    """재무제표 차입금 → 한도에서 뺄 금액 (단기 100% + 장기 30%/25%). 차입금 칸이 둘 다 비어 있으면 None"""
    fin = inp.get("fin") or {}
    sb, lb = _ht_num(fin.get("short_borrowings")), _ht_num(fin.get("long_borrowings"))
    if sb is None and lb is None:
        return None
    emp = bool(inp.get("emp_keep")) or _ht_s(inp.get("emp_trend")) in ("up", "keep")
    rate = HT_LIMIT_RULE["collateralRateEmp"] if emp else HT_LIMIT_RULE["collateralRate"]
    return max(0, sb or 0) + max(0, lb or 0) * rate

_HT_IND = {
    "restricted": _re.compile(r"도박|사행|슬롯|룰렛|경품|상품권|경마|경륜|경정|경주장|경주권|성인용\s*게임|성인오락|카지노|유흥주점|무도장|무도유흥|안마|룸살롱|룸싸롱|접객|나이트클럽|댄스홀|콜라텍|주거용\s*건물\s*(임대|개발|공급)|아파트\s*(임대|분양)|주택\s*(임대|분양)|비디오물\s*감상|비디오방|복권|점술|점집|역술|휴게텔|전화방"),
    "caution": _re.compile(r"담배|생맥주|주점|귀금속|금은방|금괴|예술품|골동품|화랑|부동산|공인중개|중개업|분양\s*대행|상가\s*분양|토지\s*임대|농지|여관|개인택시|금융|보험|가상자산|가상화폐|암호화폐|탐정|흥신소"),
    "priority": _re.compile(r"음식점|식당|한식|중식|일식|분식|치킨|떡집|빵집|베이커리|제과점|카페|커피|음료점|주점|호프|민박|여관|모텔|야영|캠핑|숙박|고시원|택시|전세버스|화물|배달|퀵|슈퍼|편의점|반찬|철물|문구|서점|화초|꽃집|화원|음반|자동판매기|당구|게임장|게임방|PC방|피시방|오락|체력단련|헬스|노래연습|노래방|무도장|기원|욕탕|목욕|사우나|세탁|미용|헤어|네일|피부관리|점술|부동산\s*중개|공인중개|비디오"),
    "kibo": _re.compile(r"제조|소프트웨어|정보통신|정보서비스|IT|아이티|프로그램\s*개발|앱\s*개발|시스템\s*(개발|통합)|플랫폼|연구개발|엔지니어링|디자인|콘텐츠|컨텐츠|영상|영화|방송|애니메이션|게임\s*(개발|제작|소프트웨어)|음반|음원|음악\s*제작|공연|연극|뮤지컬|무용|국악|오케스트라|출판|웹툰|전시\s*기획"),
}
_HT_CONTENT = _re.compile(r"콘텐츠|컨텐츠|영상|영화|방송|애니메이션|게임\s*(개발|제작)|음반|음원|음악\s*제작|공연|연극|뮤지컬|웹툰|출판")


# 2026 중소기업 정책자금 융자계획 별표5 — 업종별 융자제한 부채비율(%). 위에서부터 먼저 맞는 것
_JJ_DR_MFG = [   # 제조업 중분류 (종목·업태에서)
    (r"음료|주류|생수|맥주|소주|막걸리", 387.6),
    (r"식료품|식품|제과|제빵|빵|떡|육가공|수산물\s*가공|반찬|김치|소스|조미|도시락|두부|면류|과자|유제품|사료", 444.3),
    (r"섬유|원단|방적|직물|편직|염색", 355.5),
    (r"의복|의류|봉제|모피|액세서리", 354.9),
    (r"가죽|가방|신발|구두", 329.7),
    (r"목재|나무", 442.2),
    (r"펄프|종이|골판지|지류", 344.7),
    (r"인쇄|기록매체", 350.7),
    (r"코크스|연탄|석유\s*정제", 283.2),
    (r"의약|의료용\s*물질|제약|원료의약", 215.7),
    (r"화학|화장품|세제|도료|페인트|비료|농약|접착제", 295.8),
    (r"고무|플라스틱|비닐", 331.8),
    (r"비금속|유리|시멘트|레미콘|콘크리트|도자기|요업|석재", 278.1),
    (r"1차\s*금속|제철|제강|주조|주물|압연", 363.9),
    (r"금속\s*가공|금속\s*제품|금형|철구조물|판금|용접|도금|볼트|너트", 363.9),
    (r"전자부품|반도체|컴퓨터|영상|음향|통신\s*장비|디스플레이|PCB|인쇄회로", 280.8),
    (r"의료기기|의료\s*용\s*기기|정밀|광학|시계|측정", 283.2),
    (r"전기\s*장비|전동기|발전기|변압기|배터리|전지|조명|전선|케이블", 310.8),
    (r"자동차|트레일러|차량\s*부품", 438.9),
    (r"선박|항공|철도|운송장비|조선", 500.0),
    (r"가구", 483.9),
    (r"수리", 500.0),
    (r"기계|장비|설비", 327.9),
    (r"악기|운동\s*용품|체육\s*용품|완구|장난감|문구|사무용품|귀금속|장신구|간판|광고물|인형", 349.8),   # C33 기타 제품
]
_JJ_DR_BIG = [   # 대분류
    (r"제조", 340.2),
    (r"농업|작물|축산|원예|재배|양봉|임업", 500.0),
    (r"어업|양식", 500.0),
    (r"광업|채석|채굴", 500.0),
    (r"전기.*공급|가스.*공급|증기|공기조절|발전업", 500.0),
    (r"하수|폐기물|원료\s*재생|재활용|환경\s*복원", 318.0),
    (r"건설|공사|토목|인테리어|실내\s*건축", 329.1),
    (r"도매|소매|도소매|상품\s*중개|전자상거래|통신판매|무역", 393.0),
    (r"운수|운송|창고|물류|택배|화물|보관", 500.0),
    (r"숙박|음식|식당|카페|주점|외식", 500.0),
    (r"정보통신|소프트웨어|프로그래밍|정보\s*서비스|출판|방송|통신업|시스템\s*통합|컴퓨터", 425.7),
    (r"부동산", 500.0),
    (r"전문|과학|기술\s*서비스|연구\s*개발|광고|컨설팅|디자인|설계|엔지니어링|법무|회계|세무", 427.8),
    (r"사업\s*시설|사업\s*지원|임대\s*서비스|인력|경비|청소|방제", 500.0),
    (r"교육|학원", 456.9),
    (r"예술|스포츠|여가|공연|오락", 500.0),
]


def _jj_dr_limit(inp):
    """중진공 업종별 제한부채비율 — 업태(+종목)로 중분류 → 대분류, 맞는 게 없으면 기타 500%"""
    pairs = [(_ht_s(inp.get("industry")), _ht_s(inp.get("item"))), (_ht_s(inp.get("industry2")), _ht_s(inp.get("item2")))]
    pairs = [(a, b) for a, b in pairs if a or b]
    for ind, item in pairs:
        if "제조" in ind or ("제조" in item):
            for pat, v in _JJ_DR_MFG:
                if _re.search(pat, item) or _re.search(pat, ind.replace("제조업", "")):
                    return v
            return 340.2
        for pat, v in _JJ_DR_BIG:
            if _re.search(pat, ind):
                return v
    for ind, item in pairs:
        for pat, v in _JJ_DR_BIG:
            if _re.search(pat, item):
                return v
    return 500.0


def _ht_simple_book(inp, sales):
    """간편장부대상 개인사업자(직전연도 수입금액 기준) — 중진공 부채비율 제한 제외"""
    if inp.get("biz_type") == "corp" or not sales:
        return False
    t = _ht_ind_text(inp)
    if _re.search(r"농업|임업|어업|광업|도매|소매|도소매|부동산\s*매매", t): cut = 300000000
    elif _re.search(r"제조|숙박|음식|전기|가스|수도|건설|운수|운송|정보통신|출판|방송|금융|보험|폐기물|재생", t): cut = 150000000
    else: cut = 75000000
    return sales < cut


def _ht_num(v):
    """JS Number()와 같은 규칙: 빈 값·숫자 아님 → None"""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and _math.isnan(v)) else v
    try:
        s = str(v).strip()
        if s == "":
            return 0
        n = float(s)
        return None if _math.isnan(n) else n
    except Exception:
        return None


def _ht_s(v):
    return "" if v is None else str(v)


def _ht_ind_text(inp):
    """업태·종목 (두 번째 업태·종목까지)"""
    return " ".join(_ht_s(inp.get(k)) for k in ("industry", "item", "industry2", "item2") if _ht_s(inp.get(k))).strip()


def _ht_small_biz(inp):
    emp = _ht_num(inp.get("employees"))
    if emp is None:
        return True
    mfg = bool(_re.search(r"제조|건설|운수|광업", _ht_ind_text(inp)))
    return emp < (10 if mfg else 5)


def _ht_loan_org(name):
    n = _re.sub(r"\s", "", _ht_s(name))
    if not n:
        return None
    if _re.search(r"소진공|소상공인시장진흥|소상공인진흥|소상공인공단|신용취약|저신용", n): return "sojin"
    if _re.search(r"중진공|중소벤처기업진흥|중소기업진흥", n): return "jungjin"
    if _re.search(r"재단|[가-힣]{2}신보", n): return "jaedan"
    if _re.search(r"신용보증기금|신보", n): return "kodit"
    if _re.search(r"기술보증|기보", n): return "kibo"
    if _re.search(r"미소", n): return "miso"
    return None


def _ht_credit_factor(key, score):
    c = HT_LIMIT_RULE["credit"].get(key)
    if not c or score is None:
        return 1
    if score >= c["full"]: return 1
    if score >= c["min"]: return 0.9
    gap = c["min"] - score
    return 0.8 if gap <= 50 else 0.6 if gap <= 100 else 0.4


def _ht_industry(text):
    t = _ht_s(text)
    retail = ("소매" in t) and not _re.search(r"도매|도소매", t)
    return {
        "restricted": bool(_HT_IND["restricted"].search(t)),
        "caution": bool(_HT_IND["caution"].search(t)),
        "priority": retail or bool(_HT_IND["priority"].search(t)),
        "kibo": bool(_HT_IND["kibo"].search(t)),
    }


def _fund_round(v):
    r = _math.floor(v / 10000000) * 10000000
    return 80000000 if r == 90000000 else r


def _ht_startup_kibo(ctx, inp=None):
    R = HT_LIMIT_RULE
    k_used = other = 0
    for l in ctx["loans"]:
        k = _ht_loan_org(l.get("org")); a = _ht_loan_amt(l, inp or {})
        if a <= 0: continue
        if k == "kibo": k_used += a
        elif k != "miso": other += a          # 다른 기관 정책자금·기관 모를 대출도 차감
    fb = _ht_fin_borrow(inp or {})
    if fb is not None:
        other = max(0, fb - k_used)
    score = ctx["nice"] if ctx["nice"] is not None else ctx["kcb"]
    f = _ht_credit_factor("kibo", score)
    low = _fund_round(max(0, 50000000 * f - k_used - other)); high = _fund_round(max(0, 100000000 * f - k_used - other))
    if high < R["minAmount"]: low = high = 0
    else: low = max(low, R["minAmount"])
    return {"key": "kibo", "name": "기술보증기금", "low": low, "high": high, "used": k_used, "startup": True, "status": "ok" if high > 0 else "exhausted"}


def ht_limit_context(inp, today=None):
    """입력값에서 한도 계산에 필요한 값을 뽑는다 (화면 계산과 같은 규칙)"""
    today = today or (_dt.utcnow() + _td(hours=9))
    rows = []
    for y in (inp.get("years") or []):
        if not isinstance(y, dict): continue
        try: yr = int(str(y.get("year")).strip()[:4]) if y.get("year") not in (None, "") else None
        except Exception: yr = None
        vat = _ht_num(y.get("vat"))
        if yr and (vat is not None or _ht_num(y.get("purchase")) is not None or _ht_num(y.get("income")) is not None):
            rows.append({"year": yr, "vat": vat})
    rows.sort(key=lambda r: r["year"])
    vat_latest = next((r for r in reversed(rows) if r["vat"] is not None), None)
    ytd = inp.get("ytd") or {}
    m, s = _ht_num(ytd.get("month")), _ht_num(ytd.get("sales"))
    ytd_annual = s / m * 12 if (m and 1 <= m <= 12 and s is not None) else None
    fin = inp.get("fin") or None
    eroded = False
    debt_ratio = None
    equity = liab = None
    net_income = _ht_num((fin or {}).get("net_income")) if fin else None
    if fin:
        a, li = _ht_num(fin.get("assets")), _ht_num(fin.get("liabilities"))
        if a is not None and li is not None and a > 0:
            eroded = (a - li) <= 0
            if not eroded:
                debt_ratio = li / (a - li) * 100
                equity, liab = a - li, li
    all_loans = [l for l in (inp.get("loans") or []) if isinstance(l, dict) and (l.get("org") or l.get("date") or _ht_num(l.get("amount")) is not None)]
    loans = [l for l in all_loans if l.get("kind") != "personal"]          # 개인대출은 한도에서 빼지 않음
    risky = sum(1 for l in all_loans if _HT_RISKY_LOAN.search(_ht_s(l.get("org"))))   # 카드론·현금서비스·대부업 건수
    cr = inp.get("credit") or {}
    nice, kcb = _ht_num(cr.get("nice")), _ht_num(cr.get("kcb"))
    months = None
    mm = _re.match(r"^(\d{4})-(\d{1,2})", _ht_s(inp.get("founded")))
    if mm:
        mo = (today.year - int(mm.group(1))) * 12 + (today.month - int(mm.group(2)))
        if mo >= 0: months = mo
    return {"vat_latest": vat_latest["vat"] if vat_latest else None, "ytd_annual": ytd_annual, "eroded": eroded,
            "loans": loans, "nice": nice, "kcb": kcb, "credit_score": nice if nice is not None else kcb, "biz_age_months": months,
            "debt_ratio": debt_ratio, "net_income": net_income, "risky_loans": risky, "equity": equity, "liabilities": liab}


def ht_estimate_limit(inp, today=None):
    R = HT_LIMIT_RULE
    ctx = ht_limit_context(inp, today)
    lv = ctx["vat_latest"]
    sales = lv if (lv is not None and lv > 0) else (ctx["ytd_annual"] if (ctx["ytd_annual"] or 0) > 0 else None)
    ind_text = _ht_ind_text(inp)
    bam = ctx["biz_age_months"]
    startup_kibo = bam is not None and bam < 12 and ("제조" in ind_text or bool(_HT_CONTENT.search(ind_text)) or "특허" in _ht_s(inp.get("certs")))
    ar = (inp.get("arrears") or {}).get("status") or "unknown"
    if not sales:
        if not startup_kibo:
            return {"available": False, "status": "none", "low": 0, "high": 0, "orgs": [], "text": "매출을 입력하면 예상 한도를 계산합니다."}
        base0 = {"available": True, "sales": 0}
        if ar == "has": return {**base0, "status": "blocked", "low": 0, "high": 0, "orgs": [], "text": "국세 체납이 있어 현재는 신청이 어렵습니다. 체납을 해소한 뒤 진행하세요."}
        if ctx["risky_loans"] >= 3: return {**base0, "status": "blocked", "low": 0, "high": 0, "orgs": [], "text": "카드론·현금서비스·대부업 이용이 많아 현재는 신청이 어렵습니다."}
        k = _ht_startup_kibo(ctx, inp)
        if ctx["eroded"]:                    # 기보는 자본잠식이면 신청 불가
            k.update(status="blocked", low=0, high=0)
            return {**base0, "status": "blocked", "low": 0, "high": 0, "orgs": [k], "text": "완전자본잠식 상태라 현재는 신청이 어렵습니다."}
        if k["status"] != "ok": return {**base0, "status": "low", "low": 0, "high": 0, "orgs": [k], "text": "기대출이 많아 추가 한도 여력이 부족합니다."}
        return {**base0, "status": "ok", "low": k["low"], "high": k["high"], "orgs": [k], "text": ""}

    floor_u = lambda v: _math.floor(v / R["unit"]) * R["unit"]
    age = _ht_num((inp.get("rep") or {}).get("age"))
    youth = age is not None and age <= R["youthAge"]
    per = {"low": floor_u(sales * R["minRatio"]), "high": floor_u(sales * (R["youthMaxRatio"] if youth else R["maxRatio"]))}
    per_std = {"low": per["low"], "high": floor_u(sales * R["maxRatio"])}      # 청년 50%는 소진공·재단·신보만
    small = _ht_small_biz(inp)
    used = {"sojin": 0, "jungjin": 0, "jaedan": 0, "kodit": 0, "kibo": 0, "miso": 0}
    other = 0
    for l in ctx["loans"]:
        k = _ht_loan_org(l.get("org")); a = _ht_loan_amt(l, inp)
        if a <= 0: continue
        if k: used[k] += a
        else: other += a
    fb = _ht_fin_borrow(inp)
    if fb is not None:                       # 재무제표 차입금 기준 (정책기관 대출은 이미 차입금에 들어 있으므로 빼고)
        other = max(0, fb - sum(v for kk, v in used.items() if kk != "miso"))
    c_score = ctx["nice"] if ctx["nice"] is not None else ctx["kcb"]
    ind = _ht_industry(ind_text)
    factors = {}
    big_enough = sales >= R["fundMinSales"]

    def mk(key, name, lo, hi):
        f = _ht_credit_factor(key, c_score)
        if key == "kodit" and ind["caution"]: f *= R["koditCautionFactor"]
        factors[key] = f
        lo = floor_u(lo * f); hi = floor_u(hi * f)
        if key == "sojin": lo = min(lo, R["sojinMax"]); hi = min(hi, R["sojinMax"])
        if key == "jaedan": lo = min(lo, R["jaedanMax"]); hi = min(hi, R["jaedanMax"])
        if key in ("kodit", "kibo"): lo = min(lo, R["fundMax"]); hi = min(hi, R["fundMax"])
        if key == "jungjin":                  # 중진공 운전자금 연 5억 (수출기업 10억) — 2026 융자계획
            jm = R["jungjinExportMax"] if (exporter or emp_up10) else R["jungjinMax"]
            lo = min(lo, jm); hi = min(hi, jm)
        if big_enough and key in ("jungjin", "kodit", "kibo"): lo = max(lo, R["fundMinAmount"]); hi = max(hi, R["fundMinAmount"])
        ded = used["miso"] if key == "miso" else (sum(v for kk, v in used.items() if kk != "miso") + other)   # 다른 기관 정책자금도 모든 기관 한도에서 차감
        low = max(0, lo - ded); high = max(0, hi - ded)
        if key in ("kodit", "kibo"): low = _fund_round(low); high = _fund_round(high)
        if key == "jungjin": low = floor_u(low); high = floor_u(high)
        if low == high and high >= 200000000: low = high - 100000000
        min_a = R["misoMinAmount"] if key == "miso" else R["minAmount"]
        if high < min_a: low = high = 0
        else: low = max(low, min_a)
        return {"key": key, "name": name, "low": low, "high": high, "used": used[key], "status": "ok" if high > 0 else "exhausted"}

    orgs = []
    exporter = (_ht_num(inp.get("export_amount")) or 0) > 0 or bool(_ht_s(inp.get("export")).strip())
    emp_up10 = (_ht_num(inp.get("emp_increase")) or 0) >= 10          # 최근 1년 10명 이상 고용 증가 → 중진공 운전자금 연 10억
    kodit_ok = (not ind["restricted"]) and (exporter or (big_enough and ((not ind["priority"]) or sales >= R["koditPriorityException"])))
    kibo_ok = exporter or (big_enough and ind["kibo"])
    if small or exporter: orgs.append(mk("sojin", "소상공인시장진흥공단", per["low"], per["high"]))
    if big_enough or exporter or used["jungjin"] > 0: orgs.append(mk("jungjin", "중소벤처기업진흥공단", per_std["low"], per_std["high"]))
    g = [mk("jaedan", "지역신용보증재단", per["low"], per["high"])]
    if kodit_ok or used["kodit"] > 0: g.append(mk("kodit", "신용보증기금", per["low"], per["high"]))
    if kibo_ok or used["kibo"] > 0 or startup_kibo:
        kb = mk("kibo", "기술보증기금", per_std["low"], per_std["high"]) if (kibo_ok or used["kibo"] > 0) else None
        if startup_kibo:
            sk = _ht_startup_kibo(ctx, inp)
            if not kb or sk["high"] > kb["high"]: kb = sk
        g.append(kb)
    in_use = [o for o in g if o["used"] > 0]
    chosen = None
    if in_use:
        chosen = sorted(in_use, key=lambda o: -o["high"])[0]
        for o in g:
            if o is not chosen: o["status"] = "blocked"; o["low"] = 0; o["high"] = 0
    orgs.extend(g)
    # 부채비율·차입금 제한 (창업년월이 없으면 업력 예외 없이 = 7년 이상으로 봄)
    dr = ctx["debt_ratio"]
    mature = bam is None or bam >= 84
    loan_total = sum((_ht_num(l.get("amount")) or 0) for l in ctx["loans"])
    over_borrow = loan_total > sales
    def block(key):
        for o in orgs:
            if o["key"] == key:
                o["status"] = "blocked"; o["low"] = 0; o["high"] = 0
    # 완전자본잠식(자산 ≤ 부채) — 기관별로
    if ctx["eroded"]:
        if mature: block("sojin")                                            # 소진공: 업력 7년 이상
        ni = ctx["net_income"]
        if (bam is None or bam > 60) and (ni is None or ni < 0): block("jungjin")   # 중진공 한계기업: 업력 5년 초과 + 적자(당기 기준)
        block("jaedan")                                                      # 재단: 자기자본 전액 잠식
        block("kodit"); block("kibo")                                        # 신보·기보
    if mature and ((dr is not None and dr > 700) or over_borrow):
        block("sojin")
    if mature and dr is not None and dr > _jj_dr_limit(inp) and not _ht_simple_book(inp, sales) \
            and "협동조합" not in _ht_s(inp.get("company_name")):
        block("jungjin")
    if over_borrow:
        block("jaedan")
    if dr is not None and dr > 550:
        block("kodit"); block("kibo")
    # 대출 후에도 기관 부채비율 기준을 넘지 않는 금액까지만 (대출금만큼 부채 증가, 자본은 그대로)
    #   최대 = 자본 × 기준비율 − 현재 부채
    capped = False
    E, Lb = ctx["equity"], ctx["liabilities"]
    if E is not None and Lb is not None and E > 0:
        lim = {"kodit": 550.0, "kibo": 550.0}
        if mature:
            lim["sojin"] = 700.0
            if not _ht_simple_book(inp, sales) and "협동조합" not in _ht_s(inp.get("company_name")):
                lim["jungjin"] = _jj_dr_limit(inp)
        for o in orgs:
            r_ = lim.get(o["key"])
            if r_ is None or o["status"] != "ok":
                continue
            room = max(0, E * r_ / 100 - Lb)
            if room >= o["high"]:
                continue
            hi = _fund_round(room) if o["key"] in ("kodit", "kibo") else floor_u(room)
            capped = True
            if hi < R["minAmount"]:
                o["status"] = "exhausted"; o["low"] = 0; o["high"] = 0
                continue
            lo = min(o["low"], hi)
            if lo == hi and hi >= 200000000: lo = hi - 100000000
            o["low"], o["high"] = max(lo, R["minAmount"]), hi

    kcb, nice = ctx["kcb"], ctx["nice"]
    miso_ok = (kcb is not None and kcb <= R["misoKcb"]) or (nice is not None and nice <= R["misoNice"])
    if inp.get("biz_type") != "corp" and miso_ok: orgs.append(mk("miso", "미소금융", R["miso"]["min"], R["miso"]["max"]))

    score = ctx["credit_score"]
    if small and score is not None and score <= R["weakScore"] and (bam is None or bam >= 3):
        weak_used = sum((_ht_num(l.get("amount")) or 0) for l in ctx["loans"] if _re.search(r"신용취약|저신용", _ht_s(l.get("org"))))
        sj = next((o for o in orgs if o["key"] == "sojin"), None)
        cap = max(0, R["weakMax"] - weak_used)
        amt = min(cap, sj["high"] if sj else cap)
        fixed = _math.floor(amt / R["unit"]) * R["unit"]
        if sj:
            sj["weak"] = True
            if fixed < R["minAmount"]: sj["low"] = 0; sj["high"] = 0; sj["status"] = "exhausted"
            else: sj["high"] = fixed; sj["low"] = max(R["minAmount"], fixed - 10000000); sj["status"] = "ok"

    base = {"available": True, "sales": sales, "small": small, "orgs": orgs}
    if ar == "has": return {**base, "status": "blocked", "low": 0, "high": 0, "text": "국세 체납이 있어 현재는 신청이 어렵습니다. 체납을 해소한 뒤 진행하세요."}
    if ctx["risky_loans"] >= 3:
        for o in orgs: o["status"] = "blocked"; o["low"] = 0; o["high"] = 0
        return {**base, "status": "blocked", "low": 0, "high": 0, "text": "카드론·현금서비스·대부업 이용이 많아 현재는 신청이 어렵습니다."}
    loan_sum = sum(used.values()) + other
    ok_keys = [o["key"] for o in orgs if o["status"] == "ok" and o["key"] != "miso"]
    tf = max(factors.get(k, 1) or 1 for k in ok_keys) if ok_keys else 1
    # 총 예상한도 최대 10억 (기대출은 이 안에서 차감)
    t_per = per if any(o["status"] == "ok" and o["key"] in ("sojin", "jaedan", "kodit") for o in orgs) else per_std
    t_low = max(0, min(floor_u(t_per["low"] * tf), R["totalMax"]) - loan_sum); t_high = max(0, min(floor_u(t_per["high"] * tf), R["totalMax"]) - loan_sum)
    if capped or any(o["status"] in ("blocked", "exhausted") for o in orgs):
        okd = {o["key"]: o for o in orgs if o["status"] == "ok"}
        if not okd and not any(o["status"] == "blocked" for o in orgs):
            return {**base, "status": "low", "low": 0, "high": 0, "text": "부채 규모가 커 추가 한도 여력이 부족합니다." if capped else "기대출이 많아 추가 한도 여력이 부족합니다."}
        if not okd:
            return {**base, "status": "blocked", "low": 0, "high": 0, "text": "완전자본잠식 상태라 현재는 신청이 어렵습니다." if ctx["eroded"] else "현재 재무 상태로는 신청 가능한 기관이 없습니다."}
        grp = [okd[k] for k in ("jaedan", "kodit", "kibo") if k in okd]
        pick = [okd[k] for k in ("sojin", "jungjin", "miso") if k in okd] + ([max(grp, key=lambda o: o["high"])] if grp else [])
        t_low = min(t_low, sum(o["low"] for o in pick)); t_high = min(t_high, sum(o["high"] for o in pick))
    t_low, t_high = floor_u(t_low), floor_u(t_high)
    if t_high < R["minAmount"]: return {**base, "status": "low", "low": 0, "high": 0, "text": "부채 규모가 커 추가 한도 여력이 부족합니다." if capped else "기대출이 많아 추가 한도 여력이 부족합니다."}
    t_low = max(t_low, R["minAmount"])
    if t_low == t_high and t_high >= 200000000: t_low = t_high - 100000000                       # 한 금액이면 1억 아래까지 범위로
    return {**base, "status": "ok", "low": t_low, "high": t_high, "text": ""}


def _ht_clean(o):
    """소수점 없는 실수는 정수로 (응답을 깔끔하게)"""
    if isinstance(o, float) and o.is_integer():
        return int(o)
    if isinstance(o, dict):
        return {k: _ht_clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_ht_clean(v) for v in o]
    return o

# ══════════════════════════════════════════════════════════════
# CODEF — 홈택스 간편인증 자동 불러오기
#  Render 환경변수: CODEF_CLIENT_ID / CODEF_CLIENT_SECRET / CODEF_PUBLIC_KEY / CODEF_HOST
#  흐름: ① /codef/hometax/start → 홈택스가 휴대폰으로 인증 요청(CF-03002, 추가인증)
#        ② 사용자가 휴대폰에서 승인 → /codef/hometax/complete (simpleAuth="1", is2Way=true, twoWayInfo)
#        ③ 같은 id(로그인 세션)로 나머지 서류를 이어서 조회(다건 요청)
#  서버에 상태를 저장하지 않는다 — 1차 요청 정보(twoWayInfo 포함)를 화면에 돌려주고 2차 요청 때 그대로 받는다.
# ══════════════════════════════════════════════════════════════
import json as _json
import base64 as _b64
import time as _time
import re as _re2
from urllib.parse import quote as _cq, unquote_plus as _cuq

CODEF_OAUTH_URL = "https://oauth.codef.io/oauth/token"
_codef_token_cache = {"token": None, "exp": 0}

# 홈택스 상품 (개발가이드 기준). path가 비어 있으면 아직 연결 전.
CODEF_HOMETAX_PRODUCTS = {
    "registration": "/v1/kr/public/nt/proof-issue/corporate-registration",        # 사업자등록 증명
    "vat": "/v1/kr/public/nt/proof-issue/additional-tax-standard",                # 부가세과세표준증명
    "withholding": "/v1/kr/public/nt/report/withholding-tax",                     # 원천징수이행상황신고서 — 기관코드 0005, 근로소득 간이세액(A01) 인원 = 직원 수
    "taxfree": "/v1/kr/public/nt/proof-issue/tax-free-businessman-income",        # 부가가치세 면세사업자 수입금액증명 — 회원(0/5), 08~22시, 면세사업자만 발급
    "tax_payment": "/v1/kr/public/nt/proof-issue/tax-cert-all",                    # 납세증명서
    "financial": "/v1/kr/public/nt/proof-issue/standard-financial-statements",     # 재무제표(표준재무제표증명) — 08:00~22:00
    "employees": "/v1/kr/public/cw/kcomwel-employment/detail",                     # 근로복지공단 근로자고용정보현황 → 직원 수 (기관이 달라 간편인증 승인 1번 더)
}
CW_LEVELS = {"1", "3", "4", "5", "6", "7", "8", "10"}       # 근로복지공단이 지원하는 간편인증 수단


def _cw_body(params, telecom="", corp=None, bizno=""):
    """근로자고용정보현황 요청 — 지금 일하는 일반 근로자(고용보험·산재 전체)"""
    common = {"organization": "0001", "insuranceType": "0", "state": "1", "type": "1", "id": f"{params.get('id', 'fina')}-cw",
              "manageNoUseYN": "1"}      # 사업장관리번호를 모르니 목록을 받아 고른다(2-way). 0이면 CF-12411
    if params.get("loginType") in ("5", "6"):
        if params.get("loginTypeLevel") not in CW_LEVELS or not telecom or not params.get("phoneNo"):
            return None
        return dict(common, loginType="5", loginTypeLevel=params["loginTypeLevel"], loginUserName=params.get("userName", ""),
                    telecom=telecom, phoneNo=params["phoneNo"], userType="0")
    if params.get("loginType") == "0":
        b = dict(common, loginType="0", certType=params.get("certType", "1"), certFile=params.get("certFile", ""),
                 certPassword=params.get("certPassword", ""), userType="2" if corp else "0")
        if params.get("keyFile"):
            b["keyFile"] = params["keyFile"]
        if bizno:
            b["identity"] = bizno
        return b
    return None


def _reg_rrn(reg_data, want_biz=""):
    """사업자등록증명(주민번호 공개)에서 대표자 주민등록번호 13자리 — 서버 안에서 근로복지공단 조회에만 쓰고 저장·반환하지 않는다"""
    rows = reg_data if isinstance(reg_data, list) else [reg_data]
    rows = [r for r in rows if isinstance(r, dict)]
    digits = _re2.sub(r"\D", "", want_biz or "")
    row = next((r for r in rows if digits and _re2.sub(r"\D", "", r.get("resCompanyIdentityNo") or "") == digits), rows[0] if rows else {})
    raw = str(row.get("resUserIdentiyNo") or "")
    n = _re2.sub(r"\D", "", raw)
    bn = _re2.sub(r"\D", "", row.get("resCompanyIdentityNo") or "")
    kind = str(row.get("resBusinessmanType") or "")
    if "법인" in kind or ("개인" not in kind and len(bn) == 10 and "81" <= bn[3:5] <= "88"):
        return ""                      # 법인은 이 칸이 법인등록번호 — 대표자 주민번호가 아님
    if len(n) == 13 and n[6] not in "12345678":
        return ""
    return n if len(n) == 13 and "*" not in raw else ""


def _cw_manage_list(res):
    d = res.get("data") if isinstance(res, dict) else None
    ex = (d or {}).get("extraInfo") or {} if isinstance(d, dict) else {}
    lst = ex.get("reqManageNoList") or []
    return lst if isinstance(lst, list) and lst else None


async def _cw_resolve(body, res, company=""):
    """사업장이 여러 개라 관리번호를 고르라는 추가 요청이면 → 상호가 맞는(없으면 첫) 사업장으로 자동 선택"""
    for _ in range(2):
        lst = _cw_manage_list(res)
        if not (_codef_code(res) == "CF-03002" and lst):
            return res
        comp = (company or "").replace("(주)", "").replace("주식회사", "").strip()
        ceo = (body.get("loginUserName") or "").strip()
        active = [x for x in lst if "소멸" not in str(x.get("reqStatus") or "") + str(x.get("reqStatus1") or "")] or lst
        pick = (next((x for x in active if comp and comp in str(x.get("reqCompanyNm") or "")), None)
                or next((x for x in active if ceo and ceo == str(x.get("reqCeoName") or "").strip()), None) or active[0])
        d = res["data"]
        res = await _codef_post(CODEF_HOMETAX_PRODUCTS["employees"], dict(body, reqManageNo=pick.get("reqManageNo", ""), is2Way=True,
                                twoWayInfo={k: d.get(k) for k in ("jobIndex", "threadIndex", "jti", "twoWayTimestamp")}))
    return res


def _cw_no_data(code, msg):
    """'조회 내역 없음'·'가입 사업장 없음' 류 → 직원 0명으로 본다"""
    return code != "CF-00000" and any(k in (msg or "") for k in ("없습니다", "없음", "존재하지", "내역이 없", "결과가 없"))


def _parse_employees(data):
    """근로자고용정보현황 → 지금 고용 중인 근로자 수"""
    seen = set()
    for r in _codef_rows(data, ("resUserNm",)):
        st = f"{r.get('resEmploymentStatus1') or ''} {r.get('resEmploymentStatus') or ''}"
        if "종료" in st and "고용" not in st.replace("고용종료", ""):
            continue
        seen.add((r.get("resUserNm"), r.get("resUserIdentiyNo"), r.get("resOriginalNumber")))
    return len(seen)


def _parse_emp_trend(data, today=None):
    """근로자고용정보현황 → 최근 1년 고용 'up'(증가)/'keep'(유지)/'down'(감소). 고용보험 입사·퇴사일(없으면 산재) 기준"""
    today = today or (datetime.utcnow() + timedelta(hours=9))
    now_s = today.strftime("%Y%m%d")
    ago_s = f"{today.year - 1}{now_s[4:]}"
    def d8(v):
        v = _re2.sub(r"\D", "", str(v or ""))
        return v if len(v) == 8 else ""
    now_n = ago_n = 0
    seen = set()
    for r in _codef_rows(data, ("resUserNm",)):
        key = (r.get("resUserNm"), r.get("resUserIdentiyNo"), r.get("resOriginalNumber"), r.get("resJoinCompanyDate1"), r.get("resJoinCompanyDate"))
        if key in seen:
            continue
        seen.add(key)
        j = d8(r.get("resJoinCompanyDate1")) or d8(r.get("resJoinCompanyDate"))
        q = d8(r.get("resResignDate1")) if d8(r.get("resJoinCompanyDate1")) else d8(r.get("resResignDate"))
        if not j:
            continue
        if j <= now_s and (not q or q > now_s): now_n += 1
        if j <= ago_s and (not q or q > ago_s): ago_n += 1
    if now_n == 0:
        return None
    _parse_emp_trend.last_diff = now_n - ago_n          # 최근 1년 증감 인원 (바로 뒤에서 읽음)
    return "up" if now_n > ago_n else "keep" if now_n == ago_n else "down"


def _parse_emp_change(data, today=None):
    """(추세, 최근 1년 증감 인원)"""
    _parse_emp_trend.last_diff = None
    tr = _parse_emp_trend(data, today)
    return tr, (_parse_emp_trend.last_diff if tr else None)


def _wh_body(now):
    """원천징수이행상황신고서 — 최근 1년 신고분(신고일 기준 최대 1년)"""
    st_ = (now - timedelta(days=364)).strftime("%Y%m%d")
    return {"organization": "0005", "startDate": st_, "endDate": now.strftime("%Y%m%d"), "inquiryType": "0", "infoViewYn": "0"}


def _parse_withholding(data):
    """원천징수이행상황신고서(최근 1년) → (최근 달 근로소득 인원, 최근 1년 추세 'up'/'keep'/'down' 또는 None, 증감 인원)
    귀속연월별 A01(근로소득 간이세액) 인원. 같은 달 수정신고가 있으면 나중 신고분"""
    by = {}
    for r in _codef_rows(data, ("resYearMonth",)):
        if str(r.get("resIssueYN") or "1") == "0" or str(r.get("resDocumentType") or "0") not in ("0", ""):
            continue
        ym = _re2.sub(r"\D", "", str(r.get("resYearMonth") or ""))[:6]
        if len(ym) != 6:
            continue
        lst = r.get("resTaxAmtSpecList") or []
        lst = lst if isinstance(lst, list) else [lst]
        n = 0
        for it in lst:
            if isinstance(it, dict) and str(it.get("resCodeNo") or "").upper() == "A01":
                n = int(_codef_amt(it.get("resPersonnel")) or 0)
                break
        rd = str(r.get("resReportingDate") or "")
        if ym not in by or rd >= by[ym][0]:
            by[ym] = (rd, n)
    if not by:
        return None, None, None
    months = sorted(by)
    last = by[months[-1]][1]
    first = months[0]
    gap = (int(months[-1][:4]) - int(first[:4])) * 12 + int(months[-1][4:]) - int(first[4:])
    trend = diff = None
    if gap >= 10 and last > 0:                       # 1년 가까이 떨어진 두 달을 비교
        old = by[first][1]
        trend = "up" if last > old else "keep" if last == old else "down"
        diff = last - old
    return last, trend, diff


def _codef_host():
    return (os.getenv("CODEF_HOST") or "https://development.codef.io").rstrip("/")


def _codef_ready():
    return bool(os.getenv("CODEF_CLIENT_ID") and os.getenv("CODEF_CLIENT_SECRET"))


async def _codef_token(force=False):
    if not force and _codef_token_cache["token"] and _time.time() < _codef_token_cache["exp"] - 300:
        return _codef_token_cache["token"]
    cid, sec = os.getenv("CODEF_CLIENT_ID", ""), os.getenv("CODEF_CLIENT_SECRET", "")
    auth = _b64.b64encode(f"{cid}:{sec}".encode()).decode()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(CODEF_OAUTH_URL, headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
                              data={"grant_type": "client_credentials", "scope": "read"})
    if r.status_code != 200:
        print(f"[CODEF] 토큰 발급 실패 {r.status_code}: {r.text[:300]}")
        raise HTTPException(status_code=502, detail="본인 인증 서비스 연결에 실패했습니다. 잠시 후 다시 시도해주세요.")
    j = r.json()
    _codef_token_cache["token"] = j.get("access_token")
    _codef_token_cache["exp"] = _time.time() + int(j.get("expires_in") or 3600)
    return _codef_token_cache["token"]


def _codef_parse(text):
    """CODEF 응답은 URL 인코딩된 JSON으로 온다"""
    t = (text or "").strip()
    for cand in (t, _cuq(t)):
        try:
            return _json.loads(cand)
        except Exception:
            continue
    return {"result": {"code": "PARSE", "message": "응답을 해석하지 못했습니다."}, "data": {}}


async def _codef_post(path, body, timeout=310):
    body = {k: v for k, v in (body or {}).items() if not str(k).startswith("_")}     # '_'로 시작하는 키는 피나 내부용
    token = await _codef_token()
    payload = _cq(_json.dumps(body, ensure_ascii=False))
    for attempt in (1, 2):
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(_codef_host() + path, content=payload,
                                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        if r.status_code == 401 and attempt == 1:          # 토큰 만료 → 다시 받아서 한 번 더
            token = await _codef_token(force=True)
            continue
        return _codef_parse(r.text)
    return _codef_parse(r.text)


def _codef_code(res):
    return ((res or {}).get("result") or {}).get("code") or ""


def _codef_msg(res):
    r = (res or {}).get("result") or {}
    extra = r.get("extraMessage") or ""
    return (r.get("message") or "알 수 없는 오류") + (f" ({extra})" if extra else "")


_SIDO = [("서울", "서울"), ("부산", "부산"), ("대구", "대구"), ("인천", "인천"), ("광주", "광주"), ("대전", "대전"), ("울산", "울산"),
         ("세종", "세종"), ("경기", "경기"), ("강원", "강원"), ("충청북", "충북"), ("충북", "충북"), ("충청남", "충남"), ("충남", "충남"),
         ("전라북", "전북"), ("전북", "전북"), ("전라남", "전남"), ("전남", "전남"), ("경상북", "경북"), ("경북", "경북"),
         ("경상남", "경남"), ("경남", "경남"), ("제주", "제주")]


def _sido(addr):
    a = (addr or "").strip()
    for k, v in _SIDO:
        if a.startswith(k):
            return v
    return ""


def _split_biz(v):
    """'제조업, 도매업' / 줄바꿈 / '|' 로 나뉜 업태·종목 → 리스트"""
    return [x.strip() for x in _re2.split(r"[,\n|;]+", str(v or "")) if x.strip()]


def _split_types(v):
    """업태 나누기 — '전문, 과학 및 기술서비스업'처럼 이름 안의 쉼표는 '업'으로 끝날 때까지 다시 붙인다"""
    out, buf = [], ""
    for part in _split_biz(v):
        buf = f"{buf}, {part}" if buf else part
        if buf.endswith("업") or buf.endswith(")"):
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def _pair_biz(types_raw, items_raw):
    """업태·종목 → (업태1, 종목1, 업태2, 종목2). 개수가 안 맞아도 종목을 한 칸에 몰지 않는다"""
    types, items = _split_types(types_raw), _split_biz(items_raw)
    if len(types) <= 1:
        return (types[0] if types else ""), ", ".join(items), "", ""
    if not items:
        return types[0], "", ", ".join(types[1:]), ""
    if len(items) == 1:
        return types[0], items[0], ", ".join(types[1:]), ""
    if len(items) == len(types):
        return types[0], items[0], ", ".join(types[1:]), ", ".join(items[1:])
    return types[0], items[0], ", ".join(types[1:]), ", ".join(items[1:])


def _parse_registration(data, want_biz=""):
    """사업자등록 증명 → 원클릭 입력값"""
    rows = data if isinstance(data, list) else [data]
    rows = [x for x in rows if isinstance(x, dict) and (x.get("resCompanyIdentityNo") or x.get("resCompanyNm"))]
    if not rows:
        return {}
    digits = _re2.sub(r"\D", "", want_biz or "")
    row = next((x for x in rows if _re2.sub(r"\D", "", x.get("resCompanyIdentityNo") or "") == digits), rows[0]) if digits else rows[0]
    bn = _re2.sub(r"\D", "", row.get("resCompanyIdentityNo") or "")
    opened = _re2.sub(r"\D", "", row.get("resOpenDate") or "")
    ind1, item1, ind2, item2 = _pair_biz(row.get("resBusinessTypes"), row.get("resBusinessItems"))
    kind = (row.get("resBusinessmanType") or "") + " " + (row.get("resUserIdentiyNo") or "")
    out = {
        "company_name": (row.get("resCompanyNm") or "").strip() or (row.get("resUserNm") or "").strip(),
        "rep_name": (row.get("resUserNm") or "").strip(),
        "business_number": f"{bn[:3]}-{bn[3:5]}-{bn[5:]}" if len(bn) == 10 else bn,
        "founded": f"{opened[:4]}-{opened[4:6]}" if len(opened) >= 6 else "",
        "industry": ind1,
        "item": item1,
        "industry2": ind2,
        "item2": item2,
        "location": _sido(row.get("resUserAddr")),
        "biz_type": "corp" if "법인" in kind else "personal",
    }
    if len(rows) > 1:
        def _fmt(v):
            d = _re2.sub(r"\D", "", v or "")
            return f"{d[:3]}-{d[3:5]}-{d[5:]}" if len(d) == 10 else (v or "")
        out["_business_list"] = [{"business_number": _fmt(x.get("resCompanyIdentityNo")), "company_name": (x.get("resCompanyNm") or "").strip() or (x.get("resUserNm") or "").strip(),
                                  "industry": " / ".join(t for t in (x.get("resBusinessTypes"), x.get("resBusinessItems")) if t)[:60]} for x in rows]
    return {k: v for k, v in out.items() if v not in ("", None)}


class CodefStartBody(BaseModel):
    user_name: str
    birth: str                    # YYYYMMDD
    phone: str
    provider: str = "1"           # loginTypeLevel: 1 카카오톡, 3 삼성패스, 4 KB모바일, 5 통신사(PASS), 6 네이버, 7 신한, 8 토스, 9 뱅크샐러드, 10 NH, 11 우리
    telecom: str = ""             # provider=5 일 때: 0 SKT, 1 KT, 2 LG U+
    business_number: str = ""     # 여러 사업장이면 이 사업자번호로 조회
    member: bool = True           # 홈택스 회원 여부 (회원: loginType 5 / 비회원: 6)


def _hometax_login_params(b: CodefStartBody, user_id: int):
    birth = _re2.sub(r"\D", "", b.birth)
    phone = _re2.sub(r"\D", "", b.phone)
    if len(birth) != 8:
        raise HTTPException(status_code=400, detail="생년월일 8자리를 입력해주세요. (예: 19800101)")
    if not (10 <= len(phone) <= 11):
        raise HTTPException(status_code=400, detail="휴대폰 번호를 확인해주세요.")
    if not b.user_name.strip():
        raise HTTPException(status_code=400, detail="이름을 입력해주세요.")
    if b.provider == "5" and b.telecom not in ("0", "1", "2"):
        raise HTTPException(status_code=400, detail="통신사를 선택해주세요.")
    p = {
        "organization": "0001",
        "loginType": "5" if b.member else "6",
        "loginTypeLevel": b.provider,
        "userName": b.user_name.strip(),
        "loginIdentity": birth,
        "phoneNo": phone,
        "id": f"fina-{user_id}-{uuid.uuid4().hex[:12]}",   # 같은 로그인 세션으로 여러 서류를 이어서 조회
        "usePurposes": "99",
        "submitTargets": "99",
        "isIdentityViewYN": "0",
        "originDataYN": "0",
        "applicationType": "01",
    }
    if b.provider == "5":
        p["telecom"] = b.telecom
    bn = _re2.sub(r"\D", "", b.business_number or "")
    if len(bn) == 10:
        p["_want_biz"] = bn      # 화면에 적힌 사업자번호는 '여러 사업장 중 고르기'에만 씀 (CODEF로 보내지 않음 — 보내면 정보 불일치 오류)
    return p


class CodefCertBody(BaseModel):
    cert_type: str = "1"          # "1": signCert.der + signPri.key / "pfx": .pfx(.p12) 한 파일
    cert_file: str                # base64
    key_file: str = ""            # base64 (cert_type "1" 일 때)
    password: str
    business_number: str = ""
    banks: list[str] = []         # 대출을 불러올 거래 은행 기관코드 (선택, 최대 6곳)


def _codef_rsa(text):
    """CODEF 공개키(RSA)로 비밀번호 암호화 — 서버에서만 처리"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    pub = (os.getenv("CODEF_PUBLIC_KEY") or "").strip()
    if not pub:
        raise HTTPException(status_code=503, detail="공동인증서 인증이 아직 설정되지 않았습니다.")
    try:
        if "BEGIN" in pub:
            key = serialization.load_pem_public_key(pub.encode())
        else:
            key = serialization.load_der_public_key(_b64.b64decode(pub))
    except Exception as e:
        print(f"[CODEF] 공개키 오류: {e}")
        raise HTTPException(status_code=503, detail="공동인증서 인증 설정에 문제가 있습니다. 관리자에게 문의해주세요.")
    return _b64.b64encode(key.encrypt(text.encode("utf-8"), padding.PKCS1v15())).decode()


def _b64_clean(v):
    t = str(v or "").strip()
    if t.startswith("data:") and "," in t:
        t = t.split(",", 1)[1]
    t = _re2.sub(r"\s", "", t)
    try:
        raw = _b64.b64decode(t, validate=True)
    except Exception:
        return ""
    return t if 0 < len(raw) <= 64 * 1024 else ""


def _hometax_cert_params(b: CodefCertBody, user_id: int):
    ct = "pfx" if str(b.cert_type).lower() in ("pfx", "p12") else "1"
    cert = _b64_clean(b.cert_file)
    key = _b64_clean(b.key_file) if ct == "1" else ""
    if not cert or (ct == "1" and not key):
        raise HTTPException(status_code=400, detail="인증서 파일을 확인해주세요. (signCert.der + signPri.key 또는 .pfx 파일)")
    if not b.password:
        raise HTTPException(status_code=400, detail="인증서 비밀번호를 입력해주세요.")
    p = {
        "organization": "0001",
        "loginType": "0",
        "certType": ct,
        "certFile": cert,
        "certPassword": _codef_rsa(b.password),
        "id": f"fina-{user_id}-{uuid.uuid4().hex[:12]}",
        "usePurposes": "99",
        "submitTargets": "99",
        "isIdentityViewYN": "0",
        "originDataYN": "0",
        "applicationType": "01",
    }
    if key:
        p["keyFile"] = key
    bn = _re2.sub(r"\D", "", b.business_number or "")
    if len(bn) == 10:
        p["_want_biz"] = bn      # 화면에 적힌 사업자번호는 '여러 사업장 중 고르기'에만 씀 (CODEF로 보내지 않음 — 보내면 정보 불일치 오류)
    return p


@app.post("/codef/hometax/cert")
async def codef_hometax_cert(body: CodefCertBody, user: models.User = Depends(get_current_user)):
    """공동인증서(구 공인인증서)로 홈택스 로그인 → 서류 4종 조회. 인증서·비밀번호는 저장하지 않고 화면에 돌려주지도 않는다."""
    if not _codef_ready():
        raise HTTPException(status_code=503, detail="본인 인증 연동이 아직 설정되지 않았습니다.")
    params = _hometax_cert_params(body, user.id)
    res = await _codef_post(CODEF_HOMETAX_PRODUCTS["registration"], dict(params, isIdentityViewYN="1"))   # 대표자 주민번호 공개 → 직원 수 조회에만 사용
    code = _codef_code(res)
    if code == "CF-00000":
        banks = [b for b in dict.fromkeys(body.banks or []) if b in CODEF_BANKS][:6]
        return _hometax_start_job(params, res.get("data"), user.id, banks=banks)
    print(f"[CODEF] cert 실패 {code}: {(res.get('result') or {}).get('message')}")
    msg = _codef_msg(res)
    if "비밀번호" in msg or "password" in msg.lower():
        raise HTTPException(status_code=400, detail="인증서 비밀번호가 맞지 않습니다. 다시 확인해주세요.")
    raise HTTPException(status_code=400, detail=f"공동인증서 인증에 실패했습니다: {msg}")


# ── 공동인증서 PC → 휴대폰 보내기 ──
#  휴대폰이 12자리 번호를 만들고 그 해시(rid)만 서버에 등록한다. PC는 같은 번호로 브라우저에서 암호화(AES-GCM)해 올린다.
#  서버는 번호를 모르므로 내용을 풀 수 없고, 3분이 지나거나 휴대폰이 받으면 바로 지운다. 같은 피나 계정끼리만 주고받는다.
CERT_RELAY_TTL = 180


class CertRelayBody(BaseModel):
    rid: str
    blob: str = ""


def _relay_rid(v):
    v = (v or "").strip().lower()
    if not _re2.fullmatch(r"[0-9a-f]{64}", v):
        raise HTTPException(status_code=400, detail="인증번호가 올바르지 않습니다.")
    return v


def _relay_purge(db):
    db.query(models.CertRelay).filter(models.CertRelay.expires_at < datetime.utcnow()).delete()
    db.commit()


@app.post("/codef/cert-relay/open")
def cert_relay_open(body: CertRelayBody, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """휴대폰: 받을 준비 (번호 해시 등록)"""
    rid = _relay_rid(body.rid)
    _relay_purge(db)
    db.query(models.CertRelay).filter(models.CertRelay.user_id == user.id).delete()   # 이전 대기 건 정리
    db.add(models.CertRelay(rid=rid, user_id=user.id, expires_at=datetime.utcnow() + timedelta(seconds=CERT_RELAY_TTL)))
    db.commit()
    return {"ok": True, "expires_in": CERT_RELAY_TTL}


@app.post("/codef/cert-relay/send")
def cert_relay_send(body: CertRelayBody, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """PC: 암호화된 인증서 올리기"""
    rid = _relay_rid(body.rid)
    if not body.blob or len(body.blob) > 300_000:
        raise HTTPException(status_code=400, detail="인증서 파일을 확인해주세요.")
    row = db.query(models.CertRelay).filter(models.CertRelay.rid == rid).first()
    if not row or row.expires_at < datetime.utcnow():
        raise HTTPException(status_code=404, detail="인증번호가 맞지 않거나 시간이 지났어요. 휴대폰에 표시된 번호를 다시 확인해 주세요.")
    if row.user_id != user.id:
        raise HTTPException(status_code=403, detail="휴대폰과 같은 피나 계정으로 로그인해 주세요.")
    if row.blob:
        raise HTTPException(status_code=409, detail="이미 인증서를 보냈어요. 휴대폰을 확인해 주세요.")
    row.blob = body.blob
    db.commit()
    return {"ok": True}


@app.post("/codef/cert-relay/take")
def cert_relay_take(body: CertRelayBody, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """휴대폰: 도착했는지 확인 → 받으면 즉시 삭제"""
    rid = _relay_rid(body.rid)
    row = db.query(models.CertRelay).filter(models.CertRelay.rid == rid, models.CertRelay.user_id == user.id).first()
    if not row or row.expires_at < datetime.utcnow():
        if row:
            db.delete(row); db.commit()
        return {"status": "expired"}
    if not row.blob:
        return {"status": "waiting"}
    blob = row.blob
    db.delete(row); db.commit()
    return {"status": "done", "blob": blob}


@app.get("/codef/status")
def codef_status():
    return {"ready": _codef_ready(), "products": [k for k, v in CODEF_HOMETAX_PRODUCTS.items() if v]}


# ── 간편인증: 서류 요청을 같은 id로 0.3초 간격으로 한꺼번에 보낸다 ──
#  첫 요청만 추가인증(CF-03002)으로 돌아오고, 나머지는 사용자가 휴대폰에서 승인할 때까지 CODEF가 응답을 잡고 있다.
#  승인 후 첫 요청에 2차 요청(simpleAuth/is2Way/twoWayInfo)을 보내면 잡혀 있던 요청들이 차례로 도착 → 승인 1번으로 서류 4종.
#  (승인 뒤에 새로 보내면 매번 다시 승인해야 함 — 같은 세션 재사용 안 됨)
#  진행 중인 요청·2차 인증 정보는 이 프로세스 메모리에만 둔다. 결과는 codef_jobs(DB)에 저장해 화면이 확인한다.
_ht_live = {}     # job_id -> {"user_id", "leads": [...], "pending": n, "res": {key: [(code, data, msg)]}, "t": time}

HT_DOC_LABEL = {"registration": "사업자등록증명", "vat": "부가세과세표준증명", "tax_payment": "납세증명서", "financial": "재무제표", "employees": "직원 수(고용보험)"}


def _hometax_calls(params, now, telecom=""):
    """간편인증 세션 하나로 보낼 서류 요청 목록 [(key, body)]"""
    base = {k: params[k] for k in ("organization", "loginType", "loginTypeLevel", "userName", "loginIdentity", "id", "telecom", "identity") if params.get(k)}
    phone = params.get("phoneNo", "")
    calls = [("registration", dict(params, isIdentityViewYN="1"))]
    y = now.year
    if params.get("loginType") == "5":        # 부가세과세표준증명은 회원만
        calls.append(("vat", dict(base, phoneNo=phone, startDate=f"{y - 3}01", endDate=_vat_end(now), isIdentityViewYN="0",
                                  usePurposes="99", submitTargets="99", applicationType="01", originDataYN="0")))
    if params.get("loginType") == "5" and params.get("_want_biz"):   # 원천징수이행상황신고서(직원 수) — 사업자번호를 알 때만
        calls.append(("withholding", dict(base, phoneNo=phone, identity=params["_want_biz"], identity2=params.get("loginIdentity", ""),
                                          **_wh_body(now))))
    if params.get("loginType") == "5" and 8 <= now.hour < 22:   # 면세사업자 수입금액증명 — 면세사업자가 아니면 발급 안 됨(무시)
        calls.append(("taxfree", dict(base, phoneNo=phone, startYear=str(y - 3), endYear=str(y - 1), isIdentityViewYN="0",
                                      usePurposes="99", submitTargets="99", originDataYN="0")))
    calls.append(("tax_payment", dict(base, phoneNo=phone, isIdentityViewYN="0", isAddrViewYn="0", proofType="B0007",
                                      submitTargets="99", applicationType="01", originDataYN="0")))
    if 8 <= now.hour < 22:                    # 재무제표는 08~22시만. 법인/개인을 아직 모르니 둘 다 요청
        years = [y - 1, y - 2, y - 3] + ([y - 4] if now.month < 6 else [])   # 최근 3년 매출 (상반기엔 작년 신고 전일 수 있어 한 해 더)
        fin = dict(base, loginPhoneNo=phone, isIdentityViewYN="0", usePurposes="99", submitTargets="99", originDataYN="0")
        if params.get("_want_biz"):
            fin["identity"] = params["_want_biz"]
        for yy in years:
            calls.append(("financial", dict(fin, startDate=f"{yy}12")))                    # 법인: 사업연도 종료연월
            calls.append(("financial", dict(fin, startDate=str(yy), proofType="40")))      # 개인: 과세연도
    return calls


def _ht_is_2way(res):
    d = res.get("data") if isinstance(res, dict) else None
    return _codef_code(res) == "CF-03002" and isinstance(d, dict) and bool(d.get("continue2Way"))


def _ht_assemble(st, now, final):
    """지금까지 받은 서류 → {fill, got, failed}"""
    R = st["res"]
    ok = lambda k: [d for c, d, m in R.get(k, []) if c == "CF-00000"]
    fill = {}
    for d in ok("registration"):
        fill = _safe(_parse_registration, d, st.get("identity", ""), default={}) or {}
        break
    got, failed = [], []
    if fill:
        got.append("사업자등록증명")
    elif final or R.get("registration"):
        failed.append("사업자등록증명")
    years, ytd = next((v for v in (_safe(_parse_vat_split, d, now.year, default=([], None)) for d in ok("vat")) if v[0] or v[1]), ([], None))
    if years or ytd:
        if years:
            fill["years"] = years
        if ytd:
            fill["ytd"] = ytd
        got.append("부가세과세표준증명")
    tf = {}
    for d in ok("taxfree"):
        tf.update(_safe(_taxfree_revenues, d, default={}) or {})
    if tf:
        _merge_fin_years(fill, tf, now.year)
        got.append("면세사업자 수입금액증명")
    elif not (years or ytd) and final:
        failed.append("부가세과세표준증명")
    tax = R.get("tax_payment", [])
    arrears = None
    for c, d, m in tax:
        arrears = _safe(_parse_tax_cert, d if c == "CF-00000" else None, None if c == "CF-00000" else m)
        if arrears:
            break
    if arrears:
        fill["arrears"] = arrears
        got.append("납세증명서")
    elif final:
        failed.append("납세증명서")
    fins = [f for f in (_safe(_parse_financial, d) for d in ok("financial")) if f]
    revs = {}
    for d in ok("financial"):
        revs.update(_safe(_fin_revenues, d, default={}) or {})
    if fins:
        fin = dict(sorted(fins, key=lambda f: f.get("_year", 0))[-1])     # 재무 수치는 가장 최근 연도
        fin.pop("_revenue", None); fin.pop("_year", None)
        fill["fin"] = fin
        got.append("재무제표")
    if revs:
        _merge_fin_years(fill, revs, now.year)                          # 매출은 최근 3년 모두
    if not fins and final and fill.get("biz_type") != "personal":     # 개인사업자는 재무제표가 없는 경우가 많아 안내하지 않음
        failed.append("재무제표" if 8 <= now.hour < 22 else "재무제표(08~22시에만 조회 가능)")
    wh_n, wh_tr, wh_df = next((v for v in (_safe(_parse_withholding, d, default=(None, None, None)) for d in ok("withholding")) if v[0] is not None), (None, None, None))
    if "employees" in st.get("keys", ()) or wh_n is not None:
        n = next((v for v in (_safe(_parse_employees, d) for d in ok("employees")) if v is not None), None)
        if n is None and any(_cw_no_data(c, m) for c, d, m in R.get("employees", [])):
            n = 0                      # 고용보험 근로자 내역 없음 → 0명
        tr, df = next((v for v in (_safe(_parse_emp_change, d, default=(None, None)) for d in ok("employees")) if v[0]), (None, None)) if n else (None, None)
        if (n is None or n == 0) and wh_n:                    # 근로복지공단이 안 되면 원천세 신고 인원
            n = wh_n
        if n is None and wh_n == 0 and final:
            n = 0
        if n is not None:
            fill["employees"] = n
            got.append("직원 수(고용보험)")
            if not tr:
                tr, df = wh_tr, wh_df
            if tr:
                fill["emp_trend"] = tr
                if df is not None:
                    fill["emp_increase"] = df
        elif final:
            fill["employees_default"] = 0      # 조회 못 함 → 칸이 비어 있으면 0명으로
            failed.append("직원 수(고용보험) — 0명으로 채움")
    elif final:
        fill["employees_default"] = 0
    return {"fill": fill, "got": got, "failed": failed}


def _ht_progress(job_id, final=False):
    st = _ht_live.get(job_id)
    if not st:
        return
    now = datetime.utcnow() + timedelta(hours=9)
    out = _ht_assemble(st, now, final)
    if final:
        reason = "" if out["got"] else next((f"{m} ({c})" for k in ("registration", "vat", "tax_payment", "financial") for c, d, m in st["res"].get(k, []) if c != "CF-00000" and m), "")
        _job_save(job_id, "done", {"status": "done", "fill": out["fill"], "got": out["got"], "pending": out["failed"], "reason": reason})
        _ht_live.pop(job_id, None)
    else:
        waiting = [HT_DOC_LABEL[k] for k in HT_DOC_LABEL if k in st["keys"] and k not in st["done_keys"]]
        approve = "근로복지공단" if any(l["key"] == "employees" for l in st["leads"]) and st.get("cw_fired") else ""
        _job_save(job_id, None, {"got": out["got"], "failed": [], "doing": waiting[0] if waiting else "", "fill": out["fill"], "approve": approve})


def _ht_settle(job_id, key, res):
    st = _ht_live.get(job_id)
    if not st:
        return
    code = _codef_code(res)
    if code != "CF-00000":
        print(f"[CODEF] {key} 실패 {code}: {_codef_msg(res)}")
    st["res"].setdefault(key, []).append((code, res.get("data") if isinstance(res, dict) else None, _codef_msg(res)))
    st["left"][key] -= 1
    if st["left"][key] <= 0:
        st["done_keys"].add(key)
    st["pending"] -= 1
    # 근로복지공단(두 번째 승인)은 사업자등록증명 + 원천세 신고서 결과를 본 뒤에 — 원천세로 직원 수가 나오면 건너뛰어 승인 한 번으로 끝냄
    reg_rows = st["res"].get("registration", [])
    wh_wait = "withholding" in st["keys"] and "withholding" not in st["done_keys"]
    if key in ("registration", "withholding") and st.get("cw_body") and not st.get("cw_fired") and reg_rows and not wh_wait:
        st["cw_fired"] = True
        reg_code, reg_data, _m = reg_rows[0]
        wh_ok = any(c == "CF-00000" and (_safe(_parse_withholding, d, default=(None, None, None)) or (None,))[0] is not None
                    for c, d, m in st["res"].get("withholding", []))
        if reg_code == "CF-00000" and not wh_ok:
            rrn = _safe(_reg_rrn, reg_data, st.get("identity", ""), default="") or ""
            reg = _safe(_parse_registration, reg_data, st.get("identity", ""), default={}) or {}
            biz = _re2.sub(r"\D", "", reg.get("business_number", ""))
            cw = dict(st["cw_body"], identity=rrn or biz)       # 대표자 개인명의 → 주민번호 (서버 안에서만)
            _spawn(_ht_fire(job_id, "employees", cw))
        else:
            st["res"].setdefault("employees", []).append(("SKIP", None, "원천세 신고서로 확인" if wh_ok else "사업자등록증명 실패"))
            st["left"]["employees"] = 0
            st["done_keys"].add("employees")
            st["pending"] -= 1
    _ht_progress(job_id, final=st["pending"] <= 0 and not st["leads"])


async def _cw_watchdog(job_id, lead, wait=285):
    """근로복지공단 인증을 4분 45초 안에 승인하지 않으면 직원 수만 건너뛰고 마무리"""
    await asyncio.sleep(wait)
    st = _ht_live.get(job_id)
    if st and lead in st["leads"]:
        st["leads"].remove(lead)
        st["pending"] += 1
        _ht_settle(job_id, "employees", {"result": {"code": "TIMEOUT", "message": "근로복지공단 인증 시간이 지났습니다."}, "data": None})


async def _ht_fire(job_id, key, body):
    try:
        res = await _codef_post(CODEF_HOMETAX_PRODUCTS[key], body)
        if key == "employees":
            res = await _cw_resolve(body, res)
    except Exception as e:
        print(f"[CODEF] {key} 호출 오류: {e!r}")
        res = {"result": {"code": "NETWORK", "message": "연결 오류"}, "data": None}
    st = _ht_live.get(job_id)
    if not st:
        return
    if _ht_is_2way(res) and key == "withholding" and st["leads"]:
        res = {"result": {"code": "SKIP", "message": "별도 승인 필요 — 건너뜀"}, "data": None}   # 승인을 두 번 받게 하지 않음
    if _ht_is_2way(res):
        d = res["data"]
        lead = {"key": key, "body": body, "two_way": {k: d.get(k) for k in ("jobIndex", "threadIndex", "jti", "twoWayTimestamp")}}
        st["leads"].append(lead)
        st["pending"] -= 1          # 2차 요청 때 다시 집계
        st["approval"].set()
        if key == "employees":      # 홈택스 승인 뒤 새로 온 인증 → 확인 횟수 새로, 화면에 승인 안내
            st["tries"] = 0
            _spawn(_cw_watchdog(job_id, lead))
            _ht_progress(job_id)
        return
    _ht_settle(job_id, key, res)
    if st["pending"] <= 0:
        st["approval"].set()


def _spawn(coro):
    t = asyncio.create_task(coro)
    _codef_tasks.add(t)
    t.add_done_callback(_codef_tasks.discard)
    return t


async def _ht_launch(job_id, calls):
    for key, body in calls:
        _spawn(_ht_fire(job_id, key, body))
        await asyncio.sleep(0.3)
    await asyncio.sleep(0.5)
    st = _ht_live.get(job_id)
    if st:
        st["launched"].set()      # 모든 서류 요청이 나간 뒤에만 승인 확인(2차 요청)을 보낸다


class CodefCompleteBody(BaseModel):
    job: str
    auto: bool = False        # 화면이 자동으로 확인 (앱에서 돌아왔을 때·일정 시간 후)


HT_CONFIRM_MAX = 3            # CODEF: 승인 전 2차 확인이 3번째도 미승인이면 CF-12872로 거래 종료
HT_AUTO_MAX = 2               # 자동 확인은 2번까지 — 마지막 1번은 사용자가 누르는 '인증 완료'용


@app.post("/codef/hometax/start")
async def codef_hometax_start(body: CodefStartBody, user: models.User = Depends(get_current_user)):
    """서류 요청을 한꺼번에 보내고, 휴대폰 승인 요청이 뜨면 바로 알려준다"""
    if not _codef_ready():
        raise HTTPException(status_code=503, detail="본인 인증 연동이 아직 설정되지 않았습니다.")
    params = _hometax_login_params(body, user.id)
    now = datetime.utcnow() + timedelta(hours=9)
    calls = _hometax_calls(params, now, body.telecom)
    # 오래된 진행 정보 정리 (15분)
    for k in [k for k, v in _ht_live.items() if _time.time() - v["t"] > 900]:
        _ht_live.pop(k, None)
    job_id = uuid.uuid4().hex
    keys = [k for k, _ in calls]
    cw = _cw_body(params, body.telecom)        # 근로복지공단(직원 수)은 사업자등록증명에서 대표자 주민번호를 받은 뒤 따로 요청
    if cw:
        keys.append("employees")
    _ht_live[job_id] = {"user_id": user.id, "leads": [], "pending": len(keys), "res": {}, "t": _time.time(),
                        "keys": set(keys), "done_keys": set(), "left": {k: keys.count(k) for k in set(keys)},
                        "identity": params.get("_want_biz", ""), "approval": asyncio.Event(), "launched": asyncio.Event(),
                        "cw_body": cw}
    db = SessionLocal()
    try:
        db.query(models.CodefJob).filter(models.CodefJob.created_at < datetime.utcnow() - timedelta(hours=1)).delete()
        db.add(models.CodefJob(id=job_id, user_id=user.id, status="running", result=_json.dumps({"got": [], "doing": "사업자등록증명", "fill": {}})))
        db.commit()
    finally:
        db.close()
    _spawn(_ht_launch(job_id, calls))
    st = _ht_live[job_id]
    try:
        await asyncio.wait_for(st["approval"].wait(), timeout=60)
    except asyncio.TimeoutError:
        pass
    st0 = st
    st = _ht_live.get(job_id)
    if st and st["leads"]:
        return {"status": "need_approval", "job": job_id}
    regs0 = st0["res"].get("registration") or []
    if not st and body.member and regs0 and regs0[0][0] != "CF-00000" and _ht_not_member(regs0[0][2]):
        st = st0                                 # 전부 실패로 끝났어도 '회원 아님'이면 아래에서 비회원으로 다시
    if not st:                                   # 승인 없이 모두 끝남
        return {"status": "collecting", "job": job_id}
    regs = st["res"].get("registration") or []
    if regs and regs[0][0] != "CF-00000" and not st["leads"]:
        _ht_live.pop(job_id, None)
        if body.member and _ht_not_member(regs[0][2]):        # 홈택스 회원이 아니면 비회원 간편인증으로 자동 재시도
            print(f"[CODEF] 홈택스 회원 아님 → 비회원 인증으로 다시 요청 ({regs[0][2]})")
            _job_save(job_id, "error", {"detail": "비회원으로 다시 요청"})
            return await codef_hometax_start((body.model_copy(update={"member": False}) if hasattr(body, "model_copy") else body.copy(update={"member": False})), user)
        _job_save(job_id, "error", {"detail": regs[0][2]})
        raise HTTPException(status_code=400, detail=f"본인 인증 요청에 실패했습니다: {regs[0][2]}")
    return {"status": "collecting", "job": job_id}


def _ht_not_member(msg):
    """'홈택스 회원이 아님' 류 오류인지 — 회원 간편인증(5) 실패 시 비회원(6)으로 자동 전환"""
    m = str(msg or "").replace(" ", "")
    return ("회원" in m and any(k in m for k in ("아닙", "아님", "미가입", "비회원", "가입되", "가입정보", "없습", "없는", "존재하지"))) or "미가입" in m


@app.post("/codef/hometax/skip-employees")
async def codef_hometax_skip_employees(body: CodefCompleteBody, user: models.User = Depends(get_current_user)):
    """근로복지공단 인증을 건너뛰고(직원 수 제외) 마무리"""
    st = _ht_live.get(body.job)
    if not st or st["user_id"] != user.id:
        return {"status": "collecting", "job": body.job}
    for lead in [l for l in st["leads"] if l["key"] == "employees"]:
        st["leads"].remove(lead)
        st["pending"] += 1
        _ht_settle(body.job, "employees", {"result": {"code": "SKIP", "message": "건너뜀"}, "data": None})
    return {"status": "collecting", "job": body.job}


@app.post("/codef/hometax/complete")
async def codef_hometax_complete(body: CodefCompleteBody, user: models.User = Depends(get_current_user)):
    """사용자가 휴대폰에서 승인한 뒤 — 첫 요청에 2차 요청을 보내면 잡혀 있던 나머지 서류도 이어서 도착"""
    st = _ht_live.get(body.job)
    if not st or st["user_id"] != user.id:
        raise HTTPException(status_code=400, detail="인증 시간이 지났거나 서버가 다시 시작됐어요. 처음부터 다시 시도해 주세요.")
    leads = list(st["leads"])
    if not leads:
        return {"status": "collecting", "job": body.job}
    try:
        await asyncio.wait_for(st["launched"].wait(), timeout=10)
    except asyncio.TimeoutError:
        pass
    tries = st.get("tries", 0)
    if body.auto and tries >= HT_AUTO_MAX:
        return {"status": "waiting", "job": body.job}
    if tries >= HT_CONFIRM_MAX:
        raise HTTPException(status_code=400, detail="인증 확인 횟수를 넘었어요. 처음부터 다시 인증을 요청해 주세요.")
    st["tries"] = tries + 1

    async def confirm(lead):
        req = dict(lead["body"], simpleAuth="1", is2Way=True, twoWayInfo=lead["two_way"])
        try:
            res = await _codef_post(CODEF_HOMETAX_PRODUCTS[lead["key"]], req)
            if lead["key"] == "employees":
                regs = [d for c, d, m in st["res"].get("registration", []) if c == "CF-00000"]
                comp = (_safe(_parse_registration, regs[0], default={}) or {}).get("company_name", "") if regs else ""
                res = await _cw_resolve(lead["body"], res, comp)
        except Exception as e:
            print(f"[CODEF] 2차 요청 오류: {e!r}")
            res = {"result": {"code": "NETWORK", "message": "연결 오류"}, "data": None}
        return lead, res

    results = await asyncio.gather(*(confirm(l) for l in leads))
    waiting = False
    for lead, res in results:
        code = _codef_code(res)
        if code == "CF-03002":
            waiting = True
            continue
        if code == "CF-12872":       # 확인 횟수 초과로 거래 종료
            _ht_live.pop(body.job, None)
            _job_save(body.job, "error", {"detail": "인증 확인 횟수를 넘어 종료됐어요. 다시 요청해 주세요."})
            raise HTTPException(status_code=400, detail="휴대폰 인증이 확인되지 않아 종료됐어요. 처음부터 다시 인증을 요청해 주세요.")
        if lead in st["leads"]:
            st["leads"].remove(lead)
            st["pending"] += 1
            _ht_settle(body.job, lead["key"], res)
    if waiting:
        if body.auto:
            return {"status": "waiting", "job": body.job}
        left = HT_CONFIRM_MAX - st["tries"]
        raise HTTPException(status_code=409, detail="아직 휴대폰 인증이 완료되지 않았어요. 휴대폰에서 인증을 마친 뒤 다시 눌러주세요." + (" (마지막 확인이에요 — 꼭 인증을 마친 뒤 눌러주세요)" if left == 1 else ""))
    reg = (_ht_live.get(body.job) or {}).get("res", {}).get("registration") or []
    if reg and all(c != "CF-00000" for c, _, _ in reg) and any(l["key"] == "registration" for l, _ in results):
        print(f"[CODEF] complete 실패: {reg[0][2]}")
    return {"status": "collecting", "job": body.job}


_codef_tasks = set()      # 백그라운드 작업이 중간에 정리되지 않도록 참조 보관


# ── 은행 대출 (공동인증서 전용) ──
#  CODEF 은행 상품은 connectedId 방식: 인증서를 은행별로 등록(account/create) → 보유계좌 조회 → 대출 계좌만 추림 → 등록 삭제.
#  법인은 기업뱅킹(clientType B, /bank/b/), 개인·개인사업자는 개인뱅킹(P, /bank/p/). 은행 대출만 나온다(중진공 직접대출·캐피탈 등 제외).
CODEF_BANKS = {"0004": "국민은행", "0088": "신한은행", "0020": "우리은행", "0081": "하나은행", "0003": "기업은행", "0011": "농협은행",
               "0002": "산업은행", "0007": "수협은행", "0023": "SC은행", "0027": "씨티은행", "0031": "대구은행", "0032": "부산은행",
               "0034": "광주은행", "0035": "제주은행", "0037": "전북은행", "0039": "경남은행", "0045": "새마을금고", "0048": "신협",
               "0071": "우체국"}     # K뱅크(0089)는 2023.05 개인뱅킹 웹 서비스 종료


async def _safe_async(coro, default=None):
    try:
        return await coro
    except Exception as e:
        print(f"[CODEF] 처리 오류: {e!r}")
        return default


def _bank_loan_rows(data):
    """보유계좌 응답 → 대출 계좌 (대출 + 마이너스통장)"""
    rows = data if isinstance(data, list) else [data]
    out = []
    for d in rows:
        if not isinstance(d, dict):
            continue
        loans = d.get("resLoan") or []
        loans = loans if isinstance(loans, list) else [loans]
        for l in loans:
            if isinstance(l, dict) and str(l.get("resAccountDeposit") or "40") != "99":
                out.append({"name": l.get("resAccountName") or "대출", "balance": _codef_amt(l.get("resAccountBalance")),
                            "start": l.get("resAccountStartDate") or "", "account": l.get("resAccount") or "",
                            "exec": l.get("resAccountLoanExecNo") or ""})
        deps = d.get("resDepositTrust") or []
        deps = deps if isinstance(deps, list) else [deps]
        for a in deps:     # 마이너스통장(한도대출)
            if isinstance(a, dict) and str(a.get("resOverdraftAcctYN")) == "1":
                bal = _codef_amt(a.get("resLoanBalance"))
                if bal:
                    out.append({"name": a.get("resLoanKind") or "마이너스통장", "balance": abs(bal), "start": a.get("resLoanStartDate") or a.get("resAccountStartDate") or "",
                                "account": "", "exec": "", "overdraft": True})
    return out


_LOAN_BIZ = _re2.compile(r"사업자|기업|운전|시설|소상공인|소호|SOHO|자영업|중소|보증서|신보|기보|재단|정책|사업|매출|카드가맹|가맹점|POS|창업|법인")
_LOAN_PERSONAL = _re2.compile(r"주택|아파트|전세|월세|보금자리|디딤돌|신용대출|직장인|가계|생활|오토|자동차|학자금|햇살|사잇돌|공무원|군인|교직원|중도금|이주비|비상금|마이너스|종합통장|예금담보|적금담보")


def _loan_kind(name, corp):
    """대출 이름으로 사업자/개인 구분 — 법인은 모두 사업자. 개인은 이름으로 판단(애매하면 사업자로 보수적으로)"""
    if corp:
        return "business"
    n = str(name or "")
    if _LOAN_BIZ.search(n):
        return "business"
    if _LOAN_PERSONAL.search(n):
        return "personal"
    return "business"


BANK_P_UNSUPPORTED = {"0020": "개인 조회는 출금계좌번호·계좌 비밀번호가 추가로 필요해 지원하지 않음"}   # CODEF 은행 로그인 파라미터표(2026.11.10)


def friendly_bank_msg(m):
    m = str(m or "")
    if "미등록" in m or "등록되지 않은" in m or "등록 되어있지" in m or "등록되어 있지" in m:
        return "이 인증서가 은행에 등록돼 있지 않음"
    if "파라미터" in m:
        return "은행이 추가 정보를 요구함"
    if "비밀번호" in m:
        return "인증서 비밀번호 오류"
    if "인터넷뱅킹" in m and ("미" in m or "가입" in m):
        return "이 은행 인터넷뱅킹 미가입"
    return m.split(".")[0][:50] or "인증서 등록 실패"


async def _bank_loans(params, banks, corp):
    """고른 은행들의 대출 → ([{org, date, amount}], 성공 은행, [(은행, 사유)])"""
    client = "B" if corp else "P"
    skipped = [] if corp else [(b, BANK_P_UNSUPPORTED[b]) for b in banks if b in BANK_P_UNSUPPORTED]
    banks = [b for b in banks if corp or b not in BANK_P_UNSUPPORTED]
    if not banks:
        return [], [], skipped
    acc = {"countryCode": "KR", "businessType": "BK", "clientType": client, "loginType": "0", "password": params.get("certPassword", "")}
    if params.get("certType") == "pfx":
        acc.update(certType="pfx", certFile=params.get("certFile", ""))
    else:
        acc.update(derFile=params.get("certFile", ""), keyFile=params.get("keyFile", ""))
    res = await _codef_post("/v1/account/create", {"accountList": [dict(acc, organization=b) for b in banks]})
    data = res.get("data") or {}
    cid = data.get("connectedId") if isinstance(data, dict) else None
    ok = [x.get("organization") for x in (data.get("successList") or []) if isinstance(x, dict)] if isinstance(data, dict) else []
    bad = [(x.get("organization"), x.get("message") or "등록 실패") for x in (data.get("errorList") or []) if isinstance(x, dict)] if isinstance(data, dict) else []
    if not cid:
        print(f"[CODEF] 은행 계정 등록 실패 {_codef_code(res)}: {_codef_msg(res)}")
        for b, m in bad:
            print(f"[CODEF]   └ {CODEF_BANKS.get(b, b)}({b}): {m}")
        reasons = dict(bad)          # 전부 실패(CF-04000)여도 은행별 이유는 errorList에 있음
        return [], [], [(b, friendly_bank_msg(reasons.get(b) or "인증서 등록 실패")) for b in banks] + skipped
    loans, ok_banks = [], []
    path = f"/v1/kr/bank/{'b' if corp else 'p'}/account/account-list"
    try:
        for b in ok:
            r = await _codef_post(path, {"connectedId": cid, "organization": b})
            if _codef_code(r) != "CF-00000":
                print(f"[CODEF] {b} 보유계좌 실패 {_codef_code(r)}: {_codef_msg(r)}")
                bad.append((b, (r.get("result") or {}).get("message") or "조회 실패"))
                continue
            ok_banks.append(b)
            now_k = datetime.utcnow() + timedelta(hours=9)
            for l in _bank_loan_rows(r.get("data")):
                d = _re2.sub(r"\D", "", str(l["start"]))
                kind_txt = l["name"]
                disp = l["name"]
                if len(d) < 6 and l.get("account"):        # 신규일이 없으면 대출 거래내역에서 신규일·대출종류
                    q = {"connectedId": cid, "organization": b, "account": l["account"],
                         "startDate": (now_k - timedelta(days=30)).strftime("%Y%m%d"), "endDate": now_k.strftime("%Y%m%d")}
                    if l.get("exec"):
                        q["accountLoanExecNo"] = l["exec"]
                    t = await _safe_async(_codef_post(f"/v1/kr/bank/{'b' if corp else 'p'}/loan/transaction-list", q), default={})
                    if _codef_code(t) == "CF-00000":
                        td = t.get("data") or {}
                        td = td[0] if isinstance(td, list) and td else td
                        if isinstance(td, dict):
                            d = _re2.sub(r"\D", "", str(td.get("resAccountStartDate") or ""))
                            kind_txt = f"{kind_txt} {td.get('resLoanKind') or ''}"
                            if td.get("resLoanKind"):
                                disp = str(td["resLoanKind"]).strip()       # 계좌명보다 대출종류가 더 구체적
                    else:
                        print(f"[CODEF] {b} 대출 거래내역 실패 {_codef_code(t)}: {_codef_msg(t)}")
                loans.append({"org": f"{CODEF_BANKS.get(b, b)} · {disp}".strip(" ·"),
                              "date": f"{d[:4]}.{d[4:6]}" if len(d) >= 6 else "", "amount": l["balance"],
                              "kind": _loan_kind(kind_txt, corp)})
    finally:      # 인증서 등록은 조회 후 바로 지운다
        await _safe_async(_codef_post("/v1/account/delete", {"connectedId": cid, "accountList": [
            {"countryCode": "KR", "businessType": "BK", "clientType": client, "loginType": "0", "organization": b} for b in ok]}))
    return loans, ok_banks, [(b, friendly_bank_msg(m)) for b, m in bad] + skipped


def _job_save(job_id, status=None, result=None):
    db = SessionLocal()
    try:
        row = db.query(models.CodefJob).filter(models.CodefJob.id == job_id).first()
        if row:
            if status:
                row.status = status
            if result is not None:
                row.result = _json.dumps(result, ensure_ascii=False, default=str)
            db.commit()
    except Exception as e:
        print(f"[CODEF] 작업 저장 오류: {e!r}")
    finally:
        db.close()


async def _hometax_job(job_id, params, reg_data, banks=None):
    try:
        res = await _hometax_collect(params, reg_data, progress=lambda p: _job_save(job_id, result=dict(p, banks=bool(banks))))
        if banks:      # 공동인증서: 고른 거래 은행의 대출까지
            _job_save(job_id, result={"got": res["got"], "failed": res["pending"], "doing": "은행 대출", "fill": res["fill"], "banks": True})
            corp = (res.get("fill") or {}).get("biz_type") == "corp"
            loans, ok_banks, bad = await _safe_async(_bank_loans(params, banks, corp), default=([], [], [(b, "조회 오류") for b in banks]))
            if ok_banks:
                res["fill"]["loans"] = loans
                res["got"].append("은행 대출")
                res["bank_ok"] = [CODEF_BANKS[b] for b in ok_banks]
            if bad:
                res["bank_failed"] = [f"{CODEF_BANKS.get(b, b)}({m})" for b, m in bad]
                if not ok_banks:
                    res["pending"].append("은행 대출")
        _job_save(job_id, "done", res)
    except Exception as e:
        print(f"[CODEF] 작업 오류 {job_id}: {e!r}")
        _job_save(job_id, "error", {"detail": "홈택스 자료를 불러오는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요."})


def _hometax_start_job(params, reg_data, user_id, banks=None):
    """인증 성공 → 나머지 서류는 서버에서 이어서 조회 (요청 하나가 너무 길어 연결이 끊기는 문제 방지)"""
    job_id = uuid.uuid4().hex
    fill = _safe(_parse_registration, reg_data, params.get("_want_biz", ""), default={}) or {}
    db = SessionLocal()
    try:
        db.query(models.CodefJob).filter(models.CodefJob.created_at < datetime.utcnow() - timedelta(hours=1)).delete()
        db.add(models.CodefJob(id=job_id, user_id=user_id, status="running",
                               result=_json.dumps({"got": ["사업자등록증명"] if fill else [], "doing": "", "fill": fill}, ensure_ascii=False)))
        db.commit()
    finally:
        db.close()
    t = asyncio.create_task(_hometax_job(job_id, params, reg_data, banks))
    _codef_tasks.add(t)
    t.add_done_callback(_codef_tasks.discard)
    return {"status": "collecting", "job": job_id, "fill": fill}


# ── 크레딧포유(신용정보원) 신용정보조회서 → 기대출 ──
#  CODEF 상품은 크레딧포유 아이디·비밀번호 로그인만 지원(간편인증 X). 보안문자는 CODEF가 자동 인식, 7번 실패 시 사용자 입력.
#  결과는 조회서 PDF(Base64)뿐 → AI로 읽어 대출·보증·연체 정리. 아이디·비밀번호·PDF는 저장하지 않는다.
CODEF_CREDIT4U_REPORT = "/v1/kr/insurance/is/credit4u/credit-report"
C4U_NOT_MEMBER = "크레딧포유 미가입 회원이에요. 크레딧포유에 가입된 경우에만 대출내역을 불러올 수 있어요. (가입했다면 아이디·비밀번호를 확인해 주세요)"
_c4u_live = {}      # job_id -> {"user_id", "params", "two_way", "t"}


class Credit4uBody(BaseModel):
    user_id: str
    password: str


class Credit4uSecureBody(BaseModel):
    job: str
    secure_no: str = ""
    refresh: bool = False


def _c4u_login_fail(res):
    msg = _codef_msg(res)
    code = _codef_code(res)
    return code.startswith("CF-128") or any(k in msg for k in ("아이디", "비밀번호", "패스워드", "회원", "로그인", "일치하지"))


async def _c4u_run(job_id, req):
    """크레딧포유 요청 1번 → 보안문자 필요 / 성공(PDF 해석) / 실패를 작업에 기록"""
    st = _c4u_live.get(job_id)
    if not st:
        return
    try:
        res = await _codef_post(CODEF_CREDIT4U_REPORT, req)
    except Exception as e:
        print(f"[CODEF] credit4u 호출 오류: {e!r}")
        _c4u_live.pop(job_id, None)
        return _job_save(job_id, "error", {"detail": "크레딧포유 연결에 실패했어요. 잠시 후 다시 시도해 주세요."})
    code, data = _codef_code(res), (res.get("data") or {})
    if code == "CF-03002" and isinstance(data, dict) and data.get("continue2Way"):
        ex = data.get("extraInfo") or {}
        st["two_way"] = {k: data.get(k) for k in ("jobIndex", "threadIndex", "jti", "twoWayTimestamp")}
        img = ex.get("reqSecureNo") or ""
        return _job_save(job_id, "need_secure", {"secure_img": img if str(img).startswith("data:") else (f"data:image/png;base64,{img}" if img else "")})
    _c4u_live.pop(job_id, None)
    if code != "CF-00000":
        print(f"[CODEF] credit4u 실패 {code}: {_codef_msg(res)}")
        return _job_save(job_id, "error", {"detail": C4U_NOT_MEMBER if _c4u_login_fail(res) else f"크레딧포유 조회에 실패했어요: {_codef_msg(res)}",
                                           "not_member": _c4u_login_fail(res)})
    rows = data if isinstance(data, list) else [data]
    pdf = next((r.get("resOriGinalData") or r.get("resOriginalData") for r in rows if isinstance(r, dict) and (r.get("resOriGinalData") or r.get("resOriginalData"))), "")
    if not pdf:
        return _job_save(job_id, "error", {"detail": "크레딧포유에서 조회서를 받지 못했어요. 잠시 후 다시 시도해 주세요."})
    _job_save(job_id, "running", {"doing": "조회서 읽는 중"})
    parsed, err = await _gemini_json([{"inline_data": {"mime_type": "application/pdf", "data": pdf}}], CREDIT_DOC_PROMPT)
    if parsed is None:
        print(f"[CODEF] credit4u PDF 해석 실패: {err}")
        return _job_save(job_id, "error", {"detail": "조회서를 읽지 못했어요. 잠시 후 다시 시도해 주세요."})
    out = _credit_clean(parsed)
    out["kcb"] = out["nice"] = None        # 크레딧포유에는 신용점수가 없음
    _job_save(job_id, "done", {"credit": out})


@app.post("/codef/credit4u/report")
async def codef_credit4u_report(body: Credit4uBody, user: models.User = Depends(get_current_user)):
    if not _codef_ready():
        raise HTTPException(status_code=503, detail="연동이 아직 설정되지 않았습니다.")
    uid = (body.user_id or "").strip()
    if not uid or not body.password:
        raise HTTPException(status_code=400, detail="크레딧포유 아이디와 비밀번호를 입력해 주세요.")
    for k in [k for k, v in _c4u_live.items() if _time.time() - v["t"] > 600]:
        _c4u_live.pop(k, None)
    params = {"organization": "0001", "userId": uid, "userPass": _codef_rsa(body.password),
              "isIdentityViewYn": "0", "originDataYN": "1"}
    job_id = uuid.uuid4().hex
    _c4u_live[job_id] = {"user_id": user.id, "params": params, "two_way": None, "t": _time.time()}
    db = SessionLocal()
    try:
        db.add(models.CodefJob(id=job_id, user_id=user.id, status="running", result=_json.dumps({"doing": "크레딧포유 조회 중"})))
        db.commit()
    finally:
        db.close()
    _spawn(_c4u_run(job_id, params))
    return {"status": "running", "job": job_id}


@app.post("/codef/credit4u/secure")
async def codef_credit4u_secure(body: Credit4uSecureBody, user: models.User = Depends(get_current_user)):
    """보안문자 입력(또는 새로고침) → 이어서 조회"""
    st = _c4u_live.get(body.job)
    if not st or st["user_id"] != user.id or not st.get("two_way"):
        raise HTTPException(status_code=400, detail="조회 시간이 지났어요. 처음부터 다시 시도해 주세요.")
    req = dict(st["params"], secureNo=(body.secure_no or "").strip(), secureNoRefresh="1" if body.refresh else "0",
               is2Way=True, twoWayInfo=st["two_way"])
    if not body.refresh and not req["secureNo"]:
        raise HTTPException(status_code=400, detail="보안문자를 입력해 주세요.")
    _job_save(body.job, "running", {"doing": "크레딧포유 조회 중"})
    _spawn(_c4u_run(body.job, req))
    return {"status": "running", "job": body.job}


@app.get("/codef/hometax/job/{job_id}")
def codef_hometax_job(job_id: str, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(models.CodefJob).filter(models.CodefJob.id == job_id, models.CodefJob.user_id == user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="조회 작업을 찾을 수 없어요. 처음부터 다시 시도해 주세요.")
    data = {}
    try:
        data = _json.loads(row.result or "{}")
    except Exception:
        pass
    status = row.status
    if status == "running" and row.created_at and row.created_at < datetime.utcnow() - timedelta(minutes=8):
        status = "error"
        data = {"detail": "자료 조회가 너무 오래 걸려 중단됐어요. 다시 시도해 주세요."}
    return dict(data, status=status)      # 오래된 작업은 새 작업을 만들 때 1시간 지나면 정리


def _safe(fn, *a, default=None):
    try:
        return fn(*a)
    except Exception as e:
        print(f"[CODEF] 해석 오류 {getattr(fn, '__name__', fn)}: {e!r}")
        return default


async def _hometax_collect(params, reg_data, progress=None):
    """인증이 끝난 세션(같은 id)으로 나머지 서류를 이어서 조회해 원클릭 입력값으로 정리"""
    fill = _safe(_parse_registration, reg_data, params.get("_want_biz", ""), default={}) or {}
    got, failed = (["사업자등록증명"] if fill else []), []
    step = (lambda doing: progress and progress({"got": list(got), "failed": list(failed), "doing": doing, "fill": fill}))
    now = datetime.utcnow() + timedelta(hours=9)
    corp = fill.get("biz_type") == "corp"
    base = {k: params[k] for k in ("organization", "loginType", "loginTypeLevel", "userName", "loginIdentity", "id",
                                   "certType", "certFile", "keyFile", "certPassword") if k in params}
    if params.get("telecom"):
        base["telecom"] = params["telecom"]
    phone = params.get("phoneNo", "")
    ph = (lambda key: {key: phone} if phone else {})    # 간편인증만 휴대폰 번호를 보낸다

    errs = []

    async def call(key, extra):
        try:
            res = await _codef_post(CODEF_HOMETAX_PRODUCTS[key], dict(base, **extra))
        except Exception as e:
            print(f"[CODEF] {key} 호출 오류: {e}")
            return None, "connection"
        code = _codef_code(res)
        if code == "CF-00000":
            return res.get("data"), None
        print(f"[CODEF] {key} 실패 {code}: {_codef_msg(res)}")
        errs.append(f"{_codef_msg(res)} ({code})")
        return None, _codef_msg(res)

    # ① 부가세과세표준증명 — 최근 3개 연도 (회원 인증서·회원 간편인증만 지원)
    if params.get("loginType") in ("0", "5"):
        step("부가세과세표준증명")
        y = now.year
        data, err = await call("vat", {**ph("phoneNo"), "startDate": f"{y - 3}01", "endDate": _vat_end(now),
                                       "isIdentityViewYN": "0", "usePurposes": "99", "submitTargets": "99", "applicationType": "01", "originDataYN": "0"})
        years, ytd = _safe(_parse_vat_split, data, now.year, default=([], None)) if data is not None else ([], None)
        if years or ytd:
            if years:
                fill["years"] = years
            if ytd:
                fill["ytd"] = ytd
            got.append("부가세과세표준증명")
        else:
            failed.append("부가세과세표준증명")
    else:
        failed.append("부가세과세표준증명")
    # ①-2 면세사업자 수입금액증명 — 면세사업자(또는 부가세 증명이 안 나온 경우)만
    if params.get("loginType") in ("0", "5") and 8 <= now.hour < 22 and ("부가세과세표준증명" in failed or _safe(_is_taxfree, reg_data, default=False)):
        step("면세사업자 수입금액증명")
        data, err = await call("taxfree", {**ph("phoneNo"), "startYear": str(now.year - 3), "endYear": str(now.year - 1),
                                           "isIdentityViewYN": "0", "usePurposes": "99", "submitTargets": "99", "originDataYN": "0"})
        tf = _safe(_taxfree_revenues, data, default={}) if data is not None else {}
        if tf:
            _merge_fin_years(fill, tf, now.year)
            got.append("면세사업자 수입금액증명")
            if "부가세과세표준증명" in failed:
                failed.remove("부가세과세표준증명")      # 면세사업자는 부가세 증명이 없는 게 정상

    # ② 납세증명서 — 발급되면 체납 없음
    step("납세증명서")
    data, err = await call("tax_payment", {**ph("phoneNo"), "isIdentityViewYN": "0", "isAddrViewYn": "0", "proofType": "B0007",
                                           "submitTargets": "99", "applicationType": "01", "originDataYN": "0"})
    arrears = _safe(_parse_tax_cert, data, err)
    if arrears:
        fill["arrears"] = arrears
        got.append("납세증명서")
    else:
        failed.append("납세증명서")

    # ③ 재무제표 — 마지막 신고분 (법인: 사업연도 종료연월 / 개인: 과세연도)
    fin = None
    bizno = _re2.sub(r"\D", "", fill.get("business_number", "") or params.get("_want_biz", ""))
    if 8 <= now.hour < 22:
        step("재무제표")
        yrs = [now.year - 1, now.year - 2, now.year - 3] + ([now.year - 4] if now.month < 6 else [])
        tries = [f"{yy}12" for yy in yrs] if corp else [str(yy) for yy in yrs]
        fin_revs = {}
        for sd in tries:
            extra = {**ph("loginPhoneNo"), "startDate": sd, "isIdentityViewYN": "0", "usePurposes": "99", "submitTargets": "99", "originDataYN": "0"}
            if not corp:
                extra["proofType"] = "40"
            if bizno and params.get("loginType") == "0":
                extra["identity"] = bizno          # 인증서 로그인은 재무제표에 사업자번호 필수 (CF-12850)
            data, err = await call("financial", extra)
            if data is None:
                continue
            f1 = _safe(_parse_financial, data)
            fin_revs.update(_safe(_fin_revenues, data, default={}) or {})
            if f1 and (not fin or f1.get("_year", 0) > fin.get("_year", 0)):
                fin = f1                                   # 재무 수치는 가장 최근 연도
            if len([yy for yy in fin_revs if yy < now.year]) >= 3:
                break
        _merge_fin_years(fill, fin_revs, now.year)          # 매출은 최근 3년 모두
    if fin:
        fin.pop("_revenue", None)
        fin.pop("_year", None)
        fill["fin"] = fin
        got.append("재무제표")
    elif corp:                                # 개인사업자는 재무제표가 없는 경우가 많아 안내하지 않음
        failed.append("재무제표" if 8 <= now.hour < 22 else "재무제표(08~22시에만 조회 가능)")
    # ③-2 원천징수이행상황신고서 — 근로소득 인원(직원 수)과 최근 1년 추세 (기관코드 0005, 사업자번호 필수)
    wh_n = wh_tr = wh_df = None
    if bizno and params.get("loginType") == "0":
        data, err = await call("withholding", dict(_wh_body(now), identity=bizno))
        if data is not None:
            wh_n, wh_tr, wh_df = _safe(_parse_withholding, data, default=(None, None, None))
    # ④ 직원 수 — 근로복지공단 근로자고용정보현황 (인증서는 추가 승인 없음)
    cw = _cw_body(params, corp=corp, bizno=bizno)
    if cw:
        step("직원 수(고용보험)")
        rrn = _safe(_reg_rrn, reg_data, params.get("_want_biz", ""), default="") or ""
        # 대표자 개인명의(0)는 주민번호, 사업자명의 인증서(2)는 사업자번호 — 맞는 조합을 차례로 시도
        # 법인: 사업자명의 인증서(2)+사업자번호 먼저 / 개인: 대표자 개인명의(0)+주민번호 먼저
        tries = ([("2", bizno)] + ([("0", rrn)] if rrn else [])) if corp else (([("0", rrn)] if rrn else []) + [("2", bizno), ("0", bizno)])
        tries = [t for i, t in enumerate(tries) if t[1] and t not in tries[:i]]
        res = {}
        try:
            for i, (ut, ident) in enumerate(tries):
                body = dict(cw, userType=ut, identity=ident)
                res = await _cw_resolve(body, await _codef_post(CODEF_HOMETAX_PRODUCTS["employees"], body), fill.get("company_name", ""))
                if _codef_code(res) == "CF-00000" or _cw_no_data(_codef_code(res), _codef_msg(res)):
                    break
                print(f"[CODEF] employees 시도{i + 1}(구분 {ut}, {'주민번호' if ident == rrn else '사업자번호'}) 실패 {_codef_code(res)}: {_codef_msg(res)}")
        except Exception as e:
            print(f"[CODEF] employees 호출 오류: {e!r}")
            res = {}
        n = _safe(_parse_employees, res.get("data")) if _codef_code(res) == "CF-00000" else (0 if _cw_no_data(_codef_code(res), _codef_msg(res)) else None)
        if _codef_code(res) not in ("", "CF-00000"):
            print(f"[CODEF] employees 실패 {_codef_code(res)}: {_codef_msg(res)}")
        tr, df = _safe(_parse_emp_change, res.get("data"), default=(None, None)) if (_codef_code(res) == "CF-00000" and n) else (None, None)
        if (n is None or n == 0) and wh_n:                    # 근로복지공단이 안 되면 원천세 신고 인원
            n = wh_n
        if n is None and wh_n == 0:
            n = 0
        if n is not None:
            fill["employees"] = n
            got.append("직원 수(고용보험)")
            if not tr:
                tr, df = wh_tr, wh_df
            if tr:
                fill["emp_trend"] = tr
                if df is not None:
                    fill["emp_increase"] = df
        else:
            fill["employees_default"] = 0
            failed.append("직원 수(고용보험) — 0명으로 채움")
    elif wh_n is not None:
        fill["employees"] = wh_n
        got.append("직원 수(고용보험)")
        if wh_tr:
            fill["emp_trend"] = wh_tr
            if wh_df is not None:
                fill["emp_increase"] = wh_df
    else:
        fill["employees_default"] = 0
    return {"status": "done", "fill": fill, "got": got, "pending": failed, "reason": "" if got else (errs[0] if errs else "")}


def _codef_amt(v):
    t = str(v or "").replace(",", "").replace(" ", "").strip()
    neg = t.startswith("-") or t.startswith("△") or t.startswith("(")
    t = _re2.sub(r"[^\d.]", "", t)
    if not t:
        return None
    try:
        n = float(t)
    except ValueError:
        return None
    return -n if neg else n


def _codef_rows(obj, must):
    """응답 안에서 must 키를 가진 dict를 모두 찾는다 (단건=객체 / 다건=리스트 모두 처리)"""
    out = []
    if isinstance(obj, dict):
        if all(k in obj for k in must):
            out.append(obj)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                out.extend(_codef_rows(v, must))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_codef_rows(v, must))
    return out


def _vat_end(now):
    """조회 끝 과세기간 — 올해 1기 확정신고(7월 25일)가 끝난 8월부터는 올해 1기까지"""
    return f"{now.year}01" if now.month >= 8 else f"{now.year - 1}07"


def _parse_vat_split(data, this_year):
    """부가세과세표준증명 → (지난 연도별 매출 최근 3년, 올해 누적 {year, month, sales})"""
    by_year, months = {}, {}
    for r in _codef_rows(data, ("commStartDate",)):
        amt = _codef_amt(r.get("resIncomeTotalAmt"))
        if amt is None:
            amt = _codef_amt(r.get("resIncomeAmt"))
        st, en = str(r.get("commStartDate") or ""), str(r.get("commEndDate") or "")
        y = st[:4]
        if amt is None or not y.isdigit():
            continue
        y = int(y)
        try:
            m = int(en[4:6]) - int(st[4:6]) + 1 if len(en) >= 6 and en[:4] == st[:4] else 6
        except ValueError:
            m = 6
        by_year[y] = by_year.get(y, 0) + amt
        months[y] = months.get(y, 0) + max(1, min(12, m))
    past = [{"year": y, "vat": by_year[y]} for y in sorted(by_year) if y < this_year][-3:]
    ytd = {"year": this_year, "month": min(12, months[this_year]), "sales": by_year[this_year]} if this_year in by_year else None
    return past, ytd


def _parse_vat(data):
    """부가세과세표준증명 → 연도별 매출 (1기+2기 합계)"""
    by_year = {}
    for r in _codef_rows(data, ("commStartDate",)):
        amt = _codef_amt(r.get("resIncomeTotalAmt"))
        if amt is None:
            amt = _codef_amt(r.get("resIncomeAmt"))
        y = str(r.get("commStartDate") or "")[:4]
        if amt is None or not y.isdigit():
            continue
        by_year[int(y)] = by_year.get(int(y), 0) + amt
    return [{"year": y, "vat": by_year[y]} for y in sorted(by_year)][-3:]


def _parse_tax_cert(data, err):
    """납세증명서 → 국세 체납 여부"""
    if data is not None:
        rows = data if isinstance(data, list) else [data]
        arrears_rows = [a for r in rows if isinstance(r, dict) for a in (r.get("resArrearsList") or [])
                        if isinstance(a, dict) and (a.get("resTaxItemName") or _codef_amt(a.get("resLocalTaxAmt")))]
        if arrears_rows:
            amt = sum((_codef_amt(a.get("resLocalTaxAmt")) or 0) + (_codef_amt(a.get("resAdditionalCharges")) or 0) for a in arrears_rows)
            return {"status": "has", "amount": amt or None}
        return {"status": "none"}
    if err and "체납" in err:
        return {"status": "has"}
    return None


_FIN_KEYS = {
    "assets": ("자산총계",), "liabilities": ("부채총계",), "capital_stock": ("자본금",),
    "operating_income": ("영업이익", "영업손익"), "interest_expense": ("이자비용",),
    "net_income": ("당기순이익", "당기순손익"), "_revenue": ("매출액", "수입금액"),
    "short_borrowings": ("단기차입금",), "long_borrowings": ("장기차입금",),
}


def _fin_revenues(data):
    """표준재무제표증명 → {연도: 매출액(수입금액)} — 응답에 여러 연도가 있으면 모두"""
    rows = data if isinstance(data, list) else [data]
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        yr = str(r.get("resAttrYear") or r.get("commEndDate") or "")[:4]
        if not yr.isdigit():
            continue
        for it in (r.get("resIncomeStatement") or []):
            if not isinstance(it, dict):
                continue
            t = _re2.sub(r"[^가-힣]", "", it.get("_title") or "")
            a = _codef_amt(it.get("_amt"))
            if a is not None and any(t.startswith(n) or t.endswith(n) for n in _FIN_KEYS["_revenue"]):
                out[int(yr)] = a
                break
    return out


def _taxfree_revenues(data):
    """면세사업자 수입금액증명 → {귀속연도: 수입금액} (업종별 여러 줄이면 합계)"""
    out = {}
    for r in _codef_rows(data, ("commStartDate",)):
        a = _codef_amt(r.get("resEarningsAmt"))
        y = str(r.get("commStartDate") or r.get("commEndDate") or "")[:4]
        if a is not None and y.isdigit():
            out[int(y)] = out.get(int(y), 0) + a
    return out


def _is_taxfree(reg_data):
    rows = reg_data if isinstance(reg_data, list) else [reg_data]
    return any("면세" in str((r or {}).get("resBusinessmanType") or "") for r in rows if isinstance(r, dict))


def _merge_fin_years(fill, revs, this_year):
    """재무제표 매출로 연도별 매출 채우기 — 부가세과세표준증명 값이 있는 연도는 그대로, 최근 3년"""
    if not revs:
        return
    by = {int(y["year"]): dict(y) for y in (fill.get("years") or []) if y.get("year")}
    for yr, rev in revs.items():
        if yr >= this_year:
            continue
        cur = by.get(yr)
        if not cur or cur.get("vat") in (None, 0):
            by[yr] = dict(cur or {}, year=yr, vat=rev)
    fill["years"] = [by[y] for y in sorted(by)][-3:]


def _parse_financial(data):
    """표준재무제표증명 → 원클릭 재무 수치 (총자산·총부채·자본금·영업이익·이자비용·당기순이익)"""
    if data is None:
        return None
    rows = data if isinstance(data, list) else [data]
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return None
    row = sorted(rows, key=lambda r: str(r.get("commEndDate") or r.get("resAttrYear") or ""))[-1]   # 가장 최근 연도
    items = []
    for key in ("resBalanceSheet", "resIncomeStatement"):
        for it in (row.get(key) or []):
            if isinstance(it, dict):
                items.append((_re2.sub(r"[^가-힣]", "", it.get("_title") or ""), _codef_amt(it.get("_amt"))))
    out = {}
    for k, names in _FIN_KEYS.items():
        for title, amt in items:
            if amt is not None and any(title.startswith(n) or title.endswith(n) for n in names):   # 예: 영업이익(손실)
                out[k] = amt
                break
    if "assets" not in out and "liabilities" not in out:
        return None
    yr = str(row.get("resAttrYear") or row.get("commEndDate") or "")[:4]
    if yr.isdigit():
        out["_year"] = int(yr)
    return out


class HometaxLimitBody(BaseModel):
    input: dict
    consume: bool = False     # True = 새 분석(이용 횟수 기록), False = 분석이력 다시 보기

@app.post("/hometax/limit")
def hometax_limit(body: HometaxLimitBody, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """원클릭 분석의 예상 한도. 구독(전체 공개)일 때만 금액·기관을 내려주고, 아니면 신청 가능 기관 수만 알려준다."""
    subscribed = user_has_subscription_access(db, user)
    used = 0
    if body.consume:
        if not subscribed:
            _check_free_quota(db, user)
        used, full = _consume_analysis(db, user, subscribed)
    else:
        full = subscribed
    try:
        L = _ht_clean(ht_estimate_limit(body.input or {}))
    except Exception as e:
        print(f"[hometax_limit] 계산 오류: {e}")
        L = {"available": False, "status": "none", "low": 0, "high": 0, "orgs": [], "text": "예상 한도를 계산하지 못했습니다. 입력값을 확인해 주세요."}
    ok_orgs = sum(1 for o in (L.get("orgs") or []) if o.get("status") == "ok")
    return {"full": full, "limit": L if full else None, "ok_orgs": ok_orgs,
            "credits_used": used, "remaining_credits": user.credits, "subscribed": subscribed}


@app.post("/analyze/manual")
def analyze_manual(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    subscribed = user_has_subscription_access(db, user)
    if not subscribed:
        _check_free_quota(db, user)
    used, full = _consume_analysis(db, user, subscribed)
    return {"credits_used": used, "remaining_credits": user.credits, "subscribed": subscribed, "full": full}

def _check_free_quota(db: Session, user):
    """구독·충전분이 없는 회원의 이번 달 무료 분석 횟수 확인"""
    now = datetime.utcnow() + timedelta(hours=9)                     # 한국 시간 기준 '이번 달'
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=9)
    n = db.query(models.AnalysisLog).filter(
        models.AnalysisLog.user_id == user.id,
        models.AnalysisLog.credits_used == 0,
        models.AnalysisLog.created_at >= month_start,
    ).count()
    if n >= FREE_ANALYSES_PER_MONTH:
        raise HTTPException(status_code=402, detail=f"이번 달 무료 분석 {FREE_ANALYSES_PER_MONTH}회를 모두 사용했어요. 구독하시면 분석 횟수 제한 없이 이용할 수 있어요. (구독이 필요합니다)")

def _consume_analysis(db: Session, user, subscribed: bool):
    """분석 1회 기록. (차감 크레딧, 전체 결과 공개 여부)"""
    used, full = 0, bool(subscribed)                                 # 구독 = 전체 공개 / 무료 = 일부 공개
    db.add(models.AnalysisLog(user_id=user.id, credits_used=used))
    db.commit()
    return used, full

# 부가세과세표준증명원 / 소득금액증명원 등 국세청 서류에서 매출·소득 자동 인식
# (재무제표가 없는 개인사업자용 "직접 입력" 보조 기능 — 크레딧 차감 없음, 대출/자산은 서류에 없어 여전히 직접 입력 필요)
class IncomeDocExtractRequest(BaseModel):
    images: list[ImageItem] = []

async def _gemini_json(image_parts, prompt, max_tokens=3000):
    """이미지 + 지시문 → Gemini JSON 응답 (모델 여러 개 순서대로 시도)"""
    import json, re
    models_ = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.7-flash"]
    last_err = None
    async with httpx.AsyncClient(timeout=90) as client:
        for model in models_:
            for attempt in range(2):
                try:
                    resp = await client.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}",
                        headers={"Content-Type": "application/json"},
                        json={"contents": [{"parts": [*image_parts, {"text": prompt}]}],
                              "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens}})
                    if resp.status_code in (503, 429):
                        last_err = resp.text
                        if attempt == 0:
                            await asyncio.sleep(0.8)
                            continue
                        break
                    if resp.status_code != 200:
                        last_err = resp.text
                        break
                    txt = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                    txt = re.sub(r"```json|```", "", txt).strip()
                    a, b = txt.find("{"), txt.rfind("}")
                    if a == -1 or b == -1:
                        raise ValueError("JSON 객체를 찾을 수 없음")
                    return json.loads(txt[a:b + 1]), None
                except Exception as e:
                    last_err = str(e)
                    break
    return None, last_err


CREDIT_DOC_PROMPT = """이 문서는 한국의 개인 신용정보 조회 결과입니다 (올크레딧(KCB)·나이스지키미(NICE)의 "신용정보조회서"/"신용평점(점수) 조회", 신용정보원 크레딧포유 "신용정보 조회 결과", 토스·카카오뱅크 등 앱의 신용점수 화면 캡처 중 하나이거나 여러 장).
첨부된 모든 이미지를 함께 보고 아래 항목을 추출하세요.

1) 신용점수 (1000점 만점)
- KCB(올크레딧, 코리아크레딧뷰로) 점수 → kcb, NICE(나이스지키미, 나이스평가정보) 점수 → nice.
- 화면·문서에 어느 회사 점수인지 표시가 있습니다. 회사 표시가 없는 점수 하나만 있으면 문서 발급처(올크레딧=KCB, 지키미=NICE) 기준으로 넣으세요. 등급(1~10등급)만 있고 점수가 없으면 null.
2) 조회 대상자 이름 → name
3) 대출 현황 (대출·카드론·할부 등 "대출" 표) → loans 배열. 각 항목:
   {"org": 대출 기관명, "date": 대출(개설)일 "YYYY.MM", "amount": 현재 잔액(원 단위 숫자), "guarantor": 보증서 담보 대출이면 보증기관(신용보증기금·기술보증기금·○○신용보증재단 등) 아니면 null, "kind": 대출 종류(신용대출·담보대출·운전자금 등)}
   - 금액 단위가 "천원"이면 1000을 곱해 원으로 바꾸세요. "만원"이면 10000을 곱하세요.
   - 신용카드 일시불·카드 사용액은 넣지 마세요. 카드론·현금서비스는 넣으세요.
   - 표에 대출이 하나도 없으면(대출 없음) loans는 빈 배열 [], 대출 표 자체가 문서에 없으면 null.
4) 보증 현황 표가 따로 있으면 → guarantees 배열 [{"org": 보증기관, "date": "YYYY.MM", "amount": 원}] (없으면 null)
5) 연체 정보가 있으면 has_delinquency true, 없다고 나오면 false, 알 수 없으면 null.

아래 JSON 형식으로만 응답하세요:
{"doc_type":문서 종류,"name":문자열또는null,"kcb":숫자또는null,"nice":숫자또는null,"loans":[...] 또는 null,"guarantees":[...] 또는 null,"has_delinquency":true/false/null,"comment":"인식 관련 메모 1문장"}"""


def _credit_clean(data):
    """AI가 읽은 신용정보 → 신용점수·기대출 정리"""
    num = lambda v: _codef_amt(v)

    def score(v):
        n = num(v)
        return int(n) if n is not None and 0 < n <= 1000 else None
    out = {"doc_type": data.get("doc_type"), "name": data.get("name"), "kcb": score(data.get("kcb")), "nice": score(data.get("nice")),
           "has_delinquency": data.get("has_delinquency") if isinstance(data.get("has_delinquency"), bool) else None, "loans": None}
    if isinstance(data.get("loans"), list):
        loans = []
        for l in data["loans"]:
            if not isinstance(l, dict):
                continue
            amt = num(l.get("amount"))
            raw = str(l.get("amount") or "")
            if amt is not None and "천원" in raw:
                amt *= 1000
            elif amt is not None and "만원" in raw:
                amt *= 10000
            org = str(l.get("org") or "").strip()
            g = str(l.get("guarantor") or "").strip()
            if g and g.lower() not in ("null", "none") and g not in org:
                org = f"{org} ({g} 보증)" if org else g
            d = _re2.sub(r"[^\d]", "", str(l.get("date") or ""))
            loans.append({"org": org, "date": f"{d[:4]}.{d[4:6]}" if len(d) >= 6 else (d[:4] if len(d) >= 4 else ""), "amount": amt})
        out["loans"] = [l for l in loans if l["org"] or l["amount"]]
    return out


INCALL_PROMPT = """아래는 정책자금 컨설팅 회사의 '인콜일지(상담 기록지)'입니다. 업체와 통화하며 적은 메모라 표기가 제각각입니다(예: '1.7천' = 1,700만 원, '2.5억' = 2억 5천만 원, '5억' = 500,000,000원, 'x' = 없음).
적힌 내용만 옮기고, 적혀 있지 않은 항목은 null로 두세요. 추측해서 채우지 마세요. 금액은 모두 원 단위 숫자로 바꾸세요.
양식의 빈칸 표시 기호( '(   )', '___', ':' 등 )는 값이 아니니 빼고 실제로 적힌 글자만 옮기세요. 단 '(주)'·'주식회사' 같은 상호 표기는 그대로 둡니다.

항목:
- company_name: 업체명 / industry, item: 업태·업종(사업자등록증 기준)의 업태와 종목. 두 번째 업태·종목이 있으면 industry2, item2
- location: 사업장 소재지를 시·도 하나로 (서울,부산,대구,인천,광주,대전,울산,세종,경기,강원,충북,충남,전북,전남,경북,경남,제주 중 하나. 예: 목포 → 전남, 수원 → 경기)
- founded: 창업년월 "YYYY-MM" (연도만 있으면 "YYYY")
- years: 연도별 매출 [{"year":2024,"sales":400000000}, ...] (올해 진행 중인 매출은 제외)
- ytd: 올해 현재까지 매출 {"year":올해,"month":몇 월까지,"sales":금액} — 적혀 있을 때만
- kcb, nice: 대표자 신용점수 / co_kcb, co_nice: 공동대표·배우자 신용점수
- recovery: 신용회복 {"type":"종류·회차 문구","faithful":"yes"(성실납부)|"no"(연체)} — 없으면 null
- arrears: 국세 체납 {"status":"has"|"none","amount":금액,"note":"세목·기간"} — '없음/x'면 status "none", 아예 안 적혀 있으면 null
- arrears_other: 기타 연체·체납·가압류 문구 ('x'·없음이면 null)
- loans: 기대출 목록 [{"org":"기관(예: 지역신용보증재단, 땅 담보대출)","date":"YYYY.MM 또는 원문","amount":금액,"kind":"personal"(개인 명의·개인 담보)|"business"(사업자·정책자금)}] — 기대출이 '없음'이면 []
- age: 대표자 만 나이(숫자) / career: 동종업계 경력(년, 숫자) / assets_note: 보유 자산 문구 / marital: "married"|"single" / woman: 여성 대표면 true
- employees: 직원 수(4대보험 기준, 'x'·없음이면 0) / hire: 추가 채용 예정 인원
- naeil: 내일채움공제 문구 / umbrella: 노란우산공제 가입이면 "yes", 미가입이면 "no"
- export_amount: 수출 실적 금액 / factory: 공장 "own"(자가)|"rent"(임차)|"none"
- memo: ※ 추가사항에 적힌 상담 메모를 핵심만 3줄 이내로 요약 (분석에 참고할 사실 위주)

JSON으로만 답하세요:
{"company_name":..,"industry":..,"item":..,"industry2":..,"item2":..,"location":..,"founded":..,"years":[..],"ytd":..,"kcb":..,"nice":..,"co_kcb":..,"co_nice":..,"recovery":..,"arrears":..,"arrears_other":..,"loans":[..] 또는 null,"age":..,"career":..,"assets_note":..,"marital":..,"woman":..,"employees":..,"hire":..,"naeil":..,"umbrella":..,"export_amount":..,"factory":..,"memo":..}"""


def _docx_text(b64):
    """.docx → 표·문단 텍스트 (병합 셀 중복 제거). 외부 라이브러리 없이 XML로 읽는다"""
    import zipfile, io
    from xml.etree import ElementTree as ET
    raw = _b64.b64decode(b64)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        xml = z.read("word/document.xml")
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ET.fromstring(xml)
    body = root.find(f"{W}body")
    lines = []
    def ptext(p):
        return "".join((t.text or "") for t in p.iter(f"{W}t")).strip()
    for el in list(body) if body is not None else []:
        if el.tag == f"{W}p":
            t = ptext(el)
            if t:
                lines.append(t)
        elif el.tag == f"{W}tbl":
            for tr in el.iter(f"{W}tr"):
                cells, prev = [], None
                for tc in tr.findall(f"{W}tc"):
                    t = " / ".join(x for x in (ptext(p) for p in tc.iter(f"{W}p")) if x)
                    if t != prev:
                        cells.append(t)
                    prev = t
                if any(cells):
                    lines.append(" | ".join(cells))
    return "\n".join(lines)[:20000]


@app.post("/extract/incall")
async def extract_incall(body: IncomeDocExtractRequest, user: models.User = Depends(get_current_user)):
    """인콜일지(상담 기록지) → 원클릭 입력값. .docx는 글자만 뽑아서, 이미지·PDF는 그대로 AI가 읽는다. 저장하지 않는다."""
    if not body.images:
        raise HTTPException(status_code=400, detail="인콜일지 파일이 없습니다.")
    parts = []
    for img in body.images[:5]:
        mime = (img.mime or "").lower()
        if "wordprocessingml" in mime or mime.endswith("docx") or mime == "application/msword":
            try:
                parts.append({"text": "[인콜일지 내용]\n" + _docx_text(img.data)})
            except Exception as e:
                print(f"[incall] docx 읽기 실패: {e!r}")
                raise HTTPException(status_code=400, detail="워드 파일을 읽지 못했어요. .docx 파일인지 확인해 주세요. (.doc·.hwp는 PDF로 저장해서 올려 주세요)")
        else:
            parts.append({"inline_data": {"mime_type": img.mime or "image/jpeg", "data": img.data}})
    now = datetime.utcnow() + timedelta(hours=9)
    data, err = await _gemini_json(parts, INCALL_PROMPT + f"\n\n(오늘은 {now.year}년 {now.month}월입니다.)", max_tokens=3000)
    if data is None:
        raise HTTPException(status_code=500, detail="AI 인식 서버가 일시적으로 불안정합니다. 잠시 후 다시 시도해주세요.")
    return {"data": data if isinstance(data, dict) else {}}


DOC_CLASSIFY_PROMPT = """위에 [파일 1], [파일 2] … 순서로 한국 서류(이미지 또는 PDF)가 첨부돼 있습니다. 각 파일이 어떤 서류인지 아래 중 하나로 분류하세요.
- "financial": 재무제표 — 표준재무제표증명, 재무상태표(대차대조표), 손익계산서, 결산서
- "income": 사업자등록증·사업자등록증명, 부가가치세 과세표준증명, 부가가치세 면세사업자 수입금액증명, 소득금액증명
- "incall": 인콜일지·상담일지·상담 기록지 (업체명·매출·신용점수·기대출 등을 손으로/표로 적은 컨설팅 상담 양식)
- "credit": 신용정보조회서(올크레딧·나이스지키미·크레딧포유), 신용점수 화면 캡처, 대출 내역 화면(은행·토스·카카오뱅크 등 앱 캡처 포함)
- "other": 위에 해당하지 않음
한 파일 안에 여러 서류가 있으면 가장 많은 부분을 차지하는 서류로 고르세요. 내용은 옮겨 적지 말고 분류만 하세요.
JSON으로만 답하세요: {"types": ["financial", "income", "incall", ...]}  (파일 순서대로, 파일 수만큼)"""


@app.post("/extract/classify")
async def extract_classify(body: IncomeDocExtractRequest, user: models.User = Depends(get_current_user)):
    """서류 올리기 칸 하나로 — 파일별 서류 종류만 판별 (재무제표 / 사업자·매출 서류 / 신용정보). 저장하지 않는다."""
    if not body.images:
        raise HTTPException(status_code=400, detail="분석할 서류가 없습니다.")
    if len(body.images) > 15:
        raise HTTPException(status_code=400, detail="서류는 한 번에 최대 15개까지 올릴 수 있습니다.")
    parts = []
    for i, img in enumerate(body.images):
        parts.append({"text": f"[파일 {i + 1}]"})
        parts.append({"inline_data": {"mime_type": img.mime or "image/jpeg", "data": img.data}})
    data, err = await _gemini_json(parts, DOC_CLASSIFY_PROMPT, max_tokens=500)
    if data is None:
        raise HTTPException(status_code=500, detail="AI 인식 서버가 일시적으로 불안정합니다. 잠시 후 다시 시도해주세요.")
    types = data.get("types") if isinstance(data, dict) else None
    types = [t if t in ("financial", "income", "credit", "incall") else "other" for t in (types or [])][:len(body.images)]
    types += ["other"] * (len(body.images) - len(types))
    return {"types": types}


@app.post("/extract/credit-doc")
async def extract_credit_doc(body: IncomeDocExtractRequest, user: models.User = Depends(get_current_user)):
    """신용정보조회서·신용점수 화면 → 신용점수(KCB·NICE)·기대출 목록. 문서·결과는 저장하지 않는다."""
    if not body.images:
        raise HTTPException(status_code=400, detail="분석할 서류 이미지가 없습니다.")
    if len(body.images) > 8:
        raise HTTPException(status_code=400, detail="서류는 최대 8장까지 첨부할 수 있습니다.")
    parts = [{"inline_data": {"mime_type": img.mime or "image/jpeg", "data": img.data}} for img in body.images]
    data, err = await _gemini_json(parts, CREDIT_DOC_PROMPT)
    if data is None:
        raise HTTPException(status_code=500, detail="AI 인식 서버가 일시적으로 불안정합니다. 잠시 후 다시 시도해주세요.")
    return {"data": _credit_clean(data)}


@app.post("/extract/income-doc")
async def extract_income_doc(
    body: IncomeDocExtractRequest,
    user: models.User = Depends(get_current_user),
):
    import json, re

    if not body.images:
        raise HTTPException(status_code=400, detail="분석할 서류 이미지가 없습니다.")
    if len(body.images) > 5:
        raise HTTPException(status_code=400, detail="서류는 최대 5장까지 첨부할 수 있습니다.")

    image_parts = [
        {"inline_data": {"mime_type": img.mime or "image/jpeg", "data": img.data}}
        for img in body.images
    ]

    prompt = """이 문서는 한국 국세청에서 발급한 "사업자등록증", "부가가치세과세표준증명원", "부가가치세 면세사업자 수입금액증명", "소득금액증명원" 중 하나입니다 (여러 장이 첨부된 경우 같은 서류의 여러 페이지이거나, 여러 서류가 함께 첨부되었을 수 있습니다). 첨부된 모든 이미지를 함께 확인해 아래 항목을 추출하세요.

━━━ 1단계: 상호 / 대표자명 / 사업자등록번호 (반드시 먼저, 빠뜨리지 말 것) ━━━
두 서류 모두 문서 상단(제목 바로 아래)에 납세자·사업자 정보가 표 또는 항목 형태로 반드시 인쇄되어 있습니다. 금액을 읽기 전에 이 부분을 먼저 정확히 읽으세요.
- 라벨 표기는 서류 종류·발급 연도에 따라 다양합니다. 아래 중 어떤 표기든 같은 항목으로 취급하세요.
  · 상호(company_name): "상호", "상 호", "상호(법인명)", "법인명", "법인명(단체명)", "사업장명", "업체명", "상호명"
  · 대표자명(rep_name): "성명", "성 명", "대표자", "대표자명", "성명(대표자명)", "납세자명", "성명(법인명)"
  · 사업자등록번호(business_number): "사업자등록번호", "사업자 등록번호", "등록번호", "사업자번호"
- 라벨의 오른쪽 칸 또는 바로 아래 칸에 있는 값을 읽으세요. 라벨과 값이 ":"으로 구분되거나, 표의 머리행/데이터행으로 나뉘어 있을 수 있습니다.
- 개인사업자는 상호 칸이 비어 있거나 없을 수 있습니다. 그때만 company_name을 null로 두고 rep_name은 반드시 채우세요. 법인은 상호와 대표자 성명이 모두 있습니다.
- 사업자등록번호는 숫자 10자리입니다. 하이픈이 없거나 공백으로 띄어져 있어도 "000-00-00000" 형식으로 정리해서 반환하세요.
- 상호는 "주식회사", "(주)", "농업회사법인" 등 문서에 적힌 표기를 그대로 유지하세요.
- 글자가 흐리거나 일부 잘려 있어도 최대한 판독해서 채우고, 정말 알아볼 수 없을 때에만 null을 반환하세요. 이 세 항목을 이유 없이 null로 두지 마세요.

━━━ 2단계: 서류별 금액 추출 방법 ━━━
[부가가치세과세표준증명원]
- 상단에 상호(회사/사업체명), 성명(대표자명), 사업자등록번호가 표기됩니다.
- 표에는 보통 과세기간(예: 2025년 제1기, 2025년 제2기 등)별로 "과세표준액"(공급가액) 금액이 나열됩니다.
- 표에 나온 가장 최근 4개 과세기간(1년치, 분기 신고면 4개/반기 신고면 2개)의 과세표준액을 모두 더한 값을 revenue(연 매출액 추정치)로 반환하세요. 과세기간이 1년치가 안 되면 있는 만큼만 더하세요.
- 그리고 표의 모든 과세기간을 빠짐없이 periods 배열로 반환하세요 (가장 최근 기간, 올해 기간 포함). 각 항목: {"start":"YYYY-MM","end":"YYYY-MM","amount":과세표준액 숫자}. 과세기간 표기가 "2025년 1기"면 start "2025-01", end "2025-06", "2기"면 "2025-07"~"2025-12", 예정·확정이 나뉘어 있으면 각각의 실제 기간(예: 2025-01~2025-03)으로 적으세요.

[부가가치세 면세사업자 수입금액증명] (면세사업자는 부가세 과세표준 대신 이 서류로 매출을 확인)
- 상단에 상호, 성명(대표자명), 사업자등록번호가 표기됩니다.
- 표에는 귀속연도(또는 과세기간)별 "수입금액"이 나열됩니다. 업종별로 여러 줄이면 같은 연도끼리 더하세요.
- 표의 모든 연도를 periods 배열로 반환하세요. 연 단위면 {"start":"YYYY-01","end":"YYYY-12","amount":그 해 수입금액 합계}, 기간이 따로 적혀 있으면 실제 기간으로.
- revenue에는 가장 최근 연도의 수입금액 합계를 넣으세요.

[소득금액증명원]
- 상단에 상호 또는 성명(대표자명), 사업자등록번호가 표기됩니다.
- 표에는 귀속연도별로 "소득금액"이 나열됩니다. 가장 최근 귀속연도의 소득금액을 income_amount로 반환하세요.

[사업자등록증] (금액 정보는 없음 — revenue·income_amount는 null)
- 제목 옆 괄호에 "(법인사업자)", "(일반과세자)", "(간이과세자)", "(면세사업자)" 등이 있습니다. 법인사업자면 biz_kind="법인", 그 외 개인이면 "개인".
- "개업연월일"(예: 2019 년 05 월 10 일) → open_date를 "YYYY-MM-DD"로.
- "사업장 소재지" 주소 전체 → address (본점 소재지가 따로 있어도 사업장 소재지 우선).
- "사업의 종류" 아래 "업태"와 "종목" → business_type, business_item. 여러 개면 쉼표(,)로 구분해 문서 순서대로 모두 적으세요 (업태와 종목은 같은 순서로 짝을 맞춤).
- 법인은 "법인명(단체명)"이 상호, "대표자"가 대표자명입니다. 개인은 "상호"와 "성명"입니다.

부가세·소득금액 서류에는 개업일·주소·업태가 없으면 null로 두세요. 대출·부채·자산 정보는 추출 대상이 아닙니다.

아래 JSON 형식으로만 응답하세요. 문서에서 찾을 수 없는 항목은 null:
{"doc_type":"사업자등록증" 또는 "부가세과세표준증명원" 또는 "면세사업자수입금액증명" 또는 "소득금액증명원" 또는 "불명"(여러 서류면 쉼표로),"company_name":문자열또는null,"rep_name":문자열또는null,"business_number":문자열또는null,"revenue":숫자또는null,"periods":[{"start":"YYYY-MM","end":"YYYY-MM","amount":숫자}] 또는 null,"income_amount":숫자또는null,"open_date":문자열또는null,"address":문자열또는null,"business_type":문자열또는null,"business_item":문자열또는null,"biz_kind":"법인"또는"개인"또는null,"comment":"인식 관련 메모 1문장"}"""

    GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.7-flash"]

    async def call_gemini(client: httpx.AsyncClient):
        import asyncio
        last_err = None
        for model in GEMINI_MODELS:
            for attempt in range(2):
                try:
                    resp = await client.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}",
                        headers={"Content-Type": "application/json"},
                        json={
                            "contents": [{"parts": [*image_parts, {"text": prompt}]}],
                            "generationConfig": {"temperature": 0, "maxOutputTokens": 2000}
                        }
                    )
                    if resp.status_code in (503, 429):
                        last_err = resp.text
                        if attempt == 0:
                            await asyncio.sleep(0.8)
                            continue
                        break
                    if resp.status_code != 200:
                        last_err = resp.text
                        break
                    try:
                        candidate_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                        candidate_clean = re.sub(r"```json|```", "", candidate_text).strip()
                        start = candidate_clean.find("{")
                        end = candidate_clean.rfind("}")
                        if start == -1 or end == -1:
                            raise ValueError("JSON 객체를 찾을 수 없음")
                        return json.loads(candidate_clean[start:end + 1]), None
                    except Exception as parse_err:
                        last_err = f"JSON 파싱 실패: {parse_err}"
                        break
                except Exception as e:
                    last_err = str(e)
                    break
        return None, last_err

    async with httpx.AsyncClient(timeout=60) as client:
        data, last_error = await call_gemini(client)

    if data is None:
        raise HTTPException(status_code=500, detail=f"AI 인식 서버가 일시적으로 불안정합니다. 잠시 후 다시 시도해주세요. ({last_error[:150] if last_error else ''})")

    # 상호/대표자명/사업자등록번호 후처리 — 빈 값 정리 및 사업자번호 형식 통일
    if isinstance(data, dict):
        def _clean_text(v):
            if v is None:
                return None
            v = str(v).strip().strip('"').strip()
            if not v or v.lower() in ("null", "none", "n/a", "-", "미상", "불명", "없음"):
                return None
            return v
        for key in ("doc_type", "company_name", "rep_name", "business_number", "comment", "open_date", "address", "business_type", "business_item", "biz_kind"):
            if key in data:
                data[key] = _clean_text(data[key])
        # 사업자등록증 → 원클릭 입력값 (창업년월·소재지·업태/종목 2개까지·개인/법인)
        od = re.sub(r"\D", "", data.get("open_date") or "")
        if len(od) >= 6 and od[:2] in ("19", "20"):
            data["founded"] = f"{od[:4]}-{od[4:6]}"
        if data.get("address"):
            data["location"] = _sido(data["address"]) or None
        if data.get("business_type") or data.get("business_item"):
            i1, t1, i2, t2 = _pair_biz(data.get("business_type"), data.get("business_item"))
            data["industry"], data["item"], data["industry2"], data["item2"] = i1 or None, t1 or None, i2 or None, t2 or None
        biz = data.get("business_number")
        if biz:
            digits = re.sub(r"\D", "", biz)
            if len(digits) == 10:
                data["business_number"] = f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
            elif not digits:
                data["business_number"] = None
        # 과세기간별 금액 → 지난 연도별 매출 + 올해 누적 (CODEF와 같은 방식)
        pr = data.get("periods")
        if isinstance(pr, list) and pr:
            rows = []
            for it in pr:
                if not isinstance(it, dict):
                    continue
                st_, en_ = re.sub(r"\D", "", str(it.get("start") or "")), re.sub(r"\D", "", str(it.get("end") or ""))
                if len(st_) >= 6:
                    rows.append({"commStartDate": st_[:6] + "01", "commEndDate": (en_[:6] if len(en_) >= 6 else st_[:6]) + "28", "resIncomeTotalAmt": it.get("amount")})
            now_kst = datetime.utcnow() + timedelta(hours=9)
            yrs, ytd = _safe(_parse_vat_split, rows, now_kst.year, default=([], None))
            if yrs:
                data["years"] = yrs
            if ytd:
                data["ytd"] = ytd
        data.pop("periods", None)
        kind = data.get("biz_kind") or ""
        bd = re.sub(r"\D", "", data.get("business_number") or "")
        if "법인" in kind or (len(bd) == 10 and bd[3:5] in ("81", "82", "83", "84", "85", "86", "87", "88")):   # 사업자번호 가운데 81~88 = 법인
            data["biz_type"] = "corp"
        elif "개인" in kind:
            data["biz_type"] = "personal"

    return {"data": data}


# ══════════════════════════════════════════════════════════════
# 분석 이력 — 지난 분석 결과 조회 및 리포트 재출력
# (이용약관상 분석이력 보관기간은 30일이므로 그 기간 내 이력만 조회됨)
# ══════════════════════════════════════════════════════════════
HISTORY_RETENTION_DAYS = 30

class AnalysisHistoryCreate(BaseModel):
    company_name: Optional[str] = ""
    rep_name: Optional[str] = ""
    business_number: Optional[str] = ""
    source_type: Optional[str] = "image"   # image(재무제표 분석) / manual(직접 입력)
    revenue: Optional[float] = None
    stability_grade: Optional[str] = None
    stability_score: Optional[int] = None
    stability_max: Optional[int] = None
    data: dict = {}

def purge_expired_histories(db: Session) -> int:
    """보관기간(30일)이 지난 분석이력을 실제로 삭제.
    Render에는 별도 스케줄러가 없으므로 이력을 조회할 때마다 정리한다."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=HISTORY_RETENTION_DAYS)
    try:
        deleted = (db.query(models.AnalysisHistory)
                     .filter(models.AnalysisHistory.created_at < cutoff)
                     .delete(synchronize_session=False))
        if deleted:
            db.commit()
            print(f"[history] 보관기간이 지난 분석이력 {deleted}건 삭제")
        return deleted
    except Exception as e:
        db.rollback()
        print(f"[history] 만료 이력 삭제 실패: {e}")
        return 0


def _history_summary(h: "models.AnalysisHistory") -> dict:
    from datetime import timedelta
    expires_at = (h.created_at + timedelta(days=HISTORY_RETENTION_DAYS)) if h.created_at else None
    return {
        "id": h.id,
        "company_name": h.company_name or "",
        "rep_name": h.rep_name or "",
        "business_number": h.business_number or "",
        "source_type": h.source_type or "image",
        "revenue": h.revenue,
        "stability_grade": h.stability_grade or "",
        "stability_score": h.stability_score,
        "stability_max": h.stability_max,
        "created_at": h.created_at.isoformat() if h.created_at else None,
        "printed_at": h.printed_at.isoformat() if h.printed_at else None,
        "printed": h.printed_at is not None,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }

@app.post("/history")
def create_analysis_history(
    body: AnalysisHistoryCreate,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    import json as _json
    revenue = None
    if body.revenue is not None:
        try:
            revenue = int(round(float(body.revenue)))
        except Exception:
            revenue = None

    item = models.AnalysisHistory(
        user_id=user.id,
        company_name=(body.company_name or "").strip()[:200],
        rep_name=(body.rep_name or "").strip()[:100],
        business_number=(body.business_number or "").strip()[:50],
        source_type=(body.source_type or "image"),
        revenue=revenue,
        stability_grade=(body.stability_grade or "")[:30],
        stability_score=body.stability_score,
        stability_max=body.stability_max,
        data_json=_json.dumps(body.data or {}, ensure_ascii=False),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"history": _history_summary(item)}

@app.get("/history")
def list_analysis_history(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from datetime import timedelta
    purge_expired_histories(db)   # 보관기간이 지난 이력은 이 시점에 실제로 삭제
    cutoff = datetime.utcnow() - timedelta(days=HISTORY_RETENTION_DAYS)
    rows = (
        db.query(models.AnalysisHistory)
        .filter(models.AnalysisHistory.user_id == user.id)
        .filter(models.AnalysisHistory.created_at >= cutoff)
        .order_by(models.AnalysisHistory.created_at.desc())
        .limit(200)
        .all()
    )
    return {"histories": [_history_summary(h) for h in rows], "retention_days": HISTORY_RETENTION_DAYS}

@app.get("/history/{history_id}")
def get_analysis_history(
    history_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    import json as _json
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=HISTORY_RETENTION_DAYS)
    h = db.query(models.AnalysisHistory).filter(
        models.AnalysisHistory.id == history_id,
        models.AnalysisHistory.user_id == user.id,
        models.AnalysisHistory.created_at >= cutoff,
    ).first()
    if not h:
        raise HTTPException(status_code=404, detail="분석 이력을 찾을 수 없습니다. (보관기간 30일이 지난 이력은 삭제됩니다)")
    try:
        data = _json.loads(h.data_json or "{}")
    except Exception:
        data = {}
    return {"history": _history_summary(h), "data": data}

@app.put("/history/{history_id}")
def update_analysis_history(
    history_id: int,
    body: AnalysisHistoryCreate,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """값 수정 후 재계산한 경우 기존 이력을 갱신 (새 이력을 만들지 않음)"""
    import json as _json
    h = db.query(models.AnalysisHistory).filter(
        models.AnalysisHistory.id == history_id,
        models.AnalysisHistory.user_id == user.id,
    ).first()
    if not h:
        raise HTTPException(status_code=404, detail="분석 이력을 찾을 수 없습니다.")

    if body.company_name is not None:
        h.company_name = (body.company_name or "").strip()[:200]
    if body.rep_name is not None:
        h.rep_name = (body.rep_name or "").strip()[:100]
    if body.business_number is not None:
        h.business_number = (body.business_number or "").strip()[:50]
    if body.revenue is not None:
        try:
            h.revenue = int(round(float(body.revenue)))
        except Exception:
            pass
    if body.stability_grade is not None:
        h.stability_grade = (body.stability_grade or "")[:30]
    if body.stability_score is not None:
        h.stability_score = body.stability_score
    if body.stability_max is not None:
        h.stability_max = body.stability_max
    if body.data:
        h.data_json = _json.dumps(body.data, ensure_ascii=False)
    db.commit()
    db.refresh(h)
    return {"history": _history_summary(h)}

@app.post("/history/{history_id}/printed")
def mark_analysis_history_printed(
    history_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    h = db.query(models.AnalysisHistory).filter(
        models.AnalysisHistory.id == history_id,
        models.AnalysisHistory.user_id == user.id,
    ).first()
    if not h:
        raise HTTPException(status_code=404, detail="분석 이력을 찾을 수 없습니다.")
    if h.printed_at is None:
        h.printed_at = datetime.utcnow()
        db.commit()
        db.refresh(h)
    return {"history": _history_summary(h)}

@app.delete("/history/{history_id}")
def delete_analysis_history(
    history_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    h = db.query(models.AnalysisHistory).filter(
        models.AnalysisHistory.id == history_id,
        models.AnalysisHistory.user_id == user.id,
    ).first()
    if not h:
        raise HTTPException(status_code=404, detail="분석 이력을 찾을 수 없습니다.")
    db.delete(h)
    db.commit()
    return {"message": "삭제되었습니다."}


# ══════════════════════════════════════════════════════════════
# 관리자 API — 회원 검색 / 잔여횟수(크레딧) 조정 / 결제내역 조회
# ══════════════════════════════════════════════════════════════
def check_admin(x_admin_password: str = Header(None)):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=500, detail="ADMIN_PASSWORD 환경변수가 설정되어 있지 않습니다.")
    if not x_admin_password or x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="관리자 비밀번호가 올바르지 않습니다.")
    return True

class AdminLoginBody(BaseModel):
    password: str

@app.post("/admin/login")
def admin_login(body: AdminLoginBody):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=500, detail="ADMIN_PASSWORD 환경변수가 설정되어 있지 않습니다.")
    if body.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="비밀번호가 올바르지 않습니다.")
    return {"ok": True}

@app.get("/admin/users")
def admin_list_users(_: bool = Depends(check_admin), db: Session = Depends(get_db)):
    users = db.query(models.User).order_by(models.User.created_at.desc()).all()
    return {
        "count": len(users),
        "users": [
            {
                "email": u.email,
                "company_name": u.company_name,
                "rep_name": u.rep_name,
                "phone": u.phone,
                "credits": u.credits,
                "remaining_count": u.credits // COST_PER_ANALYSIS,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ],
    }

@app.get("/admin/user")
def admin_get_user(email: str, _: bool = Depends(check_admin), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(_email_eq(email)).first()
    if not user:
        raise HTTPException(status_code=404, detail="해당 이메일의 회원을 찾을 수 없습니다.")
    payments = db.query(models.Payment).filter(models.Payment.user_id == user.id).order_by(models.Payment.created_at.desc()).limit(20).all()
    logs = db.query(models.AnalysisLog).filter(models.AnalysisLog.user_id == user.id).order_by(models.AnalysisLog.created_at.desc()).limit(20).all()
    return {
        "id": user.id,
        "email": user.email,
        "company_name": user.company_name,
        "rep_name": user.rep_name,
        "phone": user.phone,
        "business_number": user.business_number,
        "credits": user.credits,
        "remaining_count": user.credits // COST_PER_ANALYSIS,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "team": admin_team_info(db, user),
        "subscription": admin_subscription_info(db, user),
        "payments": [
            {"order_id": p.order_id, "amount": p.amount, "credits": p.credits, "package_id": p.package_id,
             "created_at": p.created_at.isoformat() if p.created_at else None}
            for p in payments
        ],
        "recent_analyses": [
            {"credits_used": l.credits_used, "created_at": l.created_at.isoformat() if l.created_at else None}
            for l in logs
        ],
    }

class AdminAdjustCreditsBody(BaseModel):
    email: str
    delta_count: int  # 건수 단위 (+/-). 내부적으로 10을 곱해 크레딧에 반영

@app.post("/admin/adjust-credits")
def admin_adjust_credits(body: AdminAdjustCreditsBody, _: bool = Depends(check_admin), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(_email_eq(body.email)).first()
    if not user:
        raise HTTPException(status_code=404, detail="해당 이메일의 회원을 찾을 수 없습니다.")
    user.credits = max(0, user.credits + body.delta_count * COST_PER_ANALYSIS)
    db.commit()
    return {"email": user.email, "credits": user.credits, "remaining_count": user.credits // COST_PER_ANALYSIS}

class AdminSetCreditsBody(BaseModel):
    email: str
    remaining_count: int  # 건수 단위 절대값으로 설정

@app.post("/admin/set-credits")
def admin_set_credits(body: AdminSetCreditsBody, _: bool = Depends(check_admin), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(_email_eq(body.email)).first()
    if not user:
        raise HTTPException(status_code=404, detail="해당 이메일의 회원을 찾을 수 없습니다.")
    user.credits = max(0, body.remaining_count) * COST_PER_ANALYSIS
    db.commit()
    return {"email": user.email, "credits": user.credits, "remaining_count": user.credits // COST_PER_ANALYSIS}


class AdminUpdateUserBody(BaseModel):
    email: str                       # 현재 이메일 (대상 식별자)
    new_email: Optional[str] = None
    new_phone: Optional[str] = None
    new_password: Optional[str] = None

@app.post("/admin/update-user")
def admin_update_user(body: AdminUpdateUserBody, _: bool = Depends(check_admin), db: Session = Depends(get_db)):
    import re, hashlib
    user = db.query(models.User).filter(_email_eq(body.email)).first()
    if not user:
        raise HTTPException(status_code=404, detail="해당 이메일의 회원을 찾을 수 없습니다.")

    if body.new_email:
        new_email = body.new_email.strip().lower()
        if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", new_email):
            raise HTTPException(status_code=400, detail="올바른 이메일 형식이 아닙니다.")
        if new_email != user.email:
            existing = db.query(models.User).filter(_email_eq(new_email)).first()
            if existing:
                raise HTTPException(status_code=400, detail="이미 사용 중인 이메일입니다.")
            user.email = new_email

    if body.new_phone:
        user.phone = body.new_phone.strip()

    if body.new_password:
        if len(body.new_password) < 4:
            raise HTTPException(status_code=400, detail="비밀번호는 4자 이상이어야 합니다.")
        user.password_hash = hashlib.sha256(body.new_password.encode()).hexdigest()

    db.commit()
    return {
        "email": user.email,
        "phone": user.phone,
        "message": "회원 정보가 수정되었습니다.",
    }


class AdminDeleteUserBody(BaseModel):
    email: str

@app.post("/admin/delete-user")
def admin_delete_user(body: AdminDeleteUserBody, _: bool = Depends(check_admin), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(_email_eq(body.email)).first()
    if not user:
        raise HTTPException(status_code=404, detail="해당 이메일의 회원을 찾을 수 없습니다.")
    db.query(models.Payment).filter(models.Payment.user_id == user.id).delete()
    db.query(models.AnalysisLog).filter(models.AnalysisLog.user_id == user.id).delete()
    db.query(models.RefundRequest).filter(models.RefundRequest.user_id == user.id).delete()
    cleanup_user_subscription(db, user)   # 예약 결제 취소 + 빌링키 삭제
    cleanup_user_team(db, user)   # 팀 소속/초대 정리 (대표면 팀 해산)
    db.delete(user)
    db.commit()
    return {"message": f"{body.email} 회원이 탈퇴 처리되었습니다."}


# ══════════════════════════════════════════════════════════════
# 팀(구독 요금제별 사용자 수 제한)
#   - 대표(owner) 1명이 요금제를 갖고, 요금제의 사용자 수(seats)까지 팀원을 둘 수 있다.
#   - 대표 본인도 1석. 사용자 수는 '수락해서 합류한 사람'만 센다. 초대를 보내는 것만으로는 자리를 차지하지 않는다.
#   - 한도 검사는 반드시 서버에서 한다 (화면의 버튼 숨김은 편의일 뿐).
#       · 초대 시: 이미 한도를 다 채운 팀이면 초대 불가
#       · 수락 시: 그 순간의 팀원 수로 다시 검사 (대기 초대가 한도보다 많아도 먼저 수락한 순서대로 합류)
#   - 요금제 지정은 지금은 관리자(/admin/team/set-plan)만 가능. 구독 결제가 붙으면 결제 성공 시
#     set_team_plan()을 호출하도록 연결하면 된다.
# ══════════════════════════════════════════════════════════════
def _norm_email(s: str) -> str:
    return str(s or "").strip().lower()

def _invite_cutoff():
    return datetime.utcnow() - timedelta(days=TEAM_INVITE_VALID_DAYS)

def team_seat_limit(team) -> int:
    plan = SUBSCRIPTION_PLANS.get((team.plan or "") if team else "")
    return plan["seats"] if plan else 0

def team_counts(db: Session, team):
    """(팀원 수, 유효한 대기 초대 수)"""
    members = db.query(models.TeamMember).filter(models.TeamMember.team_id == team.id).count()
    pending = db.query(models.TeamInvite).filter(
        models.TeamInvite.team_id == team.id,
        models.TeamInvite.created_at >= _invite_cutoff(),
    ).count()
    return members, pending

def get_membership(db: Session, user):
    """(내 팀, 내 멤버십) — 소속이 없으면 (None, None)"""
    m = db.query(models.TeamMember).filter(models.TeamMember.user_id == user.id).first()
    if not m:
        return None, None
    team = db.query(models.Team).filter(models.Team.id == m.team_id).first()
    return team, m

def cleanup_user_team(db: Session, user):
    """회원 탈퇴/삭제 시 팀 관련 데이터 정리. 대표가 탈퇴하면 팀 전체가 해산된다. (commit은 호출한 쪽에서)"""
    from sqlalchemy import func, or_
    team = db.query(models.Team).filter(models.Team.owner_id == user.id).first()
    if team:
        db.query(models.TeamInvite).filter(models.TeamInvite.team_id == team.id).delete(synchronize_session=False)
        db.query(models.TeamMember).filter(models.TeamMember.team_id == team.id).delete(synchronize_session=False)
        db.delete(team)
    db.query(models.TeamMember).filter(models.TeamMember.user_id == user.id).delete(synchronize_session=False)
    db.query(models.TeamInvite).filter(or_(
        models.TeamInvite.invited_by == user.id,
        func.lower(models.TeamInvite.email) == _norm_email(user.email),
    )).delete(synchronize_session=False)
    db.flush()

def set_team_plan(db: Session, owner, plan: str, expires_at=None):
    """대표에게 요금제를 지정(없으면 팀 생성). plan이 ""이면 요금제 해지. 구독 결제 연동 시에도 이 함수를 사용."""
    if plan and plan not in SUBSCRIPTION_PLANS:
        raise HTTPException(status_code=400, detail="알 수 없는 요금제입니다.")
    team, membership = get_membership(db, owner)
    if team and team.owner_id != owner.id:
        raise HTTPException(status_code=400, detail="이미 다른 팀의 팀원으로 속해 있는 회원입니다. 팀에서 먼저 탈퇴해야 대표가 될 수 있습니다.")
    if not team:
        team = models.Team(owner_id=owner.id, plan="")
        db.add(team)
        db.flush()
        db.add(models.TeamMember(team_id=team.id, user_id=owner.id, role="owner"))
    team.plan = plan or ""
    team.plan_updated_at = datetime.utcnow()
    team.plan_expires_at = expires_at if plan else None
    db.commit()
    return team

def _kst_date(dt):
    """만료 시각(UTC) → 이용 가능한 마지막 날짜(한국) 'YYYY-MM-DD'"""
    if not dt:
        return None
    return (dt + timedelta(hours=9) - timedelta(seconds=1)).strftime("%Y-%m-%d")

def _plan_label(plan: str) -> str:
    return SUBSCRIPTION_PLANS.get(plan or "", {}).get("label", "요금제 없음")

def admin_team_info(db: Session, user):
    """관리자 회원 상세에 보여줄 팀/요금제 요약 (팀이 없으면 None)"""
    team, m = get_membership(db, user)
    if not team:
        return None
    _expire_admin_plan(db, team)
    owner = db.query(models.User).filter(models.User.id == team.owner_id).first()
    members_n, pending_n = team_counts(db, team)
    return {
        "role": m.role,
        "owner_email": owner.email if owner else "",
        "plan": team.plan or "",
        "plan_label": _plan_label(team.plan),
        "plan_expires_on": _kst_date(team.plan_expires_at),
        "seat_limit": team_seat_limit(team),
        "members_count": members_n,
        "pending_count": pending_n,
    }

def admin_subscription_info(db: Session, user):
    sub = db.query(models.Subscription).filter(models.Subscription.user_id == user.id).first()
    return _sub_view(sub) if sub else None

@app.get("/plans")
def list_plans():
    return _public_plans()

@app.get("/team/me")
def team_me(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    from sqlalchemy import func
    team, membership = get_membership(db, user)

    # 내게 온 초대 (아직 팀이 없는 경우 수락할 수 있음)
    incoming = []
    inv_rows = db.query(models.TeamInvite).filter(
        func.lower(models.TeamInvite.email) == _norm_email(user.email),
        models.TeamInvite.created_at >= _invite_cutoff(),
    ).order_by(models.TeamInvite.created_at.desc()).all()
    for inv in inv_rows:
        t = db.query(models.Team).filter(models.Team.id == inv.team_id).first()
        owner = db.query(models.User).filter(models.User.id == t.owner_id).first() if t else None
        if not t or not owner:
            continue
        incoming.append({
            "id": inv.id,
            "team_name": owner.company_name or owner.nickname or owner.email,
            "owner_email": owner.email,
            "plan_label": _plan_label(t.plan),
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
        })

    if not team:
        return {"has_team": False, "incoming_invites": incoming}

    # 만료된 초대는 이 팀에 한해 정리
    db.query(models.TeamInvite).filter(
        models.TeamInvite.team_id == team.id,
        models.TeamInvite.created_at < _invite_cutoff(),
    ).delete(synchronize_session=False)
    db.commit()

    is_owner = (membership.role == "owner")
    limit = team_seat_limit(team)
    members_n, pending_n = team_counts(db, team)
    member_rows = db.query(models.TeamMember).filter(models.TeamMember.team_id == team.id).order_by(models.TeamMember.joined_at).all()
    users_by_id = {u.id: u for u in db.query(models.User).filter(models.User.id.in_([m.user_id for m in member_rows])).all()}
    members = []
    for m in member_rows:
        u = users_by_id.get(m.user_id)
        if not u:
            continue
        members.append({
            "user_id": u.id,
            "email": u.email,
            "nickname": u.nickname or "",
            "role": m.role,
            "is_me": (u.id == user.id),
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
        })
    invites = []
    if is_owner:
        for inv in db.query(models.TeamInvite).filter(models.TeamInvite.team_id == team.id).order_by(models.TeamInvite.created_at.desc()).all():
            invites.append({
                "id": inv.id,
                "email": inv.email,
                "created_at": inv.created_at.isoformat() if inv.created_at else None,
                "expires_at": (inv.created_at + timedelta(days=TEAM_INVITE_VALID_DAYS)).isoformat() if inv.created_at else None,
            })
    return {
        "has_team": True,
        "role": membership.role,
        "plan": team.plan or "",
        "plan_label": _plan_label(team.plan),
        "seat_limit": limit,
        "seats_used": members_n,                       # 수락해서 합류한 사람만 센다 (대표 포함)
        "seats_remaining": max(0, limit - members_n),
        "members_count": members_n,
        "pending_count": pending_n,                    # 수락 대기 중인 초대 (자리를 차지하지 않음)
        "can_invite": bool(is_owner and limit > 0 and members_n < limit and pending_n < TEAM_MAX_PENDING_INVITES),
        "over_limit": members_n > limit,
        "members": members,
        "invites": invites,
        "incoming_invites": incoming,
    }

class TeamInviteBody(BaseModel):
    email: str

@app.post("/team/invite")
def team_invite(body: TeamInviteBody, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    import re
    from sqlalchemy import func
    team, membership = get_membership(db, user)
    if not team or membership.role != "owner":
        raise HTTPException(status_code=403, detail="팀 대표만 팀원을 초대할 수 있습니다.")
    limit = team_seat_limit(team)
    if limit <= 0:
        raise HTTPException(status_code=400, detail="적용된 요금제가 없어 팀원을 초대할 수 없습니다.")
    email = _norm_email(body.email)
    if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
        raise HTTPException(status_code=400, detail="올바른 이메일 형식이 아닙니다.")
    if email == _norm_email(user.email):
        raise HTTPException(status_code=400, detail="본인은 이미 팀 대표입니다.")
    dup = db.query(models.TeamInvite).filter(
        models.TeamInvite.team_id == team.id,
        func.lower(models.TeamInvite.email) == email,
        models.TeamInvite.created_at >= _invite_cutoff(),
    ).first()
    if dup:
        raise HTTPException(status_code=400, detail="이미 초대를 보낸 이메일입니다.")
    target = db.query(models.User).filter(func.lower(models.User.email) == email).first()
    if target:
        t_team, _m = get_membership(db, target)
        if t_team:
            if t_team.id == team.id:
                raise HTTPException(status_code=400, detail="이미 우리 팀의 팀원입니다.")
            raise HTTPException(status_code=400, detail="이미 다른 팀에 속해 있어 초대할 수 없습니다.")
    members_n, pending_n = team_counts(db, team)
    # 초대는 자리를 차지하지 않는다. 이미 한도를 다 채운 팀만 막는다. (실제 한도 검사는 수락 시점에도 다시 한다)
    if members_n >= limit:
        raise HTTPException(
            status_code=400,
            detail=f"현재 요금제의 사용자 수 한도({limit}명)를 모두 사용 중입니다. 상위 요금제로 변경하거나 기존 팀원을 정리해주세요.",
        )
    if pending_n >= TEAM_MAX_PENDING_INVITES:
        raise HTTPException(status_code=400, detail=f"수락 대기 중인 초대가 너무 많습니다(최대 {TEAM_MAX_PENDING_INVITES}건). 기존 초대를 취소한 뒤 다시 시도해주세요.")
    db.add(models.TeamInvite(team_id=team.id, email=email, invited_by=user.id))
    db.commit()
    return {"message": f"{email} 님에게 초대를 보냈습니다. 상대방이 피나에 로그인해 초대를 수락하면 팀에 합류합니다."}

@app.delete("/team/invites/{invite_id}")
def team_cancel_invite(invite_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    team, membership = get_membership(db, user)
    if not team or membership.role != "owner":
        raise HTTPException(status_code=403, detail="팀 대표만 초대를 취소할 수 있습니다.")
    inv = db.query(models.TeamInvite).filter(models.TeamInvite.id == invite_id, models.TeamInvite.team_id == team.id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="초대를 찾을 수 없습니다.")
    db.delete(inv)
    db.commit()
    return {"message": "초대를 취소했습니다."}

def _find_my_invite(db: Session, invite_id: int, user):
    from sqlalchemy import func
    inv = db.query(models.TeamInvite).filter(
        models.TeamInvite.id == invite_id,
        func.lower(models.TeamInvite.email) == _norm_email(user.email),
    ).first()
    if not inv:
        raise HTTPException(status_code=404, detail="초대를 찾을 수 없습니다.")
    if inv.created_at and inv.created_at < _invite_cutoff():
        db.delete(inv)
        db.commit()
        raise HTTPException(status_code=400, detail="초대 유효기간이 지났습니다. 대표에게 다시 초대를 요청해주세요.")
    return inv

@app.post("/team/invites/{invite_id}/accept")
def team_accept_invite(invite_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    from sqlalchemy import func
    inv = _find_my_invite(db, invite_id, user)
    my_team, _m = get_membership(db, user)
    if my_team:
        raise HTTPException(status_code=400, detail="이미 팀에 속해 있습니다. 기존 팀에서 먼저 탈퇴해주세요.")
    team = db.query(models.Team).filter(models.Team.id == inv.team_id).first()
    if not team:
        db.delete(inv)
        db.commit()
        raise HTTPException(status_code=404, detail="팀이 더 이상 존재하지 않습니다.")
    limit = team_seat_limit(team)
    members_n, _pending = team_counts(db, team)
    # 사용자 수는 수락하는 이 순간에 차감된다. 그 사이 다른 팀원이 먼저 합류해 한도가 찼으면 합류할 수 없다.
    if members_n + 1 > limit:
        raise HTTPException(status_code=400, detail="팀의 사용자 수 한도가 모두 찼거나 요금제가 변경되어 합류할 수 없습니다. 팀 대표에게 문의해주세요.")
    db.add(models.TeamMember(team_id=team.id, user_id=user.id, role="member"))
    # 다른 팀에서 온 대기 초대는 정리해서 그쪽 자리를 풀어준다
    db.query(models.TeamInvite).filter(func.lower(models.TeamInvite.email) == _norm_email(user.email)).delete(synchronize_session=False)
    db.commit()
    return {"message": "팀에 합류했습니다."}

@app.post("/team/invites/{invite_id}/decline")
def team_decline_invite(invite_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    inv = _find_my_invite(db, invite_id, user)
    db.delete(inv)
    db.commit()
    return {"message": "초대를 거절했습니다."}

@app.delete("/team/members/{member_user_id}")
def team_remove_member(member_user_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    team, membership = get_membership(db, user)
    if not team or membership.role != "owner":
        raise HTTPException(status_code=403, detail="팀 대표만 팀원을 내보낼 수 있습니다.")
    if member_user_id == user.id:
        raise HTTPException(status_code=400, detail="대표 본인은 내보낼 수 없습니다.")
    m = db.query(models.TeamMember).filter(models.TeamMember.team_id == team.id, models.TeamMember.user_id == member_user_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="해당 팀원을 찾을 수 없습니다.")
    db.delete(m)
    db.commit()
    return {"message": "팀원을 내보냈습니다."}

@app.post("/team/leave")
def team_leave(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    team, membership = get_membership(db, user)
    if not team:
        raise HTTPException(status_code=400, detail="소속된 팀이 없습니다.")
    if membership.role == "owner":
        raise HTTPException(status_code=400, detail="대표는 팀을 탈퇴할 수 없습니다. 요금제 해지는 관리자에게 문의해주세요.")
    db.delete(membership)
    db.commit()
    return {"message": "팀에서 탈퇴했습니다."}

class AdminSetPlanBody(BaseModel):
    email: str
    plan: str = ""   # starter / standard / pro / "" (요금제 해지)
    expires_on: str | None = None   # 만료일 "YYYY-MM-DD" (한국 날짜, 그날 23:59:59까지 이용). 없으면 만료 없음

@app.post("/admin/team/set-plan")
def admin_set_team_plan(body: AdminSetPlanBody, _: bool = Depends(check_admin), db: Session = Depends(get_db)):
    from sqlalchemy import func
    owner = db.query(models.User).filter(func.lower(models.User.email) == _norm_email(body.email)).first()
    if not owner:
        raise HTTPException(status_code=404, detail="해당 이메일의 회원을 찾을 수 없습니다.")
    live_sub = db.query(models.Subscription).filter(
        models.Subscription.user_id == owner.id, models.Subscription.status.in_(SUB_LIVE_STATUSES)
    ).first()
    if live_sub:
        raise HTTPException(status_code=400, detail="결제 중인 구독이 있는 회원은 요금제를 직접 바꿀 수 없습니다. 구독을 해지(종료)한 뒤 변경해주세요.")
    expires_at = None
    if (body.plan or "").strip() and body.expires_on:
        try:
            d = datetime.strptime(body.expires_on.strip()[:10], "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="만료일 형식이 올바르지 않습니다. (예: 2026-12-31)")
        expires_at = d + timedelta(days=1) - timedelta(hours=9)          # 한국 날짜 기준 그날 자정(다음 날 0시)까지
        if expires_at <= datetime.utcnow():
            raise HTTPException(status_code=400, detail="만료일은 오늘 이후 날짜여야 합니다.")
    team = set_team_plan(db, owner, (body.plan or "").strip(), expires_at)
    limit = team_seat_limit(team)
    members_n, pending_n = team_counts(db, team)
    return {
        "email": owner.email,
        "plan": team.plan or "",
        "plan_label": _plan_label(team.plan),
        "seat_limit": limit,
        "members_count": members_n,
        "pending_count": pending_n,
        # 요금제를 낮춰서 이미 한도를 넘은 경우 — 자동으로 내보내지는 않고, 새 초대만 막힌다.
        "over_limit": members_n > limit,
    }

@app.get("/admin/teams")
def admin_list_teams(_: bool = Depends(check_admin), db: Session = Depends(get_db)):
    """구독/팀 현황 — 팀(대표)별 요금제·구독 상태·팀원·수락 대기 초대·팀원별 최근 30일 분석 건수"""
    from datetime import timedelta
    hist_cutoff = datetime.utcnow() - timedelta(days=HISTORY_RETENTION_DAYS)
    result = []
    for t in db.query(models.Team).order_by(models.Team.created_at.desc()).all():
        owner = db.query(models.User).filter(models.User.id == t.owner_id).first()
        members_n, pending_n = team_counts(db, t)
        limit = team_seat_limit(t)
        sub = db.query(models.Subscription).filter(models.Subscription.user_id == t.owner_id).first()
        members = []
        for tm in (db.query(models.TeamMember).filter(models.TeamMember.team_id == t.id)
                   .order_by(models.TeamMember.joined_at.asc()).all()):
            u = db.query(models.User).filter(models.User.id == tm.user_id).first()
            if not u:
                continue
            hist_n = (db.query(models.AnalysisHistory)
                      .filter(models.AnalysisHistory.user_id == u.id,
                              models.AnalysisHistory.created_at >= hist_cutoff).count())
            members.append({
                "email": u.email,
                "nickname": u.nickname or "",
                "company_name": u.company_name or "",
                "role": tm.role,
                "joined_at": tm.joined_at.isoformat() if tm.joined_at else None,
                "history_count": hist_n,
            })
        invites = [{
            "email": i.email,
            "created_at": i.created_at.isoformat() if i.created_at else None,
        } for i in (db.query(models.TeamInvite)
                    .filter(models.TeamInvite.team_id == t.id, models.TeamInvite.created_at >= _invite_cutoff())
                    .order_by(models.TeamInvite.created_at.desc()).all())]
        result.append({
            "owner_email": owner.email if owner else "(삭제됨)",
            "company_name": owner.company_name if owner else "",
            "plan": t.plan or "",
            "plan_label": _plan_label(t.plan),
            "seat_limit": limit,
            "members_count": members_n,
            "pending_count": pending_n,
            "over_limit": members_n > limit,
            "plan_updated_at": t.plan_updated_at.isoformat() if t.plan_updated_at else None,
            "plan_expires_on": _kst_date(t.plan_expires_at),
            "subscription": _sub_view(sub) if sub else None,
            "members": members,
            "invites": invites,
        })
    return {"count": len(result), "teams": result}


@app.get("/admin/refund-requests")
def admin_list_refund_requests(_: bool = Depends(check_admin), db: Session = Depends(get_db)):
    reqs = db.query(models.RefundRequest).order_by(models.RefundRequest.created_at.desc()).all()
    result = []
    for r in reqs:
        user = db.query(models.User).filter(models.User.id == r.user_id).first()

        # ── 결제 내역 (취소된 건도 함께 보여줘야 헷갈리지 않음) ──
        pays = (db.query(models.Payment)
                  .filter(models.Payment.user_id == r.user_id)
                  .order_by(models.Payment.created_at.desc()).limit(20).all()) if user else []
        pay_rows = []
        for p in pays:
            amount = p.amount or 0
            cancelled = p.cancelled_amount or 0
            pay_rows.append({
                "order_id": p.order_id,
                "amount": amount,
                "cancelled_amount": cancelled,
                "cancellable": max(0, amount - cancelled),
                "credits": p.credits or 0,
                "package_id": p.package_id,
                "package_label": (CREDIT_PACKAGES.get(p.package_id) or {}).get("label", p.package_id),
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "cancelled_at": p.cancelled_at.isoformat() if p.cancelled_at else None,
                "status": ("취소완료" if cancelled >= amount and amount > 0
                           else ("부분취소" if cancelled else "결제완료")),
            })

        # ── 크레딧 출처 요약: 실결제 지급분과 관리자 지급분을 구분 ──
        # 관리자 지급/조정분은 별도 기록이 없으므로 (현재 잔여) - (결제 지급 - 분석 사용)으로 역산한다.
        credit_summary = None
        if user:
            paid_credits = sum((p.credits or 0) for p in pays)
            used_credits = sum(
                (l.credits_used or 0)
                for l in db.query(models.AnalysisLog).filter(models.AnalysisLog.user_id == r.user_id).all()
            )
            expected = paid_credits - used_credits
            credit_summary = {
                "paid_credits": paid_credits,          # 결제로 지급된 크레딧
                "used_credits": used_credits,          # 분석에 사용한 크레딧
                "current_credits": user.credits,       # 현재 잔여
                "admin_adjusted": user.credits - expected,   # 관리자가 넣거나 뺀 분 (추정)
            }
        result.append({
            "id": r.id,
            "email": user.email if user else "(탈퇴한 회원)",
            "company_name": user.company_name if user else "",
            "phone": user.phone if user else "",
            "remaining_count": (user.credits // COST_PER_ANALYSIS) if user else None,
            "credits": user.credits if user else 0,
            "reason": r.reason,
            "status": r.status,
            "admin_note": r.admin_note,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "processed_at": r.processed_at.isoformat() if r.processed_at else None,
            "order_id": r.order_id or "",
            "refunded_amount": r.refunded_amount or 0,
            "credits_deducted": r.credits_deducted or 0,
            # 결제 내역 전체 (취소 완료분 포함) + 크레딧 출처 요약
            "payments": pay_rows,
            "credit_summary": credit_summary,
        })
    return {"count": len(result), "requests": result}

class AdminResolveRefundBody(BaseModel):
    status: str  # processed / rejected
    admin_note: str = ""
    deduct_credits: Optional[int] = None   # 포트원에서 직접 취소한 경우 크레딧만 회수할 때 사용

@app.post("/admin/refund-requests/{req_id}/resolve")
def admin_resolve_refund(req_id: int, body: AdminResolveRefundBody, _: bool = Depends(check_admin), db: Session = Depends(get_db)):
    if body.status not in ("processed", "rejected"):
        raise HTTPException(status_code=400, detail="status는 processed 또는 rejected여야 합니다.")
    r = db.query(models.RefundRequest).filter(models.RefundRequest.id == req_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="해당 환불 신청을 찾을 수 없습니다.")
    deducted = 0
    shortfall = 0
    if body.status == "processed" and body.deduct_credits:
        user = db.query(models.User).filter(models.User.id == r.user_id).first()
        if user:
            want = max(0, int(body.deduct_credits))
            deducted = min(want, user.credits)
            shortfall = want - deducted
            user.credits -= deducted
            r.credits_deducted = (r.credits_deducted or 0) + deducted

    r.status = body.status
    note = body.admin_note.strip()
    if deducted:
        note = (note + " / " if note else "") + f"크레딧 {deducted} 회수"
    r.admin_note = note
    r.processed_at = datetime.utcnow()
    db.commit()
    return {"id": r.id, "status": r.status, "credits_deducted": deducted, "credits_shortfall": shortfall}


async def sync_payment_cancelled(payment: "models.Payment", db: Session) -> int:
    """포트원에서 실제 취소된 금액을 조회해 우리 결제 기록에 반영한다."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{PORTONE_API_BASE}/payments/{payment.order_id}",
                headers={"Authorization": f"PortOne {PORTONE_SECRET_KEY}", "Content-Type": "application/json"},
            )
        if resp.status_code != 200:
            return 0
        info = resp.json() or {}
        amt = info.get("amount") or {}
        cancelled = int(amt.get("cancelled") or 0)
        if not cancelled and str(info.get("status", "")).upper() == "CANCELLED":
            cancelled = int(payment.amount or 0)   # 금액 정보가 없으면 전액 취소로 간주
        if cancelled:
            payment.cancelled_amount = cancelled
            payment.cancelled_at = payment.cancelled_at or datetime.utcnow()
            db.commit()
        return cancelled
    except Exception as e:
        db.rollback()
        print(f"결제 취소 상태 동기화 실패: {e}")
        return 0


class AdminRefundBody(BaseModel):
    order_id: str                       # 취소할 결제건 (payments.order_id)
    amount: Optional[int] = None        # 부분 취소 금액 (비우면 취소 가능한 전액)
    reason: str = "고객 환불 요청"
    deduct_credits: Optional[int] = None  # 회수할 크레딧 (비우면 환불 비율만큼 자동 계산)
    admin_note: str = ""
    allow_reprocess: bool = False   # 이미 처리된 신청에 추가 환불을 할 때만 True


@app.post("/admin/refund-requests/{req_id}/refund")
async def admin_process_refund(
    req_id: int,
    body: AdminRefundBody,
    _: bool = Depends(check_admin),
    db: Session = Depends(get_db),
):
    """포트원 결제 취소 → 크레딧 회수 → 환불 신청 상태 변경까지 한 번에 처리.
    포트원 취소가 실패하면 아무것도 바꾸지 않는다 (돈은 안 나갔는데 크레딧만 깎이는 일 방지)."""
    if not PORTONE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="PORTONE_SECRET_KEY 환경변수가 설정되어 있지 않습니다.")

    r = db.query(models.RefundRequest).filter(models.RefundRequest.id == req_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="해당 환불 신청을 찾을 수 없습니다.")
    if r.status == "processed" and not body.allow_reprocess:
        raise HTTPException(status_code=400, detail="이미 환불 처리된 신청입니다. 추가 환불이 필요하면 '추가 환불'로 진행해주세요.")

    payment = db.query(models.Payment).filter(models.Payment.order_id == body.order_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="해당 결제 건을 찾을 수 없습니다.")
    if payment.user_id != r.user_id:
        raise HTTPException(status_code=400, detail="신청자의 결제 건이 아닙니다.")

    cancellable = max(0, (payment.amount or 0) - (payment.cancelled_amount or 0))
    if cancellable <= 0:
        raise HTTPException(status_code=400, detail="이미 전액 취소된 결제입니다.")

    amount = int(body.amount) if body.amount else cancellable
    if amount <= 0:
        raise HTTPException(status_code=400, detail="환불 금액은 1원 이상이어야 합니다.")
    if amount > cancellable:
        raise HTTPException(status_code=400, detail=f"취소 가능 금액({cancellable:,}원)을 초과했습니다.")

    # ── 포트원 결제 취소 요청 ──
    payload = {"reason": (body.reason or "고객 환불 요청")[:200]}
    if PORTONE_STORE_ID:
        payload["storeId"] = PORTONE_STORE_ID
    if amount < cancellable:            # 부분 취소일 때만 금액 지정
        payload["amount"] = amount

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{PORTONE_API_BASE}/payments/{payment.order_id}/cancel",
                headers={"Authorization": f"PortOne {PORTONE_SECRET_KEY}", "Content-Type": "application/json"},
                json=payload,
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"포트원 서버에 연결하지 못했습니다: {e}")

    if resp.status_code not in (200, 201):
        raw = resp.text or ""
        # 포트원 콘솔 등에서 이미 취소한 결제 → 우리 DB 기록을 실제 상태로 맞춰준다
        if "ALREADY_CANCELLED" in raw.upper():
            synced = await sync_payment_cancelled(payment, db)
            raise HTTPException(
                status_code=400,
                detail=(f"이 결제는 포트원에서 이미 취소되어 있습니다"
                        + (f" (취소금액 {synced:,}원)" if synced else "")
                        + ". 결제 기록을 실제 상태로 맞춰두었으니, 크레딧 회수와 상태 정리는 '완료 처리'로 진행해주세요."))
        raise HTTPException(status_code=400, detail=f"포트원 결제 취소 실패 (status={resp.status_code}) {raw[:200]}")

    # ── 여기부터는 실제로 돈이 나간 뒤이므로 기록을 반드시 남긴다 ──
    payment.cancelled_amount = (payment.cancelled_amount or 0) + amount
    payment.cancelled_at = datetime.utcnow()

    user = db.query(models.User).filter(models.User.id == r.user_id).first()
    deducted = 0
    if user:
        if body.deduct_credits is not None:
            want = max(0, int(body.deduct_credits))
        else:
            # 환불 비율만큼 자동 계산 (전액 환불이면 지급 크레딧 전부)
            want = int(round((payment.credits or 0) * amount / (payment.amount or 1)))
        deducted = min(want, user.credits)   # 이미 써버린 만큼은 회수할 수 없음
        user.credits -= deducted

    r.status = "processed"
    r.order_id = payment.order_id
    r.refunded_amount = (r.refunded_amount or 0) + amount
    r.credits_deducted = (r.credits_deducted or 0) + deducted
    r.processed_at = datetime.utcnow()
    note = (body.admin_note or "").strip()
    auto = f"{amount:,}원 환불 · 크레딧 {deducted} 회수"
    r.admin_note = (note + " / " + auto) if note else auto
    db.commit()

    shortfall = 0
    if user and body.deduct_credits is None:
        want = int(round((payment.credits or 0) * amount / (payment.amount or 1)))
        shortfall = max(0, want - deducted)

    return {
        "id": r.id,
        "status": r.status,
        "refunded_amount": amount,
        "credits_deducted": deducted,
        "credits_shortfall": shortfall,   # 이미 사용해서 회수하지 못한 크레딧
        "remaining_credits": user.credits if user else None,
        "payment": {
            "order_id": payment.order_id,
            "amount": payment.amount,
            "cancelled_amount": payment.cancelled_amount,
            "cancellable": max(0, (payment.amount or 0) - payment.cancelled_amount),
        },
    }


# ══════════════════════════════════════════════════════════════
# 오류신고 (플로팅 버튼 → 관리자 이메일로 전달)
# ══════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════
# 방문 통계 (트래픽)
#   개인정보를 남기지 않기 위해 IP는 저장하지 않는다.
#   순방문자는 브라우저가 만든 임의 식별자(visitor_id)로만 구분한다.
# ══════════════════════════════════════════════════════════════
VISIT_RETENTION_DAYS = 180   # 방문 기록 보관 기간

class VisitBody(BaseModel):
    visitor_id: Optional[str] = ""
    path: Optional[str] = ""
    source: Optional[str] = ""
    referrer: Optional[str] = ""
    promo_code: Optional[str] = ""
    device: Optional[str] = ""
    is_member: Optional[bool] = False


BOT_HINTS = ("bot", "crawl", "spider", "slurp", "curl", "wget", "python-requests",
             "headlesschrome", "lighthouse", "preview", "monitor", "pingdom", "uptime")


@app.post("/track/visit")
def track_visit(
    body: VisitBody,
    user_agent: str = Header(default="", alias="User-Agent"),
    db: Session = Depends(get_db),
):
    """방문 1건 기록 — 로그인 여부와 무관하게 누구나 호출 (인증 없음)"""
    ua = (user_agent or "").lower()
    if any(h in ua for h in BOT_HINTS):
        return {"ok": True, "skipped": "bot"}

    try:
        db.add(models.SiteVisit(
            visitor_id=(body.visitor_id or "")[:64],
            path=(body.path or "")[:100],
            source=(body.source or "direct")[:50],
            referrer=(body.referrer or "")[:200],
            promo_code=(body.promo_code or "")[:40],
            device=(body.device or "")[:20],
            is_member=1 if body.is_member else 0,
        ))
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"방문 기록 실패: {e}")
    return {"ok": True}


def purge_old_visits(db: Session) -> int:
    """보관기간이 지난 방문 기록 삭제"""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=VISIT_RETENTION_DAYS)
    try:
        n = db.query(models.SiteVisit).filter(models.SiteVisit.created_at < cutoff).delete(synchronize_session=False)
        if n:
            db.commit()
            print(f"[visits] 오래된 방문 기록 {n}건 삭제")
        return n
    except Exception as e:
        db.rollback()
        return 0


@app.get("/admin/stats")
def admin_stats(
    days: int = 30,
    _: bool = Depends(check_admin),
    db: Session = Depends(get_db),
):
    """방문 통계 — 일별 추이, 유입경로, 기기, 전환 현황"""
    from datetime import timedelta
    from sqlalchemy import func

    purge_old_visits(db)
    days = max(1, min(days, 180))
    now = datetime.utcnow()
    since = now - timedelta(days=days - 1)
    since = since.replace(hour=0, minute=0, second=0, microsecond=0)

    rows = db.query(models.SiteVisit).filter(models.SiteVisit.created_at >= since).all()

    # ── 일별 집계 (KST 기준으로 묶어야 사장님이 보는 날짜와 맞음) ──
    KST = timedelta(hours=9)
    daily = {}
    for i in range(days):
        d = (since + timedelta(days=i) + KST).strftime("%Y-%m-%d")
        daily[d] = {"date": d, "visits": 0, "visitors": set()}

    sources, devices = {}, {"mobile": 0, "pc": 0}
    all_visitors, member_visits = set(), 0

    for v in rows:
        if not v.created_at:
            continue
        d = (v.created_at + KST).strftime("%Y-%m-%d")
        if d not in daily:
            continue
        daily[d]["visits"] += 1
        vid = v.visitor_id or f"anon-{v.id}"
        daily[d]["visitors"].add(vid)
        all_visitors.add(vid)
        src = v.source or "direct"
        sources[src] = sources.get(src, 0) + 1
        if v.device in devices:
            devices[v.device] += 1
        if v.is_member:
            member_visits += 1

    daily_list = [{"date": x["date"], "visits": x["visits"], "visitors": len(x["visitors"])}
                  for x in daily.values()]

    def count_since(model, dt):
        return db.query(func.count(model.id)).filter(model.created_at >= dt).scalar() or 0

    today_kst = (now + KST).replace(hour=0, minute=0, second=0, microsecond=0) - KST
    week_ago = now - timedelta(days=7)

    today_rows = [v for v in rows if v.created_at and v.created_at >= today_kst]

    return {
        "days": days,
        "retention_days": VISIT_RETENTION_DAYS,
        "summary": {
            "today_visits": len(today_rows),
            "today_visitors": len({(v.visitor_id or f"anon-{v.id}") for v in today_rows}),
            "period_visits": len(rows),
            "period_visitors": len(all_visitors),
            "member_visits": member_visits,
        },
        "daily": daily_list,
        "sources": sorted(
            [{"source": k, "visits": v} for k, v in sources.items()],
            key=lambda x: -x["visits"]
        )[:15],
        "devices": devices,
        # ── 전환 현황: 방문이 실제 가입·결제·분석으로 이어졌는지 ──
        "conversions": {
            "signups_period": count_since(models.User, since),
            "signups_week": count_since(models.User, week_ago),
            "payments_period": count_since(models.Payment, since),
            "payments_week": count_since(models.Payment, week_ago),
            "analyses_period": count_since(models.AnalysisLog, since),
            "analyses_week": count_since(models.AnalysisLog, week_ago),
            "revenue_period": db.query(func.coalesce(func.sum(models.Payment.amount), 0))
                                .filter(models.Payment.created_at >= since).scalar() or 0,
        },
    }


# ══════════════════════════════════════════════════════════════
# 관리자 — 회원 분석이력 조회
#   ※ 업로드된 재무제표 원본은 보관하지 않으므로 조회할 수 없고,
#      분석 결과(리포트)만 확인할 수 있습니다.
# ══════════════════════════════════════════════════════════════
@app.get("/admin/histories")
def admin_list_histories(
    q: Optional[str] = None,
    limit: int = 200,
    _: bool = Depends(check_admin),
    db: Session = Depends(get_db),
):
    """전체 회원의 분석이력 조회. q로 이메일·회사명·대표자명·사업자번호 검색"""
    from datetime import timedelta
    purge_expired_histories(db)
    cutoff = datetime.utcnow() - timedelta(days=HISTORY_RETENTION_DAYS)

    query = (db.query(models.AnalysisHistory, models.User)
               .outerjoin(models.User, models.User.id == models.AnalysisHistory.user_id)
               .filter(models.AnalysisHistory.created_at >= cutoff))

    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(
            (models.User.email.ilike(like))
            | (models.AnalysisHistory.company_name.ilike(like))
            | (models.AnalysisHistory.rep_name.ilike(like))
            | (models.AnalysisHistory.business_number.ilike(like))
        )

    rows = query.order_by(models.AnalysisHistory.created_at.desc()).limit(max(1, min(limit, 500))).all()
    return {
        "count": len(rows),
        "retention_days": HISTORY_RETENTION_DAYS,
        "histories": [
            {
                **_history_summary(h),
                "email": (u.email if u else "(탈퇴한 회원)"),
                "user_company": (u.company_name if u else ""),
                "phone": (u.phone if u else ""),
            }
            for h, u in rows
        ],
    }


class AdminHometaxLimitBody(BaseModel):
    input: dict

@app.post("/admin/hometax/limit")
def admin_hometax_limit(body: AdminHometaxLimitBody, _: bool = Depends(check_admin)):
    """관리자 화면에서 회원의 원클릭 분석 이력을 볼 때 예상 한도 계산"""
    L = _ht_clean(ht_estimate_limit(body.input or {}))
    return {"full": True, "limit": L, "ok_orgs": sum(1 for o in (L.get("orgs") or []) if o.get("status") == "ok")}

@app.get("/admin/histories/{history_id}")
def admin_get_history(
    history_id: int,
    _: bool = Depends(check_admin),
    db: Session = Depends(get_db),
):
    """분석 결과 전체 데이터 (리포트 화면을 그리는 데 사용)"""
    import json as _json
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=HISTORY_RETENTION_DAYS)
    h = db.query(models.AnalysisHistory).filter(
        models.AnalysisHistory.id == history_id,
        models.AnalysisHistory.created_at >= cutoff,
    ).first()
    if not h:
        raise HTTPException(status_code=404, detail="분석 이력을 찾을 수 없습니다. (보관기간이 지났거나 삭제됨)")
    u = db.query(models.User).filter(models.User.id == h.user_id).first()
    try:
        data = _json.loads(h.data_json or "{}")
    except Exception:
        data = {}
    return {
        "history": {**_history_summary(h), "email": (u.email if u else "(탈퇴한 회원)")},
        "data": data,
    }


# ══════════════════════════════════════════════════════════════
# 관리자 — 프로모션(할인) 코드 관리
# ══════════════════════════════════════════════════════════════
def _promo_summary(p: "models.PromoCode") -> dict:
    return {
        "id": p.id,
        "code": p.code,
        "discount_percent": p.discount_percent,
        "packages": p.packages or "",
        "valid_from": p.valid_from.strftime("%Y-%m-%d") if p.valid_from else "",
        "valid_until": p.valid_until.strftime("%Y-%m-%d") if p.valid_until else "",
        "max_uses": p.max_uses,
        "used_count": p.used_count or 0,
        "once_per_user": bool(p.once_per_user),
        "enabled": bool(p.enabled),
        "memo": p.memo or "",
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _parse_date(value: str, end_of_day: bool = False):
    """YYYY-MM-DD 문자열을 datetime으로. 빈 값이면 None."""
    if not value:
        return None
    try:
        d = datetime.strptime(str(value).strip()[:10], "%Y-%m-%d")
    except Exception:
        raise HTTPException(status_code=400, detail="날짜는 YYYY-MM-DD 형식으로 입력해주세요.")
    if end_of_day:
        d = d.replace(hour=23, minute=59, second=59)
    return d


class AdminPromoBody(BaseModel):
    id: Optional[int] = None
    code: str = ""
    discount_percent: int = 0
    packages: Optional[str] = ""      # "" 이면 전체, 아니면 "single,standard,mega"
    valid_from: Optional[str] = ""    # YYYY-MM-DD
    valid_until: Optional[str] = ""   # YYYY-MM-DD
    max_uses: Optional[int] = None
    once_per_user: bool = True
    enabled: bool = True
    memo: Optional[str] = ""


@app.get("/admin/promos")
def admin_list_promos(_: bool = Depends(check_admin), db: Session = Depends(get_db)):
    rows = db.query(models.PromoCode).order_by(models.PromoCode.id.desc()).all()
    return {"promos": [_promo_summary(p) for p in rows], "packages": CREDIT_PACKAGES}


@app.post("/admin/promos")
def admin_save_promo(body: AdminPromoBody, _: bool = Depends(check_admin), db: Session = Depends(get_db)):
    """id가 있으면 수정, 없으면 새로 만들기"""
    code = (body.code or "").strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="코드를 입력해주세요.")
    if not re_match_code(code):
        raise HTTPException(status_code=400, detail="코드는 영문/숫자/하이픈만 사용할 수 있습니다. (2~30자)")
    if not (1 <= int(body.discount_percent or 0) <= 100):
        raise HTTPException(status_code=400, detail="할인율은 1~100 사이로 입력해주세요.")

    packages = ",".join([x.strip() for x in (body.packages or "").split(",") if x.strip()])
    for pid in [x for x in packages.split(",") if x]:
        if pid not in CREDIT_PACKAGES:
            raise HTTPException(status_code=400, detail=f"알 수 없는 이용권입니다: {pid}")

    valid_from  = _parse_date(body.valid_from)
    valid_until = _parse_date(body.valid_until, end_of_day=True)
    if valid_from and valid_until and valid_from > valid_until:
        raise HTTPException(status_code=400, detail="시작일이 종료일보다 늦습니다.")

    if body.id:
        promo = db.query(models.PromoCode).filter(models.PromoCode.id == body.id).first()
        if not promo:
            raise HTTPException(status_code=404, detail="코드를 찾을 수 없습니다.")
    else:
        promo = models.PromoCode()
        db.add(promo)

    dup = db.query(models.PromoCode).filter(models.PromoCode.code == code).first()
    if dup and (not body.id or dup.id != body.id):
        raise HTTPException(status_code=400, detail="이미 있는 코드입니다.")

    promo.code = code
    promo.discount_percent = int(body.discount_percent)
    promo.packages = packages
    promo.valid_from = valid_from
    promo.valid_until = valid_until
    promo.max_uses = int(body.max_uses) if body.max_uses else None
    promo.once_per_user = 1 if body.once_per_user else 0
    promo.enabled = 1 if body.enabled else 0
    promo.memo = (body.memo or "").strip()[:200]
    if promo.used_count is None:
        promo.used_count = 0

    db.commit()
    db.refresh(promo)
    return {"promo": _promo_summary(promo)}


def re_match_code(code: str) -> bool:
    import re as _re
    return bool(_re.fullmatch(r"[A-Z0-9\-]{2,30}", code))


@app.post("/admin/promos/{promo_id}/toggle")
def admin_toggle_promo(promo_id: int, _: bool = Depends(check_admin), db: Session = Depends(get_db)):
    promo = db.query(models.PromoCode).filter(models.PromoCode.id == promo_id).first()
    if not promo:
        raise HTTPException(status_code=404, detail="코드를 찾을 수 없습니다.")
    promo.enabled = 0 if promo.enabled else 1
    db.commit()
    db.refresh(promo)
    return {"promo": _promo_summary(promo)}


@app.delete("/admin/promos/{promo_id}")
def admin_delete_promo(promo_id: int, _: bool = Depends(check_admin), db: Session = Depends(get_db)):
    promo = db.query(models.PromoCode).filter(models.PromoCode.id == promo_id).first()
    if not promo:
        raise HTTPException(status_code=404, detail="코드를 찾을 수 없습니다.")
    db.query(models.PromoUse).filter(models.PromoUse.promo_id == promo_id).delete()
    db.delete(promo)
    db.commit()
    return {"message": "삭제되었습니다."}


@app.get("/admin/promos/{promo_id}/uses")
def admin_promo_uses(promo_id: int, _: bool = Depends(check_admin), db: Session = Depends(get_db)):
    rows = (db.query(models.PromoUse, models.User)
              .outerjoin(models.User, models.User.id == models.PromoUse.user_id)
              .filter(models.PromoUse.promo_id == promo_id)
              .order_by(models.PromoUse.id.desc()).limit(200).all())
    return {"uses": [
        {
            "email": (u.email if u else "(탈퇴)"),
            "package_id": pu.package_id,
            "package_label": (CREDIT_PACKAGES.get(pu.package_id) or {}).get("label", pu.package_id),
            "discount_percent": pu.discount_percent,
            "original_price": pu.original_price,
            "paid_price": pu.paid_price,
            "order_id": pu.order_id,
            "created_at": pu.created_at.isoformat() if pu.created_at else None,
        } for pu, u in rows
    ]}


class ErrorReportBody(BaseModel):
    message: str
    email: Optional[str] = None
    page: Optional[str] = None
    user_agent: Optional[str] = None

@app.post("/support/report-error")
def report_error(body: ErrorReportBody):
    msg = body.message.strip()
    if not msg:
        raise HTTPException(status_code=400, detail="오류 내용을 입력해주세요.")

    body_text = f"""FinAnalyzer 오류신고가 접수되었습니다.

신고 내용:
{msg}

신고 페이지: {body.page or '-'}
회신 이메일: {body.email or '(미입력)'}
User-Agent: {body.user_agent or '-'}
접수 시각: {datetime.utcnow().isoformat()} (UTC)
"""
    # BREVO_API_KEY가 설정되어 있지 않으면 이메일 발송은 건너뛰고 접수만 성공 처리
    if not BREVO_API_KEY:
        print("오류신고 이메일 미발송: BREVO_API_KEY 환경변수가 설정되어 있지 않음")
    else:
        try:
            email_payload = {
                "sender": {"name": "FinAnalyzer 오류신고", "email": SENDER_EMAIL},
                "to": [{"email": SENDER_EMAIL}],
                "subject": f"[FinAnalyzer 오류신고] {body.page or '알 수 없음'}",
                "textContent": body_text,
            }
            if body.email:
                email_payload["replyTo"] = {"email": body.email}
            resp = httpx.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={
                    "api-key": BREVO_API_KEY,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=email_payload,
                timeout=15,
            )
            if resp.status_code not in (200, 201):
                print(f"오류신고 이메일 발송 실패 (status={resp.status_code}): {resp.text}")
            else:
                print("오류신고 이메일 발송 성공")
        except Exception as e:
            print(f"오류신고 이메일 발송 중 예외 발생: {e}")

    return {"message": "오류 신고가 접수되었습니다."}


# ══════════════════════════════════════════════════════════════
# 문의하기 (플로팅 버튼 → 관리자 이메일로 전달)
# ══════════════════════════════════════════════════════════════
class ContactUsBody(BaseModel):
    message: str
    email: Optional[str] = None
    page: Optional[str] = None
    user_agent: Optional[str] = None

@app.post("/support/contact-us")
def contact_us(body: ContactUsBody):
    msg = body.message.strip()
    if not msg:
        raise HTTPException(status_code=400, detail="문의 내용을 입력해주세요.")

    body_text = f"""FinAnalyzer 문의하기가 접수되었습니다.

문의 내용:
{msg}

문의 페이지: {body.page or '-'}
회신 이메일: {body.email or '(미입력)'}
User-Agent: {body.user_agent or '-'}
접수 시각: {datetime.utcnow().isoformat()} (UTC)
"""
    # BREVO_API_KEY가 설정되어 있지 않으면 이메일 발송은 건너뛰고 접수만 성공 처리
    if not BREVO_API_KEY:
        print("문의하기 이메일 미발송: BREVO_API_KEY 환경변수가 설정되어 있지 않음")
    else:
        try:
            email_payload = {
                "sender": {"name": "FinAnalyzer 문의하기", "email": SENDER_EMAIL},
                "to": [{"email": SENDER_EMAIL}],
                "subject": f"[FinAnalyzer 문의하기] {body.page or '알 수 없음'}",
                "textContent": body_text,
            }
            if body.email:
                email_payload["replyTo"] = {"email": body.email}
            resp = httpx.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={
                    "api-key": BREVO_API_KEY,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=email_payload,
                timeout=15,
            )
            if resp.status_code not in (200, 201):
                print(f"문의하기 이메일 발송 실패 (status={resp.status_code}): {resp.text}")
            else:
                print("문의하기 이메일 발송 성공")
        except Exception as e:
            print(f"문의하기 이메일 발송 중 예외 발생: {e}")

    return {"message": "문의가 접수되었습니다."}


# ══════════════════════════════════════════════════════════════
# 커뮤니티 게시판 (로그인 회원만 열람/작성, 댓글 지원)
# ══════════════════════════════════════════════════════════════
def display_name(user: models.User) -> str:
    return user.nickname or user.company_name or user.rep_name or user.email.split("@")[0]

class CommunityPostCreate(BaseModel):
    title: str
    content: str

class CommunityCommentCreate(BaseModel):
    content: str

class CommunityPostUpdate(BaseModel):
    title: str
    content: str

class CommunityCommentUpdate(BaseModel):
    content: str

@app.get("/community/posts")
def list_community_posts(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    posts = db.query(models.CommunityPost).order_by(models.CommunityPost.created_at.desc()).limit(200).all()
    result = []
    for p in posts:
        author = db.query(models.User).filter(models.User.id == p.user_id).first()
        comment_count = db.query(models.CommunityComment).filter(models.CommunityComment.post_id == p.id).count()
        result.append({
            "id": p.id,
            "title": p.title,
            "content": p.content,
            "author": display_name(author) if author else "(탈퇴한 회원)",
            "author_email": author.email if author else None,
            "comment_count": comment_count,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        })
    return {"count": len(result), "posts": result}

@app.post("/community/posts")
def create_community_post(
    body: CommunityPostCreate,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    title = body.title.strip()
    content = body.content.strip()
    if not title or not content:
        raise HTTPException(status_code=400, detail="제목과 내용을 모두 입력해주세요.")
    post = models.CommunityPost(user_id=user.id, title=title, content=content)
    db.add(post)
    db.commit()
    db.refresh(post)
    return {"id": post.id, "message": "글이 등록되었습니다."}

@app.get("/community/posts/{post_id}")
def get_community_post(
    post_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    post = db.query(models.CommunityPost).filter(models.CommunityPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="해당 글을 찾을 수 없습니다.")
    author = db.query(models.User).filter(models.User.id == post.user_id).first()
    comments = db.query(models.CommunityComment).filter(models.CommunityComment.post_id == post.id).order_by(models.CommunityComment.created_at.asc()).all()
    comment_list = []
    for c in comments:
        c_author = db.query(models.User).filter(models.User.id == c.user_id).first()
        comment_list.append({
            "id": c.id,
            "content": c.content,
            "author": display_name(c_author) if c_author else "(탈퇴한 회원)",
            "author_email": c_author.email if c_author else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        })
    return {
        "id": post.id,
        "title": post.title,
        "content": post.content,
        "author": display_name(author) if author else "(탈퇴한 회원)",
        "author_email": author.email if author else None,
        "created_at": post.created_at.isoformat() if post.created_at else None,
        "updated_at": post.updated_at.isoformat() if post.updated_at else None,
        "comments": comment_list,
    }

@app.put("/community/posts/{post_id}")
def update_community_post(
    post_id: int,
    body: CommunityPostUpdate,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    post = db.query(models.CommunityPost).filter(models.CommunityPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="해당 글을 찾을 수 없습니다.")
    if post.user_id != user.id:
        raise HTTPException(status_code=403, detail="본인이 작성한 글만 수정할 수 있습니다.")
    title = body.title.strip()
    content = body.content.strip()
    if not title or not content:
        raise HTTPException(status_code=400, detail="제목과 내용을 모두 입력해주세요.")
    post.title = title
    post.content = content
    post.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "글이 수정되었습니다."}

@app.delete("/community/posts/{post_id}")
def delete_community_post(
    post_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    post = db.query(models.CommunityPost).filter(models.CommunityPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="해당 글을 찾을 수 없습니다.")
    if post.user_id != user.id:
        raise HTTPException(status_code=403, detail="본인이 작성한 글만 삭제할 수 있습니다.")
    db.query(models.CommunityComment).filter(models.CommunityComment.post_id == post.id).delete()
    db.delete(post)
    db.commit()
    return {"message": "글이 삭제되었습니다."}

@app.post("/community/posts/{post_id}/comments")
def create_community_comment(
    post_id: int,
    body: CommunityCommentCreate,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    post = db.query(models.CommunityPost).filter(models.CommunityPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="해당 글을 찾을 수 없습니다.")
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="댓글 내용을 입력해주세요.")
    comment = models.CommunityComment(post_id=post_id, user_id=user.id, content=content)
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return {"id": comment.id, "message": "댓글이 등록되었습니다."}

@app.put("/community/comments/{comment_id}")
def update_community_comment(
    comment_id: int,
    body: CommunityCommentUpdate,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    comment = db.query(models.CommunityComment).filter(models.CommunityComment.id == comment_id).first()
    if not comment:
        raise HTTPException(status_code=404, detail="해당 댓글을 찾을 수 없습니다.")
    if comment.user_id != user.id:
        raise HTTPException(status_code=403, detail="본인이 작성한 댓글만 수정할 수 있습니다.")
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="댓글 내용을 입력해주세요.")
    comment.content = content
    comment.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "댓글이 수정되었습니다."}

@app.delete("/community/comments/{comment_id}")
def delete_community_comment(
    comment_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    comment = db.query(models.CommunityComment).filter(models.CommunityComment.id == comment_id).first()
    if not comment:
        raise HTTPException(status_code=404, detail="해당 댓글을 찾을 수 없습니다.")
    if comment.user_id != user.id:
        raise HTTPException(status_code=403, detail="본인이 작성한 댓글만 삭제할 수 있습니다.")
    db.delete(comment)
    db.commit()
    return {"message": "댓글이 삭제되었습니다."}

# ── 관리자: 커뮤니티 게시판 모더레이션 ──
@app.get("/admin/community/posts")
def admin_list_community_posts(_: bool = Depends(check_admin), db: Session = Depends(get_db)):
    posts = db.query(models.CommunityPost).order_by(models.CommunityPost.created_at.desc()).limit(500).all()
    result = []
    for p in posts:
        author = db.query(models.User).filter(models.User.id == p.user_id).first()
        comment_count = db.query(models.CommunityComment).filter(models.CommunityComment.post_id == p.id).count()
        result.append({
            "id": p.id,
            "title": p.title,
            "content": p.content,
            "author_email": author.email if author else "(탈퇴한 회원)",
            "comment_count": comment_count,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        })
    return {"count": len(result), "posts": result}

@app.delete("/admin/community/posts/{post_id}")
def admin_delete_community_post(post_id: int, _: bool = Depends(check_admin), db: Session = Depends(get_db)):
    post = db.query(models.CommunityPost).filter(models.CommunityPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="해당 글을 찾을 수 없습니다.")
    db.query(models.CommunityComment).filter(models.CommunityComment.post_id == post.id).delete()
    db.delete(post)
    db.commit()
    return {"message": "글이 삭제되었습니다."}

@app.get("/admin/community/posts/{post_id}/comments")
def admin_list_community_comments(post_id: int, _: bool = Depends(check_admin), db: Session = Depends(get_db)):
    comments = db.query(models.CommunityComment).filter(models.CommunityComment.post_id == post_id).order_by(models.CommunityComment.created_at.asc()).all()
    result = []
    for c in comments:
        author = db.query(models.User).filter(models.User.id == c.user_id).first()
        result.append({
            "id": c.id,
            "content": c.content,
            "author_email": author.email if author else "(탈퇴한 회원)",
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })
    return {"count": len(result), "comments": result}

@app.delete("/admin/community/comments/{comment_id}")
def admin_delete_community_comment(comment_id: int, _: bool = Depends(check_admin), db: Session = Depends(get_db)):
    comment = db.query(models.CommunityComment).filter(models.CommunityComment.id == comment_id).first()
    if not comment:
        raise HTTPException(status_code=404, detail="해당 댓글을 찾을 수 없습니다.")
    db.delete(comment)
    db.commit()
    return {"message": "댓글이 삭제되었습니다."}


# ══════════════════════════════════════════════════════════════
# 아이디(이메일) 찾기 / 비밀번호 찾기 (임시비밀번호 이메일 발송)
# ══════════════════════════════════════════════════════════════
def mask_email(email: str) -> str:
    """이메일 앞부분을 일부만 남기고 마스킹 (예: honggildong@gmail.com -> hon********@gmail.com)"""
    try:
        local, domain = email.split("@", 1)
        if len(local) <= 2:
            masked = local[0] + "*" * (len(local) - 1)
        else:
            visible = max(2, len(local) // 3)
            masked = local[:visible] + "*" * (len(local) - visible)
        return f"{masked}@{domain}"
    except Exception:
        return "****"

def send_temp_password_email(to_email: str, temp_password: str):
    if not BREVO_API_KEY:
        raise HTTPException(status_code=500, detail="이메일 발송 설정(BREVO_API_KEY)이 되어 있지 않습니다.")
    body = f"""안녕하세요, FinAnalyzer입니다.

요청하신 임시비밀번호가 발급되었습니다.

임시비밀번호: {temp_password}

로그인 후 [내 정보] 메뉴에서 새 비밀번호로 변경해주세요.
본인이 요청하지 않았다면 이 메일을 무시하셔도 됩니다.

- FinAnalyzer"""
    try:
        resp = httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": BREVO_API_KEY,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={
                "sender": {"name": "C&G Partners", "email": SENDER_EMAIL},
                "to": [{"email": to_email}],
                "subject": "[FinAnalyzer] 임시비밀번호 안내",
                "textContent": body,
            },
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            raise HTTPException(status_code=500, detail=f"이메일 발송에 실패했습니다: {resp.text}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"이메일 발송에 실패했습니다: {e}")

class FindIdBody(BaseModel):
    phone: str

@app.post("/auth/find-id")
def find_id(body: FindIdBody, db: Session = Depends(get_db)):
    phone = body.phone.strip()
    if not phone:
        raise HTTPException(status_code=400, detail="휴대폰번호를 입력해주세요.")
    user = db.query(models.User).filter(models.User.phone == phone).first()
    if not user:
        raise HTTPException(status_code=404, detail="해당 휴대폰번호로 가입된 계정을 찾을 수 없습니다.")
    return {"masked_email": mask_email(user.email)}

class ResetPasswordBody(BaseModel):
    email: str

@app.post("/auth/reset-password")
def reset_password(body: ResetPasswordBody, db: Session = Depends(get_db)):
    import hashlib, secrets, string
    email = body.email.strip().lower()
    user = db.query(models.User).filter(_email_eq(email)).first()
    if not user:
        raise HTTPException(status_code=404, detail="해당 이메일로 가입된 계정을 찾을 수 없습니다.")

    alphabet = string.ascii_letters + string.digits
    temp_password = "".join(secrets.choice(alphabet) for _ in range(10))
    user.password_hash = hashlib.sha256(temp_password.encode()).hexdigest()
    db.commit()

    send_temp_password_email(user.email, temp_password)
    return {"message": "임시비밀번호가 이메일로 발송되었습니다."}


# ══════════════════════════════════════════════════════════════
# 구독(정기결제) — 포트원 V2 빌링키
#   흐름: ① 화면에서 카드 등록(빌링키 발급) → ② /subscription/start 가 첫 달을 바로 결제하고
#         다음 달 결제를 포트원에 예약 → ③ 이용기간이 끝난 뒤 sync_subscription()이 예약 결제 결과를
#         포트원에서 직접 조회해 기간을 연장하고 그다음 달을 다시 예약.
#   ※ Render에는 스케줄러가 없으므로 웹훅 대신 '조회 시점 확인(lazy)'을 쓰고, 안전망으로
#      /admin/subscriptions/sync 를 외부 cron(예: cron-job.org)이 하루 1번 호출하도록 할 수 있다.
#   ※ 결제 성공 여부는 항상 포트원 조회(GET /payments/{id})로 확인한다. 응답이 끊겨도 이중 결제가 나지 않도록
#      '요청 후 반드시 조회' 방식을 쓴다.
# ══════════════════════════════════════════════════════════════
from urllib.parse import quote as _urlquote
import random as _random
import string as _string

SUB_RETRY_MAX = 3            # 갱신 결제 연속 실패가 이 횟수가 되면 구독 종료
SUB_RETRY_GAP_HOURS = 24     # 실패 후 다음 재시도까지 최소 간격
SUB_GRACE_DAYS = 5           # 갱신일 이후 이 기간이 지나면 결제가 안 돼도 종료
SUB_CHECK_INTERVAL_MIN = 10  # 갱신 확인(포트원 조회) 최소 간격
SUB_PENDING_WAIT_HOURS = 6   # 예약 결제가 아직 처리 전이면 이 시간까지는 기다림
_SUB_BUSY = set()            # 같은 회원의 결제/갱신이 동시에 두 번 돌지 않게 하는 간단한 잠금

SUB_LIVE_STATUSES = ("active", "cancel_scheduled", "past_due")


def _new_sub_payment_id() -> str:
    return "SUB" + datetime.utcnow().strftime("%y%m%d%H%M%S") + "".join(_random.choices(_string.ascii_uppercase + _string.digits, k=6))

def _pt_call(method: str, path: str, body=None, timeout: int = 20):
    """포트원 V2 REST 호출. (HTTP 상태코드 또는 None, 응답 JSON) 을 돌려준다. 예외는 던지지 않는다."""
    if not PORTONE_SECRET_KEY:
        return None, {"message": "서버에 PORTONE_SECRET_KEY가 설정되어 있지 않습니다."}
    try:
        r = httpx.request(
            method, f"{PORTONE_API_BASE}{path}",
            headers={"Authorization": f"PortOne {PORTONE_SECRET_KEY}", "Content-Type": "application/json"},
            json=body, timeout=timeout,
        )
    except Exception as e:
        return None, {"message": f"포트원 통신 오류: {e}"}
    try:
        data = r.json()
    except Exception:
        data = {}
    return r.status_code, (data if isinstance(data, dict) else {})

def sub_add_month(dt: datetime, anchor_day: int) -> datetime:
    """dt의 다음 달 같은 '일'(없으면 말일). 1/31 → 2/28 → 3/31 처럼 결제일이 밀리지 않게 anchor_day 기준."""
    import calendar
    y = dt.year + (1 if dt.month == 12 else 0)
    m = 1 if dt.month == 12 else dt.month + 1
    d = min(anchor_day or dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=d)

def _sub_customer(user) -> dict:
    name = (user.rep_name or user.company_name or user.nickname or "피나회원").strip()
    return {"id": f"fa-{user.id}", "name": {"full": name}, "email": user.email}

def _sub_order_name(plan_id: str) -> str:
    label = SUBSCRIPTION_PLANS[plan_id]['label']
    return f"{label} 월 구독" if label.startswith("피나") else f"피나 {label} 월 구독"

def _sub_charge(billing_key: str, payment_id: str, plan_id: str, amount: int, user):
    """빌링키로 즉시 결제 후, 포트원 조회로 PAID·금액을 확인. (성공여부, 결제정보 또는 오류메시지)"""
    st, data = _pt_call("POST", f"/payments/{_urlquote(payment_id, safe='')}/billing-key", {
        "storeId": PORTONE_STORE_ID,
        "billingKey": billing_key,
        "orderName": _sub_order_name(plan_id),
        "customer": _sub_customer(user),
        "amount": {"total": amount},
        "currency": "KRW",
    })
    # 요청 응답과 상관없이 실제 결제 상태를 조회해서 판단 (타임아웃인데 실제로는 결제된 경우의 이중 결제 방지)
    st2, info = _pt_call("GET", f"/payments/{_urlquote(payment_id, safe='')}")
    if st2 == 200:
        if info.get("status") != "PAID":
            return False, (data.get("message") or f"결제 미완료 상태: {info.get('status')}")
        if (info.get("amount") or {}).get("total") != amount:
            return False, "결제 금액이 일치하지 않습니다."
        return True, info
    return False, (data.get("message") or "결제에 실패했습니다.")

def _sub_schedule(sub, user, when: datetime) -> str:
    """다음 결제를 포트원에 예약. 성공하면 예약한 paymentId, 실패하면 "" (그 경우 만료 시점에 직접 결제로 대체)"""
    if not sub.billing_key or when <= datetime.utcnow():
        return ""
    pid = _new_sub_payment_id()
    st, _d = _pt_call("POST", f"/payments/{_urlquote(pid, safe='')}/schedule", {
        "payment": {
            "storeId": PORTONE_STORE_ID,
            "billingKey": sub.billing_key,
            "orderName": _sub_order_name(sub.plan),
            "customer": _sub_customer(user),
            "amount": {"total": sub.amount},
            "currency": "KRW",
        },
        "timeToPay": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    return pid if st == 200 else ""

def _sub_revoke_schedules(sub):
    if sub.billing_key:
        _pt_call("DELETE", "/payment-schedules", {"storeId": PORTONE_STORE_ID, "billingKey": sub.billing_key})

def _sub_delete_billing_key(sub):
    if sub.billing_key:
        _pt_call("DELETE", f"/billing-keys/{_urlquote(sub.billing_key, safe='')}")

def _sub_record_payment(db: Session, user_id: int, payment_id: str, amount: int, plan_id: str, pg_tx: str = ""):
    if db.query(models.Payment).filter(models.Payment.order_id == payment_id).first():
        return
    db.add(models.Payment(user_id=user_id, order_id=payment_id, payment_key=pg_tx or "", amount=amount, credits=0, package_id=f"sub_{plan_id}"))

def _sub_finish(db: Session, sub, owner, reason: str = ""):
    """구독 종료: 예약 결제 취소 + 빌링키 삭제 + 팀 요금제 해제. (팀원은 자동으로 내보내지 않음)"""
    _sub_revoke_schedules(sub)
    _sub_delete_billing_key(sub)
    sub.status = "cancelled"
    sub.billing_key = ""
    sub.next_payment_id = ""
    db.commit()
    try:
        set_team_plan(db, owner, "")
    except HTTPException:
        pass
    if reason:
        notify_admin(f"[피나] 구독 종료 — {owner.email}", f"회원 {owner.email} 의 구독이 종료되었습니다.\n사유: {reason}\n플랜: {sub.plan} / 금액: {sub.amount}원")

def _sub_try_renew(db: Session, sub, owner, now: datetime) -> bool:
    """이용기간이 끝난 구독의 갱신을 한 번 시도. 기간이 연장되면 True."""
    paid_id, pg_tx = "", ""

    # 1) 예약해 둔 결제의 결과 확인
    if sub.next_payment_id:
        st, d = _pt_call("GET", f"/payments/{_urlquote(sub.next_payment_id, safe='')}")
        wait_until = sub.period_end + timedelta(hours=SUB_PENDING_WAIT_HOURS)
        if st == 200:
            status = d.get("status")
            if status == "PAID" and (d.get("amount") or {}).get("total") == sub.amount:
                paid_id, pg_tx = sub.next_payment_id, d.get("pgTxId", "")
            elif status in ("FAILED", "CANCELLED", "PARTIAL_CANCELLED"):
                sub.fail_count = (sub.fail_count or 0) + 1
                sub.last_failed_at = now
                sub.status = "past_due"
                sub.next_payment_id = ""
                db.commit()
                notify_admin(f"[피나] 구독 갱신 결제 실패 — {owner.email}", f"예약 결제가 실패했습니다. (결제번호 {d.get('id') or ''}, 상태 {status}) 자동 재시도 중입니다. 필요하면 고객에게 카드 확인을 안내해주세요.")
                return False
            elif now < wait_until:
                return False        # 아직 처리 전 — 조금 더 기다림
        elif st == 404:
            if now < wait_until:
                return False        # 예약 시각이 막 지났을 뿐일 수 있음
        else:
            return False            # 통신 오류 — 다음 확인 때 다시

    # 2) 예약이 없거나 오래 처리되지 않았으면 직접 결제 (재시도 포함)
    if not paid_id:
        # 종료 조건은 '결제 실패 이력이 있을 때'만 적용한다. (실패 이력이 없는데 갱신 확인이 늦어진 경우는
        # 서버 쪽 사정이므로 고객을 종료시키지 않고 지금이라도 결제를 시도한다)
        if (sub.fail_count or 0) >= SUB_RETRY_MAX or ((sub.fail_count or 0) >= 1 and now > sub.period_end + timedelta(days=SUB_GRACE_DAYS)):
            _sub_finish(db, sub, owner, reason="갱신 결제 실패가 반복되어 자동 종료")
            return False
        if sub.last_failed_at and now - sub.last_failed_at < timedelta(hours=SUB_RETRY_GAP_HOURS):
            return False
        pid = _new_sub_payment_id()
        ok, info = _sub_charge(sub.billing_key, pid, sub.plan, sub.amount, owner)
        if not ok:
            sub.fail_count = (sub.fail_count or 0) + 1
            sub.last_failed_at = now
            sub.status = "past_due"
            sub.next_payment_id = ""
            db.commit()
            if sub.fail_count >= SUB_RETRY_MAX:
                _sub_finish(db, sub, owner, reason=f"갱신 결제 {sub.fail_count}회 연속 실패 ({info})")
            return False
        paid_id, pg_tx = pid, info.get("pgTxId", "")

    # 3) 결제 확인됨 → 기간 연장 + 다음 달 예약
    _sub_record_payment(db, sub.user_id, paid_id, sub.amount, sub.plan, pg_tx)
    old_end = sub.period_end
    sub.period_start = old_end
    sub.period_end = sub_add_month(old_end, sub.anchor_day)
    sub.status = "active"
    sub.fail_count = 0
    sub.last_failed_at = None
    sub.last_payment_id = paid_id
    sub.next_payment_id = ""
    db.commit()
    sub.next_payment_id = _sub_schedule(sub, owner, sub.period_end)
    db.commit()
    return True

def sync_subscription(db: Session, sub, force: bool = False):
    """이용기간이 지난 구독의 상태를 정리(해지 예약 종료 / 갱신 결제 확인). 기간 안이면 아무 일도 하지 않는다."""
    now = datetime.utcnow()
    if not sub or sub.status == "cancelled" or not sub.period_end or now < sub.period_end:
        return
    owner = db.query(models.User).filter(models.User.id == sub.user_id).first()
    if not owner:
        return
    if sub.status == "cancel_scheduled":
        _sub_finish(db, sub, owner)
        return
    if not force and sub.last_check_at and now - sub.last_check_at < timedelta(minutes=SUB_CHECK_INTERVAL_MIN):
        return
    if sub.user_id in _SUB_BUSY:
        return
    _SUB_BUSY.add(sub.user_id)
    try:
        sub.last_check_at = now
        db.commit()
        for _ in range(3):   # 오래 비어 있던 경우 밀린 달을 순서대로 처리
            if sub.status == "cancelled" or now < sub.period_end:
                break
            if not _sub_try_renew(db, sub, owner, now):
                break
    finally:
        _SUB_BUSY.discard(sub.user_id)

def user_has_subscription_access(db: Session, user) -> bool:
    """이 회원이 구독(팀 요금제)으로 분석할 수 있는지. 대표·팀원 모두 소속 팀의 요금제를 따른다."""
    team, _m = get_membership(db, user)
    if not team:
        return False
    sub = db.query(models.Subscription).filter(models.Subscription.user_id == team.owner_id).first()
    if sub:
        sync_subscription(db, sub)
        db.refresh(team)
    _expire_admin_plan(db, team)
    return bool(team.plan)

def _expire_admin_plan(db: Session, team):
    """관리자가 기간을 정해 지정한 요금제가 만료됐으면 해제"""
    if team and team.plan and team.plan_expires_at and datetime.utcnow() >= team.plan_expires_at:
        team.plan = ""
        team.plan_expires_at = None
        team.plan_updated_at = datetime.utcnow()
        db.commit()

def cleanup_user_subscription(db: Session, user):
    """회원 탈퇴/삭제 시 구독 정리 — 예약 결제 취소, 빌링키 삭제. (commit은 호출한 쪽에서)"""
    sub = db.query(models.Subscription).filter(models.Subscription.user_id == user.id).first()
    if sub:
        _sub_revoke_schedules(sub)
        _sub_delete_billing_key(sub)
        db.delete(sub)
        db.flush()

def _sub_view(sub) -> dict:
    plan = SUBSCRIPTION_PLANS.get(sub.plan, {})
    return {
        "plan": sub.plan,
        "plan_label": plan.get("label", sub.plan),
        "seats": plan.get("seats", 0),
        "status": sub.status,
        "amount": sub.amount,
        "period_start": sub.period_start.isoformat() if sub.period_start else None,
        "period_end": sub.period_end.isoformat() if sub.period_end else None,
        "cancel_scheduled": sub.status == "cancel_scheduled",
        "fail_count": sub.fail_count or 0,
    }

@app.get("/subscription/me")
def subscription_me(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    sub = db.query(models.Subscription).filter(models.Subscription.user_id == user.id).first()
    if sub:
        sync_subscription(db, sub)
        db.refresh(sub)
    team, _m = get_membership(db, user)
    if team:
        db.refresh(team)
        _expire_admin_plan(db, team)
    is_member = bool(team and team.owner_id != user.id)
    live = bool(sub and sub.status in SUB_LIVE_STATUSES)
    return {
        "customer_id": f"fa-{user.id}",
        "customer_name": _sub_customer(user)["name"]["full"],
        "plans": _public_plans(),
        "is_team_member": is_member,
        "team_plan_label": _plan_label(team.plan) if is_member else None,
        "access": bool(team and team.plan),
        "subscription": _sub_view(sub) if live else None,
        # 결제 없이 관리자가 요금제를 부여한 경우
        "admin_plan_label": _plan_label(team.plan) if (team and team.owner_id == user.id and team.plan and not live) else None,
        "admin_plan_expires_on": _kst_date(team.plan_expires_at) if (team and team.owner_id == user.id and team.plan and not live) else None,
    }

class SubscriptionStartBody(BaseModel):
    plan: str
    billing_key: str

@app.post("/subscription/start")
def subscription_start(body: SubscriptionStartBody, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    plan = SUBSCRIPTION_PLANS.get(body.plan)
    if plan and plan.get("hidden"):
        plan = None
    billing_key = (body.billing_key or "").strip()
    if not plan:
        raise HTTPException(status_code=400, detail="알 수 없는 요금제입니다.")
    if not billing_key:
        raise HTTPException(status_code=400, detail="카드 등록 정보가 없습니다. 다시 시도해주세요.")
    team, _m = get_membership(db, user)
    if team and team.owner_id != user.id:
        raise HTTPException(status_code=400, detail="이미 다른 팀의 팀원으로 속해 있어 구독을 시작할 수 없습니다. 팀에서 먼저 탈퇴해주세요.")
    existing = db.query(models.Subscription).filter(models.Subscription.user_id == user.id).first()
    if existing and existing.status in SUB_LIVE_STATUSES:
        raise HTTPException(status_code=400, detail="이미 구독 중입니다.")
    if user.id in _SUB_BUSY:
        raise HTTPException(status_code=409, detail="처리 중입니다. 잠시 후 다시 확인해주세요.")

    _SUB_BUSY.add(user.id)
    try:
        pid = _new_sub_payment_id()
        ok, info = _sub_charge(billing_key, pid, body.plan, plan["price"], user)
        if not ok:
            _pt_call("DELETE", f"/billing-keys/{_urlquote(billing_key, safe='')}")   # 결제 안 된 빌링키는 정리
            raise HTTPException(status_code=400, detail=f"결제에 실패했습니다: {info}")
        # ── 여기부터는 이미 결제된 상태 — 기록이 실패하면 결제번호를 남기고 관리자에게 알린다 ──
        try:
            now = datetime.utcnow()
            sub = existing or models.Subscription(user_id=user.id)
            sub.plan = body.plan
            sub.status = "active"
            sub.billing_key = billing_key
            sub.amount = plan["price"]
            sub.anchor_day = now.day
            sub.period_start = now
            sub.period_end = sub_add_month(now, now.day)
            sub.last_payment_id = pid
            sub.fail_count = 0
            sub.last_failed_at = None
            sub.last_check_at = None
            sub.cancel_requested_at = None
            sub.next_payment_id = ""
            if not existing:
                db.add(sub)
            _sub_record_payment(db, user.id, pid, plan["price"], body.plan, info.get("pgTxId", ""))
            db.commit()
            sub.next_payment_id = _sub_schedule(sub, user, sub.period_end)
            db.commit()
            set_team_plan(db, user, body.plan)
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            notify_admin(f"[피나] 구독 결제는 되었으나 기록 실패 — {user.email}", f"결제번호 {pid}, 금액 {plan['price']}원, 플랜 {body.plan}. DB 기록 중 오류: {e}\n포트원 콘솔에서 확인 후 수동 처리가 필요합니다.")
            raise HTTPException(status_code=500, detail=f"결제는 완료되었으나 처리 중 오류가 발생했습니다. 결제번호 {pid} 로 문의해주세요.")
    finally:
        _SUB_BUSY.discard(user.id)

    notify_admin(f"[피나] 새 구독 시작 — {user.email}", f"{user.email} ({user.company_name}) 님이 {plan['label']} 구독을 시작했습니다. 월 {plan['price']:,}원 / 결제번호 {pid}")
    db.refresh(sub)
    return {"message": "구독이 시작되었습니다.", "subscription": _sub_view(sub)}

@app.post("/subscription/cancel")
def subscription_cancel(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    sub = db.query(models.Subscription).filter(models.Subscription.user_id == user.id).first()
    if not sub or sub.status not in SUB_LIVE_STATUSES:
        raise HTTPException(status_code=400, detail="해지할 구독이 없습니다.")
    if sub.status == "cancel_scheduled":
        raise HTTPException(status_code=400, detail="이미 해지 예약되어 있습니다.")
    if sub.status == "past_due":
        # 결제가 밀린 상태에서 해지하면 이용기간이 남아 있지 않으므로 바로 종료
        _sub_finish(db, sub, user)
        return {"message": "구독이 해지되었습니다.", "subscription": None}
    _sub_revoke_schedules(sub)            # 다음 달 예약 결제 취소 (빌링키는 이용기간 종료 때까지 보관 — 해지 취소 가능)
    sub.status = "cancel_scheduled"
    sub.cancel_requested_at = datetime.utcnow()
    sub.next_payment_id = ""
    db.commit()
    return {"message": "해지가 예약되었습니다. 이용기간 종료일까지 계속 사용할 수 있고, 이후 결제되지 않습니다.", "subscription": _sub_view(sub)}

@app.post("/subscription/resume")
def subscription_resume(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    sub = db.query(models.Subscription).filter(models.Subscription.user_id == user.id).first()
    if not sub or sub.status != "cancel_scheduled":
        raise HTTPException(status_code=400, detail="해지 예약된 구독이 없습니다.")
    if not sub.period_end or datetime.utcnow() >= sub.period_end or not sub.billing_key:
        raise HTTPException(status_code=400, detail="이용기간이 이미 끝나 해지를 취소할 수 없습니다. 새로 구독해주세요.")
    sub.status = "active"
    sub.cancel_requested_at = None
    sub.next_payment_id = _sub_schedule(sub, user, sub.period_end)
    db.commit()
    return {"message": "해지 예약을 취소했습니다. 구독이 계속됩니다.", "subscription": _sub_view(sub)}

# ── 관리자 ──────────────────────────────────────────────────────
class AdminEmailBody(BaseModel):
    email: str

@app.post("/admin/subscription/resume")
def admin_resume_subscription(body: AdminEmailBody, _: bool = Depends(check_admin), db: Session = Depends(get_db)):
    """관리자: 해지 예약된 결제 구독을 다시 이어가기 (다음 결제일에 자동결제 재예약)"""
    owner = db.query(models.User).filter(_email_eq(body.email)).first()
    sub = db.query(models.Subscription).filter(models.Subscription.user_id == owner.id).first() if owner else None
    if not sub or sub.status != "cancel_scheduled":
        raise HTTPException(status_code=400, detail="해지 예약된 구독이 없습니다.")
    if not sub.period_end or datetime.utcnow() >= sub.period_end or not sub.billing_key:
        raise HTTPException(status_code=400, detail="이용기간이 이미 끝나 해지를 취소할 수 없습니다.")
    sub.status = "active"
    sub.cancel_requested_at = None
    sub.next_payment_id = _sub_schedule(sub, owner, sub.period_end)
    db.commit()
    return {"email": owner.email, "status": sub.status}

@app.post("/admin/subscriptions/sync")
def admin_sync_subscriptions(_: bool = Depends(check_admin), db: Session = Depends(get_db)):
    """이용기간이 지난 모든 구독의 갱신을 확인한다. 외부 cron이 하루 1번 호출하도록 설정하면 안전망이 된다."""
    now = datetime.utcnow()
    subs = db.query(models.Subscription).filter(
        models.Subscription.status.in_(SUB_LIVE_STATUSES), models.Subscription.period_end <= now
    ).all()
    before = {s.id: (s.status, s.period_end) for s in subs}
    for s in subs:
        sync_subscription(db, s, force=True)
    changed = sum(1 for s in subs if (s.status, s.period_end) != before[s.id])
    return {"checked": len(subs), "changed": changed}

@app.get("/admin/subscriptions")
def admin_list_subscriptions(_: bool = Depends(check_admin), db: Session = Depends(get_db)):
    rows = []
    for s in db.query(models.Subscription).order_by(models.Subscription.created_at.desc()).all():
        u = db.query(models.User).filter(models.User.id == s.user_id).first()
        v = _sub_view(s)
        v.update({"email": u.email if u else "(삭제됨)", "company_name": u.company_name if u else ""})
        rows.append(v)
    return {"count": len(rows), "subscriptions": rows}

class AdminSubCancelBody(BaseModel):
    email: str
    immediate: bool = False   # True면 즉시 종료, False면 이용기간 종료 시 해지

@app.post("/admin/subscription/cancel")
def admin_cancel_subscription(body: AdminSubCancelBody, _: bool = Depends(check_admin), db: Session = Depends(get_db)):
    from sqlalchemy import func
    owner = db.query(models.User).filter(func.lower(models.User.email) == _norm_email(body.email)).first()
    sub = db.query(models.Subscription).filter(models.Subscription.user_id == owner.id).first() if owner else None
    if not sub or sub.status not in SUB_LIVE_STATUSES:
        raise HTTPException(status_code=404, detail="해지할 구독이 없습니다.")
    if body.immediate or sub.status == "past_due":
        _sub_finish(db, sub, owner)
    elif sub.status != "cancel_scheduled":
        _sub_revoke_schedules(sub)
        sub.status = "cancel_scheduled"
        sub.cancel_requested_at = datetime.utcnow()
        sub.next_payment_id = ""
        db.commit()
    return {"email": owner.email, "status": sub.status}
