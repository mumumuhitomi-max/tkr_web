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
app = FastAPI(title="Takarazuka Link Finder", version="3.2.0")

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

TROUPE_BY_NAME = {
    "花": {"jp": "花組", "cn": "花组", "emoji": "🌸", "color": "#ec4899"},
    "月": {"jp": "月組", "cn": "月组", "emoji": "🌙", "color": "#f59e0b"},
    "雪": {"jp": "雪組", "cn": "雪组", "emoji": "❄️", "color": "#22c55e"},
    "星": {"jp": "星組", "cn": "星组", "emoji": "⭐️", "color": "#3b82f6"},
    "宙": {"jp": "宙組", "cn": "宙组", "emoji": "🪐", "color": "#a855f7"},
}
TROUPE_MAP = {
    "1": TROUPE_BY_NAME["花"],
    "2": TROUPE_BY_NAME["月"],
    "3": TROUPE_BY_NAME["雪"],
    "4": TROUPE_BY_NAME["星"],
    "5": TROUPE_BY_NAME["宙"],
}

# -------------------------
# Low-level HTTP helpers
# -------------------------

def http_get(url: str, timeout: float = 15.0) -> requests.Response:
    return requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)

def http_head(url: str, timeout: float = 10.0) -> requests.Response:
    return requests.head(url, headers=DEFAULT_HEADERS, timeout=timeout, allow_redirects=True)

def http_image_ok(url: str, timeout: float = 10.0) -> bool:
    """
    Many CDNs/sites return 403/405 for HEAD. So:
      - Try HEAD
      - Fallback to GET + Range bytes=0-0 (light probe)
    """
    try:
        r = http_head(url, timeout=timeout)
        if r.status_code == 200:
            ct = (r.headers.get("content-type") or "").lower()
            return ct.startswith("image/") or ("image" in ct)

        if r.status_code in (403, 405):
            rg_headers = dict(DEFAULT_HEADERS)
            rg_headers["Range"] = "bytes=0-0"
            rg = requests.get(url, headers=rg_headers, timeout=timeout, allow_redirects=True, stream=True)
            if rg.status_code in (200, 206):
                ct = (rg.headers.get("content-type") or "").lower()
                return ct.startswith("image/") or ("image" in ct)

        return False
    except Exception:
        return False

# -------------------------
# Parsing helpers
# -------------------------

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

def infer_troupe_from_title(title: str) -> Optional[Dict[str, Any]]:
    """
    FIX: troupe icon must come from title containing 花/月/雪/星/宙.
    """
    t = title or ""
    for key, meta in TROUPE_BY_NAME.items():
        if f"{key}組" in t:
            return meta
    return None

def parse_date_and_troupe_from_any_7digit(prefix7: str) -> Tuple[Optional[str], Optional[str]]:
    """
    If prefix7 is YYMMDD{B} and MM/DD are valid -> parse date and troupe idx.
    Otherwise return (None, last_digit_if_valid)
    """
    if not re.fullmatch(r"\d{7}", prefix7):
        return None, None
    yy = prefix7[0:2]
    mm = prefix7[2:4]
    dd = prefix7[4:6]
    b = prefix7[6]
    if b not in "12345":
        return None, None

    # best-effort date parse
    try:
        year = int("20" + yy)
        m = int(mm)
        d = int(dd)
        if 1 <= m <= 12 and 1 <= d <= 31:
            dt = datetime(year, m, d).date().isoformat()
            return dt, b
    except Exception:
        pass
    return None, b

def primary_prefix_from_code(code: str) -> Optional[str]:
    """
    CRITICAL FIX:
      For collection card codes like 2251141100113
      primary still prefix is usually code[1:8] => 2511411
    """
    if not code or len(code) < 8:
        return None
    p = code[1:8]
    if re.fullmatch(r"\d{7}", p) and p[0] in "23" and p[0:2].startswith("25") and p[-1] in "12345":
        return p
    # more tolerant: just needs last digit 1..5
    if re.fullmatch(r"\d{7}", p) and p[-1] in "12345":
        return p
    return None

def extract_any_prefix7_candidates(code: str) -> List[str]:
    """
    Extract any 7-digit chunks from code that end with troupe idx (1..5).
    e.g. 2251144100110 contains ...2511441...
    """
    cands = set()
    if code:
        for m in re.finditer(r"(\d{6}[1-5])", code):
            cands.add(m.group(1))
    # Also include the primary prefix rule
    p = primary_prefix_from_code(code)
    if p:
        cands.add(p)
    return sorted(cands)

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
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

# -------------------------
# Candidate building
# -------------------------

def build_date_window_prefix_candidates(
    base_date_iso: Optional[str],
    troupe_idx: Optional[str],
    days_forward: int = 260,
    days_backward: int = 30
) -> List[Dict[str, Any]]:
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
        out.append({"prefix": prefix, "date": d.isoformat(), "delta_days": delta, "source": "date_window"})
    return out

def build_neighbor_prefix_candidates(prefix7: str, span: int = 2200) -> List[Dict[str, Any]]:
    """
    When we don't have a usable date, we still want rookie/toho candidates.
    Strategy: scan numeric neighbors around the primary prefix, but keep same troupe idx.
    Example:
      primary 2511411 (B=1)
      we scan 2511411 +/- span, only take those ending with '1'
    This is bounded and then light-probed, so it won't explode in practice.
    """
    if not re.fullmatch(r"\d{7}", prefix7):
        return []
    troupe_idx = prefix7[-1]
    core = int(prefix7[:-1])  # first 6 digits
    out = []
    for delta in range(-span, span + 1):
        v = core + delta
        if v <= 0:
            continue
        p = f"{v:06d}{troupe_idx}"
        out.append({"prefix": p, "date": None, "delta_days": None, "source": "neighbor_scan"})
    return out

# -------------------------
# Probing logic
# -------------------------

def guess_start_n_for_prefix(prefix: str, timeout: float, start_range: int = 30) -> Optional[int]:
    cand_ns = list(range(1, start_range + 1))
    urls = [(n, f"{BASE}/img/goods/S/{prefix}-{n:03d}.jpg") for n in cand_ns]
    with ThreadPoolExecutor(max_workers=24) as ex:
        futs = {ex.submit(http_image_ok, u, timeout): n for n, u in urls}
        hits = []
        for fut in as_completed(futs):
            n = futs[fut]
            try:
                if fut.result():
                    hits.append(n)
            except Exception:
                pass
    return min(hits) if hits else None

def probe_sequence_S(prefix: str, max_images: int, start_n: int, timeout: float, miss_stop: int = 60) -> List[str]:
    found = []
    misses = 0
    n = start_n
    while len(found) < max_images:
        url = f"{BASE}/img/goods/S/{prefix}-{n:03d}.jpg"
        if http_image_ok(url, timeout=timeout):
            found.append(url)
            misses = 0
        else:
            misses += 1
            if misses >= miss_stop:
                break
        n += 1
    return found

def probe_L_code(code: str, timeout: float) -> List[str]:
    url = f"{BASE}/img/goods/L/{code}.jpg"
    return [url] if http_image_ok(url, timeout=timeout) else []

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
    page_size: int = 120,
    timeout: float = 15.0,
):
    """
    Return: title + code + url + date + troupe
    troupe: title first
    date: best-effort from any prefix7 candidates
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

        uniq = []
        seen = set()
        for u in links:
            if u not in seen:
                seen.add(u)
                uniq.append(u)
        uniq = uniq[: max(20, page_size)]

        def fetch_one(u: str) -> Dict[str, Any]:
            code = u.rstrip("/").split("/g/g")[-1]
            title = ""
            try:
                rr = http_get(u, timeout=timeout)
                title = extract_goods_title(rr.text)
            except Exception:
                title = ""

            troupe = infer_troupe_from_title(title)

            # best-effort date/troupe_idx
            prefix_cands = extract_any_prefix7_candidates(code)
            date_iso = None
            troupe_idx = None
            for p7 in prefix_cands:
                di, ti = parse_date_and_troupe_from_any_7digit(p7)
                if troupe_idx is None and ti:
                    troupe_idx = ti
                if date_iso is None and di:
                    date_iso = di

            if troupe is None and troupe_idx:
                troupe = TROUPE_MAP.get(troupe_idx)

            return {
                "url": u,
                "code": code,
                "title": title,
                "date": date_iso,
                "troupe_idx": troupe_idx,
                "troupe": troupe,
            }

        items = []
        with ThreadPoolExecutor(max_workers=14) as ex:
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

        # sort: date desc then code desc
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
    neighbor_span: int = 2200,   # rookie/toho 扫描范围（可调）
):
    """
    One-click:
      A) images from goods page (S/L)
      B) special: L/{code}.jpg
      C) main stills: S/{prefix}-{NNN}.jpg (AUTO prefix candidates)
      D) rookie/toho: expanded candidates (date-window if possible, else neighbor scan)
      E) manual extra_prefix always supported
    """
    try:
        if not card_url.startswith("http"):
            card_url = BASE + card_url
        code = card_url.rstrip("/").split("/g/g")[-1]

        rr = http_get(card_url, timeout=timeout)
        html = rr.text
        title = extract_goods_title(html)

        troupe = infer_troupe_from_title(title)

        # prefix candidates from code
        prefix7_cands = extract_any_prefix7_candidates(code)
        primary_prefix = primary_prefix_from_code(code)

        # infer troupe idx (best-effort)
        troupe_idx = None
        base_date_iso = None
        for p7 in prefix7_cands:
            di, ti = parse_date_and_troupe_from_any_7digit(p7)
            if troupe_idx is None and ti:
                troupe_idx = ti
            if base_date_iso is None and di:
                base_date_iso = di

        if troupe is None and troupe_idx:
            troupe = TROUPE_MAP.get(troupe_idx)

        # A) direct imgs from page
        direct_imgs = extract_images_from_goods_page(html)
        direct_ok = []
        if direct_imgs:
            with ThreadPoolExecutor(max_workers=24) as ex:
                futs = {ex.submit(http_image_ok, u, timeout): u for u in direct_imgs}
                for fut in as_completed(futs):
                    u = futs[fut]
                    try:
                        if fut.result():
                            direct_ok.append(u)
                    except Exception:
                        pass
            direct_ok_set = set(direct_ok)
            direct_ok = [u for u in direct_imgs if u in direct_ok_set]

        # B) special L/{code}.jpg
        l_code_imgs = probe_L_code(code, timeout=timeout)

        # Candidate list building
        candidates: List[Dict[str, Any]] = []

        # 1) always include primary prefix (THIS FIXES your issue)
        if primary_prefix:
            candidates.append({"prefix": primary_prefix, "date": None, "delta_days": None, "source": "primary_from_code"})

        # 2) include other code-derived prefix7 candidates too
        for p7 in prefix7_cands:
            if p7 != primary_prefix:
                candidates.append({"prefix": p7, "date": None, "delta_days": None, "source": "prefix7_from_code"})

        # 3) if we have a real date -> use date window to cover rookie/toho
        candidates.extend(build_date_window_prefix_candidates(base_date_iso, troupe_idx, days_forward=260, days_backward=30))

        # 4) if we DON'T have real date -> neighbor scan around primary prefix (bounded)
        if base_date_iso is None and primary_prefix:
            candidates.extend(build_neighbor_prefix_candidates(primary_prefix, span=neighbor_span))

        # 5) manual prefixes
        for p in extra_prefix:
            p = (p or "").strip()
            if p and re.fullmatch(r"\d{7}", p):
                candidates.insert(0, {"prefix": p, "date": None, "delta_days": None, "source": "manual"})

        # de-dupe candidates while preserving order
        seen_p = set()
        uniq_candidates = []
        for c in candidates:
            p = c["prefix"]
            if p not in seen_p:
                seen_p.add(p)
                uniq_candidates.append(c)
        candidates = uniq_candidates

        # Light stage: find prefixes where any of 001..030 exists
        LIGHT_LIMIT = 260  # increase a bit because neighbor-scan needs some room
        light_hits: List[Dict[str, Any]] = []

        def light_probe(c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            p = c["prefix"]
            start_n = guess_start_n_for_prefix(p, timeout=timeout, start_range=30)
            if start_n is None:
                return None
            return {**c, "start_n": start_n}

        probe_targets = candidates[:LIGHT_LIMIT]
        with ThreadPoolExecutor(max_workers=28) as ex:
            futs = [ex.submit(light_probe, c) for c in probe_targets]
            for fut in as_completed(futs):
                try:
                    hit = fut.result()
                    if hit:
                        light_hits.append(hit)
                except Exception:
                    pass

        # Rank: primary_from_code first, then date_window closer to 0, then others
        def rank(x: Dict[str, Any]):
            src = x.get("source") or ""
            if src == "manual":
                return (0, 0)
            if src == "primary_from_code":
                return (1, 0)
            dd = x.get("delta_days")
            if src == "date_window" and isinstance(dd, int):
                return (2, abs(dd))
            if src == "prefix7_from_code":
                return (3, 0)
            return (4, 0)

        light_hits.sort(key=rank)

        # Fully fetch top hits
        FULL_TOP = 12  # grab more blocks, so rookie/toho is more likely to show
        full_prefixes = light_hits[:FULL_TOP]

        sequences: List[Dict[str, Any]] = []
        for c in full_prefixes:
            p = c["prefix"]
            start_n = int(c.get("start_n") or 1)
            imgs = probe_sequence_S(p, max_images=max_images, start_n=start_n, timeout=timeout, miss_stop=60)
            if imgs:
                sequences.append({
                    "prefix": p,
                    "source": c.get("source"),
                    "delta_days": c.get("delta_days"),
                    "date": c.get("date"),
                    "start_n": start_n,
                    "count": len(imgs),
                    "images": imgs,
                })

        # classify: main vs rookie (rough)
        main = []
        rookie = []
        unknown = []
        for s in sequences:
            dd = s.get("delta_days")
            src = s.get("source")
            if src in ("primary_from_code", "prefix7_from_code"):
                # treat as main-ish
                main.append(s)
            elif isinstance(dd, int):
                if dd <= 14:
                    main.append(s)
                else:
                    rookie.append(s)
            else:
                unknown.append(s)

        def dedupe_keep_order(arr: List[str]) -> List[str]:
            seen = set()
            out = []
            for u in arr:
                if u not in seen:
                    seen.add(u)
                    out.append(u)
            return out

        return JSONResponse({
            "card_url": card_url,
            "title": title,
            "code": code,
            "primary_prefix": primary_prefix,
            "prefix7_candidates": prefix7_cands,
            "base_date": base_date_iso,
            "troupe_idx": troupe_idx,
            "troupe": troupe,
            "max_images": max_images,
            "groups": {
                "card_images": {
                    "count": len(dedupe_keep_order(direct_ok + l_code_imgs)),
                    "images": dedupe_keep_order(direct_ok + l_code_imgs),
                },
                "main_stills": {"prefixes": main},
                "rookie_stills": {"prefixes": rookie},
                "unknown_prefix_stills": {"prefixes": unknown},
            },
            "debug": {
                "extra_prefix": extra_prefix,
                "candidate_count": len(candidates),
                "light_hit_count": len(light_hits),
                "light_hits_top": [x["prefix"] for x in light_hits[:20]],
            }
        })
    except Exception as e:
        logging.error("CARD_IMAGES API ERROR: %s", e)
        traceback.print_exc()
        return JSONResponse({"error": "card_images_failed", "message": str(e), "groups": {}}, status_code=200)
