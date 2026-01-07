from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional, Dict, Any
import logging, traceback, re, time
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Takarazuka Link Finder", version="2.0.0")

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

# -------------------------
# Helpers
# -------------------------

def http_get(url: str, timeout: float = 15.0) -> requests.Response:
    return requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)

def http_head_ok(url: str, timeout: float = 10.0) -> bool:
    try:
        r = requests.head(url, headers=DEFAULT_HEADERS, timeout=timeout, allow_redirects=True)
        return r.status_code == 200 and (r.headers.get("content-type", "").startswith("image/") or "image" in r.headers.get("content-type", ""))
    except Exception:
        return False

def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def extract_goods_title(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    # 先尝试商品标题常见位置
    h1 = soup.find("h1")
    if h1:
        t = normalize_text(h1.get_text())
        if t:
            return t
    # fallback: title tag
    ttag = soup.find("title")
    if ttag:
        return normalize_text(ttag.get_text())
    return ""

def extract_images_from_goods_page(html: str) -> List[str]:
    """
    从商品详情页直接提取图片（通常能拿到 L 尺寸图）
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
    # 去重保持顺序
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def guess_prefix_candidates_from_code(code: str) -> List[str]:
    """
    你之前的规律：YYMMDDB（B=组序号）
    这里我们保留：从 card 的 code 推出 prefix candidates（用于 S/{prefix}-{NNN}.jpg）
    code 是 13 位形如 2251141100113
    我们会从中抽取 YYMMDD + B 的组合候选
    """
    # 找到类似 25 11 14 1 的段（Goethe 是 2511411）
    # 经验：code 中会包含 YYMMDD + troupeIdx
    # 例如 2251141100113 -> 2511411 在中间
    m = re.findall(r"(2\d)(\d{2})(\d{2})([1-5])", code)  # 2Y + MM + DD + B
    candidates = []
    for yy, mm, dd, b in m:
        candidates.append(f"{yy}{mm}{dd}{b}")
    # 也尝试更宽松：抓 25xxxx? 7 位
    m2 = re.findall(r"(2\d\d{5}[1-5])", code)
    candidates += m2
    # 去重
    seen = set()
    out = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out

def probe_sequence_S(prefix: str, start: int, max_images: int, timeout: float, miss_stop: int = 30) -> List[str]:
    """
    探测 S/{prefix}-{NNN}.jpg，最多 max_images，连续 miss_stop 次未命中就停止。
    """
    found = []
    misses = 0
    n = start
    while len(found) < max_images:
        url = f"{BASE}/img/goods/S/{prefix}-{n:03d}.jpg"
        ok = http_head_ok(url, timeout=timeout)
        if ok:
            found.append(url)
            misses = 0
        else:
            misses += 1
            if misses >= miss_stop and len(found) > 0:
                break
            # 如果从头开始一直 miss，避免太久：miss_stop 达到也停
            if misses >= miss_stop and len(found) == 0:
                break
        n += 1
    return found

def probe_sequence_L_by_stem(code: str, start_suffix: int, max_images: int, timeout: float, miss_stop: int = 60) -> List[str]:
    """
    探测 L/{stem}{suffix:04d}.jpg
    例：2251141100113 -> stem=2251141100 -> L/2251141100229.jpg 等
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
            if misses >= miss_stop and len(found) > 0:
                break
            if misses >= miss_stop and len(found) == 0:
                break
        s += 1
    return found

def probe_single_L_code(code: str, timeout: float) -> List[str]:
    """
    探测 L/{code}.jpg 这种“单张直连”
    """
    url = f"{BASE}/img/goods/L/{code}.jpg"
    return [url] if http_head_ok(url, timeout=timeout) else []

def multi_head_probe(urls: List[str], timeout: float = 10.0, max_workers: int = 16) -> List[str]:
    """
    并发 HEAD 验证，保序返回有效图片
    """
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
    page_size: int = 40,
    timeout: float = 15.0,
):
    """
    Step 1: 搜索卡片（小卡）商品列表，返回 {url, code, title}
    - keyword 默认：コレクションカード
    - title_filter：可输入 Goethe / ゲーテ / 花組 等
    """
    try:
        params = {
            "search": "search",
            "keyword": keyword,
        }
        r = http_get(SEARCH_URL, timeout=timeout)
        # 直接请求搜索页需要 query 参数
        r = requests.get(SEARCH_URL, params=params, headers=DEFAULT_HEADERS, timeout=timeout)
        html = r.text
        soup = BeautifulSoup(html, "lxml")

        # 商品列表链接通常是 /shop/g/gXXXXXXXXXXXX/
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/shop/g/g") and href.endswith("/"):
                links.append(BASE + href)

        # 去重
        uniq = []
        seen = set()
        for u in links:
            if u not in seen:
                seen.add(u)
                uniq.append(u)

        # 限制数量（避免太大）
        uniq = uniq[: max(10, page_size)]

        # 拉取每个商品页标题（并发）
        def fetch_one(u: str) -> Dict[str, str]:
            code = u.rstrip("/").split("/g/g")[-1]
            try:
                rr = http_get(u, timeout=timeout)
                title = extract_goods_title(rr.text)
            except Exception:
                title = ""
            return {"url": u, "code": code, "title": title}

        items = []
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = [ex.submit(fetch_one, u) for u in uniq]
            for fut in as_completed(futs):
                items.append(fut.result())

        # 过滤（title_filter）
        if title_filter:
            filters = [f.lower() for f in title_filter if f.strip()]
            def match(t: str) -> bool:
                tl = (t or "").lower()
                return all(f in tl for f in filters)
            items = [it for it in items if match(it.get("title", ""))]

        # 排序：title 有值的靠前
        items.sort(key=lambda x: (0 if x.get("title") else 1, x.get("title") or "", x.get("code") or ""))
        return JSONResponse({"keyword": keyword, "title_filter": title_filter, "results": items})
    except Exception as e:
        logging.error("CARDS API ERROR: %s", e)
        traceback.print_exc()
        return JSONResponse({"error": "cards_failed", "message": str(e), "results": []}, status_code=200)

@app.get("/api/card_images")
def api_card_images(
    card_url: str,
    extra_prefix: List[str] = Query(default=[]),
    max_images: int = 200,
    start_n: int = 1,
    timeout: float = 12.0,
):
    """
    Step 2: 根据小卡链接推测图片链接（最多 max_images 张）
    支持多种策略：
    - 直接从商品页提取图片（若有）
    - L/{code}.jpg 单图
    - S/{prefix}-{NNN}.jpg 序列（prefix 来自 code 推测 + extra_prefix）
    - L/{stem}{suffix:04d}.jpg 序列（DEAN 类）
    """
    try:
        if not card_url.startswith("http"):
            card_url = BASE + card_url

        code = card_url.rstrip("/").split("/g/g")[-1]

        # 先拉商品页
        rr = http_get(card_url, timeout=timeout)
        html = rr.text
        title = extract_goods_title(html)
        direct_imgs = extract_images_from_goods_page(html)

        prefix_candidates = guess_prefix_candidates_from_code(code)
        # 增加用户手工补充的 prefix（用于新人公演等特殊情况）
        for p in extra_prefix:
            p = p.strip()
            if p:
                prefix_candidates.append(p)

        # 去重
        seen = set()
        pc = []
        for p in prefix_candidates:
            if p not in seen:
                seen.add(p)
                pc.append(p)
        prefix_candidates = pc

        # 策略1：商品页直接提取到的图片（先校验）
        direct_imgs_ok = multi_head_probe(direct_imgs, timeout=timeout) if direct_imgs else []

        # 策略2：L/{code}.jpg
        single_L = probe_single_L_code(code, timeout=timeout)

        # 策略3：S/{prefix}-{NNN}.jpg
        seq_S_all = []
        picked_prefix_S = None
        for p in prefix_candidates:
            seq = probe_sequence_S(p, start=start_n, max_images=max_images, timeout=timeout)
            if len(seq) > len(seq_S_all):
                seq_S_all = seq
                picked_prefix_S = p

        # 策略4：L/{stem}{suffix}.jpg（DEAN / 特殊摄影类）
        # 从 0200 起步更接近你给的例子（0229），你也可以改成 0001
        seq_L_stem = probe_sequence_L_by_stem(code, start_suffix=200, max_images=max_images, timeout=timeout)

        # 合并（去重保序）：优先 direct -> single -> S序列 -> L序列
        merged = []
        seen2 = set()
        for block in [direct_imgs_ok, single_L, seq_S_all, seq_L_stem]:
            for u in block:
                if u not in seen2:
                    seen2.add(u)
                    merged.append(u)

        strategy = {
            "direct_from_goods_page": len(direct_imgs_ok),
            "single_L_code": len(single_L),
            "sequence_S_prefix": picked_prefix_S,
            "sequence_S_count": len(seq_S_all),
            "sequence_L_stem_count": len(seq_L_stem),
        }

        return JSONResponse({
            "card_url": card_url,
            "title": title,
            "code": code,
            "prefix_candidates": prefix_candidates,
            "picked_prefix_S": picked_prefix_S,
            "max_images": max_images,
            "strategy": strategy,
            "images": merged,
        })
    except Exception as e:
        logging.error("CARD_IMAGES API ERROR: %s", e)
        traceback.print_exc()
        return JSONResponse({"error": "card_images_failed", "message": str(e), "images": []}, status_code=200)