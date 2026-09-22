# -*- coding: utf-8 -*-
"""
대지안의 조경 기준 조회 프로그램 (모바일 웹앱 버전 / exe 실행파일 겸용 / 클라우드 배포 겸용)
- 데스크톱 app.py와 동일한 로직(API 호출, 정규식 추출)을 그대로 재사용
- 실행: python app.py  (또는 빌드된 .exe 더블클릭)
- 접속: 컴퓨터와 같은 와이파이에 연결된 폰 브라우저에서 http://<컴퓨터IP>:5000
필요 라이브러리: pip install flask requests gunicorn
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
    """
    개발 중(python app.py)과 PyInstaller로 exe 빌드된 이후 모두에서
    templates/static 폴더를 정확히 찾기 위한 경로 처리.
    """
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)

app = Flask(
    __name__,
    template_folder=resource_path("templates"),
    static_folder=resource_path("static"),
)


# ----------------------------------------------------------------------
# 공통 유틸
# ----------------------------------------------------------------------

def ceil1(x: float) -> float:
    return math.ceil(round(x, 4) * 10 - 1e-9) / 10


def fmt1(x: float) -> str:
    return f"{ceil1(x):,.1f}"


def pct_to_korean_fraction(pct: float) -> str:
    # 분모를 20까지로 제한 - 법령상 분수는 대개 1/2, 1/3, 2/3처럼 단순한 형태라
    # 소수 오차(예: 33.3 → 1000분의333)로 이상한 분수가 나오는 것을 방지
    frac = Fraction(pct / 100).limit_denominator(20)
    return f"{frac.denominator}분의{frac.numerator}"


def korean_fraction_to_float(text: str):
    m = re.search(r"(\d+)\s*분의\s*(\d+)", text)
    if not m:
        return None
    denom, numer = int(m.group(1)), int(m.group(2))
    return numer / denom if denom else None


def parse_ratio_input(text: str):
    text = (text or "").strip()
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
        return float(text) / 100
    except ValueError:
        return None


def extract_tag(tag, block):
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.DOTALL)
    if not m:
        return ""
    inner = m.group(1)
    cdata = re.search(r"<!\[cdata\[(.*?)\]\]>", inner, re.DOTALL)
    raw = cdata.group(1) if cdata else inner
    return raw.strip()


# ----------------------------------------------------------------------
# 자치법규(조례) API
# ----------------------------------------------------------------------

def count_all_ordinances(city_name: str) -> int:
    params = {"OC": OC_KEY, "target": "ordin", "type": "XML", "query": city_name, "display": 1}
    resp = requests.get(SEARCH_URL, params=params, timeout=15)
    resp.encoding = "utf-8"
    m = re.search(r"<totalcnt>(\d+)</totalcnt>", resp.text)
    return int(m.group(1)) if m else -1


def search_building_ordinance(city_name: str):
    keyword = "건축 조례"
    city_nospace = city_name.replace(" ", "")
    keyword_nospace = keyword.replace(" ", "")

    all_laws = []
    page = 1
    total_cnt = None
    while True:
        params = {
            "OC": OC_KEY, "target": "ordin", "type": "XML",
            "query": f"{city_name} {keyword}", "display": 100, "page": page,
        }
        resp = requests.get(SEARCH_URL, params=params, timeout=15)
        resp.encoding = "utf-8"
        if total_cnt is None:
            m = re.search(r"<totalcnt>(\d+)</totalcnt>", resp.text)
            total_cnt = int(m.group(1)) if m else 0
        laws = re.findall(r"<law\b[^>]*>.*?</law>", resp.text, re.DOTALL)
        if not laws:
            break
        all_laws.extend(laws)
        if len(all_laws) >= total_cnt or len(laws) < 100:
            break
        page += 1

    results = []
    for law in all_laws:
        name = extract_tag("자치법규명", law)
        mst = extract_tag("자치법규일련번호", law)
        results.append((name, mst))

    def score(item):
        name_ns = item[0].replace(" ", "")
        if name_ns == city_nospace + keyword_nospace:
            return 0
        if name_ns.startswith(city_nospace) and name_ns.endswith(keyword_nospace):
            middle = name_ns[len(city_nospace):-len(keyword_nospace)]
            if middle == "":
                return 1
        return 2

    results.sort(key=score)
    return results


def fetch_ordinance_body(mst: str) -> str:
    params = {"OC": OC_KEY, "target": "ordin", "MST": mst, "type": "XML"}
    resp = requests.get(SERVICE_URL, params=params, timeout=15)
    resp.encoding = "utf-8"
    return resp.text


def extract_landscape_articles(full_text: str):
    articles = re.findall(r"<조\s[^>]*>.*?</조>", full_text, re.DOTALL)
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


def extract_area_tiers(content: str):
    pattern = r"(연면적[^:\n]{0,80}?)\s*:\s*대지면적의\s*([\d.]+)\s*퍼센트"
    matches = re.findall(pattern, content)
    tiers = []
    for cond, pct in matches:
        cond_clean = re.sub(r"\s+", " ", cond).strip()
        tiers.append((cond_clean, float(pct)))
    return tiers


def extract_partial_exemption_items(content: str):
    items = re.split(r"\n\s*\d+\.\s*", "\n" + content)
    results = []
    for item in items:
        if "한정한다" not in item:
            continue
        frac = korean_fraction_to_float(item)
        if frac is None:
            continue
        desc_m = re.match(r"([^(（]{1,40})", item.strip())
        desc = desc_m.group(1).strip() if desc_m else item.strip()[:20]
        results.append((desc, frac))
    return results


def extract_article_no_from_content(content: str):
    if not content:
        return None
    m = re.match(r"\s*(제\d+조(?:의\d+)?)", content.strip())
    return m.group(1) if m else None


def get_main_landscape_article(articles):
    for title, content, _ in articles:
        if "조경" in title:
            return content
    return ""


# ----------------------------------------------------------------------
# 국토교통부 고시 「조경기준」 (행정규칙, target=admrul)
# ----------------------------------------------------------------------

_ADMRUL_CACHE = {"body": None, "fetched": False, "error": None}


def search_admrul_exact(keyword: str):
    params = {"OC": OC_KEY, "target": "admrul", "type": "XML", "query": keyword, "display": 20}
    resp = requests.get(SEARCH_URL, params=params, timeout=15)
    resp.encoding = "utf-8"
    laws = re.findall(r"<admrul\b[^>]*>.*?</admrul>", resp.text, re.DOTALL)
    for law in laws:
        name = extract_tag("행정규칙명", law)
        rul_id = extract_tag("행정규칙일련번호", law)
        if name.strip() == keyword:
            return name, rul_id
    return None, None


def fetch_admrul_body(rul_id: str) -> str:
    params = {"OC": OC_KEY, "target": "admrul", "ID": rul_id, "type": "XML"}
    resp = requests.get(SERVICE_URL, params=params, timeout=15)
    resp.encoding = "utf-8"
    return resp.text


def extract_admrul_article(full_text: str, article_no: int):
    blocks = re.findall(r"<조문내용>\s*<!\[cdata\[(.*?)\]\]>\s*</조문내용>", full_text, re.DOTALL)
    prefix = f"제{article_no}조"
    for b in blocks:
        if b.strip().startswith(prefix):
            return b.strip()
    return None


def get_admrul_body():
    if _ADMRUL_CACHE["fetched"]:
        return _ADMRUL_CACHE["body"], _ADMRUL_CACHE["error"]
    _ADMRUL_CACHE["fetched"] = True
    try:
        name, rul_id = search_admrul_exact("조경기준")
        if not rul_id:
            _ADMRUL_CACHE["error"] = "국토부 고시 '조경기준'을 찾지 못했습니다."
            return None, _ADMRUL_CACHE["error"]
        body = fetch_admrul_body(rul_id)
        _ADMRUL_CACHE["body"] = body
        return body, None
    except Exception as e:
        _ADMRUL_CACHE["error"] = f"국토부 고시 조회 중 오류: {e}"
        return None, _ADMRUL_CACHE["error"]


def get_admrul_article(article_no: int):
    body, err = get_admrul_body()
    if err or not body:
        return None, err
    article = extract_admrul_article(body, article_no)
    if not article:
        return None, f"제{article_no}조를 찾지 못했습니다."
    return article, None


def extract_natural_ground_pct_from_admrul(article5_text: str):
    if not article5_text:
        return None
    m = re.search(r"조경의무면적의\s*([\d.]+)\s*퍼센트\s*이상[^자]*자연지반", article5_text)
    return float(m.group(1)) if m else None


def extract_natural_ground_pct_from_ordinance(ordinance_content: str):
    if not ordinance_content or "자연지반" not in ordinance_content:
        return None
    m = re.search(r"([\d.]+)\s*퍼센트[^자]{0,10}자연지반", ordinance_content)
    return float(m.group(1)) if m else None


def extract_planting_pct_from_admrul(article4_text: str):
    if not article4_text:
        return None
    m = re.search(r"(\d+)\s*분의\s*(\d+)\s*이상[^\n]{0,20}식재의무면적", article4_text)
    if not m:
        return None
    denom, numer = int(m.group(1)), int(m.group(2))
    return numer / denom * 100 if denom else None


def extract_planting_pct_from_ordinance(ordinance_content: str):
    if not ordinance_content or "식재의무면적" not in ordinance_content:
        return None
    m = re.search(r"([\d.]+)\s*퍼센트[^식]{0,10}식재의무면적", ordinance_content)
    return float(m.group(1)) if m else None


# ----------------------------------------------------------------------
# 건축법 시행령 (법령, target=law)
# ----------------------------------------------------------------------

_LAW_CACHE = {}


def search_law_exact(keyword: str):
    params = {"OC": OC_KEY, "target": "law", "type": "XML", "query": keyword, "display": 20}
    resp = requests.get(SEARCH_URL, params=params, timeout=15)
    resp.encoding = "utf-8"
    laws = re.findall(r"<law\b[^>]*>.*?</law>", resp.text, re.DOTALL)
    for law in laws:
        name = extract_tag("법령명한글", law)
        mst = extract_tag("법령일련번호", law)
        if name.strip() == keyword:
            return name, mst
    return None, None


def fetch_law_body(mst: str) -> str:
    params = {"OC": OC_KEY, "target": "law", "MST": mst, "type": "XML"}
    resp = requests.get(SERVICE_URL, params=params, timeout=15)
    resp.encoding = "utf-8"
    return resp.text


def get_law_body(law_name: str):
    cache = _LAW_CACHE.setdefault(law_name, {"body": None, "fetched": False, "error": None})
    if cache["fetched"]:
        return cache["body"], cache["error"]
    cache["fetched"] = True
    try:
        name, mst = search_law_exact(law_name)
        if not mst:
            cache["error"] = f"'{law_name}'을 찾지 못했습니다."
            return None, cache["error"]
        body = fetch_law_body(mst)
        cache["body"] = body
        return body, None
    except Exception as e:
        cache["error"] = f"법령 조회 중 오류: {e}"
        return None, cache["error"]


def extract_law_article(full_text: str, article_no: int):
    articles = re.findall(r"<조문단위\b[^>]*>.*?</조문단위>", full_text, re.DOTALL)
    for art in articles:
        no_m = re.search(r"<조문번호>(.*?)</조문번호>", art)
        if no_m and no_m.group(1).strip() == str(article_no):
            title = extract_tag("조문제목", art)
            hangs = re.findall(r"<항내용>\s*<!\[cdata\[(.*?)\]\]>\s*</항내용>", art, re.DOTALL)
            lines = []
            for h in hangs:
                clean = re.sub(r"<[^>]+>", " ", h)
                clean = re.sub(r"\s+", " ", clean).strip()
                if clean:
                    lines.append(clean)
            return title, "\n".join(lines)
    return None, None


def get_law_article(law_name: str, article_no: int):
    body, err = get_law_body(law_name)
    if err or not body:
        return None, None, err
    title, content = extract_law_article(body, article_no)
    if content is None:
        return None, None, f"제{article_no}조를 찾지 못했습니다."
    return title, content, None


def extract_han_by_symbol(article_content: str, symbol: str):
    if not article_content:
        return None
    for line in article_content.split("\n"):
        if line.strip().startswith(symbol):
            return line.strip()
    return None


def extract_roof_rule_from_law(hang3_text: str):
    if not hang3_text:
        return None, None
    ratio_m = re.search(r"(\d+)\s*분의\s*(\d+)\s*에\s*해당하는\s*면적", hang3_text)
    ratio = None
    if ratio_m:
        denom, numer = int(ratio_m.group(1)), int(ratio_m.group(2))
        ratio = numer / denom if denom else None
    cap_m = re.search(r"(\d+)\s*분의\s*(\d+)\s*을\s*초과할\s*수\s*없다", hang3_text)
    cap_pct = None
    if cap_m:
        denom, numer = int(cap_m.group(1)), int(cap_m.group(2))
        cap_pct = numer / denom * 100 if denom else None
    return ratio, cap_pct


def extract_roof_override_from_ordinance(ordinance_content: str):
    if not ordinance_content:
        return None
    idx = ordinance_content.find("옥상조경")
    if idx == -1:
        idx = ordinance_content.find("옥상 조경")
    if idx == -1:
        return None
    window = ordinance_content[max(0, idx - 30): idx + 400]
    ratio = korean_fraction_to_float(window)
    cap_pct = None
    cap_pct_m = re.search(r"([\d.]+)\s*퍼센트[^\.]{0,20}초과할\s*수\s*없다", window)
    if cap_pct_m:
        cap_pct = float(cap_pct_m.group(1))
    else:
        cap_frac_m = re.search(r"(\d+)\s*분의\s*(\d+)[^\.]{0,20}초과할\s*수\s*없다", window)
        if cap_frac_m:
            denom, numer = int(cap_frac_m.group(1)), int(cap_frac_m.group(2))
            cap_pct = numer / denom * 100 if denom else None
    return {"ratio": ratio, "cap_pct": cap_pct, "found_mention": True}


def extract_piloti_cap_from_ordinance(ordinance_content: str):
    if not ordinance_content:
        return None, False
    idx = ordinance_content.find("필로티")
    if idx == -1:
        return None, False
    window = ordinance_content[max(0, idx - 50): idx + 500]
    cap_m = re.search(r"(\d+)\s*분의\s*(\d+)[^\.]{0,40}산입한다", window)
    if cap_m:
        denom, numer = int(cap_m.group(1)), int(cap_m.group(2))
        return (numer / denom if denom else None), True
    cap_pct_m = re.search(r"([\d.]+)\s*퍼센트[^\.]{0,40}산입한다", window)
    if cap_pct_m:
        return float(cap_pct_m.group(1)) / 100, True
    return None, True


def extract_piloti_recognition_from_ordinance(ordinance_content: str):
    if not ordinance_content:
        return None
    idx = ordinance_content.find("필로티")
    if idx == -1:
        return None
    window = ordinance_content[max(0, idx - 50): idx + 500]
    m = re.search(r"(\d+)\s*분의\s*(\d+)[^\.]{0,20}(?:인정|산정)", window)
    if m:
        denom, numer = int(m.group(1)), int(m.group(2))
        return numer / denom if denom else None
    return None


# ----------------------------------------------------------------------
# 즐겨찾기
# ----------------------------------------------------------------------

def load_favorites():
    if os.path.exists(FAVORITES_FILE):
        with open(FAVORITES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_favorites(favs):
    with open(FAVORITES_FILE, "w", encoding="utf-8") as f:
        json.dump(favs, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# 계산 로직 (조례 > 고시 > 시행령 우선순위 판정 포함)
# ----------------------------------------------------------------------

def compute_defaults(main_content: str, city: str):
    main_no = extract_article_no_from_content(main_content)

    roof_override = extract_roof_override_from_ordinance(main_content)
    _, article27_content, _ = get_law_article("건축법 시행령", 27)
    hang3 = extract_han_by_symbol(article27_content, "③") if article27_content else None
    law_ratio, law_cap = extract_roof_rule_from_law(hang3) if hang3 else (None, None)

    if roof_override and roof_override.get("cap_pct") is not None:
        roof_cap_pct = roof_override["cap_pct"]
        roof_ratio = roof_override.get("ratio")
        roof_source = f"{city} 조례 {main_no or ''}".strip()
    else:
        if law_cap is not None:
            roof_cap_pct = law_cap
            roof_ratio = law_ratio * 100 if law_ratio is not None else None
            if roof_override and roof_override.get("found_mention"):
                roof_source = "시행령 제27조③ (조례 언급있으나 수치미확인)"
            else:
                roof_source = "시행령 제27조③"
        else:
            roof_cap_pct = 50.0
            roof_ratio = None
            roof_source = "확인 불가(기본값 50% 사용)"

    piloti_cap_ratio, piloti_found = extract_piloti_cap_from_ordinance(main_content)
    piloti_recognition_ratio = extract_piloti_recognition_from_ordinance(main_content)

    if piloti_cap_ratio is not None:
        piloti_cap_pct = piloti_cap_ratio * 100
        piloti_source = f"{city} 조례 {main_no or ''}".strip()
    elif piloti_found:
        piloti_cap_pct = 33.3
        piloti_source = "조례에 언급있으나 수치미확인 - 원문 확인 필요"
    else:
        piloti_cap_pct = 33.3
        piloti_source = "확인 불가 - 원문 확인 필요"

    return {
        "main_no": main_no,
        "roof_default_frac": pct_to_korean_fraction(roof_cap_pct),
        "roof_ratio_frac": pct_to_korean_fraction(roof_ratio) if roof_ratio is not None else "확인 불가",
        "roof_source": roof_source,
        "piloti_default_frac": pct_to_korean_fraction(piloti_cap_pct),
        "piloti_ratio_frac": pct_to_korean_fraction(piloti_recognition_ratio * 100) if piloti_recognition_ratio is not None else "확인 불가",
        "piloti_source": piloti_source,
    }


def run_calculation(main_content, city, site_area, tier_pct, exempt_mult,
                     roof_input, piloti_input, main_no, roof_source, piloti_source,
                     roof_ratio_frac, piloti_ratio_frac):
    ratio = tier_pct / 100
    required = site_area * ratio * exempt_mult

    roof_cap_pct = parse_ratio_input(roof_input)
    piloti_cap_pct = parse_ratio_input(piloti_input)
    if roof_cap_pct is None or piloti_cap_pct is None:
        return None, "최대산입비율은 '2분의1' 또는 '1/2' 형식으로 입력해주세요."

    roof_max = required * roof_cap_pct
    piloti_max = required * piloti_cap_pct

    ordinance_natural_pct = extract_natural_ground_pct_from_ordinance(main_content)
    article5_text, _ = get_admrul_article(5)
    admrul_natural_pct = extract_natural_ground_pct_from_admrul(article5_text) if article5_text else None
    if ordinance_natural_pct is not None:
        natural_pct = ordinance_natural_pct
        natural_source = f"{city} 조례 {main_no or ''}".strip()
    elif admrul_natural_pct is not None:
        natural_pct, natural_source = admrul_natural_pct, "국토부 고시 제5조"
    else:
        natural_pct, natural_source = None, "확인 불가"
    natural_min = required * natural_pct / 100 if natural_pct is not None else 0

    ordinance_planting_pct = extract_planting_pct_from_ordinance(main_content)
    article4_text, _ = get_admrul_article(4)
    admrul_planting_pct = extract_planting_pct_from_admrul(article4_text) if article4_text else None
    if ordinance_planting_pct is not None:
        planting_pct = ordinance_planting_pct
        planting_source = f"{city} 조례 {main_no or ''}".strip()
    elif admrul_planting_pct is not None:
        planting_pct, planting_source = admrul_planting_pct, "국토부 고시 제4조"
    else:
        planting_pct, planting_source = None, "확인 불가"
    planting_min = required * planting_pct / 100 if planting_pct is not None else 0

    rows = []
    label1 = "① 법정조경면적"
    label1 += f" (근거: {city} 조례 {main_no})" if main_no else f" (근거: {city} 조례)"
    rows.append({"label": label1, "value": f"{fmt1(required)}㎡"})

    if planting_pct is not None:
        planting_frac = pct_to_korean_fraction(planting_pct)
        rows.append({"label": f"② 식재의무면적 (①×{planting_frac}, 근거: {planting_source})",
                     "value": f"{fmt1(planting_min)}㎡"})
    else:
        rows.append({"label": "② 식재의무면적", "value": "확인 불가 - 원문 확인 필요"})

    if natural_pct is not None:
        natural_frac = pct_to_korean_fraction(natural_pct)
        rows.append({"label": f"③ 자연지반 최소 면적 (①×{natural_frac}, 근거: {natural_source})",
                     "value": f"{fmt1(natural_min)}㎡"})
    else:
        rows.append({"label": "③ 자연지반 최소 면적", "value": "확인 불가 - 원문 확인 필요"})

    roof_frac = pct_to_korean_fraction(roof_cap_pct * 100)
    piloti_frac = pct_to_korean_fraction(piloti_cap_pct * 100)

    rows.append({"label": f"④ 옥상조경최대가능면적 (적용비율 {roof_ratio_frac} · 최대산입비율 ①×{roof_frac}, 근거: {roof_source})",
                 "value": f"{fmt1(roof_max)}㎡"})
    rows.append({"label": f"⑤ 필로티최대가능면적 (적용비율 {piloti_ratio_frac} · 최대산입비율 ①×{piloti_frac}, 근거: {piloti_source})",
                 "value": f"{fmt1(piloti_max)}㎡"})

    return rows, None


# ----------------------------------------------------------------------
# 라우트
# ----------------------------------------------------------------------

@app.route("/")
def home():
    logo_path = os.path.join(os.path.dirname(__file__), "static", "logo.png")
    logo_available = os.path.exists(logo_path)
    return render_template("home.html", logo_available=logo_available)


@app.route("/search")
def search():
    city = request.args.get("city", "").strip()
    if not city:
        return render_template("index.html", city="", results=None, total=None)
    try:
        total = count_all_ordinances(city)
        results = search_building_ordinance(city)
    except Exception as e:
        return render_template("index.html", city=city, results=[], total=None,
                                error=f"검색 중 오류: {e}")
    return render_template("index.html", city=city, results=results, total=total)


@app.route("/calculator", methods=["GET", "POST"])
def calculator():
    city = request.values.get("city", "").strip()
    mst = request.values.get("mst", "").strip()
    if not city or not mst:
        return redirect(url_for("home"))

    try:
        full_text = fetch_ordinance_body(mst)
        articles = extract_landscape_articles(full_text)
    except Exception as e:
        return render_template("calculator.html", error=f"본문 조회 오류: {e}", city=city, mst=mst)

    main_content = get_main_landscape_article(articles)
    tiers = extract_area_tiers(main_content)
    exempt_items = extract_partial_exemption_items(main_content)
    defaults = compute_defaults(main_content, city)

    result_rows = None
    form = {
        "site_area": "1500",
        "tier_pct": tiers[0][1] if tiers else "",
        "exempt_mult": "1.0",
        "roof_input": defaults["roof_default_frac"],
        "piloti_input": defaults["piloti_default_frac"],
    }

    if request.method == "POST":
        form["site_area"] = request.form.get("site_area", "1500")
        form["tier_pct"] = request.form.get("tier_pct", "")
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
                return render_template(
                    "calculator.html", city=city, mst=mst, tiers=tiers,
                    exempt_items=exempt_items, defaults=defaults, form=form,
                    result_rows=None, error=calc_err,
                )
        except ValueError:
            return render_template(
                "calculator.html", city=city, mst=mst, tiers=tiers,
                exempt_items=exempt_items, defaults=defaults, form=form,
                result_rows=None, error="숫자를 올바르게 입력해주세요.",
            )

    return render_template(
        "calculator.html", city=city, mst=mst, tiers=tiers,
        exempt_items=exempt_items, defaults=defaults, form=form,
        result_rows=result_rows, error=None,
    )


@app.route("/result")
def result():
    city = request.args.get("city", "").strip()
    mst = request.args.get("mst", "").strip()
    if not city or not mst:
        return redirect(url_for("home"))

    full_text = fetch_ordinance_body(mst)
    articles = extract_landscape_articles(full_text)
    main_content = get_main_landscape_article(articles)
    ordinance_full_content = "\n".join(c for _, c, _ in articles)

    article4_text, err4 = get_admrul_article(4)
    article5_text, err5 = get_admrul_article(5)
    _, article27_content, err27 = get_law_article("건축법 시행령", 27)
    hang3 = extract_han_by_symbol(article27_content, "③") if article27_content else None

    ordinance_has_planting = "식재의무면적" in ordinance_full_content
    ordinance_has_natural = "자연지반" in ordinance_full_content
    roof_override = extract_roof_override_from_ordinance(ordinance_full_content)

    return render_template(
        "result.html", city=city, mst=mst, articles=articles,
        article4_text=article4_text, err4=err4,
        article5_text=article5_text, err5=err5,
        hang3=hang3, err27=err27,
        ordinance_has_planting=ordinance_has_planting,
        ordinance_has_natural=ordinance_has_natural,
        roof_override=roof_override,
    )


@app.route("/favorites")
def favorites():
    favs = load_favorites()
    return render_template("favorites.html", favorites=favs)


@app.route("/favorites/add", methods=["POST"])
def favorites_add():
    city = request.form.get("city", "").strip()
    favs = load_favorites()
    if city and city not in favs:
        favs.append(city)
        save_favorites(favs)
    return redirect(url_for("favorites"))


@app.route("/favorites/remove", methods=["POST"])
def favorites_remove():
    city = request.form.get("city", "").strip()
    favs = load_favorites()
    if city in favs:
        favs.remove(city)
        save_favorites(favs)
    return redirect(url_for("favorites"))


def get_local_ip():
    """이 컴퓨터의 LAN IP 주소를 자동으로 확인 (폰 접속 안내용)"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def open_browser():
    webbrowser.open("http://127.0.0.1:5000/")


if __name__ == "__main__":
    # 클라우드 배포 환경(PORT 환경변수) 또는 기본 5000번 포트 사용
    port = int(os.environ.get("PORT", 5000))
    is_frozen = getattr(sys, "frozen", False)
    
    # 로컬 실행 환경에서만 브라우저 자동 실행 및 IP 안내
    if "PORT" not in os.environ:
        local_ip = get_local_ip()
        print("=" * 60)
        print(" 대지안의 조경기준 검토 - 서버 시작")
        print("=" * 60)
        print(f" 이 컴퓨터에서 열기      : http://127.0.0.1:{port}")
        print(f" 폰에서 접속(같은 와이파이): http://{local_ip}:{port}")
        print("=" * 60)
        threading.Timer(1.2, open_browser).start()

    app.run(host="0.0.0.0", port=port, debug=not is_frozen, use_reloader=False)