from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Optional, Tuple
import logging, traceback, re
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Takarazuka Link Finder", version="3.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*", "null", "http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE = "https://shop.tca-pictures.net"
SEARCH_URL = f"{BASE}/shop/goods/search.aspx"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

TROUPE_MAP = {
    "1": {"jp": "花組", "cn": "花组", "emoji": "🌸", "color": "#ec4899"},
    "2": {"jp": "月組", "cn": "月组", "emoji": "🌙", "color": "#f59e0b"},
    "3": {"jp": "雪組", "cn": "雪组", "emoji": "❄️", "color": "#22c55e"},
    "4": {"jp": "星組", "cn": "星组", "emoji": "⭐️", "color": "#3b82f6"},
    "5": {"jp": "宙組", "cn": "宙组", "emoji": "🪐", "color": "#a855f7"},
}

# -------------------------
# HTTP helpers (IMPORTANT)
# -------------------------

def http_get(url: str, timeout: float = 15.0) -> requests.Response:
    return requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)

def image_exists(url: str, timeout: float = 12.0) -> bool:
    """
    用 Range GET 探测图片是否存在：
    - 避免 HEAD 被 403/405 的问题
    """
    try:
        headers = dict(DEFAULT_HEADERS)
        headers["Range"] = "bytes=0-0"
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
        if r.status_code not in (200, 206):
            return False
        ct = (r.headers.get("content-type") or "").lower()
        return ct.startswith("image/") or ("image" in ct)
    except Exception:
        return False

def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def extract_goods_title(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    if h1:
        t = normalize_text(h1.get_text())
        if t:
            return t
    ttag = soup.find("title")
    if ttag:
        return normalize_text(ttag.get_text())
    return ""

def extract_images_from_goods_page(html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    urls = []
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = BASE + src
        if "/img/goods/" in src and src.lower().endswith(".jpg"):
            urls.append(src)

    # unique keep order
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

# -------------------------
# Code -> prefix/date/troupe  (FIX #1)
# -------------------------

def parse_prefix_from_code(code: str) -> Optional[str]:
    """
    关键：小卡 code 是 13 位，例如 2251141100113
    正确 prefix 通常固定为 code[1:8] => 2511411 (YYMMDD + troupe_idx)
    这可以避免 regex 误匹配造成组别错乱。
    """
    code = (code or "").strip()
    if len(code) >= 8 and code[0].isdigit():
        prefix = code[1:8]
        if re.fullmatch(r"\d{7}", prefix):
            return prefix
    return None

def prefix_to_date_troupe(prefix: str) -> Tuple[Optional[str], Optional[str]]:
    """
    prefix: YYMMDD{B}
    date: 20YY-MM-DD
    troupe_idx: B
    """
    if not prefix or not re.fullmatch(r"\d{7}", prefix):
        return None, None
    yy = prefix[0:2]
    mm = prefix[2:4]
    dd = prefix[4:6]
    b = prefix[6:7]
    try:
        year = int("20" + yy)
        dt = datetime(year, int(mm), int(dd)).date().isoformat()
    except Exception:
        dt = None
    return dt, b

def build_prefix_candidates(base_date_iso: Optional[str], troupe_idx: Optional[str], days_forward: int = 160, days_backward: int = 10) -> List[Dict[str, Any]]:
    """
    新人公演等可能在“同一公演”里用另一套 prefix（通常更晚）
    所以对 base_date 做“向后扩展”，默认 160 天（可按需加大）
    """
    if not base_date_iso or not troupe_idx:
        return []
    try:
        base = datetime.fromisoformat(base_date_iso).date()
    except Exception:
        return []
    out = []
    for delta in range(-days_backward, days_forward + 1):
        d = base + timedelta(days=delta)
        yy = str(d.year)[-2:]
        mm = f"{d.month:02d}"
        dd = f"{d.day:02d}"
        prefix = f"{yy}{mm}{dd}{troupe_idx}"
        out.append({"prefix": prefix, "date": d.isoformat(), "delta_days": delta})
    return out

# -------------------------
# Probing strategies (FIX #2)
# -------------------------

def multi_probe(urls: List[str], timeout: float = 12.0, max_workers: int = 28) -> List[str]:
    if not urls:
        return []
    ok_map: Dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(image_exists, u, timeout): u for u in urls}
        for fut in as_completed(futs):
            u = futs[fut]
            try:
                ok_map[u] = bool(fut.result())
            except Exception:
                ok_map[u] = False
    return [u for u in urls if ok_map.get(u)]

def probe_sequence_S(prefix: str, max_images: int, timeout: float, start_n_hint: int = 1, miss_stop: int = 35) -> List[str]:
    """
    普通定妆/舞写：S/{prefix}-{NNN}.jpg
    - 先探测 001~005 找到实际起点（很多不是从001开始）
    - 再顺序扫描，连续 miss_stop 次 miss 结束
    """
    # find real start
    start_n = None
    for n in range(start_n_hint, min(start_n_hint + 5, 6)):
        u = f"{BASE}/img/goods/S/{prefix}-{n:03d}.jpg"
        if image_exists(u, timeout=timeout):
            start_n = n
            break
    if start_n is None:
        return []

    found: List[str] = []
    misses = 0
    n = start_n
    while len(found) < max_images:
        url = f"{BASE}/img/goods/S/{prefix}-{n:03d}.jpg"
        ok = image_exists(url, timeout=timeout)
        if ok:
            found.append(url)
            misses = 0
        else:
            misses += 1
            if misses >= miss_stop:
                break
        n += 1
    return found

def probe_L_code(code: str, timeout: float) -> List[str]:
    """
    特殊定妆/舞写：L/{code}.jpg
    """
    url = f"{BASE}/img/goods/L/{code}.jpg"
    return [url] if image_exists(url, timeout=timeout) else []

def probe_L_stem_series(code: str, max_images: int, timeout: float, start_suffix: int = 200, miss_stop: int = 45) -> List[str]:
    """
    少数公演：L/{stem}{suffix:04d}.jpg
    例如 stem=2251141100 -> 2251141100229.jpg
    我们从 0200 开始更贴近常见分配，避免从0001扫太久。
    """
    if len(code) < 10:
        return []
    stem = code[:10]
    found: List[str] = []
    misses = 0
    s = start_suffix
    while len(found) < max_images:
        url = f"{BASE}/img/goods/L/{stem}{s:04d}.jpg"
        ok = image_exists(url, timeout=timeout)
        if ok:
            found.append(url)
            misses = 0
        else:
            misses += 1
            if misses >= miss_stop:
                break
        s += 1
    return found

# -------------------------
# APIs
# -------------------------

@app.get("/ping")
def ping():
    return {"ok": True, "msg": "pong"}

@app.get("/api/cards")
def api_cards(
    keyword: str = "コレクションカード",
    title_filter: List[str] = Query(default=[]),
    page_size: int = 80,
    timeout: float = 15.0,
):
    """
    小卡检索：返回 title + code + url + date + troupe（用于前端时间轴）
    title_filter：多个词 AND 匹配（小写包含）
    """
    try:
        params = {"search": "search", "keyword": keyword}
        r = requests.get(SEARCH_URL, params=params, headers=DEFAULT_HEADERS, timeout=timeout)
        soup = BeautifulSoup(r.text, "lxml")

        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/shop/g/g") and href.endswith("/"):
                links.append(BASE + href)

        # unique keep order then cut
        uniq = []
        seen = set()
        for u in links:
            if u not in seen:
                seen.add(u)
                uniq.append(u)
        uniq = uniq[: max(20, page_size)]

        def fetch_one(u: str) -> Dict[str, Any]:
            code = u.rstrip("/").split("/g/g")[-1]
            prefix = parse_prefix_from_code(code)
            date_iso, troupe_idx = prefix_to_date_troupe(prefix) if prefix else (None, None)
            troupe = TROUPE_MAP.get(troupe_idx or "", None)

            title = ""
            try:
                rr = http_get(u, timeout=timeout)
                title = extract_goods_title(rr.text)
            except Exception:
                title = ""

            return {
                "url": u,
                "code": code,
                "prefix": prefix,
                "title": title,
                "date": date_iso,
                "troupe_idx": troupe_idx,
                "troupe": troupe,
            }

        items = []
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = [ex.submit(fetch_one, u) for u in uniq]
            for fut in as_completed(futs):
                items.append(fut.result())

        # filter by title
        if title_filter:
            filters = [f.lower() for f in title_filter if f.strip()]
            def match(t: str) -> bool:
                tl = (t or "").lower()
                return all(f in tl for f in filters)
            items = [it for it in items if match(it.get("title", ""))]

        # sort: date desc, then code desc
        def sort_key(it: Dict[str, Any]):
            d = it.get("date") or "1900-01-01"
            return (d, it.get("code") or "")
        items.sort(key=sort_key, reverse=True)

        return JSONResponse({"keyword": keyword, "title_filter": title_filter, "results": items})
    except Exception as e:
        logging.error("CARDS API ERROR: %s", e)
        traceback.print_exc()
        return JSONResponse({"error": "cards_failed", "message": str(e), "results": []}, status_code=200)

@app.get("/api/card_images")
def api_card_images(
    card_url: str,
    max_images: int = 200,
    timeout: float = 12.0,
    extra_prefix: List[str] = Query(default=[]),
):
    """
    一次性“探图”：
    - 小卡画像：商品页img + L/{code}.jpg
    - 普通定妆/舞写：S/{prefix}-{NNN}.jpg（prefix 来自 code[1:8]）
    - 新人公演：自动向后扩展 prefix 候选并探测
    - 特殊：L/{code}.jpg 以及 L/{stem}{suffix}.jpg
    """
    try:
        if not card_url.startswith("http"):
            card_url = BASE + card_url
        code = card_url.rstrip("/").split("/g/g")[-1]

        # fetch card page (sometimes accessible even before full publish)
        title = ""
        direct_imgs_ok: List[str] = []
        try:
            rr = http_get(card_url, timeout=timeout)
            html = rr.text
            title = extract_goods_title(html)
            direct_imgs = extract_images_from_goods_page(html)
            direct_imgs_ok = multi_probe(direct_imgs, timeout=timeout) if direct_imgs else []
        except Exception:
            title = ""

        # L/{code}.jpg
        l_single = probe_L_code(code, timeout=timeout)

        # prefix/date/troupe from code[1:8]
        prefix0 = parse_prefix_from_code(code)
        base_date_iso, troupe_idx = prefix_to_date_troupe(prefix0) if prefix0 else (None, None)
        troupe = TROUPE_MAP.get(troupe_idx or "", None)

        # build candidate prefixes (auto expand) + manual extra
        candidates = build_prefix_candidates(base_date_iso, troupe_idx, days_forward=160, days_backward=10)
        for p in extra_prefix:
            p = (p or "").strip()
            if re.fullmatch(r"\d{7}", p):
                candidates.append({"prefix": p, "date": None, "delta_days": None})

        # sort candidates by closeness
        def cand_sort(c):
            dd = c.get("delta_days")
            if dd is None:
                return 9999
            return abs(dd)
        candidates.sort(key=cand_sort)

        # LIGHT probe: check first 5 numbers for each prefix
        LIGHT_N = 90
        FULL_TOP = 8

        def light_probe(prefix: str) -> bool:
            # try 001~005 quickly
            for n in range(1, 6):
                u = f"{BASE}/img/goods/S/{prefix}-{n:03d}.jpg"
                if image_exists(u, timeout=timeout):
                    return True
            return False

        light_hits: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=28) as ex:
            futs = {ex.submit(light_probe, c["prefix"]): c for c in candidates[:LIGHT_N]}
            for fut in as_completed(futs):
                c = futs[fut]
                try:
                    if fut.result():
                        light_hits.append(c)
                except Exception:
                    pass

        # choose prefixes to fully scan:
        # - prioritize close date (main stills)
        # - but keep some later hits for rookie
        light_hits.sort(key=lambda x: (0 if x.get("delta_days") is not None else 1, abs(x.get("delta_days") or 9999)))
        full_candidates = light_hits[:FULL_TOP]

        sequences: List[Dict[str, Any]] = []
        for c in full_candidates:
            p = c["prefix"]
            seq = probe_sequence_S(p, max_images=max_images, timeout=timeout, start_n_hint=1)
            if seq:
                sequences.append({
                    "prefix": p,
                    "delta_days": c.get("delta_days"),
                    "date": c.get("date"),
                    "count": len(seq),
                    "images": seq,
                })

        # classify main vs rookie by delta_days
        main = []
        rookie = []
        unknown = []
        for s in sequences:
            dd = s.get("delta_days")
            if dd is None:
                unknown.append(s)
            elif dd <= 10:
                main.append(s)
            else:
                rookie.append(s)

        # special L stem series
        l_series = probe_L_stem_series(code, max_images=max_images, timeout=timeout, start_suffix=200)

        # build card_images group (dedup)
        card_images = list(dict.fromkeys(direct_imgs_ok + l_single))

        return JSONResponse({
            "card_url": card_url,
            "title": title,
            "code": code,
            "prefix_from_code": prefix0,
            "base_date": base_date_iso,
            "troupe_idx": troupe_idx,
            "troupe": troupe,
            "max_images": max_images,
            "groups": {
                "card_images": {"count": len(card_images), "images": card_images},
                "main_stills": {"prefixes": main},
                "rookie_stills": {"prefixes": rookie},
                "unknown_prefix_stills": {"prefixes": unknown},
                "special_L_single": {"count": len(l_single), "images": l_single},
                "special_L_series": {"count": len(l_series), "images": l_series},
            },
            "debug": {
                "light_hit_prefixes": [c["prefix"] for c in light_hits],
                "full_probed_prefixes": [c["prefix"] for c in full_candidates],
                "extra_prefix": extra_prefix,
            }
        })
    except Exception as e:
        logging.error("CARD_IMAGES API ERROR: %s", e)
        traceback.print_exc()
        return JSONResponse({"error": "card_images_failed", "message": str(e), "groups": {}}, status_code=200)