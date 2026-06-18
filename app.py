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
    """通过 ScraperAPI 抓取京东移动搜索页 (自动 JS 渲染)"""
    import os
    encoded = urllib.parse.quote(keyword)
    # 京东移动搜索页 H5, 反爬比 PC 弱
    target_url = f"https://so.m.jd.com/ware/search.action?keyword={encoded}&page={page}"

    scraperapi_key = os.environ.get("SCRAPERAPI_KEY", "").strip()
    if not scraperapi_key:
        raise Exception("未配置 SCRAPERAPI_KEY 环境变量")

    # ScraperAPI 参数说明:
    #   api_key: 你的 API Key
    #   url: 目标网址
    #   render=true: 自动 JS 渲染 (京东必须)
    #   country_code=cn: 中国代理
    api_url = "https://api.scraperapi.com/"
    params = {
        "api_key": scraperapi_key,
        "url": target_url,
        "render": "true",
        "country_code": "cn"
    }

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.get(api_url, params=params)
        if resp.status_code != 200:
            raise Exception(
                f"ScraperAPI 返回错误: status={resp.status_code}, "
                f"body={resp.text[:200]}"
            )
        return resp.text


def parse_jd_html(html: str) -> list:
    """解析京东移动搜索页HTML, 提取商品列表(三层策略)"""
    products = []
    seen_skus = set()  # 去重

    # 策略1: 抓 __SEARCH_RESULT__ 或 __INITIAL_STATE__ 内嵌JSON
    for json_var in ['__SEARCH_RESULT__', '__INITIAL_STATE__', 'mainData', 'searchData']:
        m = re.search(rf'window\.{json_var}\s*=\s*(\{{[\s\S]*?\}});', html)
        if m:
            try:
                data = json.loads(m.group(1))
                # 多种可能位置
                items = (data.get('wareInfo')
                        or data.get('itemList')
                        or data.get('searchResultList')
                        or data.get('data', {}).get('searchResultList', [])
                        or data.get('searchM', {}).get('m_topSearchResultWow', {}).get('list', []))
                if items:
                    for item in items[:30]:
                        sku = str(item.get('wareId') or item.get('skuId') or item.get('sku') or '')
                        if not sku or sku in seen_skus:
                            continue
                        seen_skus.add(sku)
                        products.append({
                            "title": (item.get('wname') or item.get('title') or item.get('name') or '')[:200],
                            "price": float(item.get('jdPrice') or item.get('jd_price') or item.get('price') or 0),
                            "shop": (item.get('shop_name') or item.get('shopName') or item.get('shop') or '')[:100],
                            "sku": sku,
                            "platform": "jd",
                            "platform_display": "京东",
                            "source_url": f"https://item.jd.com/{sku}.html"
                        })
                    if products:
                        return products
            except Exception:
                pass

    # 策略2: 抓 __NEXT_DATA__ JSON-LD
    ld_matches = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    for blob in ld_matches:
        try:
            ld = json.loads(blob)
            if isinstance(ld, list):
                for item in ld:
                    if item.get('@type') == 'Product':
                        sku = item.get('sku', '')
                        if sku in seen_skus or not sku:
                            continue
                        seen_skus.add(str(sku))
                        products.append({
                            "title": item.get('name', '')[:200],
                            "price": float(item.get('offers', {}).get('price', 0)),
                            "shop": item.get('brand', {}).get('name', ''),
                            "sku": str(sku),
                            "platform": "jd",
                            "platform_display": "京东",
                            "source_url": item.get('url', '')
                        })
            elif isinstance(ld, dict) and ld.get('@type') == 'Product':
                sku = ld.get('sku', '')
                if sku and sku not in seen_skus:
                    seen_skus.add(str(sku))
                    products.append({
                        "title": ld.get('name', '')[:200],
                        "price": float(ld.get('offers', {}).get('price', 0)),
                        "shop": ld.get('brand', {}).get('name', ''),
                        "sku": str(sku),
                        "platform": "jd",
                        "platform_display": "京东",
                        "source_url": ld.get('url', '')
                    })
            if products:
                return products
        except Exception:
            continue

    # 策略3: 抓 item.jd.com 链接 + 提取价格/标题
    jd_links = re.findall(r'item\.jd\.com/(\d+)\.html', html)
    # 抓所有价格
    all_prices = re.findall(r'(?:jdPrice|price|newPrice)[":\s]+["\']?(\d+\.?\d*)', html)
    # 抓所有可能的标题
    all_titles = re.findall(r'["\'](?:wname|title|name)["\']:\s*["\']([^"\']{5,200})["\']', html)

    for sku in jd_links:
        if sku in seen_skus:
            continue
        seen_skus.add(sku)
        products.append({
            "title": all_titles[len(products)] if len(products) < len(all_titles) else f"京东商品{sku}",
            "price": float(all_prices[len(products)]) if len(products) < len(all_prices) else 0.0,
            "shop": "",
            "sku": sku,
            "platform": "jd",
            "platform_display": "京东",
            "source_url": f"https://item.jd.com/{sku}.html"
        })
        if len(products) >= 20:
            break

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
