from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Optional, Tuple
import logging, traceback, re
import httpx
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Takarazuka Link Finder", version="2.2.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*", "null", "http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8,zh-CN;q=0.7,zh;q=0.6",
}

BASE = "https://shop.tca-pictures.net"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


async def _fetch_text(client: httpx.AsyncClient, url: str, timeout: float) -> str:
    r = await client.get(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r.text


async def _head_ok(client: httpx.AsyncClient, url: str, timeout: float) -> bool:
    try:
        r = await client.head(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
        if r.status_code in (405, 403):
            rg = await client.get(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
            return rg.status_code == 200 and ("image" in rg.headers.get("content-type", ""))
        return r.status_code == 200
    except:
        return False


def _extract_code_from_product_url(url: str) -> Optional[str]:
    m = re.search(r"/g/g(\d{10,16})/?", url)
    return m.group(1) if m else None


def _derive_troupe_digit_from_code(code: str) -> Optional[str]:
    """
    常见：小卡 code 中包含 2504014 这种 7 位片段（YYMMDDT），最后 1 位是组别数字。
    花1 月2 雪3 星4 宙5
    """
    if not code:
        return None
    segs = re.findall(r"(2\d{6})", code)
    if segs:
        return segs[0][-1]
    return None


def _extract_embedded_prefix_candidates(code: str) -> List[str]:
    if not code:
        return []
    segs = re.findall(r"(2\d{6})", code)
    seen = set()
    out = []
    for s in segs:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _near_months_from_title(title: str, year: int) -> List[int]:
    if not title:
        return list(range(1, 13))
    months = []
    for m in re.findall(rf"{year}\s*年\s*(\d{{1,2}})\s*月", title):
        try:
            months.append(int(m))
        except:
            pass
    for m in re.findall(r"(\d{1,2})\s*月", title):
        try:
            months.append(int(m))
        except:
            pass
    months = [m for m in months if 1 <= m <= 12]
    if not months:
        return list(range(1, 13))
    seen = set()
    out = []
    for m in months:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _build_month_scan_candidates(year: int, troupe_digit: str, months: List[int], dd_range: Tuple[int, int]) -> List[str]:
    """
    正确拼写：troupe_digit（之前写成 troope_digit 会导致 NameError）
    生成 7位 prefix：YYMMDDT
    """
    yy = str(year)[-2:]
    dd_start, dd_end = dd_range
    cands = []
    for mm in months:
        for dd in range(dd_start, dd_end + 1):
            cands.append(f"{yy}{mm:02d}{dd:02d}{troupe_digit}")
    return cands


def _img_url_S(prefix: str, nnn: int) -> str:
    return f"{BASE}/img/goods/S/{prefix}-{nnn:03d}.jpg"


def _img_url_L(code: str) -> str:
    return f"{BASE}/img/goods/L/{code}.jpg"


async def cards_search(keyword: str, title_filter: List[str], timeout: float = 15.0, max_pages: int = 10) -> List[Dict[str, str]]:
    if not keyword:
        return []

    async with httpx.AsyncClient() as client:
        results: List[Dict[str, str]] = []
        seen_codes = set()

        for page in range(1, max_pages + 1):
            params = {"search": "x", "keyword": keyword}
            if page > 1:
                params["page"] = str(page)

            try:
                r = await client.get(f"{BASE}/shop/goods/search.aspx", params=params, headers=HEADERS, timeout=timeout, follow_redirects=True)
                if r.status_code != 200:
                    break

                soup = BeautifulSoup(r.text, "lxml")

                links = []
                for a in soup.select("a[href]"):
                    href = a.get("href", "")
                    if "/shop/g/g" in href:
                        links.append(href if href.startswith("http") else BASE + href)

                page_items = []
                for u in links:
                    code = _extract_code_from_product_url(u)
                    if not code or code in seen_codes:
                        continue
                    seen_codes.add(code)
                    page_items.append({"url": u, "code": code, "title": ""})

                if title_filter:
                    filtered = []
                    for it in page_items:
                        try:
                            prod_html = await _fetch_text(client, it["url"], timeout)
                            ps = BeautifulSoup(prod_html, "lxml")
                            title = _norm(ps.title.get_text()) if ps.title else ""
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
                            pass
                    page_items = filtered

                results.extend(page_items)

                if not page_items and page > 1:
                    break

            except:
                break

        return results


async def detect_prefix_groups(
    card_url: Optional[str],
    code: Optional[str],
    max_images: int,
    timeout: float,
    dd_scan_max: int,
) -> Dict[str, Any]:
    if not code and card_url:
        code = _extract_code_from_product_url(card_url)

    if not code:
        return {"error": "missing_code", "message": "need card_url or code"}

    async with httpx.AsyncClient() as client:
        title = ""
        embedded_codes: List[str] = []

        if card_url:
            try:
                prod_html = await _fetch_text(client, card_url, timeout)
                ps = BeautifulSoup(prod_html, "lxml")
                title = _norm(ps.title.get_text()) if ps.title else ""
                title = re.sub(r"\s*【.*?】\s*$", "", title)

                for m in re.findall(r"/img/goods/L/(\d+)\.jpg", prod_html):
                    embedded_codes.append(m)
            except:
                pass

        special_codes = []
        seen = set()
        for c in embedded_codes:
            if c == code:
                continue
            if c not in seen:
                seen.add(c)
                special_codes.append(c)

        card_img = _img_url_L(code)
        card_img_ok = await _head_ok(client, card_img, timeout)

        special_images = []
        for c in special_codes:
            u = _img_url_L(c)
            if await _head_ok(client, u, timeout):
                special_images.append(u)

        embedded_prefixes = _extract_embedded_prefix_candidates(code)
        troupe_digit = _derive_troupe_digit_from_code(code)

        year = 2025
        try:
            # 22xxxx... -> 20YY 推断意义不大，这里只用于扫 prefix，优先从 title 里判断月份
            yy = int(str(code)[2:4])
            if 0 <= yy <= 99:
                year = 2000 + yy
        except:
            pass

        months = _near_months_from_title(title, year)

        month_scan = []
        if troupe_digit:
            month_scan = _build_month_scan_candidates(year, troupe_digit, months, (1, dd_scan_max))

        candidates = []
        seenp = set()
        for p in (embedded_prefixes + month_scan):
            if p not in seenp:
                seenp.add(p)
                candidates.append(p)

        prefix_groups: Dict[str, List[str]] = {}
        probe_nums = [1, 2, 3]

        # 候选太多会慢：最多尝试 220 个
        for p in candidates[:220]:
            try:
                ok_any = False
                for n in probe_nums:
                    if await _head_ok(client, _img_url_S(p, n), timeout):
                        ok_any = True
                        break
                if not ok_any:
                    continue

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

                if len(prefix_groups) >= 6:
                    break
            except:
                continue

        return {
            "code": code,
            "card_url": card_url,
            "title": title,
            "card_image": card_img if card_img_ok else "",
            "special_images": special_images,
            "embedded_prefixes": embedded_prefixes,
            "troupe_digit": troupe_digit or "",
            "prefix_groups": prefix_groups,
        }


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
    try:
        items = await cards_search(keyword, title_filter, timeout=timeout, max_pages=max_pages)

        # ✅ 让结果“时间倒序”：通常 code 越大越新，直接按 code desc 排
        items.sort(key=lambda x: x.get("code", ""), reverse=True)

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