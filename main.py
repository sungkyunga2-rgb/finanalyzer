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

app = FastAPI(title="FinAnalyzer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 배포 시 실제 도메인으로 변경
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TOSS_SECRET_KEY = os.getenv("TOSS_SECRET_KEY", "test_sk_YOUR_KEY_HERE")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
COST_PER_ANALYSIS = 10  # 분석 1회당 차감 크레딧

CREDIT_PACKAGES = {
    "basic":    {"price": 1000,  "credits": 100,  "label": "기본 (100크레딧)"},
    "standard": {"price": 5000,  "credits": 550,  "label": "표준 (550크레딧)"},
    "premium":  {"price": 10000, "credits": 1200, "label": "프리미엄 (1200크레딧)"},
}


# ── 모델 ─────────────────────────────────────────────
class UserCreate(BaseModel):
    email: str
    password: str

class UserLogin(BaseModel):
    email: str
    password: str

class PaymentRequest(BaseModel):
    package_id: str
    payment_key: str
    order_id: str
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
    user = models.User(email=body.email, password_hash=pw_hash, token=token, credits=0)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"token": token, "email": user.email, "credits": user.credits}

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
    return {"token": user.token, "email": user.email, "credits": user.credits}

# 내 크레딧 조회
@app.get("/me")
def me(user: models.User = Depends(get_current_user)):
    return {"email": user.email, "credits": user.credits}

# 결제 승인 (토스페이먼츠)
@app.post("/payments/confirm")
async def confirm_payment(
    body: PaymentRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    pkg = CREDIT_PACKAGES.get(body.package_id)
    if not pkg:
        raise HTTPException(status_code=400, detail="유효하지 않은 패키지입니다.")
    if pkg["price"] != body.amount:
        raise HTTPException(status_code=400, detail="결제 금액이 일치하지 않습니다.")

    # 토스페이먼츠 최종 승인 요청
    import base64
    auth = base64.b64encode(f"{TOSS_SECRET_KEY}:".encode()).decode()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.tosspayments.com/v1/payments/confirm",
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
            json={"paymentKey": body.payment_key, "orderId": body.order_id, "amount": body.amount}
        )

    if resp.status_code != 200:
        detail = resp.json().get("message", "결제 승인 실패")
        raise HTTPException(status_code=400, detail=detail)

    # 크레딧 지급
    credits_to_add = pkg["credits"]
    user.credits += credits_to_add
    payment = models.Payment(
        user_id=user.id,
        order_id=body.order_id,
        payment_key=body.payment_key,
        amount=body.amount,
        credits=credits_to_add,
        package_id=body.package_id,
    )
    db.add(payment)
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

    prompt = """이 이미지는 한국 기업의 재무제표입니다. 아래 항목들을 찾아 숫자(원 단위 정수)로 추출하세요.

추출 항목: current_assets(유동자산), noncurrent_assets(비유동자산), current_liabilities(유동부채),
noncurrent_liabilities(비유동부채), capital_stock(자본금), total_equity(자본총계),
operating_income(영업이익), interest_expense(이자비용), net_income(당기순이익)

아래 JSON 형식으로만 응답하세요. 없는 항목은 null:
{"current_assets":숫자또는null,"noncurrent_assets":숫자또는null,"current_liabilities":숫자또는null,
"noncurrent_liabilities":숫자또는null,"capital_stock":숫자또는null,"total_equity":숫자또는null,
"operating_income":숫자또는null,"interest_expense":숫자또는null,"net_income":숫자또는null,
"comment":"인식 관련 메모 1~2문장"}"""

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1000,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": body.image_mime, "data": body.image_base64}},
                        {"type": "text", "text": prompt}
                    ]
                }]
            }
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=500, detail="AI 분석 중 오류가 발생했습니다.")

    import json, re
    text = resp.json()["content"][0]["text"]
    clean = re.sub(r"```json|```", "", text).strip()
    data = json.loads(clean)

    # 크레딧 차감
    user.credits -= COST_PER_ANALYSIS
    log = models.AnalysisLog(user_id=user.id, credits_used=COST_PER_ANALYSIS)
    db.add(log)
    db.commit()

    return {"data": data, "credits_used": COST_PER_ANALYSIS, "remaining_credits": user.credits}
