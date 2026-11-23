"""회귀 테스트 — 실제로 문제가 났던 사례(익명화)를 다시 돌려 서버 후처리·한도 계산이 기대값과 같은지 확인.
사용법: 저장소 폴더에서  python tests/run_regression.py
(AI 호출은 하지 않는다. AI가 돌려준 값을 사례 파일에 넣어 두고 그 뒤 처리만 검사한다)"""
import os, sys, json, copy, tempfile, datetime as D

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "regression.db"))
import main  # noqa: E402

cases = json.load(open(os.path.join(HERE, "regression_cases.json"), encoding="utf-8"))
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  → {detail}"))
    if not cond:
        fails.append(name)


def check_fields(name, out, exp):
    for k, v in exp.items():
        if k == "checks_fields":
            got = sorted({c["field"] for c in out.get("checks", [])})
            check(f"{name} · 점검 항목", got == sorted(v), f"기대 {v}, 결과 {got}")
        else:
            check(f"{name} · {k}", out.get(k) == v, f"기대 {v}, 결과 {out.get(k)}")


for c in cases["financial"]:
    d = copy.deepcopy(c["ai"])
    main._fin_biz_info(d)
    check_fields("재무제표: " + c["name"], d, c["expect"])

for c in cases["income_doc"]:
    d = main._income_doc_post(copy.deepcopy(c["ai"]))
    check_fields("사업자등록증: " + c["name"], d, c["expect"])

for c in cases["members"]:
    rows = [{"birth": r[0], "acq": r[1], "loss": r[2], "pay": r[3]} for r in c["rows"]]
    emp, diff, avg = main._ht_members_count(rows, D.date.fromisoformat(c["today"]), c.get("founded"), c.get("periods"))
    check_fields("명부: " + c["name"], {"employees": emp, "emp_increase": diff, "emp_avg": avg}, c["expect"])

limit_inputs = []
for c in cases["limit"]:
    inp = copy.deepcopy(limit_inputs[c["input_from"]] if "input_from" in c else c["input"])
    for k, v in (c.get("patch") or {}).items():
        inp[k] = v
    limit_inputs.append(inp)
    L = main.ht_estimate_limit(copy.deepcopy(inp), D.date.fromisoformat(c["today"]) if c.get("today") else None)
    name = "한도: " + c["name"]
    if "status" in c["expect"]:
        check(f"{name} · 상태", L.get("status") == c["expect"]["status"], f"기대 {c['expect']['status']}, 결과 {L.get('status')}")
    orgs = {o["key"]: o for o in L.get("orgs", [])}
    for key, e in c["expect"].get("orgs", {}).items():
        o = orgs.get(key) or {}
        for f, v in e.items():
            if f == "funds_contains":
                check(f"{name} · {key} 자금", any(v in x for x in (o.get("funds") or [])), f"기대 '{v}' 포함, 결과 {o.get('funds')}")
            else:
                check(f"{name} · {key} {f}", o.get(f) == v, f"기대 {v}, 결과 {o.get(f)}")

for c in cases.get("closure", []):
    r = main._closure_restart(c["data"], c["founded"], c["biz"])
    check("재창업: " + c["name"], r == c["expect"], f"기대 {c['expect']}, 결과 {r}")

print()
print(f"실패 {len(fails)}건" if fails else "모두 통과")
sys.exit(1 if fails else 0)
