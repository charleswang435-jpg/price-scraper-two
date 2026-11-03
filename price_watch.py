"""
Price watcher for Clash of Clans 14000 Gems on Eldorado. All prices are in Canadian dollars.

What it does:
- Asks you for your target price in Canadian dollars when it starts.
- Opens the product page in a hidden Chrome window every 30 minutes, between 9am and 3am.
- Finds the cheapest seller offer.
- If the site shows US dollars, it converts to Canadian dollars using the Bank of Canada's daily rate.
- Emails you when the cheapest price is under your target.
- Only emails again if the price drops to a new low, or goes back above the target and then dips under it again.
- Writes every check to price_watch_log.csv next to this script.

Setup (one time):
    pip install playwright
    python -m playwright install chromium

Run it:
    python price_watch.py                  asks for your target, then checks on schedule
    python price_watch.py --target 40      skips the question and uses $40 CAD
    python price_watch.py --once           does one check right now and stops
    python price_watch.py --test-email     sends a test email so you know the email settings work

On GitHub Actions, the schedule file runs "--once" every 30 minutes and reads the email
settings and target price from the repository's secrets and variables. Leave the
placeholder email and blank password below as they are in the copy you upload.
"""

import csv
import json
import os
import re
import smtplib
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

# ----------------------------------------------------------------------
# Settings you can change
# ----------------------------------------------------------------------
URL = "https://www.eldorado.gg/clash-of-clans-gems/t/18?attribute_value_id=14000-gems"
ITEM_NAME = "Clash of Clans 14000 Gems"
CHECK_EVERY_MINUTES = 30
START_HOUR = 9                # first check at 9:00am
END_HOUR = 3                  # last check at 3:00am the next morning
END_GRACE_MINUTES = 15        # a late-starting 3:00am check still counts until 3:15am

# Email settings. You can type them here, or set them as environment variables.
# For Gmail you need an "app password", not your normal password.
SENDER_EMAIL = os.environ.get("PRICE_ALERT_FROM") or "charleswang435@gmail.com"
APP_PASSWORD = (os.environ.get("PRICE_ALERT_APP_PASSWORD") or "ettanqhstsslikyc").replace(" ", "")
RECIPIENT_EMAIL = os.environ.get("PRICE_ALERT_TO") or SENDER_EMAIL
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

# After this many failed checks in a row, you get one email saying the watcher needs a look.
FAILURES_BEFORE_WARNING = 6

# Bank of Canada daily exchange rate: Canadian dollars per 1 US dollar.
EXCHANGE_RATE_URL = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?recent=1"

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "price_watch_state.json"
LOG_FILE = HERE / "price_watch_log.csv"

PRICE_PATTERN = r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)"

target_price_cad = None  # filled in when the script starts


# ----------------------------------------------------------------------
# Asking for the target price
# ----------------------------------------------------------------------
def get_target_price():
    if "--target" in sys.argv:
        try:
            return float(sys.argv[sys.argv.index("--target") + 1].replace("$", ""))
        except (IndexError, ValueError):
            sys.exit("Put a number after --target, for example: python price_watch.py --target 40")

    # On GitHub, the target comes from a repository variable instead of a question.
    from_env = os.environ.get("PRICE_ALERT_TARGET_CAD", "").strip().replace("$", "")
    if from_env:
        try:
            return float(from_env)
        except ValueError:
            sys.exit(f"PRICE_ALERT_TARGET_CAD should be a number, but it is '{from_env}'")

    while True:
        try:
            answer = input("Alert me when the price drops below (Canadian dollars): $").strip().replace("$", "")
        except EOFError:
            sys.exit("No target price given. Use --target 40 or set PRICE_ALERT_TARGET_CAD.")
        try:
            value = float(answer)
            if value > 0:
                return value
        except ValueError:
            pass
        print("Please type a number, for example 40 or 39.50")


# ----------------------------------------------------------------------
# Reading the page
# ----------------------------------------------------------------------
def fetch_page_text():
    """Open the page in a hidden browser and return all the visible text."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        sys.exit("Playwright is not installed. Run: pip install playwright && python -m playwright install chromium")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                locale="en-CA",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
            )
            page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
            try:
                # Wait until the seller offers have shown up.
                page.wait_for_selector("text=Buy now", timeout=30_000)
            except PlaywrightTimeout:
                pass
            page.wait_for_timeout(3_000)  # give prices a moment to finish loading
            return page.inner_text("body")
        finally:
            browser.close()


def parse_prices(text):
    """
    Pull the seller prices out of the page text, in whatever currency the site shows.

    The page also lists other gem packs (80, 500, 1200 gems and so on) with their own
    cheap prices, so we only look at the part of the page between the "Total" box
    and the "Trusted by thousands" banner. That part holds the 14000 gem offers.
    """
    start = text.find("Total")
    end = text.find("Trusted by thousands")
    found_section = start != -1 and end > start
    section = text[start:end] if found_section else text

    raw = re.findall(r"Buy now\s*\(\s*[A-Z]{0,2}" + PRICE_PATTERN, section)
    if found_section:
        # Discounted prices appear right after the "-71%*" style marker.
        raw += re.findall(r"%\*\s*[A-Z]{0,2}" + PRICE_PATTERN, section)

    prices = sorted({float(p.replace(",", "")) for p in raw})

    # The currency picker at the top of the page reads like "USD - $" or "CAD - $".
    currency_match = re.search(r"\b(USD|CAD|EUR|GBP|AUD)\s*-", text) or re.search(r"\b(USD|CAD|EUR|GBP|AUD)\b", text)
    currency = currency_match.group(1) if currency_match else "unknown"
    return prices, currency


# ----------------------------------------------------------------------
# Currency
# ----------------------------------------------------------------------
def get_usd_to_cad(state):
    """Canadian dollars per 1 US dollar from the Bank of Canada. Falls back to the last rate it saved."""
    try:
        request = urllib.request.Request(EXCHANGE_RATE_URL, headers={"User-Agent": "price-watch-script"})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
        latest = data["observations"][-1]
        rate = float(latest["FXUSDCAD"]["v"])
        state["usd_to_cad"] = rate
        state["usd_to_cad_date"] = latest["d"]
        return rate, latest["d"]
    except Exception as error:
        if state.get("usd_to_cad"):
            print(f"  Could not get today's exchange rate ({error}), using the saved one.")
            return state["usd_to_cad"], state.get("usd_to_cad_date", "unknown date")
        raise RuntimeError(f"could not get the US to Canadian dollar exchange rate: {error}")


def to_cad(prices, currency, state):
    """Returns prices in Canadian dollars, plus a short note on where the number came from."""
    if currency == "CAD":
        return prices, "shown in Canadian dollars by the site"
    if currency == "USD":
        rate, date = get_usd_to_cad(state)
        converted = [round(p * rate, 2) for p in prices]
        return converted, f"converted from US dollars at {rate:.4f} (Bank of Canada, {date})"
    raise ValueError(f"the site is showing prices in {currency}, which this script doesn't convert")


# ----------------------------------------------------------------------
# Email
# ----------------------------------------------------------------------
def send_email(subject, body):
    if not APP_PASSWORD or SENDER_EMAIL.startswith("your.address"):
        print("  Email settings are not filled in, so no email was sent.")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECIPIENT_EMAIL
    msg.set_content(body)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.login(SENDER_EMAIL, APP_PASSWORD)
        server.send_message(msg)
    print(f"  Email sent to {RECIPIENT_EMAIL}")
    return True


# ----------------------------------------------------------------------
# Remembering what we already told you
# ----------------------------------------------------------------------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"alerted_price": None, "failures": 0, "failure_warned": False}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def log_check(lowest_cad, site_currency, offer_count, note=""):
    new_file = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["time", "lowest_price_cad", "site_currency", "offers_found", "note"])
        writer.writerow([datetime.now().isoformat(timespec="seconds"), lowest_cad, site_currency, offer_count, note])


# ----------------------------------------------------------------------
# One check
# ----------------------------------------------------------------------
def run_check():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    state = load_state()

    try:
        text = fetch_page_text()
        prices, site_currency = parse_prices(text)
        if not prices:
            raise ValueError("no prices found on the page (the site layout may have changed)")
        prices_cad, price_note = to_cad(prices, site_currency, state)
    except Exception as error:
        state["failures"] = state.get("failures", 0) + 1
        print(f"[{now}] Check failed ({state['failures']} in a row): {error}")
        log_check("", "", 0, f"failed: {error}")
        if state["failures"] >= FAILURES_BEFORE_WARNING and not state.get("failure_warned"):
            send_email(
                f"Price watcher needs a look: {ITEM_NAME}",
                f"The last {state['failures']} checks failed.\n\nLatest error: {error}\n\nPage: {URL}",
            )
            state["failure_warned"] = True
        save_state(state)
        return

    state["failures"] = 0
    state["failure_warned"] = False
    lowest = prices_cad[0]
    print(f"[{now}] Lowest price: ${lowest:.2f} CAD ({price_note}, {len(prices_cad)} offers found)")
    log_check(lowest, site_currency, len(prices_cad), price_note)

    if lowest < target_price_cad:
        last_alert = state.get("alerted_price")
        if last_alert is None or lowest < last_alert - 0.005:
            cheapest_few = ", ".join(f"${p:.2f}" for p in prices_cad[:5])
            send_email(
                f"Price alert: {ITEM_NAME} at ${lowest:.2f} CAD",
                (
                    f"The cheapest offer is ${lowest:.2f} CAD, under your ${target_price_cad:.2f} CAD target.\n"
                    f"Price {price_note}.\n\n"
                    f"Cheapest offers right now (CAD): {cheapest_few}\n\n"
                    f"Note: the site adds a payment fee at checkout, so your final total will be a bit higher.\n\n"
                    f"{URL}"
                ),
            )
            state["alerted_price"] = lowest
        else:
            print(f"  Still under target, but already alerted at ${last_alert:.2f} CAD. No new email.")
    else:
        state["alerted_price"] = None  # back above target, so the next dip will alert again

    save_state(state)


# ----------------------------------------------------------------------
# Schedule
# ----------------------------------------------------------------------
def in_window(moment):
    """
    True between START_HOUR (9:00am) and END_HOUR (3:00am).
    The 3:00am check gets a few extra minutes, because GitHub's scheduled runs often start late.
    """
    hour, minute = moment.hour, moment.minute
    last_check = hour == END_HOUR and minute <= END_GRACE_MINUTES
    if START_HOUR > END_HOUR:  # window runs past midnight
        return hour >= START_HOUR or hour < END_HOUR or last_check
    return START_HOUR <= hour < END_HOUR or last_check


def next_slot(moment):
    """The next :00 or :30 mark."""
    base = moment.replace(second=0, microsecond=0)
    minutes_past = base.minute % CHECK_EVERY_MINUTES
    return base + timedelta(minutes=CHECK_EVERY_MINUTES - minutes_past)


def main():
    global target_price_cad

    if "--test-email" in sys.argv:
        send_email("Price watcher test", "If you got this, the email settings work.")
        return

    if "--check-window" in sys.argv:
        # Used by GitHub: exit code 0 means "inside checking hours", 1 means "outside".
        now = datetime.now()
        inside = in_window(now)
        print(f"Local time {now:%H:%M}. {'Inside' if inside else 'Outside'} checking hours.")
        sys.exit(0 if inside else 1)

    target_price_cad = get_target_price()

    if "--once" in sys.argv:
        run_check()
        return

    print(f"\nWatching {ITEM_NAME} for prices under ${target_price_cad:.2f} CAD")
    print(f"Checks every {CHECK_EVERY_MINUTES} minutes from {START_HOUR}:00 to {END_HOUR}:00. Press Ctrl+C to stop.\n")

    if in_window(datetime.now()):
        run_check()

    while True:
        wake_at = next_slot(datetime.now())
        time.sleep(max(5, (wake_at - datetime.now()).total_seconds()))
        if in_window(datetime.now()):
            run_check()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
