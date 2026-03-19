// ==UserScript==
// @name         Newtoki Cookie Helper
// @namespace    http://tampermonkey.net/
// @version      1.0
// @description  Easy copy cookies and User-Agent for Newtoki Scraper
// @author       Antigravity
// @match        *://newtoki*.com/*
// @grant        GM_setClipboard
// ==/UserScript==

(function() {
    'use strict';

    // Create a floating button
    const btn = document.createElement('button');
    btn.innerHTML = '📋 Copy Cookies for Scraper';
    btn.style.position = 'fixed';
    btn.style.bottom = '20px';
    btn.style.right = '20px';
    btn.style.zIndex = '9999';
    btn.style.padding = '10px 15px';
    btn.style.backgroundColor = '#007bff';
    btn.style.color = 'white';
    btn.style.border = 'none';
    btn.style.borderRadius = '5px';
    btn.style.cursor = 'pointer';
    btn.style.fontSize = '14px';
    btn.style.fontWeight = 'bold';
    btn.style.boxShadow = '0 4px 6px rgba(0,0,0,0.1)';

    btn.onmouseover = () => { btn.style.backgroundColor = '#0056b3'; };
    btn.onmouseout = () => { btn.style.backgroundColor = '#007bff'; };

    btn.onclick = function() {
        const cookies = document.cookie;
        const ua = navigator.userAgent;
        
        let message = "Cookies copied to clipboard!";
        let isMissingClearance = !cookies.includes("cf_clearance");
        
        if (isMissingClearance) {
            message = "⚠️ Warning: cf_clearance missing!\n\nIt might be 'httpOnly'. If the scraper fails, you MUST copy the 'Cookie' value manually from the F12 -> Network tab.";
        }

        // Copy to clipboard
        GM_setClipboard(cookies);
        
        // Visual feedback
        const originalText = btn.innerHTML;
        btn.innerHTML = isMissingClearance ? '⚠️ Missing Clearance' : '✅ Cookies Copied!';
        btn.style.backgroundColor = isMissingClearance ? '#ffc107' : '#28a745';
        
        console.log("--- Newtoki Scraper Info ---");
        console.log("User-Agent:", ua);
        console.log("Cookies:", cookies);
        if (isMissingClearance) {
            console.warn("CRITICAL: cf_clearance cookie is missing from document.cookie. You likely need to copy it from the Network tab headers.");
        }
        console.log("----------------------------");

        setTimeout(() => {
            btn.innerHTML = originalText;
            btn.style.backgroundColor = '#007bff';
            alert(message + "\n\nAlso check Console (F12) for your User-Agent.");
        }, 2000);
    };

    document.body.appendChild(btn);
})();
