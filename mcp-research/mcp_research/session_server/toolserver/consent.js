// Best-effort auto-click of cookie-consent accept buttons. Runs in every page
// (toolserver init script); silent no-op without a banner, never throws.
(() => {
  const KW = ["accept all", "accept cookies", "accept", "agree", "i agree", "got it", "allow all",
    "godta alle", "godta", "aksepter alle", "aksepter", "jeg godtar"];
  const SEL = 'button, a, [role="button"]';
  const tryClick = () => {
    try {
      for (const el of document.querySelectorAll(SEL)) {
        const t = (el.innerText || "").trim().toLowerCase();
        if (t && t.length < 40 && KW.includes(t) && el.getBoundingClientRect().width > 0) {
          el.click();
          return true;
        }
      }
    } catch (e) {}
    return false;
  };
  try {
    if (window.top !== window) return;
    let tries = 0;
    const iv = setInterval(() => { if (tryClick() || ++tries > 20) clearInterval(iv); }, 500);
  } catch (e) {}
})();
