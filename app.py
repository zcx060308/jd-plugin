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
    """通过百度搜索结果间接获取京东商品 (绕开京东反爬)"""
    import os
    encoded = urllib.parse.quote(keyword)
    # 百度搜索: 关键词 + 限定京东站点
    target_url = f"https://www.baidu.com/s?wd={encoded}+site%3Aitem.jd.com&pn={(page-1)*10}"

    scrapingant_key = os.environ.get("SCRAPINGANT_API_KEY", "").strip()
    if not scrapingant_key:
        raise Exception("未配置 SCRAPINGANT_API_KEY 环境变量")

    api_url = "https://api.scrapingant.com/v2/general"
    params = {
        "url": target_url,
        "x-api-key": scrapingant_key,
        "browser": "false",       # 百度是静态HTML, 不需要浏览器
        "render_js": "false",
        "proxy_country": "US",    # 美国代理, ScrapingAnt的US代理最稳
        "timeout": "30"
    }

    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.get(api_url, params=params)
        if resp.status_code != 200:
            raise Exception(
                f"ScrapingAnt 返回错误: status={resp.status_code}, "
                f"body={resp.text[:200]}"
            )
        return resp.text


def parse_jd_html(html: str) -> list:
    """解析百度搜索结果, 提取京东商品列表"""
    products = []
    seen_skus = set()  # 去重

    # 百度搜索结果解析: 提取 item.jd.com 链接
    # 百度结果中京东商品格式:
    #   标题: <h3 class="c-title">...</h3>
    #   URL:  item.jd.com/100xxxx.html
    #   价格: 通常在描述里或c-color-price类

    # 找所有京东商品链接
    jd_links = re.findall(r'href="(https?://item\.jd\.com/(\d+)\.html[^"]*)"', html)

    for url, sku_id in jd_links:
        if sku_id in seen_skus:
            continue
        seen_skus.add(sku_id)

        # 找标题 (在该商品链接附近的 <em> 标签)
        # 百度搜索结果: 标题在 <h3 class="c-title"><a href="..."><em>...</em></a></h3>
        # 简单做法: 找URL前后500字符内的em内容
        url_pos = html.find(url)
        if url_pos < 0:
            continue
        nearby = html[max(0, url_pos-2000):url_pos+500]
        em_match = re.findall(r'<em[^>]*>([^<]+)</em>', nearby)
        title = em_match[0] if em_match else f"京东商品{sku_id}"

        # 价格: 找 ¥xx.xx 格式
        price_match = re.findall(r'[¥￥]\s*(\d+\.?\d*)', nearby)
        price = float(price_match[0]) if price_match else 0.0

        # 店铺: 找 "京东" 或 店名 关键字
        shop_match = re.findall(r'>([^<]{2,30}官方旗舰店|京东自营|[^<]{2,30}旗舰店)<', nearby)
        shop = shop_match[0] if shop_match else ""

        products.append({
            "title": title[:200],
            "price": price,
            "shop": shop[:100],
            "sku": sku_id,
            "platform": "jd",
            "platform_display": "京东",
            "source_url": url
        })

        if len(products) >= 20:
            break

    # 兜底: 如果上面的解析没拿到东西, 尝试从结果摘要里抓
    if not products:
        # 抓 <span class="c-color-price">¥xxx</span>
        prices = re.findall(r'[¥￥]\s*(\d+\.?\d*)', html)
        # 抓标题
        titles = re.findall(r'<em[^>]*>([^<]{5,100})</em>', html)
        # 抓 jd.com 链接
        urls = re.findall(r'item\.jd\.com/(\d+)\.html', html)
        for i, sku_id in enumerate(urls[:15]):
            if sku_id in seen_skus:
                continue
            seen_skus.add(sku_id)
            products.append({
                "title": titles[i] if i < len(titles) else f"京东商品{sku_id}",
                "price": float(prices[i]) if i < len(prices) else 0.0,
                "shop": "京东",
                "sku": sku_id,
                "platform": "jd",
                "platform_display": "京东",
                "source_url": f"https://item.jd.com/{sku_id}.html"
            })

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
            "page": page,
            "debug_html_len": len(html),
            "debug_html_sample": html[100000:103000]  # 跳到搜索结果区域
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
