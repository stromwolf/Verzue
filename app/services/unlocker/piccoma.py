from .base import BaseUnlocker

class PiccomaUnlocker(BaseUnlocker):
    async def _is_viewer_accessible(self, task) -> bool:
        """Checks whether chapter viewer is already readable without purchase API."""
        try:
            _, _, domain = self.provider._get_context_from_url(task.url)
            auth_session = await self.provider._get_authenticated_session(domain)
            res = await auth_session.get(task.url, timeout=30)
            final_url = str(getattr(res, "url", task.url))

            signin = (
                "/web/acc/signin" in final_url
                or "ログイン｜ピッコマ" in res.text
                or "PCM-loginMenu" in res.text
                or "/acc/signin?next_url=" in res.text
            )
            if signin:
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
            
            raise Exception("Piccoma coin purchase failed via API")
                
        except Exception as e:
            self.logger.error(f"Worker {context_id} Piccoma purchase failed: {e}")
            raise e
