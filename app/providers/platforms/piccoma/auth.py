import logging
import asyncio
from typing import Optional

logger = logging.getLogger("PiccomaAuth")

class PiccomaAuth:
    def __init__(self, provider):
        self.provider = provider

    async def auth_probe(self, session, strictness: str = "normal") -> dict:
        """
        Probe authentication state using REAL Piccoma endpoints.
        
        Uses /web/bookshelf/bookmark as the canonical auth check — it requires login
        and reliably returns 200 for authed users, redirects to /acc/signin otherwise.
        """
        results = {
            "homepage_ok": False,
            "pksid_present": False,
            "bookshelf_ok": None,  # None = couldn't determine, True/False = definitive
            "verdict": "UNKNOWN",
            "details": []
        }
        
        # Check 1: pksid cookie (cheap, definitive)
        results["pksid_present"] = any(c.name == "pksid" for c in session.cookies.jar)
        if not results["pksid_present"]:
            results["verdict"] = "INVALID"
            results["details"].append("No pksid")
            return results
        
        # Check 2: Homepage is not the signin shell
        try:
            from .helpers import PiccomaHelpers
            headers = PiccomaHelpers.get_navigation_headers()
            
            r = await session.get("https://piccoma.com/web/", headers=headers, allow_redirects=True, timeout=12)
            homepage_text = (r.text or "")[:5000]
            is_signin_shell = (
                "/acc/signin" in str(r.url) or
                "/acc/email/signin" in str(r.url) or
                'data-page="signin"' in homepage_text
            )
            results["homepage_ok"] = (r.status_code == 200 and not is_signin_shell)
            results["details"].append(f"Home: {r.status_code}{'(signin)' if is_signin_shell else 'OK'}")
        except Exception as e:
            results["details"].append(f"Home: ERR")
        
        # 🔧 Check 3: Probe /web/bookshelf/bookmark — the REAL protected endpoint
        if strictness in ("normal", "strict"):
            try:
                from .helpers import PiccomaHelpers
                headers = PiccomaHelpers.get_navigation_headers()
                
                r = await session.get(
                    "https://piccoma.com/web/bookshelf/bookmark",
                    headers=headers,
                    allow_redirects=False,  # Don't follow redirects — we want to SEE them
                    timeout=12
                )
                
                location = r.headers.get("Location", "")
                
                if r.status_code == 200:
                    # Authed user sees the bookshelf
                    # Sanity check: make sure the body isn't actually a signin shell
                    body_preview = (r.text or "")[:3000]
                    if "/acc/signin" in body_preview or 'data-page="signin"' in body_preview:
                        results["bookshelf_ok"] = False
                        results["details"].append("Shelf: 200(signin-body)")
                    else:
                        results["bookshelf_ok"] = True
                        results["details"].append("Shelf: 200(authed)")
                elif r.status_code in (301, 302, 303, 307, 308):
                    if "/acc/signin" in location:
                        # Definitive auth failure
                        results["bookshelf_ok"] = False
                        results["details"].append(f"Shelf: {r.status_code}(signin-redirect)")
                    else:
                        # Some other redirect — ambiguous
                        results["bookshelf_ok"] = None
                        results["details"].append(f"Shelf: {r.status_code}(other-redirect)")
                elif r.status_code in (401, 403):
                    results["bookshelf_ok"] = False
                    results["details"].append(f"Shelf: {r.status_code}(rejected)")
                elif r.status_code == 404:
                    # Shouldn't happen for /bookmark
                    results["bookshelf_ok"] = None
                    results["details"].append("Shelf: 404(URL-moved?)")
                    logger.warning(
                        "🚨 [Piccoma] /web/bookshelf/bookmark returned 404 — "
                        "endpoint may have moved. Verify probe URL!"
                    )
                else:
                    results["bookshelf_ok"] = None
                    results["details"].append(f"Shelf: {r.status_code}(?)")
            except Exception as e:
                results["bookshelf_ok"] = None
                results["details"].append(f"Shelf: ERR")
        
        # 🔧 Verdict logic
        if not results["pksid_present"]:
            results["verdict"] = "INVALID"
        elif results["bookshelf_ok"] is False:
            # Bookshelf gave a definitive negative — auth is broken
            results["verdict"] = "INVALID"
        elif results["bookshelf_ok"] is True:
            # Bookshelf gave a definitive positive — auth is solid
            results["verdict"] = "HEALTHY"
        elif results["homepage_ok"]:
            # Bookshelf was ambiguous, but homepage is fine and pksid is present
            # Fail-open: assume healthy, let the actual operation prove it wrong
            results["verdict"] = "HEALTHY"
        else:
            results["verdict"] = "DEGRADED"
        
        return results

    async def is_authenticated(self, session) -> bool:
        """Boolean version — fails OPEN on uncertainty, fails CLOSED on definitive rejection."""
        result = await self.auth_probe(session, strictness="normal")
        return result["verdict"] != "INVALID"

    async def _check_homepage(self, session) -> bool:
        """Verify the homepage content indicates an authenticated state."""
        try:
            from .helpers import PiccomaHelpers
            headers = PiccomaHelpers.get_navigation_headers()
            
            res = await session.get("https://piccoma.com/web/", headers=headers, timeout=12)
            if res.status_code != 200:
                return False
                
            is_guest = self.provider.helpers.piccoma_html_indicates_guest_shell(str(res.url), res.text)
            is_explicit_login = "ログイン｜ピッコマ" in res.text or "PCM-loginMenu" in res.text
            
            return not (is_guest or is_explicit_login)
        except Exception as e:
            logger.warning(f"⚠️ [Piccoma] Homepage check failed: {e}")
            return False
