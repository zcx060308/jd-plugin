"""
京东搜索爬虫服务 - Coze自定义插件后端 (带代理池版)
部署: pip install -r requirements.txt
运行: uvicorn app:app --host 0.0.0.0 --port $PORT
"""

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import re
import json
import random
import urllib.parse
import asyncio
import time

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]


async def fetch_jd_search(keyword: str, page: int = 1):
    """通过 ScrapingAnt 代理抓取京东搜索页 (支持 JS 渲染)"""
    import os
    encoded = urllib.parse.quote(keyword)
    target_url = f"https://so.m.jd.com/ware/search.action?keyword={encoded}&page={page}"

    # 从环境变量读取 ScrapingAnt API Key
    scrapingant_key = os.environ.get("SCRAPINGANT_API_KEY", "").strip()

    if not scrapingant_key:
        raise Exception("未配置 SCRAPINGANT_API_KEY 环境变量, 请在 Render 后台设置")

    # ScrapingAnt API URL
    # 参数说明:
    #   url: 目标网址
    #   x-api-key: 你的 API Key
    #   browser=true: 用真实 Chrome 浏览器 (京东必须, 否则过不了反爬)
    #   render_js=true: 渲染 JS
    #   proxy_country=CN: 用中国代理
    #   wait_for_selector: 等商品列表加载完
    api_url = "https://api.scrapingant.com/v2/general"
    params = {
        "url": target_url,
        "x-api-key": scrapingant_key,
        "browser": "true",       # 真实浏览器
        "render_js": "true",
        "proxy_country": "JP",
        "wait_for_selector": ".goods-list, .search-pro-list, .product-list, .gl-i-wrap, body",
        "timeout": "60"           # 浏览器模式最大 60秒
    }

    async with httpx.AsyncClient(timeout=75) as client:
        resp = await client.get(api_url, params=params)
        if resp.status_code != 200:
            raise Exception(
                f"ScrapingAnt 返回错误: status={resp.status_code}, "
                f"body={resp.text[:200]}"
            )
        return resp.text


def parse_jd_html(html: str) -> list:
    """解析京东搜索页HTML, 提取商品列表(三层策略)"""
    products = []

    # 策略1: window.__SEARCH_RESULT__ 内嵌JSON
    m = re.search(r'window\.__SEARCH_RESULT__\s*=\s*(\{[\s\S]*?\});', html)
    if m:
        try:
            data = json.loads(m.group(1))
            items = (
                data.get('wareInfo')
                or data.get('itemList')
                or data.get('searchResultList')
                or data.get('data', {}).get('searchResultList', [])
            )
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
            if products:
                return products
        except Exception:
            pass

    # 策略2: JSON-LD 结构化数据
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
        except Exception:
            continue
    if products:
        return products

    # 策略3: 正则兜底(静态HTML标签)
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
    page: int = Query(1, ge=1, le=5, description="页码 1-5")
):
    """扣子插件调用接口"""
    try:
        html = await fetch_jd_search(keyword, page)

        if "京东验证" in html or "risk_handler" in html or "bp_bizid" in html:
            return {
                "success": False,
                "products": [],
                "count": 0,
                "error": "触发京东反爬验证",
                "keyword": keyword,
                "page": page
            }

        if len(html) < 500:
            return {
                "success": False,
                "products": [],
                "count": 0,
                "error": f"响应过短({len(html)}字符)",
                "keyword": keyword,
                "page": page
            }

        products = parse_jd_html(html)
        return {
            "success": True,
            "products": products,
            "count": len(products),
            "keyword": keyword,
            "page": page
        }
    except Exception as e:
        return {
            "success": False,
            "products": [],
            "count": 0,
            "error": str(e),
            "keyword": keyword,
            "page": page
        }


@app.get("/health")
async def health():
    return {"status": "ok"}
