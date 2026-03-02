import requests
from bs4 import BeautifulSoup
import json
import os
import urllib3

# Suppress the insecure request warnings since we are bypassing SSL verification
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class HTMLScraper:
    def __init__(self, url: str):
        self.url = url
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        self.soup = None

    def fetch_page(self):
        """Fetches the HTML content of the page."""
        print(f"Fetching HTML from: {self.url}")
        try:
            # Added verify=False to bypass the SSL certificate error
            response = requests.get(self.url, headers=self.headers, verify=False, timeout=15)
            response.raise_for_status()
            self.soup = BeautifulSoup(response.text, 'html.parser')
            print("Page fetched successfully.")
        except requests.RequestException as e:
            print(f"Error fetching page: {e}")

    def extract_metadata(self):
        """Extracts basic metadata like title and description."""
        if not self.soup:
            return {}
        
        title = self.soup.title.string if self.soup.title else "No Title"
        desc_tag = self.soup.find("meta", attrs={"name": "description"})
        description = desc_tag["content"] if desc_tag else "No Description"
        
        return {"title": title, "description": description}

    def extract_images(self):
        """Finds all image URLs on the page."""
        if not self.soup:
            return []
        
        images = []
        for img in self.soup.find_all('img'):
            src = img.get('src') or img.get('data-src')
            if src:
                # Handle relative URLs
                if src.startswith('/'):
                    src = self.url.rstrip('/') + src
                images.append(src)
        return images

    def save_raw_html(self, filename="dump_page.html"):
        """Saves the raw HTML to a file."""
        if self.soup:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(self.soup.prettify())
            print(f"Raw HTML saved to {filename}")

if __name__ == "__main__":
    print("=== HTML Scraper ===")
    target_url = input("Enter the URL to scrape: ").strip()
    
    if target_url:
        # Auto-add https:// if you forgot to type it
        if not target_url.startswith('http'):
            target_url = 'https://' + target_url
            
        scraper = HTMLScraper(target_url)
        scraper.fetch_page()
        scraper.save_raw_html("dump_page.html")
        
        metadata = scraper.extract_metadata()
        print("\nMetadata Extracted:", json.dumps(metadata, indent=2))
        
        images = scraper.extract_images()
        print(f"\nFound {len(images)} images.")
    else:
        print("No URL provided. Exiting.")