"""KSM POS Telegram store bot (Python).

Same shop, same Google Sheet as the Apps Script V7 bot, but running as a
long-lived Python process: no per-tap cold start, in-memory caches, and
thread-pooled Sheets access. Deploy on Railway with:
    BOT_TOKEN=<token from @BotFather>
    APPS_SCRIPT_URL=<your Apps Script Web App /exec URL>
    TG_BRIDGE_SECRET=<same secret as the TG_BRIDGE_SECRET Script Property>
    ADMIN_IDS=123456789,987654321        (optional, falls back to Setting sheet)

The bot talks to the sheet through TelegramSheetBridge.gs (a small file you
add to the Apps Script project) — no Google Cloud console or service
account needed.

IMPORTANT: this bot uses long polling. On startup it deletes any existing
webhook for the token, so the old Apps Script bot stops receiving updates
and only this Python bot answers.
"""

import asyncio
import json
import logging
import os
import sys
import time

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ksm-pos-bot")

from store import (
    CATEGORY_PAGE_SIZE,
    PRODUCT_PAGE_SIZE,
    Store,
    category_key,
    money,
    to_number,
    trim,
)

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not TOKEN:
    sys.exit("BOT_TOKEN environment variable is not set.")

APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "").strip()
if not APPS_SCRIPT_URL:
    sys.exit("APPS_SCRIPT_URL environment variable is not set "
             "(your Apps Script Web App /exec URL).")

BRIDGE_SECRET = os.environ.get("TG_BRIDGE_SECRET", "").strip()
if not BRIDGE_SECRET:
    sys.exit("TG_BRIDGE_SECRET environment variable is not set "
             "(must match the TG_BRIDGE_SECRET Script Property).")

ADMIN_IDS = [x.strip() for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]

store = Store(APPS_SCRIPT_URL, BRIDGE_SECRET)
bot = Bot(token=TOKEN)
dp = Dispatcher()


@dp.errors()
async def on_handler_error(event: ErrorEvent):
    """Catch-all: never leave the user staring at silence.

    Replies with the actual error (truncated) so a screenshot of the
    bot's reply is enough to diagnose bridge/sheet problems.
    """
    exc = event.exception
    logger.exception("Unhandled handler error: %r", exc)
    detail = str(exc)[:180] if exc else "unknown error"
    try:
        update = event.update
        message = getattr(update, "message", None)
        if message is not None:
            await message.answer(
                "Sorry, something went wrong. Please try again.\n\n"
                f"Error: {detail}"
            )
            return
        callback = getattr(update, "callback_query", None)
        if callback is not None:
            try:
                await callback.answer(f"Error: {detail}"[:190], show_alert=True)
            except Exception:
                pass
    except Exception:
        pass

# update_id -> expires_at (drop Telegram retries of the same update)
_seen_updates = {}


@dp.update.outer_middleware()
async def dedupe_middleware(handler, event: Update, data):
    uid = event.update_id
    now = time.time()
    # prune occasionally
    if len(_seen_updates) > 5000:
        for k in [k for k, exp in _seen_updates.items() if exp < now]:
            _seen_updates.pop(k, None)
    if uid in _seen_updates and _seen_updates[uid] > now:
        return None
    _seen_updates[uid] = now + 900
    return await handler(event, data)


def _private_only(obj) -> bool:
    chat = getattr(obj, "chat", None)
    return bool(chat) and chat.type == "private"


# --------------------------------------------------------------------------
# keyboards & small send helpers
# --------------------------------------------------------------------------

def quick_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛍 Products"), KeyboardButton(text="💬 Support")],
            [KeyboardButton(text="💳 Top Up")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆕 Products", callback_data="products")],
        [InlineKeyboardButton(text="🎉 Offers", callback_data="offers")],
        [InlineKeyboardButton(text="🛒 Cart", callback_data="cart")],
        [InlineKeyboardButton(text="👤 Profile", callback_data="profile"),
         InlineKeyboardButton(text="📦 My Orders", callback_data="orders")],
        [InlineKeyboardButton(text="💵 Top Up", callback_data="topup"),
         InlineKeyboardButton(text="👨‍💻 Support", callback_data="support")],
        [InlineKeyboardButton(text="🔄 Refresh Products", callback_data="refresh")],
    ])


async def send_or_edit(chat_id, message_id, text, reply_markup=None):
    text = trim(text, 4090)
    if message_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                        text=text, reply_markup=reply_markup,
                                        disable_web_page_preview=True)
            return
        except Exception as e:
            if "message is not modified" in str(e).lower():
                return
    await bot.send_message(chat_id, text, reply_markup=reply_markup,
                           disable_web_page_preview=True)


def nav_rows(extra_top=None):
    rows = extra_top or []
    rows.append([InlineKeyboardButton(text="🏠 Home", callback_data="home")])
    return rows


# --------------------------------------------------------------------------
# screens
# --------------------------------------------------------------------------

async def show_home(chat_id, user, message_id=None, send_quick=False):
    customer = await store.get_user(user.id)
    settings = await store.settings()
    shop = settings.get("Telegram Shop Name", "KSM Mobile") or "KSM Mobile"
    welcome = settings.get("Telegram Welcome Text", "Choose an option below.") or "Choose an option below."
    cur = store.currency(settings)
    name = user.first_name or user.username or "Customer"
    text = (
        f"🏦 Welcome to {shop} 🏦\n"
        f"💵 Your Balance: {money(customer['balance'], cur)}\n\n"
        f"Main Menu\n👤 {name}\n{welcome} 👇"
    )
    await send_or_edit(chat_id, message_id, text, home_keyboard())
    if send_quick:
        await bot.send_message(chat_id, "Quick menu is ready below.",
                               reply_markup=quick_keyboard())


async def show_categories(chat_id, message_id=None, page=0, force=False):
    catalog = await store.get_catalog(force=force)
    cats = catalog["categories"]
    total_pages = max(1, (len(cats) + CATEGORY_PAGE_SIZE - 1) // CATEGORY_PAGE_SIZE)
    page = max(0, min(int(page or 0), total_pages - 1))
    sl = cats[page * CATEGORY_PAGE_SIZE:(page + 1) * CATEGORY_PAGE_SIZE]
    total_stock = sum(max(0, int(to_number(p["stock"]))) for p in catalog["products"])
    text = (f"🛍 Available Products\n\nCategories: {len(cats)}\n"
            f"Products: {len(catalog['products'])}\nTotal stock: {total_stock}\n\n"
            "Select a category below:")
    rows = [[InlineKeyboardButton(
        text=f"{c.get('emoji') or '🛍'} {c['name']} • {c['products']} item{'s' if c['products'] != 1 else ''} ({c['stock']}) »",
        callback_data=f"cat:{c['key']}:0")] for c in sl]
    rows.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="refresh"),
                 InlineKeyboardButton(text="Sort: All", callback_data="noop")])
    if total_pages > 1:
        rows.append([
            InlineKeyboardButton(text="⬅ Prev", callback_data=f"cats:{max(0, page - 1)}"),
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"),
            InlineKeyboardButton(text="Next ➡", callback_data=f"cats:{min(total_pages - 1, page + 1)}")])
    rows.append([InlineKeyboardButton(text="🛒 Cart", callback_data="cart")])
    rows.append([InlineKeyboardButton(text="🏠 Home", callback_data="home")])
    await send_or_edit(chat_id, message_id, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def show_category_products(chat_id, category_key_, message_id=None, page=0):
    catalog = await store.get_catalog()
    category = next((c for c in catalog["categories"] if c["key"] == str(category_key_)), None)
    if not category:
        raise ValueError("Category changed. Please refresh products.")
    products = [p for p in catalog["products"]
                if p["categoryKey"] == category["key"] and to_number(p["stock"]) > 0]
    total_pages = max(1, (len(products) + PRODUCT_PAGE_SIZE - 1) // PRODUCT_PAGE_SIZE)
    page = max(0, min(int(page or 0), total_pages - 1))
    sl = products[page * PRODUCT_PAGE_SIZE:(page + 1) * PRODUCT_PAGE_SIZE]
    settings = await store.settings()
    cur = store.currency(settings)
    text = (f"🛍 {category['name']}\n\nAvailable products: {len(products)}\n"
            "Please select a product below:")
    rows = [[InlineKeyboardButton(
        text=f"{p.get('emoji') or '📦'} {p['name']} | {money(p['sellPrice'], cur)} | {int(to_number(p['stock']))}",
        callback_data=f"prd:{p['id']}")] for p in sl]
    if total_pages > 1:
        rows.append([
            InlineKeyboardButton(text="⬅ Prev", callback_data=f"cat:{category['key']}:{max(0, page - 1)}"),
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"),
            InlineKeyboardButton(text="Next ➡", callback_data=f"cat:{category['key']}:{min(total_pages - 1, page + 1)}")])
    rows.append([InlineKeyboardButton(text="↩ Back to Store", callback_data="products")])
    rows.append([InlineKeyboardButton(text="🏠 Home", callback_data="home")])
    await send_or_edit(chat_id, message_id, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def show_product(chat_id, product_id, message_id=None):
    p = await store.get_product(product_id)
    if not p:
        raise ValueError("Product not found. Refresh the store.")
    settings = await store.settings()
    cur = store.currency(settings)
    old = to_number(p["oldPrice"])
    price_line = (f"💵 Price: {money(old, cur)} → {money(p['sellPrice'], cur)}"
                  if old > p["sellPrice"] else f"💵 Price: {money(p['sellPrice'], cur)}")
    desc = trim(p.get("description") or "", 2200)
    text = (f"{p.get('emoji') or '📦'} {p['name']}\n\n"
            + ("🔥 Special Offer\n" if p.get("isOffer") else "")
            + f"{price_line}\n📦 Stock: {int(to_number(p['stock']))}\n"
            f"🏷 Category: {p['category']}\n\n"
            + (f"📝 Description\n{desc}\n\n" if desc else "")
            + "Delivery/order is confirmed after payment.")
    rows = [
        [InlineKeyboardButton(text="💳 Buy Now", callback_data=f"qty:{p['id']}")],
        [InlineKeyboardButton(text="🛒 Add to Cart", callback_data=f"add:{p['id']}:1")],
        [InlineKeyboardButton(text="↩ Back to Store", callback_data=f"cat:{p['categoryKey']}:0")],
        [InlineKeyboardButton(text="🏠 Home", callback_data="home")],
    ]
    await send_or_edit(chat_id, message_id, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def show_quantity(chat_id, product_id, message_id=None):
    p = await store.get_product(product_id)
    if not p:
        raise ValueError("Product not found.")
    if to_number(p["stock"]) <= 0:
        raise ValueError("This product is out of stock.")
    settings = await store.settings()
    cur = store.currency(settings)
    desc = trim(p.get("description") or "", 1800)
    text = (f"🛒 Select Quantity\n\n{p.get('emoji') or '📦'} {p['name']}\n"
            f"💵 {money(p['sellPrice'], cur)} / unit\n"
            f"📦 Stock: {int(to_number(p['stock']))}\n\n"
            + (f"📝 Description\n{desc}\n\n" if desc else "")
            + "How many do you want?")
    rows = []
    qs = [1, 2, 3, 5, 10, 15, 20, 25]
    for i in range(0, len(qs), 4):
        rows.append([InlineKeyboardButton(text=f"📦 {n}", callback_data=f"q:{p['id']}:{n}")
                     for n in qs[i:i + 4]])
    rows.append([InlineKeyboardButton(text="🔖 Custom Amount", callback_data=f"qcustom:{p['id']}")])
    rows.append([InlineKeyboardButton(text="⬅ Back", callback_data=f"prd:{p['id']}"),
                 InlineKeyboardButton(text="🏠 Home", callback_data="home")])
    await send_or_edit(chat_id, message_id, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def show_order_summary(chat_id, user, product_id, qty, message_id=None):
    p = await store.get_product(product_id)
    if not p:
        raise ValueError("Product not found.")
    qty = int(to_number(qty))
    if qty <= 0:
        raise ValueError("Invalid quantity.")
    if qty > to_number(p["stock"]):
        raise ValueError(f"Only {int(to_number(p['stock']))} available.")
    settings = await store.settings()
    cur = store.currency(settings)
    total = p["sellPrice"] * qty
    customer = await store.get_user(user.id)
    balance = to_number(customer["balance"])
    text = (f"🛒 Order Summary\n\n📦 {p['name']}\n📦 Quantity: {qty}\n"
            f"💵 Unit price: {money(p['sellPrice'], cur)}\n💰 Total: {money(total, cur)}\n\n"
            f"💳 Wallet: {money(balance, cur)}\n\n"
            + ("Ready to pay from your wallet." if balance >= total
               else "Not enough balance. Please top up first."))
    rows = []
    if balance >= total:
        rows.append([InlineKeyboardButton(text="✅ Confirm & Pay", callback_data=f"buy:{p['id']}:{qty}")])
    else:
        rows.append([InlineKeyboardButton(text="💵 Top Up", callback_data="topup")])
    rows.append([InlineKeyboardButton(text="🛒 Add to Cart", callback_data=f"add:{p['id']}:{qty}")])
    rows.append([InlineKeyboardButton(text="⬅ Change Qty", callback_data=f"qty:{p['id']}"),
                 InlineKeyboardButton(text="🏠 Home", callback_data="home")])
    await send_or_edit(chat_id, message_id, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def show_offers(chat_id, message_id=None, page=0):
    catalog = await store.get_catalog()
    offers = [p for p in catalog["products"] if p.get("isOffer") and to_number(p["stock"]) > 0]
    settings = await store.settings()
    cur = store.currency(settings)
    total_pages = max(1, (len(offers) + PRODUCT_PAGE_SIZE - 1) // PRODUCT_PAGE_SIZE)
    page = max(0, min(int(page or 0), total_pages - 1))
    sl = offers[page * PRODUCT_PAGE_SIZE:(page + 1) * PRODUCT_PAGE_SIZE]
    text = ("🎉 Offers\n\nLimited-time products and discounted items:" if offers else
            "🎉 Offers\n\nNo special offers are marked right now.")
    rows = [[InlineKeyboardButton(text=f"🔥 {p['name']} — {money(p['sellPrice'], cur)}",
                                 callback_data=f"prd:{p['id']}")] for p in sl]
    if total_pages > 1:
        rows.append([
            InlineKeyboardButton(text="⬅ Prev", callback_data=f"offers:{max(0, page - 1)}"),
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"),
            InlineKeyboardButton(text="Next ➡", callback_data=f"offers:{min(total_pages - 1, page + 1)}")])
    rows.append([InlineKeyboardButton(text="🛍 Products", callback_data="products")])
    rows.append([InlineKeyboardButton(text="🏠 Home", callback_data="home")])
    await send_or_edit(chat_id, message_id, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def show_profile(chat_id, user, message_id=None):
    customer = await store.get_user(user.id)
    settings = await store.settings()
    cur = store.currency(settings)
    text = (f"👤 Profile\n\nName: {user.first_name or customer.get('firstName') or '-'}\n"
            f"Username: {'@' + user.username if user.username else '-'}\n"
            f"Telegram ID: {user.id}\n💵 Balance: {money(customer['balance'], cur)}")
    rows = [
        [InlineKeyboardButton(text="💵 Top Up", callback_data="topup")],
        [InlineKeyboardButton(text="📦 My Orders", callback_data="orders")],
        [InlineKeyboardButton(text="🏠 Home", callback_data="home")],
    ]
    await send_or_edit(chat_id, message_id, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def show_orders(chat_id, user, message_id=None):
    orders = await store.orders_for_user(user.id, 8)
    settings = await store.settings()
    cur = store.currency(settings)
    text = "📦 My Orders\n"
    if not orders:
        text += "\nNo orders yet."
    else:
        for o in orders:
            items = o.get("items") or []
            text += (f"\n\n{o.get('invoiceNo') or o.get('id')} • {o.get('status') or 'PAID'}\n"
                     + ", ".join(f"{i.get('qty')}× {i.get('name')}" for i in items)
                     + f"\nTotal: {money(o.get('total') or 0, cur)}\n{o.get('createdAt') or ''}")
    rows = [
        [InlineKeyboardButton(text="🛍 Products", callback_data="products")],
        [InlineKeyboardButton(text="🏠 Home", callback_data="home")],
    ]
    await send_or_edit(chat_id, message_id, trim(text, 3900), InlineKeyboardMarkup(inline_keyboard=rows))


async def show_cart(chat_id, user, message_id=None):
    cart = await store.get_cart(user.id)
    catalog = await store.get_catalog()
    pmap = {p["id"]: p for p in catalog["products"]}
    settings = await store.settings()
    cur = store.currency(settings)
    items, total = [], 0
    for pid, qty in cart.items():
        p = pmap.get(str(pid))
        q = int(to_number(qty))
        if not p or q <= 0:
            continue
        safe = min(q, max(0, int(to_number(p["stock"]))))
        if safe <= 0:
            continue
        sub = p["sellPrice"] * safe
        total += sub
        items.append((p, safe, sub))
    text = "🛒 Cart\n"
    if not items:
        text += "\nYour cart is empty."
    else:
        for i, (p, q, sub) in enumerate(items):
            text += f"\n{i + 1}. {p['name']} × {q} = {money(sub, cur)}"
        text += f"\n\n💰 Total: {money(total, cur)}"
    rows = []
    if items:
        rows.append([InlineKeyboardButton(text="✅ Checkout & Pay", callback_data="cart_pay")])
        rows.append([InlineKeyboardButton(text="🗑 Clear Cart", callback_data="cart_clear")])
    rows.append([InlineKeyboardButton(text="🛍 Continue Shopping", callback_data="products")])
    rows.append([InlineKeyboardButton(text="🏠 Home", callback_data="home")])
    await send_or_edit(chat_id, message_id, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def do_checkout_single(chat_id, user, product_id, qty, message_id=None):
    settings = await store.settings()
    cur = store.currency(settings)
    result = await store.checkout(user, [{"id": product_id, "qty": qty}])
    order = result["order"]
    text = ("✅ Payment Successful\n\n"
            f"Invoice: {order.get('invoiceNo') or order['id']}\n"
            + "\n".join(f"📦 {i['name']} × {i['qty']}" for i in order["items"])
            + f"\n\n💰 Total: {money(order['total'], cur)}\n"
            f"💵 Wallet: {money(result['walletBalance'], cur)}\n\nThank you for your order.")
    rows = [
        [InlineKeyboardButton(text="🛍 Buy More", callback_data="products")],
        [InlineKeyboardButton(text="📦 My Orders", callback_data="orders")],
        [InlineKeyboardButton(text="🏠 Home", callback_data="home")],
    ]
    await send_or_edit(chat_id, message_id, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def do_checkout_cart(chat_id, user, message_id=None):
    cart = await store.get_cart(user.id)
    items = [{"id": pid, "qty": q} for pid, q in cart.items() if to_number(q) > 0]
    if not items:
        raise ValueError("Cart is empty.")
    settings = await store.settings()
    cur = store.currency(settings)
    result = await store.checkout(user, items)
    await store.save_cart(user.id, {})
    order = result["order"]
    text = ("✅ Cart Paid Successfully\n\n"
            f"Invoice: {order.get('invoiceNo') or order['id']}\n"
            + "\n".join(f"📦 {i['name']} × {i['qty']}" for i in order["items"])
            + f"\n\n💰 Total: {money(order['total'], cur)}\n"
            f"💵 Wallet: {money(result['walletBalance'], cur)}")
    rows = [
        [InlineKeyboardButton(text="🛍 Products", callback_data="products")],
        [InlineKeyboardButton(text="📦 My Orders", callback_data="orders")],
        [InlineKeyboardButton(text="🏠 Home", callback_data="home")],
    ]
    await send_or_edit(chat_id, message_id, trim(text, 3800), InlineKeyboardMarkup(inline_keyboard=rows))


async def show_topup_amounts(chat_id, message_id=None):
    pay = await store.payment_settings()
    settings = await store.settings()
    cur = store.currency(settings)
    text = (f"💵 Top Up Wallet\n\nPayment method: KBZPay\n"
            f"Minimum: {money(pay['min_topup'], cur)}\n\nChoose an amount:")
    rows = [
        [InlineKeyboardButton(text="5,000", callback_data="topamt:5000"),
         InlineKeyboardButton(text="10,000", callback_data="topamt:10000"),
         InlineKeyboardButton(text="20,000", callback_data="topamt:20000")],
        [InlineKeyboardButton(text="50,000", callback_data="topamt:50000"),
         InlineKeyboardButton(text="100,000", callback_data="topamt:100000")],
        [InlineKeyboardButton(text="🔖 Custom Amount", callback_data="topcustom")],
        [InlineKeyboardButton(text="🏠 Home", callback_data="home")],
    ]
    await send_or_edit(chat_id, message_id, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def show_topup_instructions(chat_id, amount, message_id=None):
    pay = await store.payment_settings()
    settings = await store.settings()
    cur = store.currency(settings)
    text = (f"💳 KBZPay Top Up\n\nAmount: {money(amount, cur)}\n"
            f"Account Name: {pay['kbzpay_account_name'] or 'Not configured'}\n"
            f"KBZPay Phone: {pay['kbzpay_phone'] or 'Not configured'}\n\n"
            f"1. Pay exactly {money(amount, cur)}.\n2. Return here.\n"
            "3. Send the KBZPay payment slip as a PHOTO.\n\n"
            "Your wallet is credited only after payment verification.")
    rows = []
    if pay["kbzpay_qr_url"]:
        rows.append([InlineKeyboardButton(text="📷 Open KBZPay QR", url=pay["kbzpay_qr_url"])])
    rows.append([InlineKeyboardButton(text="⬅ Change Amount", callback_data="topup")])
    rows.append([InlineKeyboardButton(text="🏠 Home", callback_data="home")])
    await send_or_edit(chat_id, message_id, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def show_support(chat_id, message_id=None):
    settings = await store.settings()
    url = settings.get("Telegram Support URL", "") or ""
    rows = []
    if url:
        rows.append([InlineKeyboardButton(text="💬 Open Support", url=url)])
    rows.append([InlineKeyboardButton(text="🏠 Home", callback_data="home")])
    text = ("👨‍💻 Support\n\nTap the button below to contact support." if url
            else "👨‍💻 Support\n\nThe support link is not configured yet.")
    await send_or_edit(chat_id, message_id, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def admin_ids():
    if ADMIN_IDS:
        return ADMIN_IDS
    settings = await store.settings()
    raw = (settings.get("TELEGRAM_ADMIN_IDS", "") or
           settings.get("Telegram Admin IDs", "") or "")
    return [x.strip() for x in str(raw).split(",") if x.strip()]


async def notify_admins_topup(topup):
    settings = await store.settings()
    cur = store.currency(settings)
    caption = (f"💰 TOP-UP REQUEST\nID: {topup['id']}\n"
               f"Customer: {topup.get('firstName') or topup.get('username') or topup['telegramId']}\n"
               f"Telegram ID: {topup['telegramId']}\n"
               f"Amount: {money(topup['amount'], cur)}\n\n"
               "Verify the transaction in KBZPay/merchant history before approving.")
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"topup_ok:{topup['id']}"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"topup_no:{topup['id']}"),
    ]])
    for admin_id in await admin_ids():
        try:
            await bot.send_photo(admin_id, photo=topup["telegramFileId"],
                                 caption=caption[:1000], reply_markup=markup)
        except Exception:
            pass


# --------------------------------------------------------------------------
# message handlers
# --------------------------------------------------------------------------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not _private_only(message):
        return
    await store.touch_user(message.from_user)
    await show_home(message.chat.id, message.from_user, send_quick=True)


@dp.message(Command("products"))
async def cmd_products(message: Message):
    if not _private_only(message):
        return
    await store.touch_user(message.from_user)
    await show_categories(message.chat.id, page=0)


@dp.message(Command("cart"))
async def cmd_cart(message: Message):
    if not _private_only(message):
        return
    await store.touch_user(message.from_user)
    await show_cart(message.chat.id, message.from_user)


@dp.message(Command("wallet"))
async def cmd_wallet(message: Message):
    if not _private_only(message):
        return
    await store.touch_user(message.from_user)
    await show_profile(message.chat.id, message.from_user)


@dp.message(Command("orders"))
async def cmd_orders(message: Message):
    if not _private_only(message):
        return
    await store.touch_user(message.from_user)
    await show_orders(message.chat.id, message.from_user)


@dp.message(Command("topup"))
async def cmd_topup(message: Message):
    if not _private_only(message):
        return
    await store.touch_user(message.from_user)
    await show_topup_amounts(message.chat.id)


@dp.message(Command("support"))
async def cmd_support(message: Message):
    if not _private_only(message):
        return
    await store.touch_user(message.from_user)
    await show_support(message.chat.id)


@dp.message(Command("id"))
async def cmd_id(message: Message):
    if not _private_only(message):
        return
    await message.answer(f"Your Telegram ID: {message.from_user.id}")


@dp.message(F.photo)
async def handle_photo(message: Message):
    if not _private_only(message):
        return
    user = message.from_user
    await store.touch_user(user)
    state = store.get_state(user.id)
    if not state or state.get("type") != "AWAIT_TOPUP_SLIP":
        await message.answer("Photo received. If this is a KBZPay slip, open Top Up first and choose the amount.")
        return
    pay = await store.payment_settings()
    amount = int(to_number(state.get("amount")))
    if amount < pay["min_topup"]:
        store.clear_state(user.id)
        await message.answer("Invalid top-up amount.")
        return
    file_id = message.photo[-1].file_id
    topup = await store.create_topup(user, amount, file_id)
    store.clear_state(user.id)
    settings = await store.settings()
    cur = store.currency(settings)
    await notify_admins_topup(topup)
    await message.answer(
        f"🧾 Top-up submitted\n\nRequest: {topup['id']}\n"
        f"Amount: {money(amount, cur)}\nStatus: Pending verification.\n\n"
        "You will receive a message after approval.",
        reply_markup=quick_keyboard())


@dp.message(F.text)
async def handle_text(message: Message):
    if not _private_only(message):
        return
    user = message.from_user
    await store.touch_user(user)
    text = (message.text or "").strip()

    if text in ("🛍 Products", "Products"):
        await show_categories(message.chat.id, page=0)
        return
    if text in ("💬 Support", "Support"):
        await show_support(message.chat.id)
        return
    if text in ("💳 Top Up", "Top Up"):
        await show_topup_amounts(message.chat.id)
        return

    state = store.get_state(user.id)
    if state and state.get("type") == "CUSTOM_QTY":
        qty = int(to_number(text))
        if qty <= 0:
            await message.answer("Please send a valid quantity, for example: 3")
            return
        store.clear_state(user.id)
        try:
            await show_order_summary(message.chat.id, user, state.get("productId"), qty)
        except Exception as e:
            await message.answer("⚠️ " + str(e))
        return
    if state and state.get("type") == "CUSTOM_TOPUP":
        pay = await store.payment_settings()
        settings = await store.settings()
        cur = store.currency(settings)
        amount = int(to_number(text))
        if amount < pay["min_topup"]:
            await message.answer(f"Minimum top up is {money(pay['min_topup'], cur)}. Please send another amount.")
            return
        store.set_state(user.id, {"type": "AWAIT_TOPUP_SLIP", "amount": amount})
        await show_topup_instructions(message.chat.id, amount)
        return

    await message.answer("Choose an option below 👇", reply_markup=quick_keyboard())


# --------------------------------------------------------------------------
# callback router
# --------------------------------------------------------------------------

@dp.callback_query()
async def handle_callback(cb: CallbackQuery):
    user = cb.from_user
    data = cb.data or ""
    chat_id = cb.message.chat.id if cb.message and cb.message.chat else user.id
    message_id = cb.message.message_id if cb.message else None
    defer_answer = data.startswith("topup_ok:") or data.startswith("topup_no:")
    if not defer_answer:
        try:
            await cb.answer()
        except Exception:
            pass
    try:
        if data == "noop":
            return
        if data == "home":
            await show_home(chat_id, user, message_id)
        elif data == "products":
            await show_categories(chat_id, message_id, 0)
        elif data == "offers":
            await show_offers(chat_id, message_id, 0)
        elif data.startswith("offers:"):
            await show_offers(chat_id, message_id, int(data.split(":")[1] or 0))
        elif data == "cart":
            await show_cart(chat_id, user, message_id)
        elif data == "profile":
            await show_profile(chat_id, user, message_id)
        elif data == "orders":
            await show_orders(chat_id, user, message_id)
        elif data == "topup":
            await show_topup_amounts(chat_id, message_id)
        elif data == "support":
            await show_support(chat_id, message_id)
        elif data == "refresh":
            await show_categories(chat_id, message_id, 0, force=True)
        elif data == "cart_clear":
            await store.save_cart(user.id, {})
            await show_cart(chat_id, user, message_id)
        elif data == "cart_pay":
            await do_checkout_cart(chat_id, user, message_id)
        elif data.startswith("cats:"):
            await show_categories(chat_id, message_id, int(data.split(":")[1] or 0))
        elif data.startswith("cat:"):
            p = data.split(":")
            await show_category_products(chat_id, p[1], message_id, int(p[2]) if len(p) > 2 else 0)
        elif data.startswith("prd:"):
            await show_product(chat_id, data[4:], message_id)
        elif data.startswith("qty:"):
            await show_quantity(chat_id, data[4:], message_id)
        elif data.startswith("q:"):
            p = data.split(":")
            await show_order_summary(chat_id, user, p[1], int(p[2]) if len(p) > 2 else 1, message_id)
        elif data.startswith("qcustom:"):
            product_id = data[len("qcustom:"):]
            store.set_state(user.id, {"type": "CUSTOM_QTY", "productId": product_id})
            await bot.send_message(chat_id, "🔢 Send the quantity you want for this product.")
        elif data.startswith("buy:"):
            p = data.split(":")
            await do_checkout_single(chat_id, user, p[1], int(p[2]) if len(p) > 2 else 1, message_id)
        elif data.startswith("add:"):
            p = data.split(":")
            product_id, qty = p[1], int(p[2]) if len(p) > 2 else 1
            prod = await store.get_product(product_id)
            if not prod:
                raise ValueError("Product not found.")
            if qty <= 0:
                raise ValueError("Invalid quantity.")
            cart = await store.get_cart(user.id)
            nxt = int(to_number(cart.get(product_id))) + qty
            if nxt > to_number(prod["stock"]):
                raise ValueError(f"Only {int(to_number(prod['stock']))} in stock.")
            cart[product_id] = nxt
            await store.save_cart(user.id, cart)
            await show_cart(chat_id, user, message_id)
        elif data.startswith("topamt:"):
            amount = int(to_number(data[len("topamt:"):]))
            store.set_state(user.id, {"type": "AWAIT_TOPUP_SLIP", "amount": amount})
            await show_topup_instructions(chat_id, amount, message_id)
        elif data == "topcustom":
            store.set_state(user.id, {"type": "CUSTOM_TOPUP"})
            await bot.send_message(chat_id, "💰 Send the top-up amount you want, for example: 20000")
        elif data.startswith("topup_ok:"):
            if str(user.id) not in await admin_ids():
                await cb.answer("Admin only", show_alert=True)
                return
            result = await store.approve_topup(data[len("topup_ok:"):], user.id)
            topup = result["topup"]
            settings = await store.settings()
            cur = store.currency(settings)
            try:
                await bot.send_message(
                    int(topup["telegramId"]),
                    f"✅ Top-up approved\n\n{money(topup['amount'], cur)} added to your wallet.\n"
                    f"New balance: {money(topup.get('newBalance', 0), cur)}",
                    reply_markup=quick_keyboard())
            except Exception:
                pass
            await cb.answer(result["message"])
        elif data.startswith("topup_no:"):
            if str(user.id) not in await admin_ids():
                await cb.answer("Admin only", show_alert=True)
                return
            result = await store.reject_topup(data[len("topup_no:"):], user.id)
            topup = result["topup"]
            settings = await store.settings()
            cur = store.currency(settings)
            try:
                await bot.send_message(
                    int(topup["telegramId"]),
                    f"❌ Top-up rejected\n\nRequest: {topup['id']}\n"
                    f"Amount: {money(topup['amount'], cur)}\n\n"
                    "Please check the payment and submit a new slip if needed.")
            except Exception:
                pass
            await cb.answer(result["message"])
        else:
            raise ValueError("Unknown button action.")
    except Exception as e:
        msg = str(e) or "Something went wrong."
        try:
            asyncio.create_task(store.log("ERROR", "callback", {
                "data": data, "telegramId": user.id, "error": msg}))
        except Exception:
            pass
        try:
            await cb.answer(msg[:190], show_alert=True)
        except Exception:
            pass
        try:
            await bot.send_message(chat_id, "⚠️ " + msg)
        except Exception:
            pass


async def main():
    print("KSM POS bot is running (polling)...")
    # drop any pending updates accumulated while offline, like the Apps Script version did
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
