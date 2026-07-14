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
    if "users" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("users")}
    required_columns = {
        "company_name": "VARCHAR DEFAULT ''",
        "rep_name": "VARCHAR DEFAULT ''",
        "phone": "VARCHAR DEFAULT ''",
    }
    with engine.connect() as conn:
        for col_name, col_def in required_columns.items():
            if col_name not in existing_columns:
                try:
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}"))
                    conn.commit()
                except Exception as e:
                    print(f"마이그레이션 오류 ({col_name}): {e}")

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
PORTONE_API_BASE   = "https://api.portone.io"
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")       # Google AI Studio > API 키
COST_PER_ANALYSIS = 10  # 분석 1회당 차감 크레딧

CREDIT_PACKAGES = {
    "basic":    {"price": 1000,  "credits": 10,  "label": "기본 (1회)"},
    "standard": {"price": 10000, "credits": 110, "label": "스탠다드 (11회)"},
    "premium":  {"price": 30000, "credits": 360, "label": "프리미엄 (36회)"},
    "vip":      {"price": 50000, "credits": 650, "label": "VIP (65회)"},
}


# ── 모델 ─────────────────────────────────────────────
class UserCreate(BaseModel):
    email: str
    password: str
    company_name: str = ""
    rep_name: str = ""
    phone: str = ""

class UserLogin(BaseModel):
    email: str
    password: str

class PaymentRequest(BaseModel):
    payment_id: str   # 포트원 V2의 paymentId (프론트에서 전달)
    package_id: str
    amount: int

class AnalysisRequest(BaseModel):
    image_base64: str
    image_mime: str = "image/jpeg"

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

@app.get("/")
def root():
    return {"status": "ok", "service": "FinAnalyzer API"}

@app.get("/packages")
def list_packages():
    return CREDIT_PACKAGES

# 회원가입
@app.post("/auth/register")
def register(body: UserCreate, db: Session = Depends(get_db)):
    existing = db.query(models.User).filter(models.User.email == body.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="이미 사용 중인 이메일입니다.")
    import hashlib
    pw_hash = hashlib.sha256(body.password.encode()).hexdigest()
    token = str(uuid.uuid4())
    user = models.User(
        email=body.email, password_hash=pw_hash, token=token, credits=1000,
        company_name=body.company_name, rep_name=body.rep_name, phone=body.phone
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"token": token, "email": user.email, "credits": user.credits, "company_name": user.company_name}

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
    return {"token": user.token, "email": user.email, "credits": user.credits, "company_name": user.company_name}

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
        "phone": user.phone,
        "credits": user.credits
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
    if paid_amount != pkg["price"]:
        # 금액 불일치 → 포트원에 환불 요청 후 거부 (보안)
        raise HTTPException(status_code=400, detail=f"결제 금액 불일치 (요청: {pkg['price']}원, 실제: {paid_amount}원)")

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
    db.commit()
    db.refresh(user)

    return {"success": True, "credits_added": credits_to_add, "total_credits": user.credits}

# 재무제표 분석
@app.post("/analyze")
async def analyze(
    body: AnalysisRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.credits < COST_PER_ANALYSIS:
        raise HTTPException(status_code=402, detail=f"크레딧이 부족합니다. 현재 {user.credits}크레딧 (필요: {COST_PER_ANALYSIS}크레딧)")

    prompt = """이 문서는 한국 기업의 재무제표입니다(이미지 또는 PDF). 아래 항목들을 찾아 추출하세요.

추출 항목: company_name(회사명, 문자열), revenue(매출액), current_assets(유동자산), noncurrent_assets(비유동자산), current_liabilities(유동부채),
noncurrent_liabilities(비유동부채), capital_stock(자본금), total_equity(자본총계),
operating_income(영업이익), interest_expense(이자비용), net_income(당기순이익)

아래 JSON 형식으로만 응답하세요. 없는 항목은 null:
{"company_name":문자열또는null,"revenue":숫자또는null,"current_assets":숫자또는null,"noncurrent_assets":숫자또는null,"current_liabilities":숫자또는null,
"noncurrent_liabilities":숫자또는null,"capital_stock":숫자또는null,"total_equity":숫자또는null,
"operating_income":숫자또는null,"interest_expense":숫자또는null,"net_income":숫자또는null,
"comment":"인식 관련 메모 1~2문장"}"""

    # PDF/이미지 모두 지원
    import json, re
    mime = body.image_mime if body.image_mime else "image/jpeg"

    # 폴백 모델 순서 (1차 → 2차 → 3차 자동 전환)
    GEMINI_MODELS = [
        "gemini-2.5-flash-lite-preview-06-17",  # 1차: 가장 가볍고 빠름
        "gemini-2.5-flash",                      # 2차: 표준 Flash
        "gemini-2.5-flash-preview-05-20",        # 3차: 미리보기 버전
    ]

    resp = None
    last_error = None

    async with httpx.AsyncClient(timeout=60) as client:
        for model in GEMINI_MODELS:
            try:
                resp = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}",
                    headers={"Content-Type": "application/json"},
                    json={
                        "contents": [{
                            "parts": [
                                {"inline_data": {"mime_type": mime, "data": body.image_base64}},
                                {"text": prompt}
                            ]
                        }],
                        "generationConfig": {"temperature": 0, "maxOutputTokens": 4000}
                    }
                )
                # 503(과부하) 또는 429(한도초과)면 다음 모델로 전환
                if resp.status_code in (503, 429):
                    last_error = resp.text
                    resp = None
                    continue
                # 그 외 오류도 다음 모델 시도
                if resp.status_code != 200:
                    last_error = resp.text
                    resp = None
                    continue

                # 응답이 JSON으로 정상 파싱되는지 여기서 바로 검증 (실패 시 다음 모델로 폴백)
                try:
                    candidate_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                    candidate_clean = re.sub(r"```json|```", "", candidate_text).strip()
                    # 첫 '{' 부터 마지막 '}' 까지만 추출 (불필요한 앞뒤 텍스트 제거)
                    start = candidate_clean.find("{")
                    end = candidate_clean.rfind("}")
                    if start == -1 or end == -1:
                        raise ValueError("JSON 객체를 찾을 수 없음")
                    candidate_json = candidate_clean[start:end + 1]
                    data = json.loads(candidate_json)
                except Exception as parse_err:
                    last_error = f"JSON 파싱 실패: {parse_err}"
                    resp = None
                    continue
                # 성공 시 루프 탈출
                break
            except Exception as e:
                last_error = str(e)
                resp = None
                continue

    if resp is None:
        raise HTTPException(status_code=500, detail=f"AI 분석 서버가 일시적으로 불안정합니다. 잠시 후 다시 시도해주세요. ({last_error[:150] if last_error else ''})")

    # 크레딧 차감
    user.credits -= COST_PER_ANALYSIS
    log = models.AnalysisLog(user_id=user.id, credits_used=COST_PER_ANALYSIS)
    db.add(log)
    db.commit()

    return {"data": data, "credits_used": COST_PER_ANALYSIS, "remaining_credits": user.credits}
