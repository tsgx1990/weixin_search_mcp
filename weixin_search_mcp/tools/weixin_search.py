import json
import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Dict, List, Optional
import requests
from lxml import html
from urllib.parse import quote
import time

REQUEST_TIMEOUT = 15
CN_TZ = timezone(timedelta(hours=8))

# Sogou 反爬对请求头很敏感：Accept-Language / Cache-Control / Pragma 等
# "看起来更像浏览器"的头反而会触发 /antispider/ 跳转，只保留 User-Agent 最稳。
BASE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
}

# 翻页请求间隔（秒）：避免连续翻页触发搜狗反爬限流
PAGE_INTERVAL_SECONDS = 3


def _is_antispider_response(response: requests.Response) -> bool:
    """Detect Sogou anti-spider pages that otherwise look like empty results."""
    final_url = response.url.lower()
    body = response.text.lower()
    return "antispider" in final_url or "seccoderight" in body or "anti.min.css" in body


def _parse_publish_time(raw: str) -> str:
    """Sogou 用 document.write(timeConvert('<epoch>')) 延迟渲染发布时间，
    静态抓取拿到的是这段未执行的 JS 源码而不是时间文本；这里把内嵌的
    Unix 时间戳解析出来转成可读时间，解析不出来时原样返回。"""
    match = re.search(r"timeConvert\('(\d+)'\)", raw)
    if not match:
        return raw
    try:
        return datetime.fromtimestamp(int(match.group(1)), tz=CN_TZ).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return raw


def sogou_weixin_search(
    query: Annotated[str, "搜索关键词"],
    page: int = 1,
    strict: bool = False,
) -> List[Dict[str, str]]:
    """在搜狗微信搜索中搜索指定关键词并返回结果列表，包含真实URL

    Args:
        query: 搜索关键词
        page: 页码，默认1
    """
    session = requests.Session()

    params = {
        'type': '2',
        's_from': 'input',
        'query': query,
        'ie': 'utf8',
        'page': page,
        '_sug_': 'n',
        '_sug_type_': '',
    }

    try:
        response = session.get(
            'https://weixin.sogou.com/weixin',
            params=params,
            headers=BASE_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code != 200:
            if strict:
                raise RuntimeError(f"搜狗微信搜索返回异常状态码: {response.status_code}")
            return []

        if _is_antispider_response(response):
            if strict:
                raise RuntimeError("搜狗微信触发反爬验证，分页搜索已中止")
            return []

        tree = html.fromstring(response.text)
        results = []

        elements = tree.xpath("//a[contains(@id, 'sogou_vr_11002601_title_')]")
        publish_time = tree.xpath(
            "//li[contains(@id, 'sogou_vr_11002601_box_')]/div[@class='txt-box']/div[@class='s-p']/span[@class='s2']")

        for element, time_elem in zip(elements, publish_time):
            title = element.text_content().strip()
            link = element.get('href')
            if link and not link.startswith('http'):
                link = 'https://weixin.sogou.com' + link

            # 获取真实URL：复用本次搜索建立的会话 cookie，
            # 否则一个"裸"请求会被搜狗反爬直接 302 到 antispider 验证页
            real_url = ""
            try:
                real_url = get_real_url_from_sogou(link, session=session)
            except Exception:
                pass

            results.append({
                'title': title,
                'link': link,
                'real_url': real_url,
                'publish_time': _parse_publish_time(time_elem.text_content().strip()),
                'page': str(page)  # str to match Dict[str, str] type signature
            })

        return results
    except requests.RequestException as e:
        if strict:
            raise RuntimeError(f"请求搜狗微信搜索失败: {str(e)}") from e
        return []
    except Exception as e:
        if strict:
            raise RuntimeError(f"解析搜狗微信搜索结果失败: {str(e)}") from e
        return []


def sogou_weixin_search_all(query: str, max_pages: int = 10) -> List[Dict[str, str]]:
    """搜索所有页面的结果，自动翻页直到无结果或达到 max_pages

    Args:
        query: 搜索关键词
        max_pages: 最大页数，默认10
    Returns:
        List[Dict[str, str]]: 所有页的搜索结果
    """
    all_results = []
    for page in range(1, max_pages + 1):
        results = sogou_weixin_search(query, page=page, strict=True)
        if not results:
            break
        all_results.extend(results)
        # 避免请求过快被限流
        if page < max_pages:
            time.sleep(PAGE_INTERVAL_SECONDS)

    return all_results


def get_real_url_from_sogou(
    sogou_url: str,
    session: Optional[requests.Session] = None,
) -> str:
    """从搜狗微信链接获取真实的微信公众号文章链接

    优先复用调用方传入的 session（承载着搜索请求建立的 cookie）；
    一个脱离搜索会话的裸请求会被搜狗反爬直接 302 到 antispider 验证页，
    导致 real_url 恒为空。
    """
    requester = session if session is not None else requests

    try:
        response = requester.get(sogou_url, headers=BASE_HEADERS, timeout=REQUEST_TIMEOUT)

        if _is_antispider_response(response):
            return ""

        # 用正则一次性抓全部 url += '...' 分片；旧版手写的 find() 循环会把
        # 第一个分片（正好是 "https://mp."）跳过，导致拼出来的链接丢了协议头
        url_parts = re.findall(r"url \+= '([^']*)'", response.text)
        full_url = ''.join(url_parts).replace("@", "")
        return full_url
    except Exception:
        return ""


def get_real_url(sogou_url: Annotated[str, "搜狗微信链接,来自于sogou_weixin_search工具结果"]) -> str:
    """从搜狗微信链接获取真实的微信公众号文章链接（独立调用，无搜索会话可复用，
    命中反爬的概率比 sogou_weixin_search 内部调用更高）"""
    return get_real_url_from_sogou(sogou_url)


def get_article_content(real_url: Annotated[str, "真实微信公众号文章链接"], referer: Annotated[Optional[str], "请求来源,get_real_url的返回值"] = None) -> str:
    """获取微信公众号文章的正文内容"""
    try:
        if not real_url or real_url == "https://mp." or not real_url.startswith("http"):
            return "获取文章内容失败: 未拿到有效的微信公众号文章链接"

        response = requests.get(real_url, headers=BASE_HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        tree = html.fromstring(response.text)
        content_elements = tree.xpath("//div[@id='js_content']//text()")
        cleaned_content = [text.strip() for text in content_elements if text.strip()]
        main_content = '\n'.join(cleaned_content)
        return main_content
    except Exception as e:
        return f"获取文章内容失败: {str(e)}"

def get_wechat_article(query: str, number=10):
    """
    获取前10篇文章
    """
    start_time = time.time()
    results = sogou_weixin_search(query)
    if not results:
        return f"没有搜索到{query}相关的文章"
    articles = []
    results = results[:number]
    for every_result in results:
        sougou_link = every_result["link"]
        real_url = every_result["real_url"] or get_real_url(sougou_link)
        content = get_article_content(real_url)
        article = {
            "title": every_result["title"],
            "publish_time": every_result["publish_time"],
            "real_url": real_url,
            "content": content
        }
        articles.append(article)
    end_time = time.time()
    print(f"关键词{query}相关的文章已经获取完毕，获取到{len(articles)}篇, 耗时{end_time - start_time}秒")
    return articles

if __name__ == '__main__':
    get_wechat_article(query="吉利汽车",number=2)
