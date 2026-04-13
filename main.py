import os
import json
import yaml
import time
import requests
import datetime
from pathlib import Path
from openai import OpenAI

# === 1. 配置与初始化 ===
def load_config():
    # 如果配置文件不存在，提供默认值
    if not os.path.exists("config.yaml"):
        return {
            "settings": {
                "enable_llm": False,
                "llm_top_n": 5,
                "history_file": "data/history.json",
                "archive_dir": "archives",
                "readme_file": "README.md",
                "readme_header": "# 📈 OSSInsight 每日开源热点报告\n\n"
            },
            "collections": []
        }
        
    with open("config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    
    # 环境变量覆盖 (支持 GitHub Actions)
    env_enable_llm = os.environ.get("ENABLE_LLM")
    if env_enable_llm is not None:
        config['settings']['enable_llm'] = (env_enable_llm.lower() == 'true')
        
    return config

# === 2. 历史缓存管理 (省钱核心) ===
def load_history(filepath):
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def save_history(filepath, history):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

# === 3. 数据获取 ===
def fetch_trending(language, period, limit=10):
    url = "https://api.ossinsight.io/q/trending-repos"
    params = {"language": language, "period": period, "format": "json"}
    try:
        print(f"📡 正在抓取: {language} ({period})...")
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return data[:limit] 
    except Exception as e:
        print(f"❌ 抓取失败: {e}")
        return []

def fetch_by_collection_name(collection_name, period, limit=10):
    url = "https://api.ossinsight.io/q/trending-repos"
    params = {"language": "All", "period": period, "format": "json"}
    try:
        print(f"📡 正在抓取 Collection: {collection_name} ({period})...")
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        filtered = [r for r in data if collection_name in (r.get('collection_names') or '')]
        return filtered[:limit]
    except Exception as e:
        print(f"❌ Collection 抓取失败: {e}")
        return []

def get_github_total_stars(repo_name):
    try:
        url = f"https://github.com/{repo_name}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            import re
            match = re.search(r'aria-label="([\d,]+)\s*users starred this repository"', resp.text)
            if match:
                return int(match.group(1).replace(',', ''))
    except:
        pass
    return 0

# === 4. 过滤规则 ===
def should_filter(repo, filters, total_stars_cache={}):
    desc = repo.get('description', '').strip().lower()
    if filters.get('skip_no_description', False):
        if not desc or desc in ['无描述', 'no description', '']:
            return True, "无描述"

    stars = repo.get('stars', 0)
    min_total = filters.get('min_total_stars', 0)
    
    if min_total > 0 and stars < min_total:
        repo_name = repo.get('repo_name', '')
        if repo_name not in total_stars_cache:
            print(f"🔍 获取总星数: {repo_name}")
            total_stars_cache[repo_name] = get_github_total_stars(repo_name)
        total_stars = total_stars_cache[repo_name]
        if total_stars == 0:
            return False, ""
        if total_stars >= min_total:
            return False, ""
        return True, f"星数不足 (增量{stars}/总数{total_stars})"

    return False, ""

# === 5. AI 摘要生成 (随机顺序调用) ===
def generate_ai_summary(clients, repo, model_names):
    import random
    
    name = repo['repo_name']
    desc = repo.get('description', '')
    prompt = (
        f"项目: {name}\n"
        f"描述: {desc}\n"
        "请用中文一句话概括这个项目的核心功能，不要废话，不超过50字。"
    )
    
    available = [(c, m) for c, m in zip(clients, model_names) if c]
    if not available:
        return "", ""
    
    random.shuffle(available)
    
    for client, model_name in available:
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "你是一个技术专家。"},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=100,
                temperature=0.3,
                timeout=20
            )
            text = response.choices[0].message.content.strip()
            text = re.sub(r'<think>[\s\S]*?</think>',
 '', text).strip()
            text = re.sub(r'<think>.*$', '', text, flags=re.MULTILINE).strip()
            text = text.replace('\n', ' ').replace('\r', '')
            if text:
                return text, model_name
            print(f"⚠️ [{model_name}] 返回内容为空，尝试其他模型")
        except Exception as e:
            print(f"⚠️ [{model_name}] 接口错误: {e}")
            time.sleep(0.5)  # 失败后稍等再试下一个
            continue
    
    return "", ""

# === 6. Markdown 内容构建 ===
def build_markdown_section(title, repos, settings, history, llm_clients, model_names):
    section = f"## {title}\n\n"
    section += "| 排名 | 项目 | Stars | 简介 (AI/Raw) |\n"
    section += "| :--- | :--- | :--- | :--- |\n"

    filters = settings.get('filters', {})
    total_stars_cache = {}

    for idx, repo in enumerate(repos, 1):
        name = repo['repo_name']
        url = f"https://github.com/{name}"
        stars = repo.get('stars', 0)
        raw_desc = repo.get('description', '').replace('|', r'\|').replace('\n', ' ')
        
        display_stars = stars
        final_desc = raw_desc
        is_filtered, filter_reason = should_filter(repo, filters, total_stars_cache)
        
        if is_filtered:
            final_desc = f"⛔ [{filter_reason}] {final_desc}"
        else:
            if name in total_stars_cache and total_stars_cache[name] > stars:
                display_stars = total_stars_cache[name]
            
            if settings['enable_llm'] and idx <= settings.get('llm_top_n', 5):
                if name in history:
                    hist = history[name]
                    model = hist.get('model', 'Legacy') or 'Legacy'
                    final_desc = f"🤖 [{model}] {hist['summary']}"
                elif any(llm_clients):
                    ai_sum, model_used = generate_ai_summary(llm_clients, repo, model_names)
                    if ai_sum:
                        final_desc = f"🤖 [{model_used}] {ai_sum}"
                        history[name] = {
                            "summary": ai_sum,
                            "model": model_used,
                            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d")
                        }
                    time.sleep(1.5)
        
        if len(final_desc) > 150:
            final_desc = final_desc[:147] + "..."

        section += f"| {idx} | [{name}]({url}) | 🔥 {display_stars} | {final_desc} |\n"
    
    return section


# === 7. 归档索引更新 ===
def get_archives_list(archive_dir):
    if not os.path.exists(archive_dir):
        return []
    
    files = [f for f in os.listdir(archive_dir) if f.endswith('.md')]
    # 按文件名(日期)倒序排列
    files.sort(reverse=True)
    
    links = []
    for f in files:
        date_str = f.replace('.md', '')
        # 生成相对路径链接
        links.append(f"| {date_str} | [查看报告](./{archive_dir}/{f}) |")
    
    return links

# === 主程序 ===
def main():
    config = load_config()
    settings = config['settings']
    history = load_history(settings['history_file'])
    
    llm_clients = [None, None]
    model_names = ["", ""]
    
    if settings['enable_llm']:
        minimax_key = os.environ.get("MINIMAX_API_KEY")
        minimax_url = os.environ.get("MINIMAX_BASE_URL", "https://api.minimaxi.com/anthropic")
        if minimax_key:
            llm_clients[0] = OpenAI(api_key=minimax_key, base_url=minimax_url)
            model_names[0] = os.getenv("LLM_MODEL", "MiniMax-M2.7")
        
        openai_key = os.environ.get("OPENAI_API_KEY")
        openai_url = os.environ.get("OPENAI_BASE_URL")
        if openai_key:
            llm_clients[1] = OpenAI(api_key=openai_key, base_url=openai_url)
            model_names[1] = settings.get('ai_model_backup', 'gpt-3.5-turbo')

    # 2. 生成今日报告内容
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    update_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    
    # 报告头部
    report_content = settings.get('readme_header', "# Daily Report").replace("{{ update_time }}", update_time)
    report_content += f"> 更新时间: {update_time}\n\n"

    # 遍历集合抓取数据
    for col in config['collections']:
        limit = settings.get('top_list_limit', 10)
        
        if 'collection_name' in col:
            period = col.get('period', 'past_month')
            repos = fetch_by_collection_name(col['collection_name'], period, limit)
        else:
            repos = fetch_trending(col['language'], col['period'], limit=limit)
        
        if repos:
            section_md = build_markdown_section(col['title'], repos, settings, history, llm_clients, model_names)
            report_content += section_md + "\n"
        time.sleep(1)

    # 3. 保存每日归档 (archives/202X-XX-XX.md)
    archive_dir = settings['archive_dir']
    os.makedirs(archive_dir, exist_ok=True)
    archive_path = os.path.join(archive_dir, f"{today_str}.md")
    
    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(report_content)
    print(f"✅ 今日归档已保存: {archive_path}")

    # 4. 更新主 README (包含今日内容 + 历史索引)
    
    # 生成历史列表部分
    archive_links = get_archives_list(archive_dir)
    history_section = "\n\n## 🗄️ 历史归档 (Archives)\n\n| 日期 | 报告 |\n| :--- | :--- |\n"
    # 只显示最近 10 条，避免主页太长，或者显示全部
    history_section += "\n".join(archive_links[:14]) # 显示过去两周
    if len(archive_links) > 14:
         history_section += f"\n| ... | [更多历史](./{archive_dir}) |"

    final_readme = report_content + history_section
    
    with open(settings['readme_file'], "w", encoding="utf-8") as f:
        f.write(final_readme)
    print(f"✅ README 更新成功")

    # 5. 保存缓存
    save_history(settings['history_file'], history)

if __name__ == "__main__":
    main()
