from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional, Dict, Any
import logging, traceback, re, time
import requests
from bs4 import BeautifulSoup

# 你原来已有的逻辑（不改）
from logic import bro_guess, program_search

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Takarazuka Link Finder", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 本地联调方便；上线后建议收紧
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

def _http_get(url: str, timeout: float = 15.0) -> requests.Response:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.8,en;q=0.6,zh-CN;q=0.5",
        "Connection": "keep-alive",
    }
    return requests.get(url, headers=headers, timeout=timeout)

def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out

def _extract_goods_links_from_html(html: str) -> List[str]:
    """
    从搜索页抓取 /shop/g/gXXXXXXXX/ 这种商品小卡链接
    """
    soup = BeautifulSoup(html, "lxml")
    urls = []
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        # 统一成绝对链接
        if href.startswith("/shop/g/g"):
            urls.append("https://shop.tca-pictures.net" + href)
        elif href.startswith("https://shop.tca-pictures.net/shop/g/g"):
            urls.append(href)
    # 只保留 /shop/g/g123456/ 形式
    urls = [u for u in urls if re.search(r"/shop/g/g\d+/?$", u)]
    return _dedupe_keep_order(urls)

def cards_search(keyword: str, title_filter: List[str], timeout: float = 15.0) -> List[Dict[str, str]]:
    """
    你最新研究的第 1 步：爬搜索界面的“小卡链接”
    """
    # 注意：TCA 的搜索参数有点怪，这里按你给的示例做（search=x & search=search）
    # keyword 需要 URL 编码，但 requests 会帮我们处理 params
    url = "https://shop.tca-pictures.net/shop/goods/search.aspx"
    params = {
        "search": "x",
        "keyword": keyword,
        "search": "search",
    }
    r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()

    urls = _extract_goods_links_from_html(r.text)

    # title_filter：这里做“宽松过滤”：只要商品页 HTML 中命中任意一个词即可
    # （避免你之前那种“必须全部命中”导致全被过滤掉）
    tf = [t.strip() for t in title_filter if t and t.strip()]
    results: List[Dict[str, str]] = []

    if not tf:
        for u in urls:
            m = re.search(r"/g/g(\d+)/?$", u)
            results.append({"url": u, "code": m.group(1) if m else ""})
        return results

    for u in urls:
        try:
            rr = _http_get(u, timeout=timeout)
            html = rr.text
            hit = False
            for t in tf:
                if t.lower() in html.lower():
                    hit = True
                    break
            if hit:
                m = re.search(r"/g/g(\d+)/?$", u)
                results.append({"url": u, "code": m.group(1) if m else ""})
        except Exception:
            # 过滤失败不要影响全局
            continue

    return results

def derive_image_prefix_candidates(card_code: str) -> List[str]:
    """
    你最新研究的第 2 步：从小卡 code 推导图片前缀（如 2511411）
    经验规则：优先从 code 中提取 YYMM + troupe + ??? 组合
    但你现在已经能从 card -> prefix_candidates/picked_prefix 得到稳定结果
    所以这里给一个“尽量不误伤”的候选策略：
    - 如果 code 是 13 位：2251141100113（例）
      我们先尝试：25 11 41 1  -> 2511411（你贴的 picked_prefix 就是它）
    """
    digits = re.sub(r"\D+", "", card_code or "")
    cands = []

    # 常见：2251141100113 -> 2511411
    # 取 digits 的第 3-8 位当 YYMM??，再拼 troupe 位
    # 更稳：直接用正则抓 "2511411" 这种 7 位
    # 这里按照你成功案例：从 digits 中构造 25 + 114 + 11? 不可靠
    # 所以采用：扫描可能出现的 "25xxxx1" 形式
    for m in re.finditer(r"(2[0-9]{1}[0-9]{4}[1-5])", digits):
        # 2 + 6位 + troupe(1-5) => 总长度 8；但你 prefix 是 7 位，所以这里不直接用
        pass

    # 直接按你提供的成功映射：2251141100113 -> 2511411
    # 规则：取 digits[2:4]=25? 不对。我们采用固定： "25" + digits[3:7] + digits[7]（更贴近你例子）
    # digits: 2 2 5 1 1 4 1 1 0 0 1 1 3
    #          0 1 2 3 4 5 6 7 8 9 10 11 12
    # 目标 2511411 = 25 + 114 + 11? => 25(2-3?) + 114(4-6?) + 1(7?) ——我们直接写成：
    try:
        if len(digits) >= 8:
            yy = digits[2:4]      # "51"（不对）
            # 所以不用上面这个
    except Exception:
        pass

    # 采用更稳定的：从 code 的中间取出 "11411" 这种结构，拼成 "25"+"114"+"11"? ——仍然不通用
    # 最终：我们以“回退策略”为主：由 API 自己在尝试图片是否存在时自动挑对 prefix。
    # 给几个可能的前缀：从 code 中提取连续的 6 位 + troupe 位
    # 这里给一个可工作的候选：取 digits 中的第 3-9 位，滑窗拼成 7 位且以 2 开头（例如 2511411）
    for i in range(0, max(0, len(digits) - 6)):
        seg = digits[i:i+7]
        if re.fullmatch(r"2\d{5}[1-5]", seg):
            cands.append(seg)

    # 再加一个“经验：25 开头 + 6 位”
    for i in range(0, max(0, len(digits) - 6)):
        seg = digits[i:i+6]
        if re.fullmatch(r"\d{6}", seg):
            cands.append("25" + seg[-4:] + "1")  # 兜底，保证有候选

    cands = _dedupe_keep_order([c for c in cands if len(c) == 7])
    return cands[:8]

def probe_images(prefix: str, start: int = 1, max_count: int = 60, timeout: float = 10.0) -> List[str]:
    """
    尝试从 https://shop.tca-pictures.net/img/goods/S/{prefix}-{NNN}.jpg 连续探测图片
    规则：允许中间少量断档（防止只因为某一张缺就提前停止）
    """
    found = []
    miss_streak = 0
    max_miss_streak = 8  # 连续 8 张不存在就停
    for n in range(start, start + max_count):
        url = f"https://shop.tca-pictures.net/img/goods/S/{prefix}-{n:03d}.jpg"
        try:
            # 用 HEAD 更快；若站点不支持 HEAD，则 fallback GET
            resp = requests.head(url, headers={"User-Agent": UA}, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200 and (resp.headers.get("content-type","").startswith("image/") or resp.headers.get("content-length")):
                found.append(url)
                miss_streak = 0
            else:
                miss_streak += 1
        except Exception:
            miss_streak += 1

        if miss_streak >= max_miss_streak and len(found) > 0:
            break
    return found

def card_images(card_url: str, timeout: float = 15.0) -> Dict[str, Any]:
    """
    输入小卡链接 => 输出图片列表（自动选出正确 prefix）
    """
    m = re.search(r"/g/g(\d+)/?$", card_url)
    code = m.group(1) if m else ""

    prefix_candidates = derive_image_prefix_candidates(code)
    # 如果候选为空，尝试从 card_url 页面再抽一次
    if not prefix_candidates:
        prefix_candidates = []

    picked_prefix = None
    images: List[str] = []

    # 优先：尝试从候选中找能命中的
    for p in prefix_candidates:
        imgs = probe_images(p, start=1, max_count=80, timeout=min(10.0, timeout))
        if len(imgs) >= 3:
            picked_prefix = p
            images = imgs
            break

    # 兜底：如果候选都不行，做一个小范围 brute force（仅在非常必要时）
    # 例如你知道 Goethe 是 2511441，但你的小卡 code 推不出
    if not images:
        # 尝试从 card_code 中取出可能的 YYMMDD+troupe 结构，构造 25?????
        # 这里保守一点：只扫 25xxxx1-5 的 7 位前缀（最多 120 次）
        brute = []
        digits = re.sub(r"\D+", "", code)
        # 从 digits 中抓 4 位窗口作为“MMDD”并拼 "25" + MMDD + troupe
        for i in range(0, max(0, len(digits) - 3)):
            mmdd = digits[i:i+4]
            if re.fullmatch(r"\d{4}", mmdd):
                for troupe in ["1","2","3","4","5"]:
                    brute.append("25" + mmdd + troupe)
        brute = _dedupe_keep_order(brute)[:120]

        for p in brute:
            imgs = probe_images(p, start=1, max_count=60, timeout=min(10.0, timeout))
            if len(imgs) >= 3:
                picked_prefix = p
                images = imgs
                prefix_candidates = _dedupe_keep_order(prefix_candidates + [p])
                break

    return {
        "card_url": card_url,
        "code": code,
        "prefix_candidates": prefix_candidates,
        "picked_prefix": picked_prefix,
        "images": images,
    }


@app.get("/ping")
def ping():
    return {"ok": True, "msg": "pong"}


@app.get("/api/program")
def api_program(
    year: int = 2025,
    q: List[str] = Query(default=[]),
    delay_min: float = 0.6,
    delay_max: float = 1.5,
    timeout: float = 15.0
):
    try:
        rows = program_search(year, q, delay_min, delay_max, timeout)
        return JSONResponse({"year": year, "queries": q, "results": rows})
    except Exception as e:
        logging.error("PROGRAM API ERROR: %s", e)
        traceback.print_exc()
        return JSONResponse(
            {"error": "program_search_failed", "message": str(e), "queries": q, "results": []},
            status_code=200
        )


@app.get("/api/bro")
def api_bro(
    prefix: str,
    ss_min: int = 1,
    ss_max: int = 40,
    delay_min: float = 0.6,
    delay_max: float = 1.5,
    timeout: float = 15.0
):
    try:
        rows = bro_guess(prefix, ss_min, ss_max, delay_min, delay_max, timeout)
        return JSONResponse({"prefix": prefix, "results": rows})
    except Exception as e:
        logging.error("BRO API ERROR: %s", e)
        traceback.print_exc()
        return JSONResponse({"error": "bro_failed", "message": str(e), "results": []}, status_code=200)


@app.get("/api/goethe")
def api_goethe(
    ss_min: int = 1, ss_max: int = 40,
    delay_min: float = 0.6, delay_max: float = 1.6,
    timeout: float = 15.0
):
    try:
        forum = bro_guess("2511161", ss_min, ss_max, delay_min, delay_max, timeout)
        umeda = bro_guess("2512011", ss_min, ss_max, delay_min, delay_max, timeout)
        pro = program_search(2025, ["Goethe", "花組"], delay_min, delay_max, timeout)
        return JSONResponse({"forum_prefix": "2511161", "umeda_prefix": "2512011",
                             "forum": forum, "umeda": umeda, "program": pro})
    except Exception as e:
        logging.error("GOETHE API ERROR: %s", e)
        traceback.print_exc()
        return JSONResponse({"error": "goethe_failed", "message": str(e), "forum": [], "umeda": [], "program": []}, status_code=200)


# ✅ 新增：小卡搜索（你现在需要的关键接口）
@app.get("/api/cards")
def api_cards(
    keyword: str = "コレクションカード",
    title_filter: List[str] = Query(default=[]),
    timeout: float = 15.0
):
    try:
        rows = cards_search(keyword, title_filter, timeout=timeout)
        return JSONResponse({"keyword": keyword, "title_filter": title_filter, "results": rows})
    except Exception as e:
        logging.error("CARDS API ERROR: %s", e)
        traceback.print_exc()
        return JSONResponse({"error": "cards_failed", "message": str(e), "results": []}, status_code=200)


# ✅ 新增：小卡 => 图片序列（你现在需要的关键接口）
@app.get("/api/card_images")
def api_card_images(
    card_url: str,
    timeout: float = 15.0
):
    try:
        data = card_images(card_url, timeout=timeout)
        return JSONResponse(data)
    except Exception as e:
        logging.error("CARD_IMAGES API ERROR: %s", e)
        traceback.print_exc()
        return JSONResponse({"error": "card_images_failed", "message": str(e), "images": []}, status_code=200)
