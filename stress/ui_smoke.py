"""Browser smoke test (Playwright, headless Chromium).

Walks the real UI: sign-up wizard -> app shell -> every section and tab ->
chat with the AI -> terminal / background tasks -> settings -> sign out ->
Google-like account chooser -> sign back in. Fails on any console error,
uncaught exception or CSP violation. Screenshots go to --shots.
  python stress/ui_smoke.py --base http://127.0.0.1:8700 --shots data/ui_shots
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

PASSWORD = "Passw0rdUi"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8700")
    ap.add_argument("--shots", default=str(Path(__file__).resolve().parent.parent / "data" / "ui_shots"))
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--no-ai", action="store_true")
    a = ap.parse_args()
    shots = Path(a.shots)
    shots.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    steps: list[str] = []
    user = "ui" + uuid.uuid4().hex[:8]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark" if a.dark else "light")
        page = ctx.new_page()
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}") if m.type in ("error",) else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))

        def shot(name: str) -> None:
            page.screenshot(path=str(shots / f"{name}.png"), full_page=False)
            steps.append(name)

        page.goto(a.base + "/")
        page.wait_for_load_state("networkidle")
        shot("01-home")

        page.goto(a.base + "/register")
        page.fill("#name", "Ui Tester")
        page.fill("#username", user)
        page.fill("#email", f"{user}@example.com")
        page.fill("#password", PASSWORD)
        shot("02-register")
        page.click("#next")
        page.fill("#ai_name", "Nova")
        page.click("#next")
        page.check("#owns_business")
        page.fill("#business_name", "Nova Labs")
        page.fill("#business_description", "We build smart lamps")
        page.click("#next")
        page.click("#next")
        page.click("#submit")
        page.wait_for_url("**/app", timeout=30000)
        page.wait_for_selector("#c-input", timeout=30000)
        time.sleep(1.5)
        shot("03-app-personal-chat")

        for section in ("personal", "business", "coding"):
            page.click(f"#section-switch [data-section='{section}']")
            time.sleep(1.2)
            tabs = page.locator("#tabs button")
            for i in range(tabs.count()):
                label = tabs.nth(i).inner_text().strip()
                tabs.nth(i).click()
                time.sleep(1.4)
                shot(f"10-{section}-{i:02d}-{label.lower().replace(' ', '-')}")
        page.click("#section-switch [data-section='personal']")
        time.sleep(1)
        page.locator("#tabs button").first.click()
        time.sleep(1)

        if not a.no_ai:
            page.fill("#c-input", "Say hello in exactly five words.")
            page.keyboard.press("Enter")
            page.wait_for_selector(".bubble-ai .md p", timeout=180000)
            page.wait_for_function("() => !document.querySelector('#c-stop') || document.querySelector('#c-stop').classList.contains('hidden')",
                                   timeout=240000)
            time.sleep(1)
            shot("20-chat-reply")

        page.click("#toggle-terminal")
        time.sleep(1)
        shot("21-terminal")
        page.click("#term-close")
        page.click("#open-jobs")
        time.sleep(1)
        shot("22-jobs-drawer")
        page.click("#close-jobs")

        page.goto(a.base + "/settings")
        page.wait_for_selector("#profile-form", timeout=20000)
        time.sleep(1)
        shot("30-settings")

        page.goto(a.base + "/app")
        page.wait_for_selector("#logout")
        page.click("#logout")
        page.wait_for_url("**/login")
        page.wait_for_selector("#account-list li", timeout=10000)
        shot("40-account-chooser")
        page.locator("#account-list li").first.click()
        page.fill("#password", PASSWORD)
        page.click("#login-btn")
        page.wait_for_url("**/app", timeout=20000)
        steps.append("relogin-ok")

        # phone layout
        page.set_viewport_size({"width": 390, "height": 844})
        page.reload()
        page.wait_for_selector("#c-input", timeout=20000)
        time.sleep(1)
        shot("50-mobile")
        browser.close()

    ignore = ("favicon", "net::ERR_ABORTED")
    problems = [x for x in problems if not any(i in x for i in ignore)]
    print(json.dumps({"steps": steps, "problems": problems}, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
