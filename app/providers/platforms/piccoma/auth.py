import logging
import asyncio
from typing import Optional

logger = logging.getLogger("PiccomaAuth")

class PiccomaAuth:
    def __init__(self, provider):
        self.provider = provider

    async def auth_probe(self, session, strictness: str = "normal") -> bool:
        """
        Verify session is genuinely logged in, not just has a pksid cookie.
        
        strictness:
          - 'lenient': homepage + pksid only (fast, uses existing logic)
          - 'normal': adds bookshelf 200 OK check (ensures access to protected resources)
          - 'strict': reserved for deeper checks (e.g. history + viewer pre-flight)
        """
        
        # 1. Check Homepage (Check if we are in a guest shell)
        homepage_ok = await self._check_homepage(session)
        if not homepage_ok:
            logger.warning("🚨 [Piccoma] Auth probe FAILED: Homepage is a login shell.")
            return False
            
        # 2. check pksid cookie presence
        # For curl_cffi session.cookies is a CookieJar-like object
        pksid_present = any(c.name == "pksid" and c.value for c in session.cookies)
        if not pksid_present:
            logger.warning("🚨 [Piccoma] Auth probe FAILED: 'pksid' cookie missing.")
            return False
            
        if strictness == "lenient":
            return True
            
        # 3. Test a PROTECTED endpoint that requires real auth (Bookshelf)
        # If bookshelf returns 404 or signin redirect, the session is NOT really authed
        try:
            # We use navigation headers to mimic a real user visit
            from .helpers import PiccomaHelpers
            headers = PiccomaHelpers.get_navigation_headers()
            
            r = await session.get(
                "https://piccoma.com/web/bookshelf",
                headers=headers,
                allow_redirects=False,
                timeout=12
            )
            
            # Auth is valid if status is 200 and we didn't get redirected to signin
            location = str(r.headers.get("Location", ""))
            bookshelf_authed = (
                r.status_code == 200 and
                "/acc/signin" not in location and
                "ログイン｜ピッコマ" not in r.text[:5000]
            )
            
            if not bookshelf_authed:
                logger.warning(
                    f"🚨 [Piccoma] Auth probe DEGRADED: pksid present but bookshelf rejected "
                    f"(Status: {r.status_code}, Location: {location})."
                )
        except Exception as e:
            logger.warning(f"⚠️ [Piccoma] Auth probe bookshelf check failed with error: {e}")
            bookshelf_authed = False
            
        return bookshelf_authed

    async def _check_homepage(self, session) -> bool:
        """Verify the homepage content indicates an authenticated state."""
        try:
            from .helpers import PiccomaHelpers
            headers = PiccomaHelpers.get_navigation_headers()
            
            res = await session.get("https://piccoma.com/web/", headers=headers, timeout=12)
            if res.status_code != 200:
                return False
                
            # Use assistant's helper to detect guest shell
            # If the helper returns True, it means it IS a guest shell (invalid auth)
            is_guest = self.provider.helpers.piccoma_html_indicates_guest_shell(str(res.url), res.text)
            
            # Additional content markers from user's Fix 3
            is_explicit_login = "ログイン｜ピッコマ" in res.text or "PCM-loginMenu" in res.text
            
            return not (is_guest or is_explicit_login)
        except Exception as e:
            logger.warning(f"⚠️ [Piccoma] Homepage check failed: {e}")
            return False
