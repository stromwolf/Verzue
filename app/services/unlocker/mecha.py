import re
import json
import logging
import asyncio
import random
import os
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from .base import BaseUnlocker

class MechaUnlocker(BaseUnlocker):
    """
    S-Grade Mecha Discovery Matrix: Modularizing the unlocking logic
    to ensure maximum resilience and diagnostic visibility.
    """

    async def unlock(self, context_id: int, task, view, update_progress):
        """
        Main entry point for Mecha unlocking tasks.
        """
        update_progress(10, "Initializing Mecha Identity...")
        
        try:
            # 1. Warm-up / Ritual (Optional but recommended for consistency)
            # await self.provider.run_ritual(auth_session)
            
            # 2. Run Discovery Matrix
            update_progress(20, "Executing Discovery Matrix...")
            viewer_url = await self.run_discovery(task, update_progress)
            
            if viewer_url:
                update_progress(95, "Identity Verified & Unlocked")
                # We return the viewer URL so BatchUnlocker knows it succeeded
                return viewer_url
            
            raise Exception("Discovery Matrix failed to secure a viewer URL.")
                
        except Exception as e:
            self.logger.error(f"Worker {context_id} Mecha Discovery failed: {e}")
            # Dump diagnostic on failure
            await self._dump_diagnostic(task, f"fail_{task.episode_id}")
            raise e

    async def run_discovery(self, task, update_progress) -> str | None:
        """
        S-Grade Discovery Loop: Evaluates scenarios A through D to unlock content.
        """
        real_id = task.episode_id
        auth_session = await self.provider._get_authenticated_session()
        target_url = f"{self.provider.BASE_URL}/chapters/{real_id}"
        
        # Scenarios Logic
        # ---------------------------------------------------------
        
        # Scenario A: Verification (Check if already accessible)
        update_progress(30, "Scenario A: Verification...")
        viewer_url = await self.provider._check_chapter_access(auth_session, target_url, real_id)
        if viewer_url:
            self.logger.info(f"[Mecha Discovery] Scenario A Success: Already accessible for {real_id}")
            return viewer_url

        # Fetch page for detailed analysis
        res = await auth_session.get(target_url, timeout=15)
        if res.status_code != 200:
            return None
        
        body = res.text
        soup = BeautifulSoup(body, 'html.parser')
        
        # CSRF Extraction (Multi-Source Pattern)
        token = self._extract_authenticity_token(soup, body)
        self.logger.debug(f"[Mecha Discovery] Extracted Authenticity Token: {token[:10]}...")

        # Scope search to relevant container
        container = soup.select_one(".p-buyConfirm-currentChapter") or soup

        # Scenario B: Free/Zero-Yen Discovery
        update_progress(50, "Scenario B: Free/Read Button Discovery...")
        read_link = container.select_one("a.c-btn-read-end, a.c-btn-free, a.js-bt_read")
        if read_link and read_link.get("href"):
            link_url = urljoin(self.provider.BASE_URL, read_link["href"])
            self.logger.info(f"[Mecha Discovery] Scenario B: Triggering GET link {link_url}")
            follow_res = await auth_session.get(link_url, timeout=15, allow_redirects=True)
            if 'contents_vertical' in str(follow_res.url): return str(follow_res.url)
            # Check body of redirection
            viewer_url = await self.provider._check_chapter_access(auth_session, link_url, real_id)
            if viewer_url: return viewer_url

        # Scenario C: Timed/Wait-Free Handling (Implicit in Button check usually)
        # In Mecha, Wait-Free often uses a form-based button too.
        
        # Scenario D: Point-Based Purchase (Form Submission)
        update_progress(70, "Scenario D: Form-Based Purchase...")
        buy_btn = container.select_one("input.js-bt_buy_and_download, input.c-btn-buy, input.c-btn-free, input.c-btn-read-end, button.c-btn-read-end")
        if buy_btn:
            form = buy_btn.find_parent("form")
            if form:
                action = urljoin(self.provider.BASE_URL, form.get("action", f"/chapters/{real_id}/download"))
                method = form.get("method", "post").lower()
                
                # Payload mapping
                payload = {h.get("name"): h.get("value", "") for h in form.find_all("input", type="hidden") if h.get("name")}
                if token: payload["authenticity_token"] = token
                if buy_btn.get("name"):
                    payload[buy_btn["name"]] = buy_btn.get("value", "")
                
                headers = {"Referer": target_url, "Origin": self.provider.BASE_URL}
                
                self.logger.info(f"[Mecha Discovery] Scenario D: Submitting {method.upper()} to {action}")
                if method == "get":
                    post_res = await auth_session.get(action, params=payload, headers=headers)
                else:
                    post_res = await auth_session.post(action, data=payload, headers=headers)
                
                if 'contents_vertical' in str(post_res.url): return str(post_res.url)
                return await self.provider._check_chapter_access(auth_session, action, real_id)

        self.logger.warning(f"[Mecha Discovery] Matrix Exhausted for {real_id}. No viewer URL found.")
        return None

    def _extract_authenticity_token(self, soup, body) -> str | None:
        """
        S-Grade CSRF Discovery: Pulls authenticity_token from multiple sources.
        """
        # Source 1: Standard Hidden Input
        token_elem = soup.find("input", {"name": "authenticity_token"})
        if token_elem: return token_elem.get("value")
        
        # Source 2: Meta Tags
        meta_token = soup.select_one('meta[name="csrf-token"]')
        if meta_token: return meta_token.get("content")
        
        # Source 3: Raw Regex in Body (for JS variables)
        match = re.search(r'authenticity_token\s*[:=]\s*["\'](.*?)["\']', body)
        if match: return match.group(1)
        
        return None

    async def _dump_diagnostic(self, task, label: str):
        """
        S-Grade Diagnostic: Dumps HTML to local file for analysis.
        """
        try:
            dump_dir = os.path.join(os.getcwd(), "tmp", "mecha_dev")
            os.makedirs(dump_dir, exist_ok=True)
            
            # We don't have the response content here easily without re-fetching, 
            # so we fetch one last time for the dump
            auth_session = await self.provider._get_authenticated_session()
            res = await auth_session.get(f"{self.provider.BASE_URL}/chapters/{task.episode_id}")
            
            filepath = os.path.join(dump_dir, f"{label}.html")
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(res.text)
            self.logger.info(f"[Mecha Diagnostic] Dumped failed page to {filepath}")
        except: pass
