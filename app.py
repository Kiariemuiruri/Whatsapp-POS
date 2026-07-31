
import os
import json
import re
import requests
import gspread
from datetime import datetime, date, timedelta
import calendar
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
#GOOGLE_SERVICE_ACCOUNT_JSON = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))  # path to your json key file

WHATSAPP_API_URL = f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"

# ---------------------------------------------------------------------------
# GOOGLE SHEETS CONNECTION
# ---------------------------------------------------------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly", 'https://www.googleapis.com/auth/drive']

def get_sheet(sheet_name: str):
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    return spreadsheet.worksheet(sheet_name)


# ---------------------------------------------------------------------------
# INTENT LOGIC — one function per query type, matching existing sheet layout
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
# DATE RANGES
# ---------------------------------------------------------------------------
def parse_sheet_date(date_str: str):
    """Your sheet dates are stored as dd/MM/yyyy HH:mm:ss — parse just the date part."""
    try:
        return datetime.strptime(date_str.split(" ")[0], "%d/%m/%Y").date()
    except (ValueError, IndexError):
        return None


def get_date_range(period: str):
    today = date.today()

    if period == "today":
        return today, today
    elif period == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    elif period == "this_week":
        start = today - timedelta(days=today.weekday())  # Monday
        return start, today
    elif period == "last_week":
        this_week_start = today - timedelta(days=today.weekday())
        last_week_start = this_week_start - timedelta(days=7)
        last_week_end = this_week_start - timedelta(days=1)
        return last_week_start, last_week_end
    elif period == "this_month":
        start = today.replace(day=1)
        return start, today
    elif period == "last_month":
        first_of_this_month = today.replace(day=1)
        last_day_prev_month = first_of_this_month - timedelta(days=1)
        first_day_prev_month = last_day_prev_month.replace(day=1)
        return first_day_prev_month, last_day_prev_month
    else:
        return today, today  # fallback
    
# ---------------------------------------------------------------------
# GET PURCHASES FOR PERIOD
# ---------------------------------------------------------------------
def get_purchases_for_period(period: str) -> str:
    ws = get_sheet("Stock In")
    rows = ws.get_all_records()
    start_date, end_date = get_date_range(period)

    total = 0
    count = 0
    for row in rows:
        row_date = parse_sheet_date(str(row.get("Date", "")))
        if row_date and start_date <= row_date <= end_date:
            qty = float(row.get("Quantity", 0) or 0)
            cost = float(row.get("Buying Price", 0) or 0)
            total += qty * cost
            count += 1

    label = period.replace("_", " ").title()
    if count == 0:
        return f"No purchases recorded for {label}."
    return f"🛒 Purchases ({label}): KES {total:,.2f} across {count} entries."

PURCHASE_PERIOD_OPTIONS = [
    {"id": "purchases_today", "title": "Today"},
    {"id": "purchases_yesterday", "title": "Yesterday"},
    {"id": "purchases_this_week", "title": "This Week"},
    {"id": "purchases_last_week", "title": "Last Week"},
    {"id": "purchases_this_month", "title": "This Month"},
    {"id": "purchases_last_month", "title": "Last Month"},
]

def send_purchases_period_menu(to: str):
    send_whatsapp_list(
        to=to,
        header="Purchases Report",
        body="Which period would you like to see?",
        button_text="Choose Period",
        options=PURCHASE_PERIOD_OPTIONS,
    )

# ---------------------------------------------------------------------------
# GET SALES FOR PERIOD
# ---------------------------------------------------------------------------
def get_sales_for_period(period: str) -> str:
    ws = get_sheet("Stock Out")
    rows = ws.get_all_records()
    start_date, end_date = get_date_range(period)

    total = 0
    count = 0
    for row in rows:
        row_date = parse_sheet_date(str(row.get("Date", "")))
        if row_date and start_date <= row_date <= end_date:
            total += float(row.get("Total", 0) or 0)
            count += 1

    label = period.replace("_", " ").title()
    if count == 0:
        return f"No sales recorded for {label}."
    return f"📊 Sales ({label}): KES {total:,.2f} across {count} transaction(s)."

PERIOD_OPTIONS = [
    {"id": "sales_today", "title": "Today"},
    {"id": "sales_yesterday", "title": "Yesterday"},
    {"id": "sales_this_week", "title": "This Week"},
    {"id": "sales_last_week", "title": "Last Week"},
    {"id": "sales_this_month", "title": "This Month"},
    {"id": "sales_last_month", "title": "Last Month"},
]

def send_sales_period_menu(to: str):
    send_whatsapp_list(
        to=to,
        header="Sales Report",
        body="Which period would you like to see?",
        button_text="Choose Period",
        options=PERIOD_OPTIONS,
    )
# ---------------------------------------------------------------------------
# INTENT MATCHING — simple keyword routing (upgrade to Claude API later if needed)
# ---------------------------------------------------------------------------

INTENT_PATTERNS = [
    (r"sales.*today|made.*today|how much.*today", get_sales_today),
    (r"low stock|running low|restock|out of stock", get_low_stock),
    (r"top product|best sell|top sell", get_top_products),
    (r"stock value|worth|total stock", get_stock_value),
]

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

# ------------------------------------------------------------------------------------
# TEMPLATES
# ------------------------------------------------------------------------------------
def send_whatsapp_list(to: str, header: str, body: str, button_text: str, options: list[dict]):
    """
    options = [{"id": "sales_today", "title": "Sales Today", "description": "Today's total sales"}, ...]
    Each option needs: id, title (max 24 chars), description (optional, max 72 chars)
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": header},
            "body": {"text": body},
            "action": {
                "button": button_text,
                "sections": [
                    {
                        "title": "Menu",
                        "rows": options
                    }
                ]
            }
        }
    }
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    resp = requests.post(WHATSAPP_API_URL, json=payload, headers=headers)
    print(f"WhatsApp API response: {resp.status_code} - {resp.text}")
    resp.raise_for_status()
    return resp.json()

MENU_OPTIONS = [
    {"id": "sales_menu", "title": "📊 Sales Report", "description": "Pick a time period"},
    {"id": "purchases_menu", "title": "🛒 Purchases Report", "description": "Pick a time period"},
    {"id": "low_stock", "title": "⚠️ Low Stock", "description": "Products running low"},
    {"id": "top_products", "title": "🏆 Top Products", "description": "Best sellers this period"},
    {"id": "stock_value", "title": "💰 Stock Value", "description": "Total current stock worth"},
]

INTENT_HANDLERS = {
    "sales_today": get_sales_today,
    "low_stock": get_low_stock,
    "top_products": get_top_products,
    "stock_value": get_stock_value,
}

def send_main_menu(to: str):
    send_whatsapp_list(
        to=to,
        header="Shop Assistant",
        body="What would you like to check?",
        button_text="View Options",
        options=MENU_OPTIONS,
    )
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
    body = await request.json()

    try:
        entry = body["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]

        if "messages" not in value:
            return Response(status_code=200)

        message = value["messages"][0]
        from_number = message["from"]
        msg_type = message.get("type")

        if msg_type == "interactive":
            selected_id = message["interactive"]["list_reply"]["id"]

            if selected_id == "sales_menu":
                send_sales_period_menu(from_number)


            elif selected_id == "purchases_menu":
                send_purchases_period_menu(from_number)

            elif selected_id.startswith("purchases_"):
                period = selected_id.replace("purchases_", "")
                reply = get_purchases_for_period(period)
                send_whatsapp_text(from_number, reply)
                send_whatsapp_text(from_number, "Type 'menu' anytime to see more options.")

            elif selected_id.startswith("sales_"):
                period = selected_id.replace("sales_", "")  # "today", "last_month", etc.
                reply = get_sales_for_period(period)
                send_whatsapp_text(from_number, reply)
                send_whatsapp_text(from_number, "Type 'menu' anytime to see more options.")

            else:
                handler = INTENT_HANDLERS.get(selected_id)
                if handler:
                    reply = handler()
                    send_whatsapp_text(from_number, reply)
                    send_whatsapp_text(from_number, "Type 'menu' anytime to see more options.")

            # send_main_menu(from_number)  # show menu again after replying

        elif msg_type == "text":
            text = message.get("text", {}).get("body", "").strip().lower()

            # Try matching a specific keyword intent first (backward compatible)
            matched = False
            for pattern, handler in INTENT_PATTERNS:
                if re.search(pattern, text):
                    reply = handler()
                    send_whatsapp_text(from_number, reply)
                    matched = True
                    break

            # Anything unrecognized (including a first-ever "Hi", a typo, or literally
            # anything else) falls back to showing the menu — so there's no dead end
            if not matched:
                send_whatsapp_text(from_number, "👋 Welcome to Zuri Urban's assistant!")
                send_main_menu(from_number)

    except (KeyError, IndexError):
        pass

    return Response(status_code=200)


# ---------------------------------------------------------------------------
# EXAMPLE: proactive daily summary using a template (call from a scheduler)
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