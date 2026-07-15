import os
import re
import json
import random
import smtplib
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
import trafilatura
from jinja2 import Template
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
TO_EMAIL = os.getenv("TO_EMAIL")

NEWS_SOURCES = "bbc-news,cnn,the-guardian-uk,reuters,associated-press,abc-news"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

MIN_CONTENT_LENGTH = 2000   # 太短跳过
MAX_CONTENT_LENGTH = 15000  # 太长跳过（10分钟阅读 ≈ 2000词 ≈ 10000字）
TARGET_LENGTH = 8000        # 最理想长度

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "template.html")


def validate_config():
    missing = [k for k in ["NEWS_API_KEY", "DEEPSEEK_API_KEY", "SMTP_USER", "SMTP_PASS", "TO_EMAIL"]
               if not globals().get(k)]
    if missing:
        raise RuntimeError(f"缺少环境变量: {', '.join(missing)}")


def fetch_articles():
    logger.info("Fetching news from NewsAPI...")
    url = "https://newsapi.org/v2/top-headlines"
    resp = requests.get(url, params={"sources": NEWS_SOURCES, "apiKey": NEWS_API_KEY, "pageSize": 30}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"NewsAPI error: {data.get('message')}")
    articles = data.get("articles", [])
    logger.info(f"Got {len(articles)} articles")
    return articles


def extract_content(article):
    url = article.get("url", "")
    logger.info(f"Extracting: {url[:80]}...")
    downloaded = trafilatura.fetch_url(url)
    if downloaded:
        text = trafilatura.extract(downloaded, include_formatting=True, favor_precision=True)
        if text and MIN_CONTENT_LENGTH <= len(text) <= MAX_CONTENT_LENGTH:
            return text.strip()
        elif text and len(text) < MIN_CONTENT_LENGTH:
            logger.info(f"  Too short ({len(text)} chars), skipping")
        elif text and len(text) > MAX_CONTENT_LENGTH:
            logger.info(f"  Too long ({len(text)} chars), skipping")
    return None


def pick_article(articles):
    candidates = []
    for article in articles:
        text = extract_content(article)
        if text:
            published_raw = article.get("publishedAt", "")
            published_display = ""
            if published_raw:
                try:
                    dt = datetime.strptime(published_raw, "%Y-%m-%dT%H:%M:%SZ")
                    published_display = dt.strftime("%B %d, %Y · %H:%M UTC")
                except ValueError:
                    published_display = published_raw

            candidates.append({
                "title": article.get("title", "Unknown Title"),
                "content": text,
                "url": article.get("url", ""),
                "source": (article.get("source") or {}).get("name", ""),
                "author": article.get("author") or "",
                "published_at": published_display,
                "length": len(text),
            })
    if not candidates:
        raise RuntimeError("无法提取任何长文文章")

    # 按接近目标长度的程度排序（越接近 TARGET_LENGTH 越靠前）
    candidates.sort(key=lambda a: abs(a["length"] - TARGET_LENGTH))
    top = candidates[:5]
    logger.info(f"Best candidates (lengths): {[(c['title'][:30], c['length']) for c in top]}")
    return random.choice(top)


def highlight_content(content, vocabulary, difficult_sentence):
    """在原文中高亮标记重点词汇和长难句，返回 HTML 字符串列表（按段落）"""
    # 先按段落分割
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    raw_paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    # 先 HTML 转义每个段落
    paragraphs = [p.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for p in raw_paragraphs]

    # 按词汇长度降序排列，避免短词先匹配破坏长词
    sorted_vocab = sorted(vocabulary, key=lambda v: len(v["word"]), reverse=True)

    result = []
    for para in paragraphs:
        # 第一步：标记长难句（先于词汇，因为长难句内可能包含重点词汇）
        if difficult_sentence and difficult_sentence.get("original"):
            ds_orig = difficult_sentence["original"].strip()
            ds_escaped = ds_orig.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if ds_escaped in para:
                para = para.replace(
                    ds_escaped,
                    f'<span style="border-bottom:3px dotted #8b5cf6;padding-bottom:2px;background:rgba(139,92,246,0.06);">{ds_escaped}</span>'
                )

        # 第二步：标记重点词汇
        for v in sorted_vocab:
            word = v["word"]
            escaped_word = re.escape(word)
            pattern = rf'\b({escaped_word})\b'
            replacement = (
                r'<span style="background:#fef3c7;border-bottom:2px solid #f59e0b;'
                r'padding:0 2px;border-radius:2px;cursor:help;" title="'
                + v.get("meaning_cn", "").replace('"', '&quot;')
                + r'">\1</span>'
            )
            para = re.sub(pattern, replacement, para, flags=re.IGNORECASE)

        result.append(para)

    return result


def call_deepseek(article):
    logger.info("Calling DeepSeek API for translation & analysis...")

    if not DEEPSEEK_API_KEY or not DEEPSEEK_API_KEY.startswith("sk-"):
        raise ValueError(
            "DEEPSEEK_API_KEY 无效，请检查 .env 文件。\n"
            "去 https://platform.deepseek.com/ 注册获取密钥，格式应为 sk-xxxxxxxx"
        )

    prompt = f"""You are an expert English-Chinese translator and language teacher. Analyze the following English news article and return a JSON object. No markdown, no code fences — pure JSON only.

Article title: {article['title']}

Article content:
{article['content']}

Return exactly this JSON structure:
{{
  "title_cn": "文章标题的中文翻译",
  "translation": "全文的中文翻译。保持原文段落结构，用 \\n\\n 分隔段落。翻译要流畅自然，符合中文新闻语体。",
  "vocabulary": [
    {{"word": "英文原词（保持原文中的大小写形式）", "phonetic": "/音标/", "meaning_cn": "中文释义", "sentence": "一个简短的自创例句，不超过15个单词，必须包含该词汇，帮助用户理解词的用法"}}
  ],
  "difficult_sentence": {{
    "original": "从文中选一句语法结构最复杂的句子（必须是原文中真实存在的完整句子）",
    "parsing": "用中文详细拆解这句的语法结构，包括主句从句关系、关键语法点、修饰成分等",
    "translation": "这句的中文翻译"
  }},
  "daily_quote": "从文中选一句最值得背诵的地道英文表达（必须是原文原句）",
  "difficulty": "文章难度等级，从 B1 / B2 / C1 中选择一个",
  "reading_time_min": 整数，估算阅读时间（分钟），按每分钟200词计算
}}

Rules:
- vocabulary: pick 6-8 medium-to-hard words (not basic words like 'the', 'say', 'people'). For each word, create a SHORT original example sentence (max 15 words) — do NOT quote from the article
- phonetic: use standard IPA notation
- difficult_sentence: pick ONE genuinely complex sentence with layered clauses, must be an exact original sentence from the article
- daily_quote: ONE sentence worth memorizing, idiomatic and useful, must be from the article
- difficulty: prefer B1 or B2 (intermediate level, suitable for English learners). B1=中等难度, B2=中高难度. Avoid C1 unless the article is truly academic
- Return ONLY the JSON object, nothing else."""

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 8192,
    }

    resp = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=180)
    resp.raise_for_status()
    result = resp.json()
    raw = result["choices"][0]["message"]["content"].strip()

    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error(f"DeepSeek returned invalid JSON:\n{raw[:500]}")
        raise


def split_paragraphs(text):
    if not text:
        return ["(no content)"]
    paragraphs = text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n")
    return [p.strip() for p in paragraphs if p.strip()]


def chunk_vocabulary(vocab):
    rows = []
    for i in range(0, len(vocab), 2):
        pair = vocab[i:i+2]
        rows.append(pair)
    return rows


def highlight_word_in_sentence(sentence, word):
    """在例句中用黄色高亮当前词汇"""
    escaped = re.escape(sentence.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    pattern = rf'\b({re.escape(word)})\b'
    replacement = r'<span style="background:#fef3c7;font-weight:700;color:#92400e;padding:0 1px;">\1</span>'
    return re.sub(pattern, replacement, escaped, flags=re.IGNORECASE)


def render_email(article, analysis):
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = Template(f.read())

    vocab = analysis.get("vocabulary", [])
    ds = analysis.get("difficult_sentence") or {}

    # 为每个词汇的例句加高亮
    for v in vocab:
        v["sentence_html"] = highlight_word_in_sentence(v.get("sentence", ""), v.get("word", ""))

    # 生成高亮版原文段落
    highlighted = highlight_content(article["content"], vocab, ds if ds.get("original") else None)

    html = template.render(
        title=article["title"],
        date=datetime.now().strftime("%B %d, %Y"),
        source=article.get("source", ""),
        author=article.get("author", ""),
        published_at=article.get("published_at", ""),
        content_paragraphs=highlighted,
        translation_paragraphs=split_paragraphs(analysis.get("translation", "")),
        vocab_rows=chunk_vocabulary(vocab),
        difficult_sentence=ds if ds.get("original") else None,
        daily_quote=analysis.get("daily_quote", ""),
        difficulty=analysis.get("difficulty", "B2"),
        reading_time_min=analysis.get("reading_time_min", 3),
        source_url=article["url"],
        source_name=article["source"],
    )
    return html


def send_email(html_body, article_title):
    logger.info(f"Sending email to {TO_EMAIL}...")

    now = datetime.now()
    subject = f"📰 Daily English · {article_title[:40]} · {now.strftime('%b %d')}"

    msg = MIMEMultipart("alternative")
    msg["From"] = f"Daily English <{SMTP_USER}>"
    msg["To"] = TO_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

    logger.info("Email sent successfully!")


def main():
    try:
        validate_config()
        articles = fetch_articles()
        article = pick_article(articles)
        logger.info(f"Selected: {article['title'][:60]}... ({article['length']} chars)")
        analysis = call_deepseek(article)
        html = render_email(article, analysis)
        send_email(html, article["title"])
        logger.info("Done!")
    except Exception as e:
        logger.error(f"Failed: {e}")
        raise


if __name__ == "__main__":
    main()
