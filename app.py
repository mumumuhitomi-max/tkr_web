from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Optional, Tuple
import logging, traceback, re, asyncio
import httpx
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Takarazuka Link Finder", version="2.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*","null","http://127.0.0.1:5173","http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8,zh-CN;q=0.7,zh;q=0.6",
}

BASE = "https://shop.tca-pictures.net"

# ---------------------------
# Utilities
# ---------------------------

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def _safe_int(x: str, default: int = 0) -> int:
    try:
        return int(x)
    except:
        return default

async def _fetch_text(client: httpx.AsyncClient, url: str, timeout: float) -> str:
    r = await client.get(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r.text

async def _head_ok(client: httpx.AsyncClient, url: str, timeout: float) -> bool:
    try:
        r = await client.head(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
        # 有些站对 HEAD 不友好，退化成 GET 探测
        if r.status_code in (405, 403):
            rg = await client.get(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
            return rg.status_code == 200 and (rg.headers.get("content-type","").startswith("image/") or "image" in rg.headers.get("content-type",""))
        return r.status_code == 200
    except:
        return False

def _extract_code_from_product_url(url: str) -> Optional[str]:
    # https://shop.tca-pictures.net/shop/g/g2251141100113/
    m = re.search(r"/g/g(\d{10,16})/?", url)
    return m.group(1) if m else None

def _derive_troupe_digit_from_code(code: str) -> Optional[str]:
    """
    经验：小卡 code 内常含 2504014 这种 7 位片段，最后 1 位是组别(花1月2雪3星4宙5)。
    如果找不到，就返回 None。
    """
    if not code:
        return None
    # 优先抓 7 位形如 25xxxx?
    segs = re.findall(r"(2\d{6})", code)
    if segs:
        # 取最像 YYMMDDT 的那个（YY=25/26...）
        # 这里直接取第一个足够用了
        return segs[0][-1]
    return None

def _extract_embedded_prefix_candidates(code: str) -> List[str]:
    """
    从 code 里直接提取可能出现的 7位 prefix，如 2504014。
    """
    if not code:
        return []
    segs = re.findall(r"(2\d{6})", code)
    # 去重保序
    seen = set()
    out = []
    for s in segs:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out

def _build_month_scan_candidates(year: int, troupe_digit: str, months: List[int], dd_range: Tuple[int,int]) -> List[str]:
    """
    生成 YYMMDDT 形式 prefix（7位）。dd_range 比如(1, 20)。
    """
    yy = str(year)[-2:]
    dd_start, dd_end = dd_range
    cands = []
    for mm in months:
        for dd in range(dd_start, dd_end + 1):
            cands.append(f"{yy}{mm:02d}{dd:02d}{troope_digit}")  # 7 digits
    return cands

def _near_months_from_title(title: str, year: int) -> List[int]:
    """
    尝试从标题里提取类似 '2025年11月'、'11月' 之类；提取不到则返回全年 1..12。
    这一步是为了更快探测“东京prefix/新人prefix”等。
    """
    if not title:
        return list(range(1,13))
    months = []
    # 2025年11月 / 2025年 11月
    for m in re.findall(rf"{year}\s*年\s*(\d{{1,2}})\s*月", title):
        months.append(_safe_int(m))
    # 11月
    for m in re.findall(r"(\d{1,2})\s*月", title):
        months.append(_safe_int(m))
    months = [m for m in months if 1 <= m <= 12]
    if months:
        # 去重保序
        seen=set(); out=[]
        for m in months:
            if m not in seen:
                seen.add(m); out.append(m)
        return out
    return list(range(1,13))

def _img_url_S(prefix: str, nnn: int) -> str:
    return f"{BASE}/img/goods/S/{prefix}-{nnn:03d}.jpg"

def _img_url_L(code: str) -> str:
    return f"{BASE}/img/goods/L/{code}.jpg"


# ---------------------------
# Core: cards search
# ---------------------------

async def cards_search(keyword: str, title_filter: List[str], timeout: float = 15.0, max_pages: int = 10) -> List[Dict[str,str]]:
    """
    抓取搜索页：商品小卡链接。
    注意：搜索页分页结构可能变化，所以做得尽量宽松：
    - 逐页抓取
    - 在页面里找 /shop/g/gXXXXXXXXXXXX/ 的链接
    - 取出 code，并尽力抓到商品标题（如果页面能抓到的话）
    """
    if not keyword:
        return []

    # 你给的样例链接形如：
    # https://shop.tca-pictures.net/shop/goods/search.aspx?search=x&keyword=...&search=search
    # 实际上参数有点怪，但我们尽量复用它的形式：
    search_url = f"{BASE}/shop/goods/search.aspx?search=x&keyword={httpx.QueryParams({'k':keyword})['k']}&search=search"
    # 上面那种 URL 编码技巧不稳定，下面直接用 params 更稳
    params = {"search": "x", "keyword": keyword}

    async with httpx.AsyncClient() as client:
        results: List[Dict[str,str]] = []
        seen_codes = set()

        for page in range(1, max_pages + 1):
            # 很多 EC 的分页参数是 page / p / etc，这里做两种尝试：
            # 1) 不带 page
            # 2) 带 page=2...
            page_params = dict(params)
            if page > 1:
                page_params["page"] = str(page)

            try:
                r = await client.get(f"{BASE}/shop/goods/search.aspx", params=page_params, headers=HEADERS, timeout=timeout, follow_redirects=True)
                if r.status_code != 200:
                    break

                html = r.text
                soup = BeautifulSoup(html, "lxml")

                # 找所有商品详情链接
                links = []
                for a in soup.select("a[href]"):
                    href = a.get("href","")
                    if "/shop/g/g" in href:
                        # 可能是相对路径
                        if href.startswith("http"):
                            links.append(href)
                        else:
                            links.append(BASE + href)

                # 去重并抽取 code
                page_items = []
                for u in links:
                    code = _extract_code_from_product_url(u)
                    if not code:
                        continue
                    if code in seen_codes:
                        continue
                    seen_codes.add(code)
                    # 尝试找标题：用 a 标签文本，如果太短就先留空，后续由 /api/images 再补
                    t = _norm("")
                    # 尝试：找到同 href 的 a
                    # 这里随缘，不保证
                    page_items.append({"url": u, "code": code, "title": t})

                # title filter：如果用户给了过滤词，我们用“懒办法”：请求商品页拿 title 再过滤
                # 为了性能：只有 title_filter 非空时才逐个补 title
                if title_filter:
                    filtered = []
                    for it in page_items:
                        try:
                            prod_html = await _fetch_text(client, it["url"], timeout)
                            ps = BeautifulSoup(prod_html, "lxml")
                            title = _norm(ps.title.get_text()) if ps.title else ""
                            # 页面title一般包含店名，清理一下
                            title = re.sub(r"\s*【.*?】\s*$", "", title)
                            it["title"] = title
                            ok = True
                            for f in title_filter:
                                if f and f not in title:
                                    ok = False
                                    break
                            if ok:
                                filtered.append(it)
                        except:
                            # 拿不到 title 就跳过
                            pass
                    page_items = filtered
                else:
                    # 不过滤也可以补一次 title（但会很慢），所以默认不补
                    pass

                results.extend(page_items)

                # 简单的“终止条件”：如果这一页没有新增商品，认为到底了
                if not page_items and page > 1:
                    break

            except Exception:
                break

        return results


# ---------------------------
# Core: image probing
# ---------------------------

async def detect_prefix_groups(
    card_url: Optional[str],
    code: Optional[str],
    max_images: int,
    timeout: float,
    dd_scan_max: int,
) -> Dict[str, Any]:
    """
    给 card_url / code，返回：
    - card_image: L/{code}.jpg
    - special_images: 从商品页里发现的其它 L/{code}.jpg
    - prefix_groups: 探测到存在的 prefix => images(S/{prefix}-{NNN}.jpg)
    """
    if not code and card_url:
        code = _extract_code_from_product_url(card_url)

    if not code:
        return {"error": "missing_code", "message": "need card_url or code"}

    async with httpx.AsyncClient() as client:
        # 1) 尝试抓商品页 title + 提取可能出现的其它大图 code（L/xxxxxxxx.jpg）
        title = ""
        embedded_codes: List[str] = []
        if card_url:
            try:
                prod_html = await _fetch_text(client, card_url, timeout)
                ps = BeautifulSoup(prod_html, "lxml")
                title = _norm(ps.title.get_text()) if ps.title else ""
                title = re.sub(r"\s*【.*?】\s*$", "", title)
                # 抓页面内出现的 L/xxxxxxxx.jpg
                for img in ps.select("img[src]"):
                    src = img.get("src","")
                    m = re.search(r"/img/goods/L/(\d+)\.jpg", src)
                    if m:
                        embedded_codes.append(m.group(1))
                # 也可能写在脚本/链接里
                for m in re.findall(r"/img/goods/L/(\d+)\.jpg", prod_html):
                    embedded_codes.append(m)
            except:
                pass

        # 去重 embedded_codes，且剔除自身 code
        seen=set()
        special_codes=[]
        for c in embedded_codes:
            if c == code:
                continue
            if c not in seen:
                seen.add(c)
                special_codes.append(c)

        # 2) 小卡主图（固定：L/{code}.jpg）
        card_img = _img_url_L(code)
        card_img_ok = await _head_ok(client, card_img, timeout)

        # 3) 特殊画像：L/{othercode}.jpg（例如你提到 DEAN 的 2251141100229.jpg）
        special_images = []
        for c in special_codes:
            u = _img_url_L(c)
            if await _head_ok(client, u, timeout):
                special_images.append(u)

        # 4) 标准舞写/定妆：S/{prefix}-{NNN}.jpg
        # 4.1 先从 code 中提取直嵌 prefix（通常就是大剧场那套）
        embedded_prefixes = _extract_embedded_prefix_candidates(code)

        # 4.2 推断组别 digit
        troupe_digit = _derive_troupe_digit_from_code(code)

        # 4.3 生成“东京/新人/其它”候选 prefix：
        # - 以 year=20YY，从标题里尽量猜月份；否则扫全年
        year = 2000 + int(str(code)[2:4]) if len(code) >= 4 and str(code)[2:4].isdigit() else 2025
        months = _near_months_from_title(title, year)

        # dd 范围：1..dd_scan_max（默认 20，覆盖 01/02/11 这类常见情况）
        month_scan = []
        if troupe_digit:
            month_scan = _build_month_scan_candidates(year, troupe_digit, months, (1, dd_scan_max))

        # 合并候选：embedded_prefixes 优先 + 扫月
        candidates = []
        seenp=set()
        for p in (embedded_prefixes + month_scan):
            if p not in seenp:
                seenp.add(p)
                candidates.append(p)

        # 4.4 探测：对每个 prefix 先探测 001/002/003 是否存在，有就认为这一套 prefix 生效
        prefix_groups: Dict[str, List[str]] = {}
        probe_nums = [1, 2, 3]
        for p in candidates:
            ok_any = False
            for n in probe_nums:
                if await _head_ok(client, _img_url_S(p, n), timeout):
                    ok_any = True
                    break
            if not ok_any:
                continue

            # 找到有效 prefix 后：拉取最多 max_images（默认 200）
            # 用“连续 miss 阈值”加速：连续 20 张不存在就停
            imgs = []
            miss = 0
            for n in range(1, max_images + 1):
                u = _img_url_S(p, n)
                if await _head_ok(client, u, timeout):
                    imgs.append(u)
                    miss = 0
                else:
                    miss += 1
                    if miss >= 20 and n >= 30:
                        break
            if imgs:
                prefix_groups[p] = imgs

            # 防止扫太久：如果已经找到了 4 组 prefix 就停止（一般够用了）
            if len(prefix_groups) >= 4:
                break

        return {
            "code": code,
            "card_url": card_url,
            "title": title,
            "card_image": card_img if card_img_ok else "",
            "special_images": special_images,
            "embedded_prefixes": embedded_prefixes,
            "troupe_digit": troupe_digit or "",
            "prefix_groups": prefix_groups,  # 多套 prefix（可能对应：大剧场/东京/新人/其它）
        }


# ---------------------------
# Routes
# ---------------------------

@app.get("/ping")
def ping():
    return {"ok": True, "msg": "pong"}

@app.get("/api/cards")
async def api_cards(
    keyword: str = "コレクションカード",
    title_filter: List[str] = Query(default=[]),
    timeout: float = 15.0,
    max_pages: int = 10,
):
    """
    Step 1: 搜索小卡商品链接。
    - keyword 默认：コレクションカード
    - title_filter 可选：例如 Goethe / ゲーテ / 花組
    """
    try:
        items = await cards_search(keyword, title_filter, timeout=timeout, max_pages=max_pages)
        return JSONResponse({"keyword": keyword, "title_filter": title_filter, "results": items})
    except Exception as e:
        logging.error("CARDS API ERROR: %s", e)
        traceback.print_exc()
        return JSONResponse({"error": "cards_failed", "message": str(e), "results": []}, status_code=200)

@app.get("/api/images")
async def api_images(
    card_url: str = "",
    code: str = "",
    max_images: int = 200,
    timeout: float = 15.0,
    dd_scan_max: int = 20,
):
    """
    Step 2: 探图（一步完成）
    你可以传 card_url 或 code：
    - card_url: https://shop.tca-pictures.net/shop/g/g2250401400116/
    - code: 2250401400116
    返回：
    - card_image (L/{code}.jpg)
    - special_images (L/{othercode}.jpg)
    - prefix_groups: {prefix: [S/{prefix}-001..]}
    """
    try:
        data = await detect_prefix_groups(
            card_url=card_url or None,
            code=code or None,
            max_images=max_images,
            timeout=timeout,
            dd_scan_max=dd_scan_max,
        )
        return JSONResponse(data)
    except Exception as e:
        logging.error("IMAGES API ERROR: %s", e)
        traceback.print_exc()
        return JSONResponse({"error": "images_failed", "message": str(e)}, status_code=200)
