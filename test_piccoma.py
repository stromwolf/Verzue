import asyncio
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

async def main():
    async with AsyncSession() as session:
        # User agent
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"})
        res = await session.get("https://piccoma.com/web/product/201578/episodes?etype=E")
        print(f"Status: {res.status_code}")
        soup = BeautifulSoup(res.text, 'html.parser')
        forms = soup.find_all("form")
        for f in forms:
            print("Form Action:", f.get("action"), "Method:", f.get("method"), "ID:", f.get("id"))
            for inp in f.find_all("input"):
                print("  Input:", inp.get("name"), "=", inp.get("value"))
        
        # also print the button waitfree if any
        btns = soup.select(".btn-waitfree, .PCM-btn-waitfree, .btn")
        for b in btns:
            print("Button:", b)

asyncio.run(main())
