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
        "business_number": "VARCHAR DEFAULT ''",
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
ADMIN_PASSWORD     = os.getenv("ADMIN_PASSWORD", "")       # 관리자 페이지 접근 비밀번호 (Render 환경변수에 설정)
COST_PER_ANALYSIS = 10  # 분석 1회당 차감 크레딧

CREDIT_PACKAGES = {
    "single":   {"price": 9900,  "credits": 10,  "label": "1건"},
    "standard": {"price": 49500, "credits": 60,  "label": "5+1건 (총 6건)"},
    "mega":     {"price": 99000, "credits": 130, "label": "10+3건 (총 13건)"},
}


# ── 모델 ─────────────────────────────────────────────
class UserCreate(BaseModel):
    email: str
    password: str
    company_name: str = ""
    rep_name: str = ""
    phone: str = ""
    business_number: str = ""

class UserLogin(BaseModel):
    email: str
    password: str

class PaymentRequest(BaseModel):
    payment_id: str   # 포트원 V2의 paymentId (프론트에서 전달)
    package_id: str
    amount: int

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
        company_name=body.company_name, rep_name=body.rep_name, phone=body.phone,
        business_number=body.business_number
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {
        "token": token, "email": user.email, "credits": user.credits,
        "company_name": user.company_name, "rep_name": user.rep_name,
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
        "company_name": user.company_name, "rep_name": user.rep_name,
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

    # 폴백 모델 순서 (정확도 우선 1차 → 속도우선 폴백 → 미리보기)
    GEMINI_MODELS = [
        "gemini-2.5-flash",                      # 1차: 표준 Flash — 정확도 우선
        "gemini-2.5-flash-lite-preview-06-17",  # 2차: 1차 과부하/오류 시 가볍고 빠른 폴백
        "gemini-2.5-flash-preview-05-20",        # 3차: 미리보기 버전
    ]

    async def call_gemini(client: httpx.AsyncClient, prompt_text: str):
        """이미지 + 프롬프트로 Gemini를 호출하고, 모델 폴백을 거쳐 파싱된 JSON(dict)을 반환. 실패 시 (None, 에러메시지)."""
        last_err = None
        for model in GEMINI_MODELS:
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
                if resp.status_code in (503, 429) or resp.status_code != 200:
                    last_err = resp.text
                    continue
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
                    continue
            except Exception as e:
                last_err = str(e)
                continue
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

    async with httpx.AsyncClient(timeout=60) as client:
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
