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
    很多站点对 HEAD 会 403/405，导致“全部不存在”的假阴性。
    所以：先 HEAD；若不行就 GET + Range: bytes=0-0 做轻量探测。
    """
    try:
        r = http_head(url, timeout=timeout)
        if r.status_code == 200:
            ct = (r.headers.get("content-type") or "").lower()
            return ct.startswith("image/") or ("image" in ct)
        if r.status_code in (403, 405):
            # fallback GET (range)
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
    关键修复：按公演名称/标题里的（花月雪星宙）识别组别
    常见格式：
      - "...＜月組＞"
      - "月組『...』"
      - "（月組）"
    """
    t = title or ""
    for key, meta in TROUPE_BY_NAME.items():
        if f"{key}組" in t:
            return meta
    return None

def parse_date_and_troupe_from_code(code: str) -> Tuple[Optional[str], Optional[str]]:
    """
    从 code 里找出形如 25MMDD{B} 的片段（兜底用）
    """
    m = re.search(r"(2\d)(\d{2})(\d{2})([1-5])", code)
    if not m:
        return None, None
    yy, mm, dd, b = m.group(1), m.group(2), m.group(3), m.group(4)
    year = int("20" + yy[1:])
    try:
        dt = datetime(year, int(mm), int(dd)).date().isoformat()
    except Exception:
        dt = None
    return dt, b

def extract_images_from_goods_page(html: str) -> List[str]:
    """
    直接从商品页抓出 img/goods 的图片（S/L 都收）
    这对“特殊 L/{code}.jpg”或者 L/stemxxxx.jpg 很有帮助
    """
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

def build_prefix_candidates(
    base_date_iso: Optional[str],
    troupe_idx: Optional[str],
    days_forward: int = 260,
    days_backward: int = 30
) -> List[Dict[str, Any]]:
    """
    新人公演/东宝舞写可能比小卡日期晚不少，所以 forward 扩大到 260 天
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
# Probing logic
# -------------------------

def probe_sequence_S(prefix: str, max_images: int, start_n: int, timeout: float, miss_stop: int = 50) -> List[str]:
    """
    S/{prefix}-{NNN}.jpg
    连续 miss_stop 次 miss 后停止
    """
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

def guess_start_n_for_prefix(prefix: str, timeout: float, start_range: int = 30) -> Optional[int]:
    """
    关键修复：序列不一定从 001 开始，先并发探测 001..030，命中哪个就从哪个开始抓
    """
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

def probe_L_code(code: str, timeout: float) -> List[str]:
    """
    特殊定妆・舞写：L/{code}.jpg
    """
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
    小卡检索：返回 title + code + url + date + troupe（时间轴用）
    重要修复：troupe 优先按 title 中出现的 花/月/雪/星/宙 识别
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
            # fetch title
            title = ""
            try:
                rr = http_get(u, timeout=timeout)
                title = extract_goods_title(rr.text)
            except Exception:
                title = ""
            # troupe: title first
            troupe = infer_troupe_from_title(title)
            # fallback: code
            date_iso, troupe_idx = parse_date_and_troupe_from_code(code)
            if troupe is None and troupe_idx:
                troupe = TROUPE_MAP.get(troupe_idx)
            else:
                # if title gives troupe, also build troupe_idx as best-effort
                troupe_idx = troupe_idx

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
):
    """
    一次性“探图”：
      A) 小卡商品页能直接拿到的图（S/L都可能）
      B) 特殊：L/{code}.jpg
      C) 定妆・舞写：S/{prefix}-{NNN}.jpg（prefix 自动候选扩展 + 自动探测起始 NNN）
      D) 新人公演/东宝舞写：同样通过候选 prefix 扩展命中；也支持手动补 prefix
    """
    try:
        if not card_url.startswith("http"):
            card_url = BASE + card_url
        code = card_url.rstrip("/").split("/g/g")[-1]

        # fetch card page
        rr = http_get(card_url, timeout=timeout)
        html = rr.text
        title = extract_goods_title(html)

        # troupe: title first (fix #1)
        troupe = infer_troupe_from_title(title)
        base_date_iso, troupe_idx = parse_date_and_troupe_from_code(code)
        if troupe is None and troupe_idx:
            troupe = TROUPE_MAP.get(troupe_idx)

        # A) direct imgs from page
        direct_imgs = extract_images_from_goods_page(html)
        # validate direct imgs (use image_ok with fallback GET)
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
            # keep order as in page
            direct_ok_set = set(direct_ok)
            direct_ok = [u for u in direct_imgs if u in direct_ok_set]

        # B) special L/{code}.jpg
        l_code_imgs = probe_L_code(code, timeout=timeout)

        # Candidate prefixes
        candidates = build_prefix_candidates(base_date_iso, troupe_idx, days_forward=260, days_backward=30)

        # add manual prefixes
        for p in extra_prefix:
            p = (p or "").strip()
            if p:
                candidates.insert(0, {"prefix": p, "date": None, "delta_days": None})

        # If troupe_idx missing, still allow manual prefixes to work
        # Light stage: find prefixes where any of 001..030 exists
        LIGHT_LIMIT = 120
        full_prefixes: List[Dict[str, Any]] = []

        def light_probe(c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            p = c["prefix"]
            start_n = guess_start_n_for_prefix(p, timeout=timeout, start_range=30)
            if start_n is None:
                return None
            return {**c, "start_n": start_n}

        probe_targets = candidates[:LIGHT_LIMIT] if candidates else []
        if probe_targets:
            with ThreadPoolExecutor(max_workers=28) as ex:
                futs = [ex.submit(light_probe, c) for c in probe_targets]
                for fut in as_completed(futs):
                    try:
                        hit = fut.result()
                        if hit:
                            full_prefixes.append(hit)
                    except Exception:
                        pass

        # Choose a few prefixes to fully fetch (enough to cover main+rookie)
        # Sort by abs(delta_days), manual (delta_days None) goes first
        def rank(x: Dict[str, Any]):
            dd = x.get("delta_days")
            if dd is None:
                return (-1, 0)
            return (0, abs(dd))
        full_prefixes.sort(key=rank)
        FULL_TOP = 8
        full_prefixes = full_prefixes[:FULL_TOP]

        sequences: List[Dict[str, Any]] = []
        for c in full_prefixes:
            p = c["prefix"]
            start_n = int(c.get("start_n") or 1)
            imgs = probe_sequence_S(p, max_images=max_images, start_n=start_n, timeout=timeout, miss_stop=60)
            if imgs:
                sequences.append({
                    "prefix": p,
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
            if dd is None:
                unknown.append(s)
            elif dd <= 14:
                main.append(s)
            else:
                rookie.append(s)

        # unify + dedupe images
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
                "light_hits": [x["prefix"] for x in full_prefixes],
            }
        })
    except Exception as e:
        logging.error("CARD_IMAGES API ERROR: %s", e)
        traceback.print_exc()
        return JSONResponse({"error": "card_images_failed", "message": str(e), "groups": {}}, status_code=200)
