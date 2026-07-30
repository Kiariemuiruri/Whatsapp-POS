
import os
import json
import re
import requests
import gspread
from datetime import datetime, date
from fastapi import FastAPI, Request, Response
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

app = FastAPI()

load_dotenv()

# ---------------------------------------------------------------------------
# CONFIG — pull these from environment variables in production, never hardcode
# ---------------------------------------------------------------------------
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")              # permanent access token
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")  # you choose this string yourself
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")  # path to your json key file

WHATSAPP_API_URL = f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"

# ---------------------------------------------------------------------------
# GOOGLE SHEETS CONNECTION
# ---------------------------------------------------------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly", 'https://www.googleapis.com/auth/drive']

def get_sheet(sheet_name: str):
    creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    return spreadsheet.worksheet(sheet_name)


# ---------------------------------------------------------------------------
# INTENT LOGIC — one function per query type, matching your existing sheet layout
# ---------------------------------------------------------------------------

def get_sales_today() -> str:
    ws = get_sheet("Stock Out")
    rows = ws.get_all_records()  # each row as a dict using header names
    today_str = date.today().strftime("%d/%m/%Y")

    total = 0
    count = 0
    for row in rows:
        row_date = str(row.get("Date", ""))
        if row_date.startswith(today_str):
            total += float(row.get("Total", 0) or 0)
            count += 1

    if count == 0:
        return "No sales recorded yet today."
    return f"📊 Sales today: KES {total:,.2f} across {count} transaction(s)."


def get_low_stock(threshold: int = 5) -> str:
    ws = get_sheet("Current Stock")
    rows = ws.get_all_records()

    low_items = [
        f"- {row.get('Product Name')} ({row.get('Quantity')} left)"
        for row in rows
        if float(row.get("Quantity", 0) or 0) < threshold
    ]

    if not low_items:
        return f"✅ No products below {threshold} units. Stock looks healthy."
    return "⚠️ Low stock:\n" + "\n".join(low_items)


def get_top_products(limit: int = 5) -> str:
    ws = get_sheet("Stock Out")
    rows = ws.get_all_records()

    totals = {}
    for row in rows:
        name = row.get("Product Name")
        qty = float(row.get("Quantity", 0) or 0)
        totals[name] = totals.get(name, 0) + qty

    ranked = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:limit]
    if not ranked:
        return "No sales data yet."

    lines = [f"{i+1}. {name} — {qty:.0f} sold" for i, (name, qty) in enumerate(ranked)]
    return "🏆 Top products:\n" + "\n".join(lines)


def get_stock_value() -> str:
    ws = get_sheet("Current Stock")
    rows = ws.get_all_records()
    total = sum(float(row.get("Stock Value", 0) or 0) for row in rows)
    return f"💰 Total stock value: KES {total:,.2f}"


# ---------------------------------------------------------------------------
# INTENT MATCHING — simple keyword routing (upgrade to Claude API later if needed)
# ---------------------------------------------------------------------------

INTENT_PATTERNS = [
    (r"sales.*today|made.*today|how much.*today", get_sales_today),
    (r"low stock|running low|restock|out of stock", get_low_stock),
    (r"top product|best sell|top sell", get_top_products),
    (r"stock value|worth|total stock", get_stock_value),
]

HELP_TEXT = (
    "👋 Hi! You can ask me things like:\n"
    "- \"sales today\"\n"
    "- \"low stock\"\n"
    "- \"top products\"\n"
    "- \"stock value\"\n"
)

def route_message(text: str) -> str:
    text_lower = text.strip().lower()
    for pattern, handler in INTENT_PATTERNS:
        if re.search(pattern, text_lower):
            return handler()
    return HELP_TEXT


# ---------------------------------------------------------------------------
# SENDING MESSAGES BACK
# ---------------------------------------------------------------------------

def send_whatsapp_text(to: str, body: str):
    """Free-form text reply. Only works within 24h of the user's last message."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    print(f"Sending via URL: {WHATSAPP_API_URL}")  # add this line

    resp = requests.post(WHATSAPP_API_URL, json=payload, headers=headers)
    print(f"WhatsApp API response: {resp.status_code} - {resp.text}")

    resp.raise_for_status()
    return resp.json()


def send_whatsapp_template(to: str, template_name: str, language_code: str = "en", parameters: list[str] = None):
    """
    Send a pre-approved template message. Required for proactive messages
    (e.g. daily summary) sent outside the 24h customer-service window.
    Template must already be approved in Meta Business Manager, with
    placeholder slots ({{1}}, {{2}}, ...) matching `parameters`.
    """
    components = []
    if parameters:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": p} for p in parameters]
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": components,
        },
    }
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    resp = requests.post(WHATSAPP_API_URL, json=payload, headers=headers)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# WEBHOOK ENDPOINTS
# ---------------------------------------------------------------------------

@app.get("/whatsapp/webhook")
async def verify_webhook(request: Request):
    """Meta calls this once when you register the webhook URL, to confirm you own it."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    return Response(status_code=403)


@app.post("/whatsapp/webhook")
async def receive_message(request: Request):
    """Meta POSTs incoming messages here."""
    body = await request.json()
    print(f"Full webhook payload: {body}")  # temporary debug line

    try:
        entry = body["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]

        if "messages" not in value:
            # could be a status update (delivered/read), not a new message — ignore
            return Response(status_code=200)

        message = value["messages"][0]
        from_number = message["from"]
        text = message.get("text", {}).get("body", "")

        reply = route_message(text)
        send_whatsapp_text(from_number, reply)

        reply = route_message(text)
        print(f"Sending reply: {reply}")
        send_whatsapp_text(from_number, reply)

    except (KeyError, IndexError):
        # malformed or non-message payload — acknowledge anyway so Meta doesn't retry
        pass

    return Response(status_code=200)


# ---------------------------------------------------------------------------
# EXAMPLE: proactive daily summary using a template (call this from a scheduler)
# ---------------------------------------------------------------------------

def send_daily_summary(to: str):
    """
    Requires a template named e.g. 'daily_summary' pre-approved in Meta
    Business Manager, with body like:
    'Good morning! Yesterday's sales: KES {{1}}. Low stock items: {{2}}.'
    """
    sales_text = get_sales_today()  # swap for yesterday's totals in production
    low_stock_text = get_low_stock()
    send_whatsapp_template(
        to=to,
        template_name="daily_summary",
        parameters=[sales_text, low_stock_text],
    )