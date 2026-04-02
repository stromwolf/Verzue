import requests
import json
from bs4 import BeautifulSoup
import os

cookies_dict = {}
try:
    with open('data/secrets/piccoma/cookies.json', 'r') as f:
        cookies = json.load(f)
        for c in cookies:
            cookies_dict[c['name']] = c['value']
except:
    pass

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
req = requests.get('https://piccoma.com/web/product/206094/episodes?etype=E', cookies=cookies_dict, headers=headers)
soup = BeautifulSoup(req.text, 'html.parser')
form = soup.select_one('#js_purchaseForm, form[action*="purchase"], form[action*="episode"]')

if form:
    print(form.prettify())
else:
    print("Form not found. Searching for 'purchase' in input elements:")
    for t in soup.find_all('input'):
        print(t)
    print("Checking for _NEXT_DATA_:")
    next_data = soup.select_one('script#__NEXT_DATA__')
    if next_data:
        print("NEXT_DATA exists!")
