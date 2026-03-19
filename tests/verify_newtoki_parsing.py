import re
import os
from bs4 import BeautifulSoup

def decode_html_data(hex_str):
    # Hex string format is '64.61.74.61...'
    # Split by '.' and convert each hex pair to char
    try:
        parts = [p for p in hex_str.split('.') if p.strip()]
        return "".join([chr(int(p, 16)) for p in parts])
    except Exception as e:
        print(f"Error decoding hex: {e}")
        return ""

def test_series_parsing(file_path):
    print(f"Testing Series Parsing: {file_path}")
    with open(file_path, 'r', encoding='utf-8') as f:
        html = f.read()
    
    soup = BeautifulSoup(html, 'html.parser')
    
    # 1. Title
    title = ""
    meta_title = soup.find('meta', property='og:title')
    if meta_title:
        title = meta_title['content'].split('>')[0].strip()
    print(f"Title Found: {title}")
    
    # 2. Thumbnail
    thumb = ""
    meta_thumb = soup.find('meta', property='og:image')
    if meta_thumb:
        thumb = meta_thumb['content']
    print(f"Thumbnail Found: {thumb}")
    
    # 3. Chapters
    chapters = []
    # Based on previous view_file, chapters are in a list with data-index
    # Example: <li class="list-item" ...> <a href="..." ...> ... </a> </li>
    # Let's look for the specific pattern again or use a common one
    # Newtoki usually uses <li> with wr-id or similar in a list named 'serial-list' or 'list-body'
    chapter_list = soup.select('ul.list-body li.list-item')
    for item in chapter_list:
        link = item.find('a', class_='item-subject')
        if link:
            ch_title = link.get_text(strip=True)
            ch_url = link['href']
            # Chapter ID is usually at the end of the URL
            ch_id = ch_url.split('/')[-1].split('?')[0]
            chapters.append({'title': ch_title, 'url': ch_url, 'id': ch_id})
    
    print(f"Chapters Count: {len(chapters)}")
    if chapters:
        print(f"First Chapter: {chapters[-1]}")
        print(f"Last Chapter: {chapters[0]}")
    return chapters

def test_episode_parsing(file_path):
    print(f"\nTesting Episode Parsing: {file_path}")
    with open(file_path, 'r', encoding='utf-8') as f:
        html = f.read()
    
    # 1. Extract html_data
    # Regex to find html_data+='...'; lines
    hex_blocks = re.findall(r"html_data\s*\+?=\s*'([^']+)';", html)
    full_hex = "".join(hex_blocks)
    
    if not full_hex:
        print("Error: No html_data hex blocks found!")
        return []

    decoded_html = decode_html_data(full_hex)
    # print(f"Decoded HTML preview: {decoded_html[:200]}...")
    
    # 2. Extract Image URLs
    # The image URLs are in data-l... attributes
    # We saw data-l8c01dba256 in the grep output
    soup = BeautifulSoup(decoded_html, 'html.parser')
    images = []
    
    # Find all attributes starting with 'data-l'
    for img in soup.find_all('img'):
        for attr, value in img.attrs.items():
            if attr.startswith('data-l') and value.startswith('http'):
                images.append(value)
                break
    
    print(f"Images Count: {len(images)}")
    if images:
        print(f"First Image: {images[0]}")
        print(f"Last Image: {images[-1]}")
    return images

if __name__ == "__main__":
    series_path = r"E:\Code Files\Verzue Bot\Newtoki\Series ID"
    episode_path = r"E:\Code Files\Verzue Bot\Newtoki\Episode ID"
    
    if os.path.exists(series_path):
        test_series_parsing(series_path)
    else:
        print(f"File not found: {series_path}")
        
    if os.path.exists(episode_path):
        test_episode_parsing(episode_path)
    else:
        print(f"File not found: {episode_path}")
