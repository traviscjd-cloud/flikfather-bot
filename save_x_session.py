import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto("https://x.com/login")

        print("Log into X manually in the browser window.")
        print("Use your dedicated scraper X account, not your main.")
        print("After you are fully logged in, come back here and press ENTER.")

        input()

        await context.storage_state(path="x_session.json")
        print("✅ Saved X login session to x_session.json")

        await browser.close()

asyncio.run(main())
