import re
from .base import BaseUnlocker

class PiccomaUnlocker(BaseUnlocker):
    async def _is_viewer_accessible(self, task) -> bool:
        """Checks whether chapter viewer is already readable without purchase API."""
        try:
            base_url, _, domain = self.provider._get_context_from_url(task.url)
            auth_session = await self.provider._get_authenticated_session(domain)
            m = re.search(r"/web/viewer/(?:s/)?(\d+)/(\d+)", task.url)
            ref = (
                f"{base_url}/web/product/{m.group(1)}/episodes?etype=E"
                if m
                else f"{base_url}/web/"
            )
            vh = self.provider._build_browser_headers(referer=ref)
            res = await auth_session.get(task.url, timeout=30, headers=vh)
            final_url = str(getattr(res, "url", task.url))

            if self.provider.helpers.piccoma_html_indicates_guest_shell(final_url, res.text):
                return False

            if self.provider.helpers.viewer_redirected_to_product_page(task.url, final_url):
                return False

            is_locked_ui = (
                "js_purchaseForm" in res.text
                or "チャージ中" in res.text
                or "ポイントで読む" in res.text
            )
            if is_locked_ui:
                return False

            pdata = self.provider._extract_pdata_heuristic(res.text)
            return bool(pdata)
        except Exception:
            return False

    async def unlock(self, context_id: int, task, view, update_progress):
        update_progress(10, "Checking direct viewer access")
        if await self._is_viewer_accessible(task):
            self.logger.info(f"Worker {context_id} Piccoma viewer already accessible; skipping API purchase for {task.chapter_str}.")
            update_progress(95, "Already unlocked (viewer accessible)")
            return True

        update_progress(20, "API Coin Purchase Attempt")
        try:
            success = await self.provider.fast_purchase(task)
            
            if success:
                update_progress(90, "Coin Purchase Successful")
                return True
            
            wf = getattr(task, "piccoma_wait_free", None)
            if wf is True:
                raise Exception("Piccoma wait-free unlock failed via API (no valid endpoint response).")
            if wf is False:
                raise Exception(
                    "Piccoma unlock failed (session/CSRF or v2/coin|point API did not unlock the chapter)."
                )
            raise Exception("Piccoma unlock failed via API.")
                
        except Exception as e:
            self.logger.error(f"Worker {context_id} Piccoma purchase failed: {e}")
            raise e
