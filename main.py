from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import httpx
import os
import uuid
from datetime import datetime
from database import get_db, engine
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

CREDIT_PACKAGES = {
    "single":   {"price": 13900, "credits": 10,  "label": "1건"},
    "standard": {"price": 59075, "credits": 50,  "label": "5건 (15% 할인)"},
    "mega":     {"price": 97300, "credits": 100, "label": "10건 (30% 할인)"},
}


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
    existing = db.query(models.User).filter(models.User.email == body.email).first()
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
        models.User.email == body.email,
        models.User.password_hash == pw_hash
    ).first()
    if not user:
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 틀렸습니다.")
    return {
        "token": user.token, "email": user.email, "credits": user.credits,
        "company_name": user.company_name, "rep_name": user.rep_name, "nickname": user.nickname,
        "business_number": user.business_number
    }

# 내 크레딧 조회
@app.get("/me")
def me(user: models.User = Depends(get_current_user)):
    return {"email": user.email, "credits": user.credits}

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
    db.query(models.Payment).filter(models.Payment.user_id == user.id).delete()
    db.query(models.AnalysisLog).filter(models.AnalysisLog.user_id == user.id).delete()
    db.query(models.AnalysisHistory).filter(models.AnalysisHistory.user_id == user.id).delete()
    db.query(models.PromoUse).filter(models.PromoUse.user_id == user.id).delete()
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
    if user.credits < COST_PER_ANALYSIS:
        raise HTTPException(status_code=402, detail=f"크레딧이 부족합니다. 현재 {user.credits}크레딧 (필요: {COST_PER_ANALYSIS}크레딧)")

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

추출 항목: company_name(회사명, 문자열), rep_name(대표자명, 문자열), business_number(사업자등록번호, 문자열, 000-00-00000 형식),
revenue(매출액), current_assets(유동자산), noncurrent_assets(비유동자산), current_liabilities(유동부채),
noncurrent_liabilities(비유동부채), capital_stock(자본금), total_equity(자본총계),
operating_income(영업이익), interest_expense(이자비용), net_income(당기순이익)

대표자명과 사업자등록번호는 재무제표 표지, 법인/개인 정보란, 사업자등록증 첨부 등에서 찾을 수 있습니다. 문서에 없으면 null로 두세요.

아래 JSON 형식으로만 응답하세요. 없는 항목은 null:
{"company_name":문자열또는null,"rep_name":문자열또는null,"business_number":문자열또는null,"revenue":숫자또는null,"current_assets":숫자또는null,"noncurrent_assets":숫자또는null,"current_liabilities":숫자또는null,
"noncurrent_liabilities":숫자또는null,"capital_stock":숫자또는null,"total_equity":숫자또는null,
"operating_income":숫자또는null,"interest_expense":숫자또는null,"net_income":숫자또는null,
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

    # 크레딧 차감
    user.credits -= COST_PER_ANALYSIS
    log = models.AnalysisLog(user_id=user.id, credits_used=COST_PER_ANALYSIS)
    db.add(log)
    db.commit()

    return {"data": data, "credits_used": COST_PER_ANALYSIS, "remaining_credits": user.credits}

# 직접 입력(수동) 분석 — AI 호출 없이 프론트에서 계산하지만, 크레딧 차감은 서버에서 검증
@app.post("/analyze/manual")
def analyze_manual(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.credits < COST_PER_ANALYSIS:
        raise HTTPException(status_code=402, detail=f"크레딧이 부족합니다. 현재 {user.credits}크레딧 (필요: {COST_PER_ANALYSIS}크레딧)")
    user.credits -= COST_PER_ANALYSIS
    log = models.AnalysisLog(user_id=user.id, credits_used=COST_PER_ANALYSIS)
    db.add(log)
    db.commit()
    return {"credits_used": COST_PER_ANALYSIS, "remaining_credits": user.credits}

# 부가세과세표준증명원 / 소득금액증명원 등 국세청 서류에서 매출·소득 자동 인식
# (재무제표가 없는 개인사업자용 "직접 입력" 보조 기능 — 크레딧 차감 없음, 대출/자산은 서류에 없어 여전히 직접 입력 필요)
class IncomeDocExtractRequest(BaseModel):
    images: list[ImageItem] = []

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

    prompt = """이 문서는 한국 국세청 홈택스에서 발급한 "부가가치세과세표준증명원" 또는 "소득금액증명원" 중 하나입니다 (여러 장이 첨부된 경우 같은 서류의 여러 페이지이거나, 두 서류가 함께 첨부되었을 수 있습니다). 첨부된 모든 이미지를 함께 확인해 아래 항목을 추출하세요.

━━━ 1단계: 상호 / 대표자명 / 사업자등록번호 (반드시 먼저, 빠뜨리지 말 것) ━━━
두 서류 모두 문서 상단(제목 바로 아래)에 납세자·사업자 정보가 표 또는 항목 형태로 반드시 인쇄되어 있습니다. 금액을 읽기 전에 이 부분을 먼저 정확히 읽으세요.
- 라벨 표기는 서류 종류·발급 연도에 따라 다양합니다. 아래 중 어떤 표기든 같은 항목으로 취급하세요.
  · 상호(company_name): "상호", "상 호", "상호(법인명)", "법인명", "사업장명", "업체명", "상호명"
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

[소득금액증명원]
- 상단에 상호 또는 성명(대표자명), 사업자등록번호가 표기됩니다.
- 표에는 귀속연도별로 "소득금액"이 나열됩니다. 가장 최근 귀속연도의 소득금액을 income_amount로 반환하세요.

두 서류 모두 대출·부채·자산 정보는 포함하지 않으므로 추출 대상이 아닙니다.

아래 JSON 형식으로만 응답하세요. 문서에서 찾을 수 없는 항목은 null:
{"doc_type":"부가세과세표준증명원" 또는 "소득금액증명원" 또는 "불명","company_name":문자열또는null,"rep_name":문자열또는null,"business_number":문자열또는null,"revenue":숫자또는null,"income_amount":숫자또는null,"comment":"인식 관련 메모 1문장"}"""

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
        for key in ("doc_type", "company_name", "rep_name", "business_number", "comment"):
            if key in data:
                data[key] = _clean_text(data[key])
        biz = data.get("business_number")
        if biz:
            digits = re.sub(r"\D", "", biz)
            if len(digits) == 10:
                data["business_number"] = f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
            elif not digits:
                data["business_number"] = None

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
    user = db.query(models.User).filter(models.User.email == email).first()
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
    user = db.query(models.User).filter(models.User.email == body.email).first()
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
    user = db.query(models.User).filter(models.User.email == body.email).first()
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
    user = db.query(models.User).filter(models.User.email == body.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="해당 이메일의 회원을 찾을 수 없습니다.")

    if body.new_email:
        new_email = body.new_email.strip().lower()
        if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", new_email):
            raise HTTPException(status_code=400, detail="올바른 이메일 형식이 아닙니다.")
        if new_email != user.email:
            existing = db.query(models.User).filter(models.User.email == new_email).first()
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
    user = db.query(models.User).filter(models.User.email == body.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="해당 이메일의 회원을 찾을 수 없습니다.")
    db.query(models.Payment).filter(models.Payment.user_id == user.id).delete()
    db.query(models.AnalysisLog).filter(models.AnalysisLog.user_id == user.id).delete()
    db.query(models.RefundRequest).filter(models.RefundRequest.user_id == user.id).delete()
    db.delete(user)
    db.commit()
    return {"message": f"{body.email} 회원이 탈퇴 처리되었습니다."}


@app.get("/admin/refund-requests")
def admin_list_refund_requests(_: bool = Depends(check_admin), db: Session = Depends(get_db)):
    reqs = db.query(models.RefundRequest).order_by(models.RefundRequest.created_at.desc()).all()
    result = []
    for r in reqs:
        user = db.query(models.User).filter(models.User.id == r.user_id).first()
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
            "order_id": r.order_id or "",
            "refunded_amount": r.refunded_amount or 0,
            "credits_deducted": r.credits_deducted or 0,
            # 취소 가능한 결제 내역 (환불 처리 시 선택)
            "payments": ([
                {
                    "order_id": p.order_id,
                    "amount": p.amount or 0,
                    "cancelled_amount": p.cancelled_amount or 0,
                    "cancellable": max(0, (p.amount or 0) - (p.cancelled_amount or 0)),
                    "credits": p.credits or 0,
                    "package_id": p.package_id,
                    "package_label": (CREDIT_PACKAGES.get(p.package_id) or {}).get("label", p.package_id),
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                }
                for p in db.query(models.Payment)
                            .filter(models.Payment.user_id == r.user_id)
                            .order_by(models.Payment.created_at.desc()).limit(10).all()
            ] if user else []),
        })
    return {"count": len(result), "requests": result}

class AdminResolveRefundBody(BaseModel):
    status: str  # processed / rejected
    admin_note: str = ""

@app.post("/admin/refund-requests/{req_id}/resolve")
def admin_resolve_refund(req_id: int, body: AdminResolveRefundBody, _: bool = Depends(check_admin), db: Session = Depends(get_db)):
    if body.status not in ("processed", "rejected"):
        raise HTTPException(status_code=400, detail="status는 processed 또는 rejected여야 합니다.")
    r = db.query(models.RefundRequest).filter(models.RefundRequest.id == req_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="해당 환불 신청을 찾을 수 없습니다.")
    r.status = body.status
    r.admin_note = body.admin_note.strip()
    r.processed_at = datetime.utcnow()
    db.commit()
    return {"id": r.id, "status": r.status}


class AdminRefundBody(BaseModel):
    order_id: str                       # 취소할 결제건 (payments.order_id)
    amount: Optional[int] = None        # 부분 취소 금액 (비우면 취소 가능한 전액)
    reason: str = "고객 환불 요청"
    deduct_credits: Optional[int] = None  # 회수할 크레딧 (비우면 환불 비율만큼 자동 계산)
    admin_note: str = ""


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
    if r.status == "processed":
        raise HTTPException(status_code=400, detail="이미 환불 처리된 신청입니다.")

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
        detail = resp.text[:300]
        raise HTTPException(status_code=400, detail=f"포트원 결제 취소 실패 (status={resp.status_code}) {detail}")

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
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="해당 이메일로 가입된 계정을 찾을 수 없습니다.")

    alphabet = string.ascii_letters + string.digits
    temp_password = "".join(secrets.choice(alphabet) for _ in range(10))
    user.password_hash = hashlib.sha256(temp_password.encode()).hexdigest()
    db.commit()

    send_temp_password_email(user.email, temp_password)
    return {"message": "임시비밀번호가 이메일로 발송되었습니다."}
