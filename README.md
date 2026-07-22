# FinAnalyzer — 프로젝트 현재 상태 (최종본)

씨앤지파트너스의 AI 재무제표 분석 서비스. 이 문서는 다른 채팅에서 이어서 작업할 때
현재 상태를 빠르게 파악하기 위한 요약입니다.

---

## 1. 배포 구조

| 구성요소 | 위치 | 주소 |
|---|---|---|
| 프론트엔드 | GitHub Pages | `https://sungkyunga2-rgb.github.io/analyze/` |
| 백엔드 | Render Web Service (`analyze-1`) | `https://analyze-1-250m.onrender.com` |
| DB | Render PostgreSQL (`finanalyzer-db`) | Render 환경변수 `DATABASE_URL`로 연결 |
| GitHub 저장소 | `github.com/sungkyunga2-rgb/analyze` | 루트에 `backend/`, `frontend/` 폴더 구조 |

**중요**: Render에는 `analyze-1` 서비스 하나만 존재해야 합니다 (과거 `analyze`라는 중복 서비스가 있었으나 삭제됨). 새로 서비스를 만들지 말고 `analyze-1`만 계속 사용하세요.

Render 배포 설정(analyze-1 → Settings):
- Root Directory: `backend`
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`

---

## 2. 환경변수 (Render `analyze-1` → Environment)

| 변수명 | 값 (예시/실제) |
|---|---|
| `DATABASE_URL` | Render가 자동 연결 (PostgreSQL) |
| `GEMINI_API_KEY` | Google AI Studio에서 발급받은 키 |
| `PORTONE_SECRET_KEY` | `test_sk_d26DlbXAaV0xQbpa7y1VqY50Q9RB` (테스트키) |
| `ADMIN_PASSWORD` | 관리자 페이지(`admin.html`) 접근 비밀번호. Render 환경변수에 직접 원하는 값으로 설정 |

### 관리자 페이지
- 경로: `admin.html` (예: `https://sungkyunga2-rgb.github.io/analyze/admin.html`)
- 이메일로 회원을 검색해서 잔여횟수(건) 조정, 결제/분석 내역 확인 가능
- Render 환경변수 `ADMIN_PASSWORD`를 설정해야 로그인 가능 (설정 안 하면 500 에러)
- `robots.txt` 등록이나 별도 색인 방지는 되어있지 않으니, 검색엔진 노출을 막으려면 URL을 외부에 공유하지 말 것

프론트엔드(`index.html`) 상단 설정값:
```js
const API_BASE = "https://analyze-1-250m.onrender.com";
const PORTONE_STORE_ID    = "store-9354e198-29ea-4866-91dc-ddecebe8661e";
const PORTONE_CHANNEL_KEY = "channel-key-6a2c4072-bda8-4975-8ff2-7fd5d1b9db29";
```

---

## 3. 완성된 기능

### 인증 / 회원
- 회원가입 시 회사명·대표자명·휴대폰번호·이메일(ID)·비밀번호 입력 (전부 필수)
- 가입 시 크레딧 1,000 자동 지급 (테스트용 보너스)
- 로그인/로그아웃, 우측 상단 "홈" / "내 정보" 버튼
- "내 정보"는 비밀번호 재확인 후 가입정보 열람 가능 (`/auth/verify` API)
- 가입 시 입력한 회사명이 "직접 입력" 화면의 회사이름 칸에 자동 반영

### 재무제표 분석
- 이미지(JPG/PNG) 및 PDF 업로드 → Gemini Vision으로 자동 인식 (모델 3중 폴백 처리)
- 직접 입력 방식: 회사이름(필수), 매출금액(필수), 부채금액(필수), 총자산(필수), 월 이자비용(선택), 월 순수익(선택)
- Gemini 추출 항목: `company_name`, `revenue`(매출액), `current_assets`, `noncurrent_assets`,
  `current_liabilities`, `noncurrent_liabilities`, `capital_stock`, `total_equity`,
  `operating_income`, `interest_expense`, `net_income`, `comment`

### 계산 지표 및 기준
| 지표 | 계산식 | 기준 |
|---|---|---|
| 부채비율 | 부채총계 ÷ 자본총계 × 100 | ~200% 양호(초록) / 200~400% 주의(노랑) / 400%↑ 위험(빨강) |
| 이자보상배수 | 영업이익 ÷ 이자비용 | 2배↑ 양호 / 1~2배 주의 / 1배 미만 위험 |
| 매출대비 대출비율 | 부채총계 ÷ 매출액 × 100 | 0~15% 적정 / 15~30% 주의 / 30%↑ 과다 |
| 자본잠식 여부 | 자본총계 vs 자본금 비교 | 정상 / 부분잠식 / 완전잠식 |
| 결손 여부 | 당기순이익 부호 | 이익 / 손실 |
| 사업 안정성 종합점수 | 100점 만점 (부채비율30+이자보상배수25+자본건전성20+수익성25) | A(80↑)/B(60↑)/C(40↑)/D |

색상 규칙: 양호=초록(#0F6E56), 주의=노랑(#B8860B), 위험=빨강(#A32D2D)

### 결제 (포트원 V2 연동)
- 4단계 이용권: 1,000원(1회) / 10,000원(11회,10%할인) / 30,000원(36회,20%할인) / 50,000원(65회,30%할인)
- 분석 1회당 10크레딧 차감 (`COST_PER_ANALYSIS = 10`)
- 결제 완료 후 서버사이드 검증(`/payments/confirm`) → 크레딧 자동 지급
- 결제 버튼 하단에 "분석 결과는 결제 후 3개월간 보관" 안내

### PDF 리포트 다운로드
- 분석 결과 화면 하단 "📥 분석 결과 다운로드 (PDF)" 버튼
- 브라우저 인쇄 다이얼로그를 통해 PDF 저장 (별도 라이브러리 없이 window.print() 사용)
- 블루-화이트 톤 전문 리포트 디자인, 1페이지에 핵심지표+진단+종합의견, 2페이지에 체크리스트+상세수치
- 회사명이 리포트 헤더에 자동 표시

### 법적 페이지
- 이용약관 / 개인정보처리방침 페이지 (씨앤지파트너스 실제 내용 반영)
- 사이트 하단 푸터에 사업자 정보 표시 (전화번호는 요청에 따라 제거됨)

---

## 4. 알려진 이슈 / 진행 중이던 문제

1. **CORS 설정**: `allow_credentials=False`로 되어있어야 정상 작동함 (True로 되돌리면 브라우저가 요청 차단함 — 재발 주의)
2. **DB 자동 마이그레이션**: `main.py` 상단에 서버 시작 시 `users` 테이블에 `company_name`/`rep_name`/`phone` 컬럼이 없으면 자동으로 `ALTER TABLE`로 추가하는 코드가 있음 (Render 무료플랜은 Shell 접근이 안 되므로 이 방식으로 우회)
3. **Gemini 모델 폴백**: 무료 API가 종종 503/429/JSON파싱실패를 일으켜서, 3개 모델(`gemini-2.5-flash-lite-preview-06-17` → `gemini-2.5-flash` → `gemini-2.5-flash-preview-05-20`)을 순서대로 자동 시도하도록 구현되어 있음. JSON 파싱 시 첫 `{`~마지막 `}` 추출 방식으로 안정성 강화, `maxOutputTokens`는 4000으로 설정됨.
4. 매출대비 대출비율 카드 우측 안내문구 제거 건: **해결 완료로 확인됨.**

5. **PDF 리포트 표지 페이지 추가 완료**: 리포트 최상단(1페이지)에 화이트 배경 + 블루 포인트 톤의 표지 페이지 신설. 업체명/대표자명/사업자번호/평가완료일 표시. 대표자명·사업자번호는 회원가입 정보(`business_number` 컬럼 신규 추가)에서 자동으로 채워짐. 회원가입 폼에 "사업자등록번호" 필수 입력란 추가됨(`reg-biz`). 기존 가입자는 사업자번호가 빈 값이므로 안내 필요.

---

## 5. 배포 방법 (파일 업로드 시)

1. GitHub `analyze` 레포 접속 → `Add file` → `Upload files`
2. `backend/` 안의 4개 파일(`main.py`, `models.py`, `database.py`, `requirements.txt`)을 **backend 폴더 안에** 업로드
3. `frontend/index.html`을 **frontend 폴더 안에** 업로드
4. Commit 하면 Render가 자동으로 `analyze-1` 재배포
5. 재배포 완료(Events에서 "Deploy live" 확인) 후 사이트에서 강력 새로고침(Ctrl+Shift+R) 하여 확인

---

## 6. 파일 구성

```
finanalyzer_complete/
├── README.md            ← 이 문서
├── backend/
│   ├── main.py           ← FastAPI 서버 (인증/결제/AI분석/자동마이그레이션)
│   ├── models.py         ← DB 테이블 정의 (User/Payment/AnalysisLog)
│   ├── database.py       ← DB 연결 설정
│   └── requirements.txt  ← Python 패키지 목록
└── frontend/
    └── index.html        ← 전체 웹사이트 (랜딩/가입/로그인/대시보드/결제/내정보/약관)
```
