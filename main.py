import os
import feedparser
import requests
import schedule
import time
import json
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from openai import OpenAI
from dotenv import load_dotenv

# 加载 .env 文件中的环境变量
load_dotenv()

# ==================== 配置区域 ====================
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

RSS_FEEDS = [
    # ===== 国内权威 =====
    "http://www.people.com.cn/rss/politics.xml",
    "http://www.people.com.cn/rss/world.xml",
    "http://www.xinhuanet.com/politics/news_politics.xml",
    "http://www.cctv.com/program/rss/02/01/index.xml",
    "https://www.chinanews.com.cn/rss/scroll-news.xml",
    # ===== 国际权威 =====
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rsshub.app/apnews/topics/ap-top-news",
]
# =================================================

def fetch_news():
    """从所有 RSS 源抓取最新新闻标题、链接和日期"""
    all_news = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    for feed_url in RSS_FEEDS:
        try:
            print(f"正在抓取: {feed_url} ...")
            response = requests.get(feed_url, headers=headers, timeout=10)
            response.raise_for_status()
            
            feed = feedparser.parse(response.content)
            
            if not feed.entries:
                print(f"  -> 警告：抓取到了网页，但没有解析出新闻条目。")
                continue
                
            for entry in feed.entries[:5]:
                # 提取并格式化日期
                pub_date = "未知日期"
                if "published_parsed" in entry and entry.published_parsed:
                    pub_date = time.strftime("%Y-%m-%d", entry.published_parsed)
                
                all_news.append({
                    "title": entry.title,
                    "link": entry.link,
                    "source": feed.feed.get("title", "未知来源"),
                    "pub_date": pub_date
                })
            print(f"  -> 成功抓取 {len(feed.entries[:5])} 条新闻")
            
        except Exception as e:
            print(f"  -> 抓取失败: {e}")
            
    return all_news

SEEN_FILE = "seen_news.json"

def load_seen_news():
    """加载已发送的新闻链接"""
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_seen_news(seen_set):
    """保存已发送的新闻链接"""
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen_set), f, ensure_ascii=False, indent=2)

def summarize_with_deepseek(news_list):
    """调用 DeepSeek API 对新闻进行筛选、排序和摘要"""
    news_text = "\n".join([f"- [{item['source']}] [{item['pub_date']}] {item['title']} (链接: {item['link']})" for item in news_list])

    prompt = f"""
    你是一个资深的新闻总编辑。以下是过去 24 小时内来自不同来源的新闻标题和链接（含日期）。
    请完成以下任务：
    1. 筛选出**国内新闻 5 条**和**国际新闻 5 条**，共 10 条新闻。
    2. 筛选标准：突发性 > 政策变动/地缘政治 > 经济/社会重大影响 > 常规报道。
    3. 对这 10 条新闻进行**重要性排序**（rank 从 1 到 10，1 为最重要），必须把最重要的放在最前面。
    4. 为每一条写一段 40-80 字的中文摘要，必须基于标题内容，不要编造信息。
    5. **必须原样保留每条新闻的日期。**（如果在新闻列表里看到了日期，就填那个日期，不要自己编造日期）。
    
    输出格式要求：
    请输出一个 JSON 对象，必须包含一个名为 "news" 的数组。数组里每个对象包含字段：title（标题）, summary（摘要）, source（来源）, link（链接）, pub_date（日期，保留原本的 YYYY-MM-DD 格式）, category（分类，填“国内”或“国际”）, rank（数字，1-10）。
    严禁输出 JSON 以外的任何内容。

    新闻列表：
    {news_text}
    """

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "你是一个专业的新闻总编辑，擅长判断新闻重要性并提炼摘要。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            response_format={"type": "json_object"}
        )
        
        raw_content = response.choices[0].message.content
        # print("DeepSeek 原始返回内容:", raw_content[:200], "...") # 调试用，平时可注释
        
        result = json.loads(raw_content)
        
        # 兼容处理：AI 可能返回 {"news": [...]} 或者直接返回 [...]
        if isinstance(result, dict):
            news_items = result.get("news", [])
        elif isinstance(result, list):
            news_items = result
        else:
            news_items = []
        
        news_items.sort(key=lambda x: x.get("rank", 999))
        return news_items
    except Exception as e:
        print(f"调用 DeepSeek API 失败: {e}")
        return []

def send_email(news_items):
    """将新闻摘要以邮件形式发送"""
    if not news_items:
        print("没有可推送的新闻。")
        return

    email_host = os.getenv("EMAIL_HOST")
    email_port = int(os.getenv("EMAIL_PORT", 465))
    email_user = os.getenv("EMAIL_USER")
    email_password = os.getenv("EMAIL_PASSWORD")
    email_to = os.getenv("EMAIL_TO")

    content_lines = ["📰 今日新闻摘要（按重要性排序）\n"]
    
    for i, item in enumerate(news_items, 1):
        prefix = "【重点关注】" if i <= 3 else ""
        content_lines.append(f"{prefix}{i}. [{item.get('category', '')}] {item['title']}")
        content_lines.append(f"   📅 日期：{item.get('pub_date', '未知')}")
        content_lines.append(f"   {item['summary']}")
        content_lines.append(f"   来源：{item['source']} | 链接：{item['link']}\n")

    content = "\n".join(content_lines)

    message = MIMEText(content, "plain", "utf-8")
    message["Subject"] = Header("每日新闻摘要", "utf-8")
    message["From"] = email_user
    message["To"] = email_to

    try:
        with smtplib.SMTP_SSL(email_host, email_port) as server:
            server.login(email_user, email_password)
            server.sendmail(email_user, [email_to], message.as_string())
        print("邮件发送成功。")
    except Exception as e:
        print(f"邮件发送失败: {e}")

def job():
    """完整的任务流程：抓取 -> 去重 -> 摘要 -> 推送"""
    print("开始执行每日新闻摘要任务...")
    news = fetch_news()
    if not news:
        print("未抓取到任何新闻，任务结束。")
        return

    seen = load_seen_news()
    new_news = [item for item in news if item["link"] not in seen]

    if not new_news:
        print("没有新新闻，任务结束。")
        return

    print(f"共抓取到 {len(news)} 条新闻，去重后剩余 {len(new_news)} 条新新闻。")

    summaries = summarize_with_deepseek(new_news)

    if summaries:
        print("摘要生成成功，正在发送邮件...")
        send_email(summaries)
        # 重点：把本次抓取到的所有新闻链接都加入记录，防止明天重复
        for item in new_news:
            seen.add(item["link"])
        save_seen_news(seen)
        print(f"已更新已读记录，当前共记录 {len(seen)} 条链接。")
    else:
        print("摘要生成失败。")

    print("任务执行完毕。")

if __name__ == "__main__":
    # 本地测试或云端运行都调用 job()
    job()

    # 如果你想在本地电脑用定时任务（不推荐，推荐用 GitHub Actions），可以取消下面这几行的注释，并注释掉上面的 job()
    # schedule.every().day.at("07:30").do(job)
    # print("定时任务已启动，等待执行...")
    # while True:
    #     schedule.run_pending()
    #     time.sleep(60)
