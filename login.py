import time
import json
import os
import sys
import logging
from pathlib import Path
import undetected_chromedriver as uc # <--- The secret weapon

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '.')))

from config.settings import Settings

# Setup simple logging for the helper
logging.basicConfig(level=logging.INFO, format='[%(levelname)s] - %(message)s')
logger = logging.getLogger("LoginHelper")

def save_account_cookies(new_cookies, platform, account_name):
    """Saves cookies to a specific account file in the platform folder."""
    platform_dir = Settings.SECRETS_DIR / platform
    platform_dir.mkdir(parents=True, exist_ok=True)
    
    # Sanitize account name for filename
    safe_name = "".join([c for c in account_name if c.isalnum() or c in (' ', '.', '_')]).strip()
    target_path = platform_dir / f"{safe_name}.json"

    with open(target_path, 'w') as f:
        json.dump(new_cookies, f, indent=4)
    
    return target_path

def run_login(target_url, site_name, platform):
    logger.info(f"🚀 Launching Stealth Browser for {site_name}...")
    
    options = uc.ChromeOptions()
    options.add_argument("--start-maximized")
    
    # 🟢 FORCE CHROME 120 USER AGENT
    static_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    options.add_argument(f"--user-agent={static_ua}")
    
    # PERSISTENT IDENTITY: Use the same profile folder as the main bot
    user_profile = str(Settings.BROWSER_PROFILE_DIR)
    
    try:
        driver = uc.Chrome(options=options, user_data_dir=user_profile, version_main=144, use_subprocess=True)
        driver.get(target_url)

        print("\n" + "="*60)
        print(f"🔒 MANUAL LOGIN: {site_name.upper()}")
        print("="*60)
        print(f"1. Complete the login process for {site_name} in the browser.")
        print("2. Once logged in, return here.")
        print("="*60)
        
        input(">>> Press ENTER after you have logged in <<<")
        
        account_name = input(f"Enter a name for this {site_name} account (e.g. main, alt1): ").strip() or "account"

        logger.info("📡 Capturing session data...")
        raw_cookies = driver.get_cookies()
        
        target_path = save_account_cookies(raw_cookies, platform, account_name)
        logger.info(f"✅ SUCCESS: Account saved to {target_path}")
        
        time.sleep(1)

    except Exception as e:
        logger.error(f"❌ Login helper failed: {e}")
    finally:
        logger.info("🛑 Closing browser...")
        try:
            driver.quit()
        except: pass
        sys.exit(0)

if __name__ == "__main__":
    print("Verzue Service Login Utility")
    print("1. MechaComic (JP)")
    print("2. Jumptoon (Next.js)")
    print("3. KakaoPage (KR)")
    print("4. Tencent AC.QQ (CN)")
    print("5. Piccoma (JP/FR)")
    print("q. Quit")
    
    choice = input("\nSelect target: ").lower()
    
    if choice == "1":
        run_login("https://mechacomic.jp/login", "MechaComic", "mecha")
    elif choice == "2":
        run_login("https://jumptoon.com/", "Jumptoon", "jumptoon")
    elif choice == "3":
        run_login("https://page.kakao.com/", "KakaoPage", "kakao")
    elif choice == "4":
        run_login("https://ac.qq.com/login", "Tencent AC.QQ", "acqq")
    elif choice == "5":
        run_login("https://piccoma.com/web/acc/signin", "Piccoma", "piccoma")
    elif choice == "q":
        sys.exit()
    else:
        print("Invalid choice.")
