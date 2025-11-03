import re, time, random
from typing import List, Tuple, Dict, Any
import requests
from bs4 import BeautifulSoup

BASE = "https://shop.tca-pictures.net"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
TAIL_SEQUENCE = [25,24,23,22,21,20,29,28,27,26]

def http_get(session: requests.Session, url: str, timeout: float) -> Tuple[int, str]:
    try:
        r = session.get(url, timeout=timeout)
        return r.status_code, r.text if r.status_code == 200 else ""
    except Exception:
        return 0, ""

def normalize_url(href: str) -> str:
    if not href: return ""
    if href.startswith("//"): return "https:" + href
    if href.startswith("/"): return BASE + href
    return href

def venue_group_from_code(code: str) -> str:
    if not code: return "UNK"
    if code.startswith("670"): return "TG(宝塚大劇場)"
    if code.startswith("671"): return "TT(東京宝塚劇場)"
    if code.startswith("673"): return "OTH1(バウ/Brillia等)"
    if code.startswith("674"): return "OTH2(フォーラム/梅田/全国ツアー等)"
    return "UNK"

def extract_title_and_image(html: str) -> Tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    h1 = soup.select_one("h1, .goodstitle, .goods_name, .txtBox h1, .txtBox h2")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(strip=True)
    elif soup.title and soup.title.text.strip():
        title = soup.title.text.strip()
    img_url = ""
    for sel in ["img#mainImg", ".mainimage img", ".goods_img img", ".goods_image img", ".imgBox img", ".photo_area img", "img[src*='/img/goods/']"]:
        img = soup.select_one(sel)
        if not img: continue
        src = img.get("src", "")
        if src.startswith("//"): img_url = "https:" + src; break
        if src.startswith("/"): img_url = BASE + src; break
        if src: img_url = src; break
    return title, img_url

def bro_guess(prefix: str, ss_min: int, ss_max: int, delay_min: float, delay_max: float, timeout: float):
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    rows = []
    for ss in range(ss_min, ss_max+1):
        matched = False
        for tail in TAIL_SEQUENCE:
            url = f"{BASE}/shop/g/g2{prefix}0{ss:02d}{tail:02d}/"
            time.sleep(random.uniform(delay_min, delay_max))
            status, html = http_get(session, url, timeout)
            if status == 200:
                title, img_url = extract_title_and_image(html)
                rows.append({"prefix": prefix, "ss": f"{ss:02d}", "tail": tail, "status": status, "title": title, "url": url, "image_url": img_url})
                matched = True
                break
        if not matched:
            rows.append({"prefix": prefix, "ss": f"{ss:02d}", "tail": None, "status": "404/NOTFOUND", "title": "", "url": "", "image_url": ""})
    return rows

def parse_category_for_items(session: requests.Session, url: str, timeout: float):
    status, html = http_get(session, url, timeout)
    if status != 200 or not html: return []
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.select("a[href]"):
        href = normalize_url(a.get("href",""))
        if re.search(r"/shop/g/g(\d{6,})/?$", href):
            links.add(href)
    return sorted(links)

def parse_program_page(html: str, url: str):
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    h1 = soup.select_one("h1, .goodstitle, .goods_name, .txtBox h1, .txtBox h2")
    if h1 and h1.get_text(strip=True): title = h1.get_text(strip=True)
    price = ""
    price_el = soup.find(string=re.compile(r"￥\\s*\\d[\\d,]*\\s*\\(税込\\)"))
    if price_el: price = price_el.strip()
    text = soup.get_text("\\n", strip=True)
    rel_date = ""
    m = re.search(r"発売日\\s*([0-9]{4}/[0-9]{1,2}/[0-9]{1,2})", text)
    if m: rel_date = m.group(1)
    m2 = re.search(r"/g/g(\\d{6,})/?$", url); code = m2.group(1) if m2 else ""
    venue_group = venue_group_from_code(code)
    return {"title": title, "price": price, "release_date": rel_date, "url": url, "code": code, "venue_group": venue_group}

def program_search(year: int, queries: List[str], delay_min: float, delay_max: float, timeout: float):
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    if year == 2025:
        cats = [f"{BASE}/shop/c/cpro2025o/", f"{BASE}/shop/c/cpro2025d/"]
    else:
        cats = [f"{BASE}/shop/c/cpro{year}o/", f"{BASE}/shop/c/cpro{year}d/"]
    rows = []
    for cat in cats:
        time.sleep(random.uniform(delay_min, delay_max))
        items = parse_category_for_items(session, cat, timeout)
        for item in items:
            time.sleep(random.uniform(delay_min, delay_max))
            status, html = http_get(session, item, timeout)
            if status != 200: continue
            info = parse_program_page(html, item)
            hay = (info["title"] + " " + info["url"]).lower()
            ok = all(q.lower() in hay for q in queries) if queries else True
            if ok: rows.append(info)
    return rows
