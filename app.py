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
    """获取京东搜索页HTML (带代理池轮询)"""
    encoded = urllib.parse.quote(keyword)
    url = f"https://so.m.jd.com/ware/search.action?keyword={encoded}&page={page}"

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
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

    # 代理池 (优先用环境变量, 没有就用内置的免费代理)
    import os
    proxy_env = os.environ.get("PROXY_URL", "").strip()
    if proxy_env:
        # 付费代理模式: 一个代理用到底
        proxies = [proxy_env]
    else:
        # 免费代理模式: 轮询多个
        proxies = [
            "",  # 不使用代理(直连)也试一次
            # 免费代理示例(从 https://proxyscrape.com 复制粘贴新的进来)
            "http://190.61.88.147:999",
            "http://45.70.198.171:999",
            "http://181.129.74.58:32650",
            "http://181.198.32.211:999",
            "http://177.93.37.55:999",
        ]

    # 轮询代理试连, 任一成功就返回
    last_error = ""
    for i, proxy in enumerate(proxies):
        try:
            proxy_url = proxy if proxy else None
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,
                proxy=proxy_url
            ) as client:
                resp = await client.get(url, headers=headers)
                html = resp.text
                # 判断是否触发反爬
                if "京东验证" not in html and "risk_handler" not in html and "bp_bizid" not in html:
                    if len(html) >= 500:
                        return html
                    last_error = f"代理{i}({proxy or '直连'}): 响应过短({len(html)}字符)={html[:50]}"
                else:
                    last_error = f"代理{i}({proxy or '直连'}): 触发反爬"
                # 触发反爬, 试下一个代理
                continue
        except Exception as e:
            last_error = f"代理{i}({proxy or '直连'}): {type(e).__name__}: {str(e)[:80]}"
            continue

    # 所有代理都失败
    raise Exception(f"所有代理都失败, 共试了{len(proxies)}个, 最后错误: {last_error}")


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
