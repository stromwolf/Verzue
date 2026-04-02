import asyncio
from app.providers.platforms.piccoma import PiccomaProvider
from bs4 import BeautifulSoup

async def main():
    try:
        provider = PiccomaProvider()
        # Mock task
        class Task:
            url = "https://piccoma.com/web/viewer/200113/6155263"
        session = await provider._get_authenticated_session(".piccoma.com")
        res = await session.get("https://piccoma.com/web/product/200113/episodes?etype=E", timeout=15)
        soup = BeautifulSoup(res.text, 'html.parser')
        form = soup.select_one('#js_purchaseForm, form[action*="purchase"], form[action*="episode"]')
        if form:
            print("FOUND FORM:")
            print(form.prettify())
        else:
            print("FORM NOT FOUND. Status:", res.status_code)
            if len(res.text) < 10000 and "Japan" in res.text:
                print("Geo-blocked.")
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    asyncio.run(main())
