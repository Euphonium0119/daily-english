import os
import re
import json
import random
import smtplib
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import time
import requests
import trafilatura
from concurrent.futures import ThreadPoolExecutor, as_completed
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

NEWS_SOURCES = os.getenv("NEWS_SOURCES", "bbc-news,cnn,the-guardian-uk,reuters,associated-press,abc-news")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

MIN_CONTENT_LENGTH = 2000   # 太短跳过
MAX_CONTENT_LENGTH = 15000  # 太长跳过（10分钟阅读 ≈ 2000词 ≈ 10000字）
TARGET_LENGTH = 8000        # 最理想长度

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "template.html")

# ── 每日主题调色板（按星期几轮换） ──
THEME_PALETTES = {
    0: {  # 周一 · 深海蓝
        "name": "深海蓝",
        "header_start": "#1a3a4f", "header_end": "#2c5f7a",
        "bg": "#edf0f3", "accent": "#3a6b8c",
        "card_bg": "#f7f9fb", "card_border": "#d5dde5",
        "section_bg": "#f0f3f6", "quote_bg": "#e6eaef",
        "divider": "#d5dde5", "text_secondary": "#7a8a9a",
        "header_text": "#c8dde8", "header_sub": "#a0c4d6", "header_meta": "#88aec0",
    },
    1: {  # 周二 · 森林绿
        "name": "森林绿",
        "header_start": "#1b3a2d", "header_end": "#2d6b4f",
        "bg": "#edf2ee", "accent": "#3a7d5a",
        "card_bg": "#f6faf7", "card_border": "#d0e0d5",
        "section_bg": "#f0f4f1", "quote_bg": "#e5ede7",
        "divider": "#d0ddd3", "text_secondary": "#6b8a73",
        "header_text": "#c0dcc8", "header_sub": "#9cc4a8", "header_meta": "#80b090",
    },
    2: {  # 周三 · 暖陶土
        "name": "暖陶土",
        "header_start": "#4a2c1e", "header_end": "#7a4a3a",
        "bg": "#f2eeea", "accent": "#c08060",
        "card_bg": "#fdf8f4", "card_border": "#e8d8cc",
        "section_bg": "#f5f0eb", "quote_bg": "#efe5db",
        "divider": "#e0d4c8", "text_secondary": "#9a8070",
        "header_text": "#e8d0c0", "header_sub": "#d4b89c", "header_meta": "#c0a080",
    },
    3: {  # 周四 · 暮光紫
        "name": "暮光紫",
        "header_start": "#2a1f3d", "header_end": "#4a3570",
        "bg": "#efecf5", "accent": "#6b5098",
        "card_bg": "#f8f6fc", "card_border": "#ddd5e8",
        "section_bg": "#f2eff8", "quote_bg": "#e8e3f2",
        "divider": "#d8d0e5", "text_secondary": "#8a78a8",
        "header_text": "#d0c8e8", "header_sub": "#b8a8d8", "header_meta": "#a090c0",
    },
    4: {  # 周五 · 石墨蓝
        "name": "石墨蓝",
        "header_start": "#1e2d3d", "header_end": "#3d5a7a",
        "bg": "#eaedf2", "accent": "#5a80a8",
        "card_bg": "#f5f8fb", "card_border": "#d2dae5",
        "section_bg": "#eef1f5", "quote_bg": "#e2e7ee",
        "divider": "#d0d8e2", "text_secondary": "#7088a0",
        "header_text": "#c0d0e4", "header_sub": "#a0b8d0", "header_meta": "#88a0b8",
    },
    5: {  # 周六 · 玫瑰金
        "name": "玫瑰金",
        "header_start": "#3d1e2d", "header_end": "#6b3d50",
        "bg": "#f2eaed", "accent": "#a55a78",
        "card_bg": "#faf5f7", "card_border": "#e5d2d8",
        "section_bg": "#f5edf0", "quote_bg": "#efe0e5",
        "divider": "#e0d0d5", "text_secondary": "#9a7080",
        "header_text": "#e8c8d4", "header_sub": "#d8a8b8", "header_meta": "#c890a0",
    },
    6: {  # 周日 · 琥珀金
        "name": "琥珀金",
        "header_start": "#3d2d1e", "header_end": "#6b4d2d",
        "bg": "#f2efe2", "accent": "#b89850",
        "card_bg": "#faf8f0", "card_border": "#e5ddc5",
        "section_bg": "#f5f2e8", "quote_bg": "#efe8d5",
        "divider": "#e0d8c0", "text_secondary": "#9a8a60",
        "header_text": "#e8dbb8", "header_sub": "#d8c898", "header_meta": "#c0b080",
    },
}


def validate_config():
    missing = [k for k in ["NEWS_API_KEY", "DEEPSEEK_API_KEY", "SMTP_USER", "SMTP_PASS", "TO_EMAIL"]
               if not globals().get(k)]
    if missing:
        raise RuntimeError(f"缺少环境变量: {', '.join(missing)}")


def fetch_articles():
    logger.info("Fetching news from NewsAPI...")
    url = "https://newsapi.org/v2/top-headlines"
    for attempt in range(3):
        try:
            resp = requests.get(url, params={"sources": NEWS_SOURCES, "apiKey": NEWS_API_KEY, "pageSize": 30}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "ok":
                raise RuntimeError(f"NewsAPI error: {data.get('message')}")
            articles = data.get("articles", [])
            logger.info(f"Got {len(articles)} articles")
            return articles
        except (requests.RequestException, Exception) as e:
            if attempt < 2:
                wait = (attempt + 1) * 10
                logger.warning(f"NewsAPI fetch failed (attempt {attempt+1}/3): {e}. Retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise


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


def compute_article_score(c):
    """对候选文章进行复合评分，返回 0-1 之间的分数."""
    s = 0.0
    # 长度接近度 (40%)
    length_diff_ratio = abs(c["length"] - TARGET_LENGTH) / TARGET_LENGTH
    s += 0.4 * max(0, 1 - length_diff_ratio)
    # 有作者 (10%)
    if c.get("author"):
        s += 0.1
    # 优质新闻源 (20%)
    preferred = {"reuters", "associated-press", "the-guardian-uk", "bbc-news"}
    if c.get("source", "").lower() in preferred:
        s += 0.2
    # 标题质量：20-80 字符理想 (10%)
    t_len = len(c.get("title", ""))
    if 20 <= t_len <= 80:
        s += 0.1
    # 时效性：有发布时间 (20%)
    if c.get("published_at"):
        s += 0.2
    return s


def pick_article(articles):
    # 预过滤无效标题
    valid = []
    for article in articles:
        title = article.get("title", "")
        if not title or title == "[Removed]" or len(title) < 10:
            continue
        valid.append(article)

    if not valid:
        raise RuntimeError("没有可用的文章（标题过滤后）")

    candidates = []
    target_candidates = 12

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(extract_content, a): a for a in valid}
        for future in as_completed(futures):
            article = futures[future]
            try:
                text = future.result(timeout=30)
            except Exception:
                logger.warning(f"  Extraction failed for {article.get('url', '')[:60]}")
                continue

            if text is None:
                continue

            published_raw = article.get("publishedAt", "")
            published_display = ""
            if published_raw:
                try:
                    dt = datetime.strptime(published_raw, "%Y-%m-%dT%H:%M:%SZ")
                    published_display = dt.strftime("%B %d, %Y %H:%M UTC")
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

            if len(candidates) >= target_candidates:
                for f in futures:
                    f.cancel()
                break

    if not candidates:
        raise RuntimeError("无法提取任何长文文章")

    candidates.sort(key=compute_article_score, reverse=True)
    top = candidates[:5]
    logger.info(f"Best candidates (scores): {[(c['title'][:30], round(compute_article_score(c), 3)) for c in top]}")
    return random.choice(top)


def extract_word_forms(derivatives_text):
    """从派生词描述中提取所有英文词形，用于扩展高亮匹配."""
    if not derivatives_text:
        return []
    forms = []
    # 匹配英文单词（2+ 字母），排除中文和词性标注
    for m in re.finditer(r'\b([A-Za-z]{2,})\b', derivatives_text):
        w = m.group(1)
        # 跳过常见词性缩写和中文拼音
        if w.lower() in ('n', 'v', 'adj', 'adv', 'prep', 'conj', 'to', 'in', 'of', 'or', 'and', 'the'):
            continue
        forms.append(w)
    return list(dict.fromkeys(forms))  # 去重保持顺序


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
                    f'<span style="border-bottom:3px dotted #8b5cf6;padding-bottom:2px;background:rgba(139,92,246,0.08);">{ds_escaped}</span>'
                )

        # 第二步：标记重点词汇（含词形变体）
        for v in sorted_vocab:
            word = v["word"]
            # 收集所有需要匹配的词形：原词 + 派生词形
            all_forms = [word]
            derivatives_text = v.get("derivatives", "")
            if derivatives_text:
                forms = extract_word_forms(derivatives_text)
                all_forms.extend(f for f in forms if f != word)
            # 按长度降序，避免短词先匹配
            unique_forms = sorted(set(all_forms), key=len, reverse=True)
            escaped_forms = [re.escape(f) for f in unique_forms]
            pattern = rf'\b({"|".join(escaped_forms)})\b'
            replacement = (
                r'<span style="background:#fef3c7;'
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

    prompt = f"""# ROLE
You are an expert English-Chinese translator and language teacher. Your job is to analyze an English news article and produce a structured JSON that helps Chinese learners understand every aspect of the text.

# TASK
Analyze the article below and return a single JSON object. No markdown, no code fences — pure JSON only.

ARTICLE TITLE: {article['title']}

ARTICLE CONTENT:
{article['content']}

# OUTPUT FORMAT
Return exactly this JSON structure (fields marked * are new):

{{
  "title_cn": "中文标题",
  "category": "文章主题分类，从下列选择一个: politics / technology / business / science / health / environment / culture / sports",
  "bilingual_paragraphs": [
    {{"en": "原文段落（逐字一致）", "zh": "中文翻译"}}
  ],
  "vocabulary": [
    {{
      "word": "英文原词",
      "variants": ["该词在文中出现的所有词形变化，不含原形。如原形为 'negotiate'，文中还出现了 'negotiations', 'negotiating'，则填 ['negotiations', 'negotiating']。若文中只用了一种形式则填 []"],
      "phonetic": "/IPA音标/",
      "meaning_cn": "中文释义",
      "definition": "英文简明释义，剑桥词典风格，一句话",
      "sentence": "自创简短例句，≤15词，含该词汇",
      "phrases": "2-4个常用搭配，分号分隔。无则 ''",
      "derivatives": "派生词，格式: 'create → creation (n. 创造), creative (adj. 有创造力的)'。无则 ''",
      "same_root_words": "同根词，格式: '词根: dict- (=say, 说) → predict, dictate, contradict'。无则 ''",
      "synonyms": "2-4个近义词，分号分隔。无则 ''"
    }}
  ],
  "difficult_sentence": {{
    "original": "文中语法最复杂的一句（原文原句）",
    "parsing": "中文语法拆解",
    "translation": "该句中译"
  }},
  "grammar_tip": {{
    "pattern_en": "语法点英文名，如 'Third Conditional' 或 'Relative Clauses'",
    "sentence_from_article": "文中包含该语法点的原句",
    "explanation_cn": "用中文简要解释该语法点，2-3句即可，实用为主"
  }},
  "word_of_the_day": {{
    "word": "从vocabulary列表中选一个最值得掌握的词",
    "why_cn": "用中文一句话解释为什么要学这个词"
  }},
  "daily_quote": "文中一句最值得背诵的地道表达（原句）",
  "difficulty": "B1 / B2 / C1",
  "reading_time_min": 整数
}}

# CONSTRAINTS
1. vocabulary: pick ALL medium-to-hard words worth learning (10-25 words). Include every word an intermediate English learner might not know. Exclude basic words: the, say, people, day, make, good, etc.
2. variants (*new): list every morphological form of the word that actually appears in the article (plurals, past tense, participles, etc.). Do NOT include the base form itself. Check the article text to ensure each variant really exists. Empty array [ ] if the word only appears in one form.
3. definition: concise English explanation, Cambridge-style, one sentence. Use "" for function words.
4. sentence: SHORT original example, max 15 words. Natural and helpful.
5. phrases: 2-4 fixed collocations. "" if none.
6. derivatives: word family with Chinese meanings in parentheses. "" if none.
7. same_root_words: state root + meaning first, then list words. "" if none.
8. synonyms: 2-4 near-synonyms. "" if none.
9. phonetic: standard IPA notation.
10. bilingual_paragraphs: split by natural paragraph breaks. The en field MUST be character-for-character identical to the source. Count MUST match the source paragraph count.
11. difficult_sentence: ONE genuinely complex sentence with layered clauses. Must be exact original text.
12. grammar_tip (*new): pick ONE grammar pattern worth teaching. Choose something practical (conditionals, relative clauses, passive voice, inversions, participle phrases, etc.). Keep explanation concise and useful.
13. word_of_the_day (*new): pick the most valuable word for learners from the vocabulary list. Explain why in one Chinese sentence.
14. category (*new): classify the article's primary topic.
15. daily_quote: ONE idiomatic and memorable sentence from the article.
16. difficulty: prefer B1 or B2. Avoid C1 unless truly academic.
17. Return ONLY the JSON object. No markdown, no explanation, no code fences."""

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 16384,
        "response_format": {"type": "json_object"},
    }

    resp = None
    for attempt in range(3):
        try:
            resp = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=300)
            resp.raise_for_status()
            break
        except (requests.RequestException, Exception) as e:
            if attempt < 2:
                wait = (attempt + 1) * 20
                logger.warning(f"DeepSeek API failed (attempt {attempt+1}/3): {e}. Retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise

    result = resp.json()
    raw = result["choices"][0]["message"]["content"].strip()

    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("JSON parse failed, attempting repair...")
        fixed = re.sub(r'(?<=[\x20-\x7e])[\x00-\x1f](?=[\x20-\x7e])', '', raw)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            logger.error(f"DeepSeek returned invalid JSON (recovery failed):\n{raw[:800]}")
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


def highlight_word_in_sentence(sentence, word, derivatives_text=""):
    """在例句中用黄色高亮当前词汇（含词形变体）"""
    escaped = re.escape(sentence.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    all_forms = [word]
    if derivatives_text:
        forms = extract_word_forms(derivatives_text)
        all_forms.extend(f for f in forms if f != word)
    unique_forms = sorted(set(all_forms), key=len, reverse=True)
    escaped_forms = [re.escape(f) for f in unique_forms]
    pattern = rf'\b({"|".join(escaped_forms)})\b'
    replacement = r'<span style="background:#fef3c7;font-weight:700;color:#92400e;padding:0 1px;">\1</span>'
    return re.sub(pattern, replacement, escaped, flags=re.IGNORECASE)


def render_email(article, analysis):
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = Template(f.read())

    vocab = analysis.get("vocabulary", [])
    ds = analysis.get("difficult_sentence") or {}

    # 为每个词汇的例句加高亮
    for v in vocab:
        v["sentence_html"] = highlight_word_in_sentence(
            v.get("sentence", ""), v.get("word", ""), v.get("derivatives", ""))

    # 生成高亮版原文段落（用于 English Original 区）
    highlighted = highlight_content(article["content"], vocab, ds if ds.get("original") else None)

    # 构建逐段对照：直接用 DeepSeek 返回的段落配对，对每段英文做高亮
    bilingual = analysis.get("bilingual_paragraphs", [])
    paragraph_pairs = []
    for pair in bilingual:
        en_text = pair.get("en", "")
        zh_text = pair.get("zh", "")
        # 对单个段落做高亮
        highlighted_parts = highlight_content(en_text, vocab, ds if ds.get("original") else None)
        en_html = highlighted_parts[0] if highlighted_parts else en_text
        paragraph_pairs.append((en_html, zh_text))
    if not paragraph_pairs:
        logger.warning("bilingual_paragraphs 为空，回退到 zip 模式")
        trans_paras = split_paragraphs(analysis.get("translation", ""))
        pair_count = min(len(highlighted), len(trans_paras))
        paragraph_pairs = list(zip(highlighted[:pair_count], trans_paras[:pair_count]))

    # 每日主题色（按星期几自动轮换）
    theme = THEME_PALETTES[datetime.now().weekday()]

    # 首字母下沉：给第一个段落的第一个可见字符加 drop cap
    if highlighted and highlighted[0]:
        first_para = highlighted[0]
        # 跳过开头的 HTML 标签，找到第一个可见字符
        stripped = re.sub(r'^<[^>]+>', '', first_para)
        if stripped and stripped[0].isalpha():
            first_char = stripped[0]
            # 只在段落开头是普通字符时处理（不是已经被 span 包裹的）
            if first_para[0] != '<':
                highlighted[0] = (
                    f'<span style="float:left;font-size:48px;line-height:40px;'
                    f'padding-right:6px;color:{theme["accent"]};'
                    f"font-family:'Playfair Display',Georgia,serif;font-weight:700;\">"
                    f'{first_char}</span>{first_para[1:]}'
                )

    html = template.render(
        title=article["title"],
        title_cn=analysis.get("title_cn", ""),
        date=datetime.now().strftime("%B %d, %Y"),
        source=article.get("source", ""),
        author=article.get("author", ""),
        published_at=article.get("published_at", ""),
        content_paragraphs=highlighted,
        paragraph_pairs=paragraph_pairs,
        vocab_rows=chunk_vocabulary(vocab),
        difficult_sentence=ds if ds.get("original") else None,
        daily_quote=analysis.get("daily_quote", ""),
        difficulty=analysis.get("difficulty", "B2"),
        reading_time_min=analysis.get("reading_time_min", 3),
        source_url=article["url"],
        source_name=article["source"],
        theme=theme,
        grammar_tip=analysis.get("grammar_tip"),
        word_of_the_day=analysis.get("word_of_the_day"),
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
