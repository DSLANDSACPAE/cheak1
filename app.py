# -*- coding: utf-8 -*-
"""
대지안의 조경 기준 조회 프로그램 (모바일 웹앱 / exe / 클라우드 겸용)
- 파싱 예외 처리 강화: 조례 및 고시 파싱 실패 시에도 오류 없이 기본값으로 계산 완료
- CDATA 및 특수문자 완벽 정제
- 최상단 지자체 건축 조례 우선 정렬
"""

import json
import math
import os
import re
import sys
import threading
import webbrowser
from fractions import Fraction

import requests
from flask import Flask, render_template, request, redirect, url_for

OC_KEY = "dsland"
SEARCH_URL = "http://www.law.go.kr/DRF/lawSearch.do"
SERVICE_URL = "http://www.law.go.kr/DRF/lawService.do"

DATA_DIR = os.path.join(os.path.expanduser("~"), ".landscape_app")
os.makedirs(DATA_DIR, exist_ok=True)
FAVORITES_FILE = os.path.join(DATA_DIR, "favorites.json")


def resource_path(relative_path: str) -> str:
    """개발 환경 및 PyInstaller exe 빌드 환경 공통 경로 처리"""
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


app = Flask(
    __name__,
    template_folder=resource_path("templates"),
    static_folder=resource_path("static"),
)


# ----------------------------------------------------------------------
# 안전 유틸리티 및 변환 함수
# ----------------------------------------------------------------------

def clean_cdata(text: str) -> str:
    """XML CDATA 및 HTML/XML 태그 제거 (None 대처 가능)"""
    if not text:
        return ""
    try:
        text = re.sub(r"", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", "", text)
        return text.strip()
    except Exception:
        return str(text).strip()


def ceil1(x: float) -> float:
    try:
        return math.ceil(round(x, 4) * 10 - 1e-9) / 10
    except Exception:
        return 0.0


def fmt1(x: float) -> str:
    return f"{ceil1(x):,.1f}"


def pct_to_korean_fraction(pct: float) -> str:
    if pct is None:
        return "확인 불가"
    try:
        frac = Fraction(pct / 100).limit_denominator(20)
        return f"{frac.denominator}분의{frac.numerator}"
    except Exception:
        return f"{pct}%"


def korean_fraction_to_float(text: str):
    if not text:
        return None
    try:
        m = re.search(r"(\d+)\s*분의\s*(\d+)", text)
        if not m:
            return None
        denom, numer = int(m.group(1)), int(m.group(2))
        return numer / denom if denom != 0 else None
    except Exception:
        return None


def parse_ratio_input(text: str):
    text = (text or "").strip()
    if not text:
        return None
    frac = korean_fraction_to_float(text)
    if frac is not None:
        return frac
    if "/" in text:
        try:
            a, b = text.split("/")
            return float(a) / float(b)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        val = float(text)
        return val / 100 if val > 1 else val
    except ValueError:
        return None


def extract_tag(tag: str, block: str) -> str:
    try:
        m = re.search(rf"<{tag}[^>]*>(.*?)", block, re.DOTALL)
        if not m:
            return ""
        return clean_cdata(m.group(1))
    except Exception:
        return ""


# ----------------------------------------------------------------------
# 자치법규(조례) API 및 예외 안전 파서
# ----------------------------------------------------------------------

def count_all_ordinances(city_name: str) -> int:
    try:
        params = {"OC": OC_KEY, "target": "ordin", "type": "XML", "query": city_name, "display": 1}
        resp = requests.get(SEARCH_URL, params=params, timeout=10)
        resp.encoding = "utf-8"
        m = re.search(r"(\d+)", resp.text)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def search_building_ordinance(city_name: str):
    keyword = "건축 조례"
    city_nospace = city_name.replace(" ", "")
    keyword_nospace = keyword.replace(" ", "")

    all_laws = []
    page = 1
    total_cnt = None
    
    try:
        while True:
            params = {
                "OC": OC_KEY, "target": "ordin", "type": "XML",
                "query": f"{city_name} {keyword}", "display": 100, "page": page,
            }
            resp = requests.get(SEARCH_URL, params=params, timeout=10)
            resp.encoding = "utf-8"
            if total_cnt is None:
                m = re.search(r"(\d+)", resp.text)
                total_cnt = int(m.group(1)) if m else 0
            
            laws = re.findall(r"]*>.*?", resp.text, re.DOTALL)
            if not laws:
                break
            all_laws.extend(laws)
            if len(all_laws) >= total_cnt or len(laws) < 100 or page >= 5:
                break
            page += 1
    except Exception:
        pass

    results = []
    for law in all_laws:
        name = extract_tag("자치법규명", law)
        mst = extract_tag("자치법규일련번호", law)
        if name and mst:
            results.append((name, mst))

    def score(item):
        name_clean = clean_cdata(item[0])
        name_ns = name_clean.replace(" ", "")
        
        if name_ns == city_nospace + keyword_nospace:
            return 0
        if name_ns == city_nospace + "건축조례":
            return 1
        if name_ns.startswith(city_nospace) and keyword_nospace in name_ns:
            return 2
        return 3

    results.sort(key=score)
    return results


def fetch_ordinance_body(mst: str) -> str:
    try:
        params = {"OC": OC_KEY, "target": "ordin", "MST": mst, "type": "XML"}
        resp = requests.get(SERVICE_URL, params=params, timeout=10)
        resp.encoding = "utf-8"
        return resp.text
    except Exception as e:
        return ""


def extract_landscape_articles(full_text: str):
    if not full_text:
        return []
    try:
        articles = re.findall(r"]*>.*?", full_text, re.DOTALL)
        result = []
        for art in articles:
            if "조경" not in art and "필로티" not in art:
                continue
            title = extract_tag("조제목", art)
            content = extract_tag("조내용", art)
            content = re.sub(r"[ \t]+\n", "\n", content).strip()
            has_piloti = "필로티" in art
            result.append((title, content, has_piloti))
        return result
    except Exception:
        return []


def extract_area_tiers(content: str):
    if not content:
        return []
    tiers = []
    try:
        # 다중 정규식 검색 패턴 적용 (퍼센트, %)
        patterns = [
            r"(연면적[^:\n]{0,80}?)\s*:\s*대지면적의\s*([\d.]+)\s*퍼센트",
            r"(연면적[^:\n]{0,80}?)\s*:\s*대지면적의\s*([\d.]+)\s*%",
        ]
        for pattern in patterns:
            matches = re.findall(pattern, content)
            if matches:
                for cond, pct in matches:
                    cond_clean = re.sub(r"\s+", " ", cond).strip()
                    tiers.append((cond_clean, float(pct)))
                break
    except Exception:
        pass
    return tiers


def extract_partial_exemption_items(content: str):
    if not content:
        return []
    results = []
    try:
        items = re.split(r"\n\s*\d+\.\s*", "\n" + content)
        for item in items:
            if "한정한다" not in item and "가감" not in item:
                continue
            frac = korean_fraction_to_float(item)
            if frac is None:
                continue
            desc_m = re.match(r"([^(（]{1,40})", item.strip())
            desc = desc_m.group(1).strip() if desc_m else item.strip()[:20]
            results.append((desc, frac))
    except Exception:
        pass
    return results


def extract_article_no_from_content(content: str):
    if not content:
        return None
    m = re.match(r"\s*(제\d+조(?:의\d+)?)", content.strip())
    return m.group(1) if m else None


def get_main_landscape_article(articles):
    for title, content, _ in (articles or []):
        if "조경" in title:
            return content
    return articles[0][1] if articles else ""


# ----------------------------------------------------------------------
# 국토부 고시 및 건축법 시행령 데이터 안전 조회
# ----------------------------------------------------------------------

_CACHE = {"admrul": None, "law": {}}

def get_admrul_body():
    if _CACHE["admrul"] is not None:
        return _CACHE["admrul"], None
    try:
        params = {"OC": OC_KEY, "target": "admrul", "type": "XML", "query": "조경기준", "display": 10}
        resp = requests.get(SEARCH_URL, params=params, timeout=10)
        resp.encoding = "utf-8"
        laws = re.findall(r"]*>.*?", resp.text, re.DOTALL)
        rul_id = None
        for law in laws:
            if extract_tag("행정규칙명", law).strip() == "조경기준":
                rul_id = extract_tag("행정규칙일련번호", law)
                break
        if not rul_id:
            return None, "고시 정보 없음"
        
        body_resp = requests.get(SERVICE_URL, params={"OC": OC_KEY, "target": "admrul", "ID": rul_id, "type": "XML"}, timeout=10)
        body_resp.encoding = "utf-8"
        _CACHE["admrul"] = body_resp.text
        return _CACHE["admrul"], None
    except Exception as e:
        return None, str(e)


def get_admrul_article(article_no: int):
    body, err = get_admrul_body()
    if err or not body:
        return None, err
    try:
        blocks = re.findall(r"\s*\s*", body, re.DOTALL)
        prefix = f"제{article_no}조"
        for b in blocks:
            b_clean = clean_cdata(b)
            if b_clean.startswith(prefix):
                return b_clean, None
    except Exception:
        pass
    return None, f"제{article_no}조 미발견"


def extract_natural_ground_pct_from_ordinance(ordinance_content: str):
    if not ordinance_content or "자연지반" not in ordinance_content:
        return None
    try:
        m = re.search(r"([\d.]+)\s*(?:퍼센트|%)[^자]{0,15}자연지반", ordinance_content)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def extract_planting_pct_from_ordinance(ordinance_content: str):
    if not ordinance_content or "식재의무면적" not in ordinance_content:
        return None
    try:
        m = re.search(r"([\d.]+)\s*(?:퍼센트|%)[^식]{0,15}식재의무면적", ordinance_content)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def extract_roof_override_from_ordinance(ordinance_content: str):
    if not ordinance_content:
        return None
    try:
        idx = ordinance_content.find("옥상조경")
        if idx == -1:
            idx = ordinance_content.find("옥상 조경")
        if idx == -1:
            return None
        window = ordinance_content[max(0, idx - 30): idx + 400]
        ratio = korean_fraction_to_float(window)
        cap_pct = None
        cap_pct_m = re.search(r"([\d.]+)\s*(?:퍼센트|%)[^\.]{0,25}초과할\s*수\s*없다", window)
        if cap_pct_m:
            cap_pct = float(cap_pct_m.group(1))
        return {"ratio": ratio, "cap_pct": cap_pct, "found_mention": True}
    except Exception:
        return None


def extract_piloti_cap_from_ordinance(ordinance_content: str):
    if not ordinance_content or "필로티" not in ordinance_content:
        return None, False
    try:
        idx = ordinance_content.find("필로티")
        window = ordinance_content[max(0, idx - 30): idx + 400]
        frac = korean_fraction_to_float(window)
        if frac:
            return frac, True
        pct_m = re.search(r"([\d.]+)\s*(?:퍼센트|%)[^\.]{0,30}산입", window)
        if pct_m:
            return float(pct_m.group(1)) / 100, True
    except Exception:
        pass
    return None, True


# ----------------------------------------------------------------------
# 계산 엔진 (FallBack 안전장치 결합)
# ----------------------------------------------------------------------

def compute_defaults(main_content: str, city: str):
    main_no = extract_article_no_from_content(main_content)

    # 옥상조경 설정 예외 처리
    roof_override = extract_roof_override_from_ordinance(main_content)
    if roof_override and roof_override.get("cap_pct") is not None:
        roof_cap_pct = roof_override["cap_pct"]
        roof_source = f"{city} 조례 {main_no or ''}".strip()
    else:
        roof_cap_pct = 50.0
        roof_source = "시행령 제27조③ (기본값 50% 적용)"

    # 필로티 설정 예외 처리
    piloti_cap_ratio, piloti_found = extract_piloti_cap_from_ordinance(main_content)
    if piloti_cap_ratio is not None:
        piloti_cap_pct = piloti_cap_ratio * 100
        piloti_source = f"{city} 조례 {main_no or ''}".strip()
    else:
        piloti_cap_pct = 33.333
        piloti_source = "기본값 3분의1 적용"

    return {
        "main_no": main_no,
        "roof_default_frac": pct_to_korean_fraction(roof_cap_pct),
        "roof_ratio_frac": "2분의1",
        "roof_source": roof_source,
        "piloti_default_frac": pct_to_korean_fraction(piloti_cap_pct),
        "piloti_ratio_frac": "3분의1",
        "piloti_source": piloti_source,
    }


def run_calculation(main_content, city, site_area, tier_pct, exempt_mult,
                    roof_input, piloti_input, main_no, roof_source, piloti_source,
                    roof_ratio_frac, piloti_ratio_frac):
    try:
        ratio = tier_pct / 100
        required = site_area * ratio * exempt_mult

        roof_cap_pct = parse_ratio_input(roof_input) or 0.5
        piloti_cap_pct = parse_ratio_input(piloti_input) or (1 / 3)

        roof_max = required * roof_cap_pct
        piloti_max = required * piloti_cap_pct

        # 식재의무면적 파싱 및 Fallback 예외 대처 (기본값 50%)
        ordinance_planting_pct = extract_planting_pct_from_ordinance(main_content)
        if ordinance_planting_pct is not None:
            planting_pct = ordinance_planting_pct
            planting_source = f"{city} 조례 {main_no or ''}".strip()
        else:
            planting_pct, planting_source = 50.0, "국토부 고시 제4조 (기본값 50% 적용)"
        planting_min = required * (planting_pct / 100)

        # 자연지반 파싱 및 Fallback 예외 대처 (기본값 10%)
        ordinance_natural_pct = extract_natural_ground_pct_from_ordinance(main_content)
        if ordinance_natural_pct is not None:
            natural_pct = ordinance_natural_pct
            natural_source = f"{city} 조례 {main_no or ''}".strip()
        else:
            natural_pct, natural_source = 10.0, "국토부 고시 제5조 (기본값 10% 적용)"
        natural_min = required * (natural_pct / 100)

        rows = []
        label1 = f"① 법정조경면적 (근거: {city} 조례 {main_no or ''})"
        rows.append({"label": label1, "value": f"{fmt1(required)}㎡"})

        planting_frac = pct_to_korean_fraction(planting_pct)
        rows.append({"label": f"② 식재의무면적 (①×{planting_frac}, 근거: {planting_source})",
                     "value": f"{fmt1(planting_min)}㎡"})

        natural_frac = pct_to_korean_fraction(natural_pct)
        rows.append({"label": f"③ 자연지반 최소 면적 (①×{natural_frac}, 근거: {natural_source})",
                     "value": f"{fmt1(natural_min)}㎡"})

        roof_frac = pct_to_korean_fraction(roof_cap_pct * 100)
        piloti_frac = pct_to_korean_fraction(piloti_cap_pct * 100)

        rows.append({"label": f"④ 옥상조경최대가능면적 (적용비율 {roof_ratio_frac} · 최대산입비율 ①×{roof_frac}, 근거: {roof_source})",
                     "value": f"{fmt1(roof_max)}㎡"})
        rows.append({"label": f"⑤ 필로티최대가능면적 (적용비율 {piloti_ratio_frac} · 최대산입비율 ①×{piloti_frac}, 근거: {piloti_source})",
                     "value": f"{fmt1(piloti_max)}㎡"})

        return rows, None
    except Exception as e:
        return None, f"계산 도중 에러가 발생했습니다: {str(e)}"


# ----------------------------------------------------------------------
# Flask 웹 라우트
# ----------------------------------------------------------------------

@app.route("/")
def home():
    return render_template("home.html")


@app.route("/search")
def search():
    city = request.args.get("city", "").strip()
    if not city:
        return render_template("index.html", city="", results=None, total=None)
    try:
        total = count_all_ordinances(city)
        results = search_building_ordinance(city)
    except Exception as e:
        return render_template("index.html", city=city, results=[], total=0, error=f"검색 실패: {e}")
    return render_template("index.html", city=city, results=results, total=total)


@app.route("/calculator", methods=["GET", "POST"])
def calculator():
    city = request.values.get("city", "").strip()
    mst = request.values.get("mst", "").strip()
    if not city or not mst:
        return redirect(url_for("home"))

    full_text = fetch_ordinance_body(mst)
    articles = extract_landscape_articles(full_text)
    main_content = get_main_landscape_article(articles)
    
    tiers = extract_area_tiers(main_content)
    exempt_items = extract_partial_exemption_items(main_content)
    defaults = compute_defaults(main_content, city)

    result_rows = None
    form = {
        "site_area": "1500",
        "tier_pct": tiers[0][1] if tiers else "10",
        "exempt_mult": "1.0",
        "roof_input": defaults["roof_default_frac"],
        "piloti_input": defaults["piloti_default_frac"],
    }

    if request.method == "POST":
        form["site_area"] = request.form.get("site_area", "1500")
        form["tier_pct"] = request.form.get("tier_pct", "10")
        form["exempt_mult"] = request.form.get("exempt_mult", "1.0")
        form["roof_input"] = request.form.get("roof_input", defaults["roof_default_frac"])
        form["piloti_input"] = request.form.get("piloti_input", defaults["piloti_default_frac"])

        try:
            site_area = float(form["site_area"])
            tier_pct = float(form["tier_pct"])
            exempt_mult = float(form["exempt_mult"])
            
            result_rows, calc_err = run_calculation(
                main_content, city, site_area, tier_pct, exempt_mult,
                form["roof_input"], form["piloti_input"], defaults["main_no"],
                defaults["roof_source"], defaults["piloti_source"],
                defaults["roof_ratio_frac"], defaults["piloti_ratio_frac"],
            )
            if calc_err:
                return render_template("calculator.html", city=city, mst=mst, tiers=tiers,
                                       exempt_items=exempt_items, defaults=defaults, form=form,
                                       result_rows=None, error=calc_err)
        except ValueError:
            return render_template("calculator.html", city=city, mst=mst, tiers=tiers,
                                   exempt_items=exempt_items, defaults=defaults, form=form,
                                   result_rows=None, error="숫자 형식 입력을 확인해주세요.")

    return render_template("calculator.html", city=city, mst=mst, tiers=tiers,
                           exempt_items=exempt_items, defaults=defaults, form=form,
                           result_rows=result_rows, error=None)


@app.route("/result")
def result():
    city = request.args.get("city", "").strip()
    mst = request.args.get("mst", "").strip()
    if not city or not mst:
        return redirect(url_for("home"))

    full_text = fetch_ordinance_body(mst)
    articles = extract_landscape_articles(full_text)
    article4_text, err4 = get_admrul_article(4)
    article5_text, err5 = get_admrul_article(5)

    return render_template(
        "result.html", city=city, mst=mst, articles=articles,
        article4_text=article4_text, err4=err4,
        article5_text=article5_text, err5=err5,
    )


# ----------------------------------------------------------------------
# 실행 설정
# ----------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    is_frozen = getattr(sys, "frozen", False)
    app.run(host="0.0.0.0", port=port, debug=not is_frozen, use_reloader=False)
