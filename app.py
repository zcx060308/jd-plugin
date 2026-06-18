"""
京东搜索爬虫服务 - Coze自定义插件后端
部署: pip install fastapi uvicorn httpx beautifulsoup4
运行: uvicorn app:app --host 0.0.0.0 --port 8080
"""

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import re
import json
import random
from typing import Optional

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/149.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Version/17.5 Mobile/15E148 Safari/604.1",
]


async def fetch_jd_search(keyword: str, page: int = 1):
    """获取京东搜索页HTML"""
    url = f"https://so.m.jd.com/ware/search.action?keyword={keyword}&page={page}"
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "sec-ch-ua": '"Microsoft Edge";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "Sec-Fetch-Storage-Access": "active",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://so.m.jd.com/",
    }

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, http2=True) as client:
        resp = await client.get(url, headers=headers)
        return resp.text


def parse_jd_html(html: str) -> list:
    """解析京东搜索页HTML, 提取商品列表"""
    products = []

    # 策略1: window.__SEARCH_RESULT__ 内嵌JSON
    m = re.search(r'window\.__SEARCH_RESULT__\s*=\s*(\{[\s\S]*?\});', html)
    if m:
        try:
            data = json.loads(m.group(1))
            items = data.get('wareInfo', data.get('itemList', []))
            for item in items[:30]:
                products.append({
                    "title": (item.get('wname') or item.get('title') or '')[:200],
                    "price": float(item.get('jdPrice') or item.get('jd_price') or 0),
                    "shop": (item.get('shop_name') or item.get('shopName') or '')[:100],
                    "sku": str(item.get('wareId') or item.get('skuId') or ''),
                    "platform": "jd",
                    "platform_display": "京东",
                    "source_url": f"https://item.jd.com/{item.get('wareId', '')}.html"
                })
            return products
        except:
            pass

    # 策略2: JSON-LD
    ld_matches = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    for blob in ld_matches:
        try:
            ld = json.loads(blob)
            if isinstance(ld, list):
                for item in ld:
                    if item.get('@type') == 'Product':
                        products.append({
                            "title": item.get('name', '')[:200],
                            "price": float(item.get('offers', {}).get('price', 0)),
                            "shop": item.get('brand', {}).get('name', ''),
                            "sku": "",
                            "platform": "jd",
                            "platform_display": "京东",
                            "source_url": ""
                        })
        except:
            continue
    if products:
        return products

    # 策略3: 正则兜底
    skus = re.findall(r'data-sku="(\d+)"', html)
    prices = re.findall(r'<i[^>]*>[￥¥]?\s*(\d+\.?\d*)</i>', html)
    titles = re.findall(r'<em[^>]*>([^<]{5,150})</em>', html)
    shops = re.findall(r'data-shopname="([^"]+)"', html)

    for i, sku in enumerate(skus[:20]):
        p = {
            "title": titles[i] if i < len(titles) else "",
            "price": float(prices[i]) if i < len(prices) else 0.0,
            "shop": shops[i] if i < len(shops) else "",
            "sku": sku,
            "platform": "jd",
            "platform_display": "京东",
            "source_url": f"https://item.jd.com/{sku}.html"
        }
        if p["price"] > 0 or p["title"]:
            products.append(p)

    return products


@app.get("/api/jd/search")
async def jd_search(
        keyword: str = Query(..., description="搜索关键词"),
        page: int = Query(1, ge=1, le=5, description="页码1-5")
):
    """扣子插件调用接口"""
    try:
        html = await fetch_jd_search(keyword, page)

        if "京东验证" in html or len(html) < 500:
            return {"success": False, "products": [], "count": 0, "error": "触发京东反爬验证"}

        products = parse_jd_html(html)
        return {
            "success": True,
            "products": products,
            "count": len(products),
            "page": page
        }
    except Exception as e:
        return {"success": False, "products": [], "count": 0, "error": str(e)}


@app.get("/health")
async def health():
    return {"status": "ok"}