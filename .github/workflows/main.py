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
def fetch_trending(language, period):
    url = "https://api.ossinsight.io/q/trending-repos"
    params = {"language": language, "period": period, "format": "json"}
    try:
        print(f"📡 正在抓取: {language} ({period})...")
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return data
    except Exception as e:
        print(f"❌ 抓取失败: {e}")
        return []

# === 4. AI 摘要生成 ===
def generate_ai_summary(client, repo_info):
    """
    生成单个项目的精简评价
    """
    if not client: return ""
    
    name = repo_info.get('repo_name')
    desc = repo_info.get('description', '')
    stars = repo_info.get('stars', 0)

    prompt = (
        f"项目: {name}\n"
        f"描述: {desc}\n"
        f"Stars: {stars}\n"
        "请用中文一句话概括这个项目的核心价值或用途，不要超过50个字。"
    )

    try:
        # print(f"🤖 AI 分析中: {name}...") 
        response = client.chat.completions.create(
            model="gpt-4o-mini", # 或 gpt-3.5-turbo
            messages=[
                {"role": "system", "content": "你是一个开源技术专家。"},
                {"role": "user", "content": prompt}
            ],
            max_tokens=100
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"⚠️ AI Error: {e}")
        return ""

# === 5. Markdown 内容构建 ===
def build_markdown_section(title, repos, settings, history, llm_client):
    section = f"## {title}\n\n"
    section += "| 排名 | 项目 | Stars | 简介 |\n"
    section += "| :--- | :--- | :--- | :--- |\n"

    for idx, repo in enumerate(repos, 1):
        name = repo['repo_name']
        url = f"https://github.com/{name}"
        stars = repo.get('stars', 0)
        raw_desc = repo.get('description', '').replace('|', '\|').replace('\n', ' ')
        
        # --- AI 逻辑 ---
        final_desc = raw_desc
        # 仅对前 N 个项目启用 AI，且检查缓存
        if idx <= settings.get('llm_top_n', 5) and settings['enable_llm']:
            # 检查缓存
            if name in history:
                final_desc = f"🤖 {history[name]['summary']}"
            else:
                # 调用 AI
                ai_summary = generate_ai_summary(llm_client, repo)
                if ai_summary:
                    final_desc = f"🤖 {ai_summary}"
                    # 更新缓存
                    history[name] = {
                        "summary": ai_summary,
                        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d")
                    }
        
        # 截断过长描述防止表格炸裂
        if len(final_desc) > 150:
            final_desc = final_desc[:147] + "..."

        section += f"| {idx} | [{name}]({url}) | 🔥 {stars} | {final_desc} |\n"
    
    return section

# === 6. 归档索引更新 ===
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
    # 1. 准备环境
    config = load_config()
    settings = config['settings']
    history = load_history(settings['history_file'])
    
    # 初始化 LLM
    llm_client = None
    if settings['enable_llm']:
        api_key = os.environ.get("OPENAI_API_KEY")
        base_url = os.environ.get("OPENAI_BASE_URL")
        if api_key:
            llm_client = OpenAI(api_key=api_key, base_url=base_url)

    # 2. 生成今日报告内容
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    update_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    
    # 报告头部
    report_content = settings.get('readme_header', "# Daily Report").replace("{{ update_time }}", update_time)
    report_content += f"> 更新时间: {update_time}\n\n"

    # 遍历集合抓取数据
    for col in config['collections']:
        repos = fetch_trending(col['language'], col['period'])
        if repos:
            section_md = build_markdown_section(col['title'], repos, settings, history, llm_client)
            report_content += section_md + "\n"
        time.sleep(1) # 礼貌请求，防止限流

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
