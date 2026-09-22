#!/usr/bin/env python3
"""
Gramedia.com restock watcher.

Watches one or more Gramedia.com product pages and pings a Discord webhook
the moment a book flips from "out of stock" to "in stock".

Gramedia.com is a client-rendered (Next.js) site with no public stock API,
so this uses a real headless browser (Playwright) to load each page the
way a shopper's browser would, then looks for Indonesian stock-status text
and the state of the "add to cart" / "buy now" button.

Usage:
    python restock_watcher.py            # run forever, checking on a loop
    python restock_watcher.py --once      # check every book once and exit (good for cron)
    python restock_watcher.py --debug     # print what text/buttons were found on each page
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

# Phrases that indicate a book is OUT of stock on Gramedia's product pages.
# If you find the bot getting it wrong for a specific book, run with --debug,
# look at the printed page text, and add/adjust phrases here.
OUT_OF_STOCK_PHRASES = [
    "stok habis",
    "produk habis",
    "sedang habis",
    "kosong",
    "habis terjual",
    "sold out",
    "segera hadir",
    "pre-order habis",
]

# Phrases on an enabled "buy" button that indicate the item CAN be purchased.
IN_STOCK_BUTTON_PHRASES = [
    "tambah ke keranjang",
    "tambah keranjang",
    "beli sekarang",
    "beli langsung",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("restock_watcher")


def load_config():
    """
    Config can come from environment variables (used when deployed to a
    host like Railway) or from config.json (used for local runs). Env
    vars take priority if both are present.

    Env vars:
      DISCORD_WEBHOOK_URL          required
      BOOKS_JSON                   required - JSON array, e.g.
                                    [{"name":"Man Boy","url":"https://www.gramedia.com/products/man-boy"}]
      CHECK_INTERVAL_MINUTES       optional, default 15
    """
    import os

    env_webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    env_books = os.environ.get("BOOKS_JSON")

    if env_webhook and env_books:
        try:
            books = json.loads(env_books)
        except json.JSONDecodeError as e:
            log.error(f"BOOKS_JSON env var is not valid JSON: {e}")
            sys.exit(1)
        return {
            "discord_webhook_url": env_webhook,
            "books": books,
            "check_interval_minutes": int(os.environ.get("CHECK_INTERVAL_MINUTES", 15)),
        }

    if not CONFIG_PATH.exists():
        log.error(
            "No config found. Either set DISCORD_WEBHOOK_URL and BOOKS_JSON "
            "environment variables, or copy config.example.json to config.json "
            "and fill in your webhook URL and book URLs."
        )
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def check_book_stock(page, url, debug=False):
    """
    Loads a Gramedia product page and returns (in_stock: bool, page_title: str).
    """
    page.goto(url, wait_until="networkidle", timeout=45000)
    # Give any client-side rendering a moment to settle.
    page.wait_for_timeout(1500)

    body_text = page.inner_text("body").lower()

    title = ""
    try:
        title = page.locator("h1").first.inner_text(timeout=3000).strip()
    except Exception:
        pass

    found_out_of_stock = [p for p in OUT_OF_STOCK_PHRASES if p in body_text]

    # Look for an enabled buy button as a positive signal.
    buy_button_enabled = False
    for phrase in IN_STOCK_BUTTON_PHRASES:
        try:
            btn = page.get_by_text(phrase, exact=False).first
            if btn.count() and btn.is_visible():
                is_disabled = btn.evaluate(
                    "el => el.closest('button') ? el.closest('button').disabled : false"
                )
                if not is_disabled:
                    buy_button_enabled = True
                    break
        except Exception:
            continue

    if debug:
        snippet = body_text[:800].replace("\n", " ")
        log.info(f"[DEBUG] {url}")
        log.info(f"[DEBUG] Title detected: {title!r}")
        log.info(f"[DEBUG] Out-of-stock phrases found: {found_out_of_stock}")
        log.info(f"[DEBUG] Enabled buy button found: {buy_button_enabled}")
        log.info(f"[DEBUG] Page text snippet: {snippet}")

    if found_out_of_stock:
        in_stock = False
    else:
        # No out-of-stock phrase found. Trust an enabled buy button if we saw one;
        # otherwise assume in stock (Gramedia usually shows an explicit "habis" message).
        in_stock = True

    return in_stock, title or url


def send_discord_notification(webhook_url, book_name, url):
    payload = {
        "content": f"📚 **Restock alert!** *{book_name}* is back in stock on Gramedia.com!\n{url}"
    }
    resp = requests.post(webhook_url, json=payload, timeout=15)
    if resp.status_code not in (200, 204):
        log.error(f"Discord webhook failed ({resp.status_code}): {resp.text}")
    else:
        log.info(f"Sent Discord notification for: {book_name}")


def run_check(config, state, debug=False):
    webhook_url = config["discord_webhook_url"]
    books = config["books"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )

        for book in books:
            url = book["url"]
            name = book.get("name", url)
            try:
                in_stock, detected_title = check_book_stock(page, url, debug=debug)
                display_name = book.get("name") or detected_title

                was_in_stock = state.get(url, {}).get("in_stock")
                log.info(
                    f"{display_name}: {'IN STOCK' if in_stock else 'out of stock'} "
                    f"(previous: {was_in_stock})"
                )

                # Notify only on a transition from out-of-stock -> in-stock,
                # so you don't get spammed every single check.
                if in_stock and was_in_stock is False:
                    send_discord_notification(webhook_url, display_name, url)

                state[url] = {"name": display_name, "in_stock": in_stock}

            except Exception as e:
                log.error(f"Failed to check {name} ({url}): {e}")

        browser.close()

    save_state(state)


def main():
    parser = argparse.ArgumentParser(description="Gramedia.com restock watcher")
    parser.add_argument(
        "--once", action="store_true", help="Check all books once and exit (good for cron/Task Scheduler)"
    )
    parser.add_argument(
        "--debug", action="store_true", help="Print extra detail about what was detected on each page"
    )
    args = parser.parse_args()

    config = load_config()
    state = load_state()
    interval_minutes = config.get("check_interval_minutes", 15)

    if args.once:
        run_check(config, state, debug=args.debug)
        return

    log.info(f"Starting watcher. Checking every {interval_minutes} minute(s). Ctrl+C to stop.")
    while True:
        try:
            run_check(config, state, debug=args.debug)
        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as e:
            log.error(f"Unexpected error in check loop: {e}")
        time.sleep(interval_minutes * 60)


if __name__ == "__main__":
    main()
