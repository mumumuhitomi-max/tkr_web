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
app = FastAPI(title="Takarazuka Link Finder", version="3.0.0")

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
# Helpers
# -------------------------

def http_get(url: str, timeout: float = 15.0) -> requests.Response:
    return requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)

def http_head_ok(url: str, timeout: float = 10.0) -> bool:
    try:
        r = requests.head(url, headers=DEFAULT_HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code != 200:
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

def parse_date_and_troupe_from_code(code: str) -> Tuple[Optional[str], Optional[str]]:
    """
    从 13 位 code 里找出形如 25MMDD{B} 的片段：
      - date: 2025-MM-DD
      - troupe_idx: "1".."5"
    """
    # 常见：code 内含 25MMDD{B}（7位），例如 2251141100113 -> 2511411
    m = re.search(r"(2\d)(\d{2})(\d{2})([1-5])", code)
    if not m:
        return None, None
    yy, mm, dd, b = m.group(1), m.group(2), m.group(3), m.group(4)
    year = int("20" + yy[1:])  # "25" -> 2025
    try:
        dt = datetime(year, int(mm), int(dd)).date().isoformat()
    except Exception:
        dt = None
    return dt, b

def build_prefix_candidates(base_date_iso: Optional[str], troupe_idx: Optional[str], days_forward: int = 120, days_backward: int = 10) -> List[Dict[str, Any]]:
    """
    为了解决新人公演/舞写 prefix 不同的问题：
    - 以小卡日期为基准，向后扩展 days_forward 天，向前扩展 days_backward 天
    - 生成 prefix: YYMMDD{B}
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

def multi_head_probe(urls: List[str], timeout: float = 10.0, max_workers: int = 24) -> List[str]:
    if not urls:
        return []
    ok_map = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(http_head_ok, u, timeout): u for u in urls}
        for fut in as_completed(futs):
            u = futs[fut]
            try:
                ok_map[u] = bool(fut.result())
            except Exception:
                ok_map[u] = False
    return [u for u in urls if ok_map.get(u)]

def probe_sequence_S(prefix: str, max_images: int, start_n: int, timeout: float, miss_stop: int = 40) -> List[str]:
    """
    S/{prefix}-{NNN}.jpg
    连续 miss_stop 次 miss 后停止（避免无意义扫描）
    """
    found = []
    misses = 0
    n = start_n
    while len(found) < max_images:
        url = f"{BASE}/img/goods/S/{prefix}-{n:03d}.jpg"
        ok = http_head_ok(url, timeout=timeout)
        if ok:
            found.append(url)
            misses = 0
        else:
            misses += 1
            if misses >= miss_stop:
                break
        n += 1
    return found

def probe_single_L_code(code: str, timeout: float) -> List[str]:
    url = f"{BASE}/img/goods/L/{code}.jpg"
    return [url] if http_head_ok(url, timeout=timeout) else []

def probe_sequence_L_by_stem(code: str, max_images: int, timeout: float, start_suffix: int = 1, miss_stop: int = 60) -> List[str]:
    """
    L/{stem}{suffix:04d}.jpg  (stem = code[:10])
    用于 DEAN 等特殊情况：2251141100113 -> stem 2251141100 -> 2251141100229.jpg 这类
    """
    if len(code) < 10:
        return []
    stem = code[:10]
    found = []
    misses = 0
    s = start_suffix
    while len(found) < max_images:
        url = f"{BASE}/img/goods/L/{stem}{s:04d}.jpg"
        ok = http_head_ok(url, timeout=timeout)
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
# API
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
            date_iso, troupe_idx = parse_date_and_troupe_from_code(code)
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
    start_n: int = 1,
    timeout: float = 12.0,
    extra_prefix: List[str] = Query(default=[]),
):
    """
    一次性“探图”：
    1) 卡片本身图片（商品页直取 + L/code）
    2) 定妆/舞写：S/prefix-NNN（prefix 自动扩展日期候选）
    3) 新人公演舞写：同样由日期扩展候选命中（通常比小卡日期晚一段时间）

    返回 groups：
      - card_images
      - main_stills (delta_days <= 7 的 prefix 命中)
      - rookie_stills (delta_days > 7 的 prefix 命中)
      - special_L_series (L/stem+suffix)
    """
    try:
        if not card_url.startswith("http"):
            card_url = BASE + card_url
        code = card_url.rstrip("/").split("/g/g")[-1]

        # fetch card page
        rr = http_get(card_url, timeout=timeout)
        html = rr.text
        title = extract_goods_title(html)
        direct_imgs = extract_images_from_goods_page(html)
        direct_imgs_ok = multi_head_probe(direct_imgs, timeout=timeout) if direct_imgs else []

        # card single L
        single_L = probe_single_L_code(code, timeout=timeout)

        # date & troupe
        base_date_iso, troupe_idx = parse_date_and_troupe_from_code(code)
        troupe = TROUPE_MAP.get(troupe_idx or "", None)

        # build candidate prefixes by date expansion
        candidates = build_prefix_candidates(base_date_iso, troupe_idx, days_forward=120, days_backward=10)

        # extra_prefix manual (for corner cases)
        for p in extra_prefix:
            p = (p or "").strip()
            if p:
                candidates.append({"prefix": p, "date": None, "delta_days": None})

        # probe S-sequences for prefixes
        # 为了性能：先挑“更可能命中”的日期范围（0~90天）在前
        def cand_sort(c):
            dd = c.get("delta_days")
            if dd is None:
                return 9999
            return abs(dd)
        candidates.sort(key=cand_sort)

        # 我们不可能对 130 个 prefix 全扫到 200 张，会太重
        # 策略：先对前 N 个候选做“轻探测”命中 1 张即可升级为全探测
        LIGHT_N = 80
        FULL_TOP = 6  # 最多全量探测 6 个prefix（足够覆盖：定妆/舞写/新人公演）
        light_hits: List[Dict[str, Any]] = []

        def light_probe(prefix: str) -> bool:
            url = f"{BASE}/img/goods/S/{prefix}-{start_n:03d}.jpg"
            return http_head_ok(url, timeout=timeout)

        with ThreadPoolExecutor(max_workers=24) as ex:
            futs = []
            for c in candidates[:LIGHT_N]:
                futs.append(ex.submit(light_probe, c["prefix"]))
            for c, fut in zip(candidates[:LIGHT_N], futs):
                try:
                    if fut.result():
                        light_hits.append(c)
                except Exception:
                    pass

        # 选择要 full 探测的 prefix：优先 delta_days 小（更像定妆/舞写），其次更多候选
        # 同时保留可能新人公演（delta_days 正向较大）的一部分
        light_hits.sort(key=lambda x: (0 if x.get("delta_days") is not None else 1, abs(x.get("delta_days") or 9999)))
        full_candidates = light_hits[:FULL_TOP]

        sequences: List[Dict[str, Any]] = []
        for c in full_candidates:
            p = c["prefix"]
            seq = probe_sequence_S(p, max_images=max_images, start_n=start_n, timeout=timeout)
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
            elif dd <= 7:
                main.append(s)
            else:
                rookie.append(s)

        # special L series (DEAN-like)
        # 先从 1 开始尝试，若你觉得常见 0229，可以把 start_suffix 改成 200
        l_series = probe_sequence_L_by_stem(code, max_images=max_images, timeout=timeout, start_suffix=1)

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
                    "count": len(direct_imgs_ok) + len(single_L),
                    "images": list(dict.fromkeys(direct_imgs_ok + single_L)),
                },
                "main_stills": {
                    "prefixes": main,
                },
                "rookie_stills": {
                    "prefixes": rookie,
                },
                "unknown_prefix_stills": {
                    "prefixes": unknown,
                },
                "special_L_series": {
                    "count": len(l_series),
                    "images": l_series,
                }
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