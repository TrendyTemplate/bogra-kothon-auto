# Bogra Kothon - Fully Automatic Version (GitHub Actions)

import os
import requests
import feedparser
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

# Environment variables (GitHub Secrets থেকে আসবে)
PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "")
ACCESS_TOKEN = os.getenv("FACEBOOK_ACCESS_TOKEN", "")

NEWS_SOURCES = [
    "https://www.prothomalo.com/feed/",
    "https://www.jugantor.com/feed",
    "https://www.ittefaq.com.bd/feed/",
    "https://banglatribune.com/feed",
    "https://www.bdnews24.com/rss/rss.xml",
]

def collect_news():
    news = []
    for url in NEWS_SOURCES:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:2]:
                news.append({
                    "title": entry.title,
                    "summary": getattr(entry, "summary", "")[:200],
                    "link": entry.link
                })
        except:
            continue
    return news[:3]

def create_image(title, filename="card.png"):
    img = Image.new("RGB", (1080, 1080), "#F8F4E9")
    draw = ImageDraw.Draw(img)
    
    # লোগো যোগ (যদি থাকে)
    if os.path.exists("logo.png"):
        logo = Image.open("logo.png").convert("RGBA").resize((280, 280))
        img.paste(logo, (400, 40), logo)
    
    # সহজ টেক্সট
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
    except:
        font = ImageFont.load_default()
    
    y = 360
    words = title.split()
    line = ""
    for word in words:
        test = line + word + " "
        if len(test) > 28:
            draw.text((80, y), line, fill="#0A3D2E", font=font)
            y += 50
            line = word + " "
        else:
            line = test
    draw.text((80, y), line, fill="#0A3D2E", font=font)
    
    img.save(filename)
    return filename

def post_to_facebook(message, image_path):
    if not PAGE_ID or not ACCESS_TOKEN:
        print("Facebook credentials missing")
        return False
    
    url = f"https://graph.facebook.com/v19.0/{PAGE_ID}/photos"
    data = {
        "message": message,
        "access_token": ACCESS_TOKEN,
        "published": "true"
    }
    files = {"source": open(image_path, "rb")}
    
    r = requests.post(url, data=data, files=files, timeout=30)
    if r.status_code == 200:
        print("Posted successfully")
        return True
    else:
        print("Facebook error:", r.text)
        return False

def main():
    print("Starting Bogra Kothon Automation...")
    news_list = collect_news()
    
    if not news_list:
        print("No news found")
        return
    
    for i, news in enumerate(news_list):
        caption = f"{news['title']}\n\nবগুড়া কথন\n#বগুড়াকথন #BograKothon"
        image = create_image(news["title"], f"card_{i}.png")
        post_to_facebook(caption, image)
        print(f"Processed: {news['title'][:50]}")

if __name__ == "__main__":
    main()
