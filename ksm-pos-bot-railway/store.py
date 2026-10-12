"""Google Sheets data layer for the KSM POS Telegram store bot.

Same sheet layout as the Apps Script V7 bot, so the Python bot can take over
the same spreadsheet:
  Products: Item Code | formData (JSON)      (also supports direct-column
            and legacy ID/Data layouts as fallbacks)
  Setting:  Setting | Value
  TelegramUsers / TelegramCarts / TelegramOrders / WalletTransactions /
  Topups / TelegramBotLogs

All blocking Sheets I/O runs in a thread pool (asyncio.to_thread) so the bot
stays responsive. Hot data lives in small in-memory TTL caches instead of
re-reading the sheet on every tap.
"""

import asyncio
import hashlib
import json
import re
import time
import uuid

from bridge import Bridge, BridgeError, BridgeSheet

TAB = {
    "users": "TelegramUsers",
    "carts": "TelegramCarts",
    "orders": "TelegramOrders",
    "wallet": "WalletTransactions",
    "topups": "Topups",
    "logs": "TelegramBotLogs",
    "setting": "Setting",
}

CATALOG_TTL = 1800        # 30 minutes; checkout always validates fresh stock/price
PRODUCT_TTL = 900         # 15 minutes per product
SETTINGS_TTL = 60
USER_TOUCH_TTL = 900      # skip the user-sheet lookup for 15 min
STATE_TTL = 3600

CATEGORY_PAGE_SIZE = 10
PRODUCT_PAGE_SIZE = 8


# --------------------------------------------------------------------------
# small helpers (same behaviour as the Apps Script version)
# --------------------------------------------------------------------------

def _norm_header(value):
    return re.sub(r"[\s_\-]+", "", str(value or "").strip().lower())


def to_number(value):
    if isinstance(value, (int, float)):
        return value if value == value and abs(value) != float("inf") else 0
    try:
        n = float(str(value or "").replace(",", "").strip() or 0)
    except (Value, TypeError):
        return 0
    return n


def money(value, currency="MMK"):
    n = to_number(value)
    s = f"{n:,.2f}".rstrip("0").rstrip(".")
    return f"{s} {currency}"


def trim(text, maximum):
    text = str(text or "")
    return text if len(text) <= maximum else text[: max(0, maximum - 1)] + "…"


def category_key(name):
    return hashlib.md5(("category:" + str(name)).encode("utf-8")).hexdigest()[:10]


def _rc_to_a1(row, col):
    s = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        s = chr(65 + rem) + s
    return f"{s}{row}"


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

class Store:
    def __init__(self, base_url, secret):
        self._bridge = Bridge(base_url, secret)
        self._worksheets = {}
        self._ensured = set()
        self._lock = asyncio.Lock()

        # (expires_at, value) caches
        self._catalog = None
        self._products = {}
        self._settings = None
        self._user_touch = {}
        self._states = {}

    # -- connection ------------------------------------------------------
    async def _io(self, func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    def _ws(self, name):
        if name not in self._worksheets:
            self._worksheets[name] = BridgeSheet(self._bridge, name)
        return self._worksheets[name]

    def _ensure_ws(self, name, headers):
        """BridgeSheet for a tab, creating it (once per process) if missing."""
        ws = self._ws(name)
        if name not in self._ensured:
            self._bridge.call("ensureSheet", sheet=name, headers=headers or [])
            self._ensured.add(name)
        return ws

    # -- settings --------------------------------------------------------
    async def settings(self):
        now = time.time()
        if self._settings and self._settings[0] > now:
            return self._settings[1]

        def _do():
            try:
                ws = self._ws(TAB["setting"])
                rows = ws.get("A2:B")
            except Exception:
                return {}
            out = {}
            for r in rows:
                if r and r[0]:
                    out[str(r[0]).strip()] = r[1] if len(r) > 1 else ""
            return out

        data = await self._io(_do)
        self._settings = (now + SETTINGS_TTL, data)
        return data

    async def payment_settings(self):
        s = await self.settings()
        return {
            "kbzpay_account_name": s.get("KBZPay Account Name", ""),
            "kbzpay_phone": s.get("KBZPay Phone", ""),
            "kbzpay_qr_url": s.get("KBZPay QR URL", ""),
            "min_topup": to_number(s.get("Telegram Min Topup", 1000)) or 1000,
        }

    def currency(self, settings):
        return settings.get("Telegram Currency", "MMK") or "MMK"

    # -- catalog ---------------------------------------------------------
    async def get_catalog(self, force=False):
        now = time.time()
        if not force and self._catalog and self._catalog[0] > now:
            return self._catalog[1]
        sheet_name, products, cat_order = await self._io(self._read_catalog_bundle)
        catalog = self._build_catalog(products, sheet_name, cat_order)
        # prime the per-product cache with the visible products
        for p in catalog["products"]:
            self._products[p["id"]] = (now + PRODUCT_TTL, p)
        self._catalog = (now + CATALOG_TTL, catalog)
        return catalog

    def invalidate_catalog(self):
        self._catalog = None

    def invalidate_product(self, product_id):
        self._products.pop(str(product_id), None)

    def _read_catalog_bundle(self):
        sheet_name, products = self._read_product_rows()
        order = {}
        try:
            for i, r in enumerate(self._ws("Categories").get("A2:A")):
                if r and r[0] and str(r[0]).strip():
                    order[str(r[0]).strip()] = i
        except Exception:
            pass
        return sheet_name, products, order

    def _read_product_rows(self):
        """Returns (sheet_name, [product dicts]) mirroring tgsReadProductRowsFresh_."""
        preferred = "Products"
        try:
            for r in self._ws("Setting").get("A2:B"):
                if r and str(r[0]).strip() == "Telegram Product Sheet" and len(r) > 1 and r[1]:
                    preferred = str(r[1]).strip()
                    break
        except Exception:
            pass

        candidates = []
        for c in (preferred, "Products", "Items"):
            if c and c not in candidates:
                candidates.append(c)

        for name in candidates:
            try:
                values = self._ws(name).get_all_values()
            except Exception:
                continue
            if len(values) < 2:
                continue
            headers = [_norm_header(h) for h in values[0]]
            col = {}
            for i, h in enumerate(headers):
                if h and h not in col:
                    col[h] = i

            def idx(key):
                return col.get(_norm_header(key))

            # FORMAT A: Item Code | formData (JSON)
            item_code_col = idx("Item Code")
            form_data_col = idx("formData")
            if item_code_col is not None and form_data_col is not None:
                products = []
                for r in range(1, len(values)):
                    row = values[r]
                    item_code = row[item_code_col].strip() if item_code_col < len(row) else ""
                    raw = row[form_data_col] if form_data_col < len(row) else ""
                    if not item_code and not raw:
                        continue
                    try:
                        p = json.loads(raw) if isinstance(raw, str) else raw
                    except Exception:
                        continue
                    if not isinstance(p, dict):
                        continue
                    pid = str(p.get("id") or item_code or "").strip()
                    pname = str(p.get("name") or "").strip()
                    if not pid or not pname:
                        continue
                    products.append({
                        "id": pid, "name": pname,
                        "category": str(p.get("category") or "Other").strip() or "Other",
                        "buyPrice": to_number(p.get("buyPrice")),
                        "sellPrice": to_number(p.get("sellPrice")),
                        "stock": to_number(p.get("stock")),
                        "comment": str(p.get("comment") or ""),
                        "barcode": "" if p.get("barcode") is None else str(p.get("barcode")),
                        "imageId": "" if p.get("imageId") is None else str(p.get("imageId")),
                        "oldPrice": to_number(p.get("oldPrice") or p.get("originalPrice") or 0),
                        "description": str(p.get("telegramDescription") or p.get("comment") or p.get("description") or ""),
                        "emoji": str(p.get("telegramEmoji") or p.get("emoji") or "📦"),
                        "isOffer": p.get("isOffer") is True,
                        "_row": r + 1,
                        "_stockCol": form_data_col + 1,
                        "_sourceType": "products_formdata",
                    })
                return name, products

            # FORMAT B: direct columns id | name | category | buyPrice | sellPrice | stock ...
            id_col, name_col = idx("id"), idx("name")
            sell_col, stock_col = idx("sellPrice"), idx("stock")
            if id_col is not None and name_col is not None and sell_col is not None and stock_col is not None:
                cat_col, buy_col = idx("category"), idx("buyPrice")
                comment_col, barcode_col, image_col = idx("comment"), idx("barcode"), idx("imageId")
                old_col = idx("oldPrice")
                if old_col is None:
                    old_col = idx("originalPrice")
                products = []
                for r in range(1, len(values)):
                    row = values[r]
                    pid = row[id_col].strip() if id_col < len(row) else ""
                    pname = row[name_col].strip() if name_col < len(row) else ""
                    if not pid or not pname:
                        continue
                    products.append({
                        "id": pid, "name": pname,
                        "category": (row[cat_col].strip() if cat_col is not None and cat_col < len(row) and row[cat_col].strip() else "Other"),
                        "buyPrice": to_number(row[buy_col]) if buy_col is not None and buy_col < len(row) else 0,
                        "sellPrice": to_number(row[sell_col]) if sell_col < len(row) else 0,
                        "stock": to_number(row[stock_col]) if stock_col < len(row) else 0,
                        "comment": row[comment_col] if comment_col is not None and comment_col < len(row) else "",
                        "barcode": row[barcode_col] if barcode_col is not None and barcode_col < len(row) else "",
                        "imageId": row[image_col] if image_col is not None and image_col < len(row) else "",
                        "oldPrice": to_number(row[old_col]) if old_col is not None and old_col < len(row) else 0,
                        "description": "",
                        "emoji": "📦",
                        "isOffer": False,
                        "_row": r + 1,
                        "_stockCol": stock_col + 1,
                        "_sourceType": "direct_columns",
                    })
                return name, products

            # FORMAT C: legacy ID | Data(JSON)
            if len(values[0]) >= 2:
                products = []
                for r in range(1, len(values)):
                    row = values[r]
                    row_id = row[0].strip() if row else ""
                    raw = row[1] if len(row) > 1 else ""
                    if not row_id or not raw:
                        continue
                    try:
                        p = json.loads(raw) if isinstance(raw, str) else raw
                    except Exception:
                        continue
                    if not isinstance(p, dict) or not p.get("name"):
                        continue
                    p = dict(p)
                    p["id"] = str(p.get("id") or row_id)
                    products.append({
                        "id": p["id"], "name": str(p.get("name") or ""),
                        "category": str(p.get("category") or "Other").strip() or "Other",
                        "buyPrice": to_number(p.get("buyPrice")),
                        "sellPrice": to_number(p.get("sellPrice")),
                        "stock": to_number(p.get("stock")),
                        "comment": str(p.get("comment") or ""),
                        "barcode": str(p.get("barcode") or ""),
                        "imageId": str(p.get("imageId") or ""),
                        "oldPrice": to_number(p.get("oldPrice") or p.get("originalPrice") or 0),
                        "description": str(p.get("telegramDescription") or p.get("comment") or p.get("description") or ""),
                        "emoji": str(p.get("telegramEmoji") or p.get("emoji") or "📦"),
                        "isOffer": p.get("isOffer") is True,
                        "_row": r + 1,
                        "_stockCol": 2,
                        "_sourceType": "legacy_json",
                    })
                if products:
                    return name, products

        raise RuntimeError("No compatible product data found in the spreadsheet.")

    def _build_catalog(self, products, sheet_name, cat_order):
        normed = []
        for p in products:
            if not p.get("id") or not p.get("name"):
                continue
            category = str(p.get("category") or "Other").strip() or "Other"
            sell = max(0, to_number(p.get("sellPrice")))
            old = max(0, to_number(p.get("oldPrice")))
            normed.append({
                "id": str(p["id"]), "name": str(p["name"]),
                "category": category, "categoryKey": category_key(category),
                "sellPrice": sell, "oldPrice": old,
                "stock": max(0, to_number(p.get("stock"))),
                "barcode": str(p.get("barcode") or ""),
                "description": str(p.get("description") or p.get("comment") or ""),
                "emoji": str(p.get("emoji") or "📦"),
                "isOffer": bool(p.get("isOffer")) or (old > sell > 0),
                "_row": p.get("_row"), "_stockCol": p.get("_stockCol"),
                "_sourceType": p.get("_sourceType"),
            })
        normed.sort(key=lambda p: (p["category"].lower(), p["name"].lower()))

        cat_map = {}
        for p in normed:
            c = cat_map.setdefault(p["categoryKey"], {
                "key": p["categoryKey"], "name": p["category"],
                "emoji": "🛍", "products": 0, "stock": 0,
            })
            c["products"] += 1
            c["stock"] += p["stock"]

        categories = sorted(
            cat_map.values(),
            key=lambda c: (cat_order.get(c["name"], 999999), c["name"].lower()),
        )
        return {"products": normed, "categories": categories, "sourceSheet": sheet_name}

    async def get_product(self, product_id):
        pid = str(product_id or "").strip()
        if not pid:
            return None
        now = time.time()
        cached = self._products.get(pid)
        if cached and cached[0] > now:
            return cached[1]
        fresh = await self._io(self._read_product_row, pid)
        if fresh:
            self._products[pid] = (now + PRODUCT_TTL, fresh)
        return fresh

    def _read_product_row(self, product_id):
        """Fast single-product lookup: find the Item Code row, read one cell."""
        preferred = "Products"
        try:
            for r in self._ws("Setting").get("A2:B"):
                if r and str(r[0]).strip() == "Telegram Product Sheet" and len(r) > 1 and r[1]:
                    preferred = str(r[1]).strip()
                    break
        except Exception:
            pass

        for name in [preferred, "Products", "Items"]:
            try:
                ws = self._ws(name)
                values = ws.get_all_values()
            except Exception:
                continue
            if len(values) < 2:
                continue
            headers = [_norm_header(h) for h in values[0]]
            col = {}
            for i, h in enumerate(headers):
                if h and h not in col:
                    col[h] = i

            def idx(key):
                return col.get(_norm_header(key))

            item_code_col = idx("Item Code")
            form_data_col = idx("formData")
            if item_code_col is not None and form_data_col is not None:
                for r in range(1, len(values)):
                    row = values[r]
                    code = row[item_code_col].strip() if item_code_col < len(row) else ""
                    if code != product_id:
                        continue
                    raw = row[form_data_col] if form_data_col < len(row) else ""
                    try:
                        p = json.loads(raw) if isinstance(raw, str) else raw
                    except Exception:
                        return None
                    if not isinstance(p, dict):
                        return None
                    return self._normalize_single(p, product_id, r + 1, form_data_col + 1, "products_formdata")

            id_col, name_col = idx("id"), idx("name")
            sell_col, stock_col = idx("sellPrice"), idx("stock")
            if id_col is not None and name_col is not None and sell_col is not None and stock_col is not None:
                for r in range(1, len(values)):
                    row = values[r]
                    code = row[id_col].strip() if id_col < len(row) else ""
                    if code != product_id:
                        continue
                    cat_col, buy_col = idx("category"), idx("buyPrice")
                    return {
                        "id": code,
                        "name": row[name_col] if name_col < len(row) else "",
                        "category": (row[cat_col].strip() if cat_col is not None and cat_col < len(row) and row[cat_col].strip() else "Other"),
                        "sellPrice": to_number(row[sell_col]) if sell_col < len(row) else 0,
                        "oldPrice": 0,
                        "stock": to_number(row[stock_col]) if stock_col < len(row) else 0,
                        "barcode": "", "description": "", "emoji": "📦", "isOffer": False,
                        "_row": r + 1, "_stockCol": stock_col + 1, "_sourceType": "direct_columns",
                    }
        return None

    @staticmethod
    def _normalize_single(p, fallback_id, row, stock_col, source_type):
        category = str(p.get("category") or "Other").strip() or "Other"
        sell = max(0, to_number(p.get("sellPrice")))
        old = max(0, to_number(p.get("oldPrice") or p.get("originalPrice") or 0))
        return {
            "id": str(p.get("id") or fallback_id),
            "name": str(p.get("name") or ""),
            "category": category, "categoryKey": category_key(category),
            "sellPrice": sell, "oldPrice": old,
            "stock": max(0, to_number(p.get("stock"))),
            "barcode": "" if p.get("barcode") is None else str(p.get("barcode")),
            "description": str(p.get("telegramDescription") or p.get("comment") or p.get("description") or ""),
            "emoji": str(p.get("telegramEmoji") or p.get("emoji") or "📦"),
            "isOffer": bool(p.get("isOffer")) or (old > sell > 0),
            "_row": row, "_stockCol": stock_col, "_sourceType": source_type,
        }

    # -- users -----------------------------------------------------------
    async def touch_user(self, tg_user):
        """Create/update the user row, but at most once per USER_TOUCH_TTL."""
        uid = str(tg_user.id)
        sig = f"{tg_user.username or ''}|{tg_user.first_name or ''}"
        now = time.time()
        cached = self._user_touch.get(uid)
        if cached and cached[0] > now and cached[1] == sig:
            return None
        row = await self._io(self._get_or_create_user, uid, tg_user.username or "",
                             tg_user.first_name or "")
        self._user_touch[uid] = (now + USER_TOUCH_TTL, sig)
        return row

    async def get_user(self, telegram_id):
        return await self._io(self._get_or_create_user, str(telegram_id), "", "")

    def _get_or_create_user(self, uid, username, first_name):
        ws = self._ensure_ws(TAB["users"],
                             ["TelegramId", "Username", "FirstName", "Balance", "CreatedAt", "UpdatedAt"])
        col_a = ws.col_values(1)
        row_no = None
        for i, v in enumerate(col_a):
            if v == uid:
                row_no = i + 1
                break
        now = _now_iso()
        if row_no and row_no > 1:
            vals = ws.row_values(row_no)
            while len(vals) < 6:
                vals.append("")
            if username and username != vals[1]:
                vals[1] = username
            if first_name and first_name != vals[2]:
                vals[2] = first_name
            # only write the name cells back when something changed
            ws.update(f"B{row_no}:C{row_no}", [[vals[1], vals[2]]])
            ws.update_cell(row_no, 6, now)
            return {"row": row_no, "telegramId": uid, "username": vals[1],
                    "firstName": vals[2], "balance": to_number(vals[3]),
                    "createdAt": vals[4], "updatedAt": now}
        ws.append_row([uid, username, first_name, 0, now, now])
        return {"row": len(ws.col_values(1)), "telegramId": uid,
                "username": username, "firstName": first_name,
                "balance": 0, "createdAt": now, "updatedAt": now}

    async def set_balance(self, telegram_id, balance):
        def _do():
            user = self._get_or_create_user(str(telegram_id), "", "")
            ws = self._ws(TAB["users"])
            bal = round(to_number(balance) * 100) / 100
            ws.update_cell(user["row"], 4, bal)
            ws.update_cell(user["row"], 6, _now_iso())
            return bal
        return await self._io(_do)

    # -- carts -----------------------------------------------------------
    async def get_cart(self, telegram_id):
        def _do():
            ws = self._ensure_ws(TAB["carts"], ["TelegramId", "Data", "UpdatedAt"])
            for i, v in enumerate(ws.col_values(1)):
                if v == str(telegram_id) and i + 1 > 1:
                    raw = ws.cell(i + 1, 2).value or "{}"
                    try:
                        return json.loads(raw)
                    except Exception:
                        return {}
            return {}
        return await self._io(_do)

    async def save_cart(self, telegram_id, cart):
        def _do():
            ws = self._ensure_ws(TAB["carts"], ["TelegramId", "Data", "UpdatedAt"])
            uid = str(telegram_id)
            row_no = None
            for i, v in enumerate(ws.col_values(1)):
                if v == uid:
                    row_no = i + 1
                    break
            payload = json.dumps(cart or {})
            if row_no and row_no > 1:
                ws.update(f"A{row_no}:C{row_no}", [[uid, payload, _now_iso()]])
            else:
                ws.append_row([uid, payload, _now_iso()])
        await self._io(_do)

    # -- orders ----------------------------------------------------------
    async def orders_for_user(self, telegram_id, limit=8):
        def _do():
            try:
                ws = self._ws(TAB["orders"])
            except Exception:
                return []
            col_b = ws.col_values(2)[1:]  # skip header
            out = []
            for raw in reversed(col_b):
                try:
                    o = json.loads(raw)
                except Exception:
                    continue
                if str(o.get("telegramId")) == str(telegram_id):
                    out.append(o)
                    if len(out) >= limit:
                        break
            return out
        return await self._io(_do)

    def _next_invoice_no(self):
        ws = self._ws(TAB["orders"])
        n = max(0, len(ws.col_values(1)) - 1)
        return "TG-" + str(1001 + n)

    # -- checkout --------------------------------------------------------
    async def checkout(self, tg_user, request_items):
        """Secure checkout: fresh prices/stock, wallet debit, stock update."""
        async with self._lock:
            return await self._io(self._checkout_sync, tg_user, request_items)

    def _checkout_sync(self, tg_user, request_items):
        normalized = {}
        for item in request_items:
            pid = str(item.get("id") or "").strip()
            qty = int(to_number(item.get("qty")))
            if not pid or qty < 1:
                raise ValueError("Invalid cart item.")
            normalized[pid] = normalized.get(pid, 0) + qty

        # fresh catalog read so prices/stock cannot be stale
        sheet_name, rows = self._read_product_rows()
        product_map = {str(p["id"]): p for p in rows}

        total, total_cost, sale_items = 0, 0, []
        for pid, qty in normalized.items():
            p = product_map.get(pid)
            if not p:
                raise ValueError("Product not found: " + pid)
            stock = max(0, to_number(p.get("stock")))
            if stock < qty:
                raise ValueError(f'Not enough stock for "{p.get("name") or pid}". Available: {int(stock)}')
            sell = max(0, to_number(p.get("sellPrice")))
            buy = max(0, to_number(p.get("buyPrice")))
            total += sell * qty
            total_cost += buy * qty
            sale_items.append({
                "id": str(p["id"]), "name": str(p.get("name") or pid),
                "category": str(p.get("category") or ""),
                "buyPrice": buy, "sellPrice": sell, "qty": qty,
                "_row": p.get("_row"), "_stockCol": p.get("_stockCol"),
                "_sourceType": p.get("_sourceType"),
            })
        total = round(total * 100) / 100

        customer = self._get_or_create_user(str(tg_user.id), tg_user.username or "", tg_user.first_name or "")
        before = to_number(customer["balance"])
        if before < total:
            raise ValueError(f"Not enough balance. Need {money(total)}, available {money(before)}.")

        ws = self._ws(sheet_name)
        # validate stock again right before writing
        for item in sale_items:
            if item["_sourceType"] == "direct_columns":
                current = to_number(ws.cell(item["_row"], item["_stockCol"]).value)
            else:
                raw = ws.cell(item["_row"], item["_stockCol"] or 2).value
                try:
                    current = to_number((json.loads(raw) if isinstance(raw, str) else raw or {}).get("stock"))
                except Exception:
                    current = 0
            if current < item["qty"]:
                raise ValueError(f'Stock changed for "{item["name"]}". Available: {int(current)}')

        # debit wallet
        new_balance = round((before - total) * 100) / 100
        users_ws = self._ws(TAB["users"])
        users_ws.update_cell(customer["row"], 4, new_balance)
        users_ws.update_cell(customer["row"], 6, _now_iso())

        # decrement stock (batch where possible)
        try:
            updates = []
            for item in sale_items:
                if item["_sourceType"] == "direct_columns":
                    cell = ws.cell(item["_row"], item["_stockCol"])
                    updates.append({"range": f"R{cell.row}C{cell.col}",
                                    "values": [[to_number(cell.value) - item["qty"]]]})
                else:
                    cell = ws.cell(item["_row"], item["_stockCol"] or 2)
                    raw = cell.value
                    p = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    p["stock"] = to_number(p.get("stock")) - item["qty"]
                    updates.append({"range": f"R{cell.row}C{cell.col}",
                                    "values": [[json.dumps(p, ensure_ascii=False)]]})
            if updates:
                reqs = []
                for u in updates:
                    m = re.match(r"R(\d+)C(\d+)", u["range"])
                    r, c = int(m.group(1)), int(m.group(2))
                    reqs.append({"range": _rc_to_a1(r, c), "values": u["values"]})
                ws.batch_update(reqs)
        except Exception as err:
            # roll the wallet debit back
            users_ws.update_cell(customer["row"], 4, before)
            raise err

        invoice_no = self._next_invoice_no()
        order_id = "TGO-" + uuid.uuid4().hex[:8].upper()
        public_items = [{"id": i["id"], "name": i["name"], "category": i["category"],
                         "qty": i["qty"], "price": i["sellPrice"], "buyPrice": i["buyPrice"]}
                        for i in sale_items]
        order = {
            "id": order_id, "telegramId": str(tg_user.id),
            "username": tg_user.username or "", "customerName": tg_user.first_name or "",
            "invoiceNo": invoice_no, "total": total, "totalCost": total_cost,
            "profit": round((total - total_cost) * 100) / 100,
            "items": public_items, "paymentMethod": "Telegram Wallet",
            "status": "PAID", "createdAt": _now_iso(),
        }
        orders_ws = self._ensure_ws(TAB["orders"], ["ID", "Data"])
        orders_ws.append_row([order_id, json.dumps(order, ensure_ascii=False)])
        self._append_wallet_tx({
            "telegramId": str(tg_user.id), "type": "DEBIT", "amount": -total,
            "balanceBefore": before, "balanceAfter": new_balance,
            "reference": order_id, "invoiceNo": invoice_no,
            "status": "COMPLETED", "createdAt": _now_iso(),
        })
        try:
            self._mirror_sale_to_legacy_pos(order, tg_user, total_cost)
        except Exception:
            pass
        return {"order": order, "walletBalance": new_balance}

    def _append_wallet_tx(self, data):
        tx_id = "WTX-" + uuid.uuid4().hex[:10].upper()
        row = dict(data or {})
        row["id"] = tx_id
        ws = self._ensure_ws(TAB["wallet"], ["ID", "Data"])
        ws.append_row([tx_id, json.dumps(row, ensure_ascii=False)])
        return tx_id

    async def append_wallet_tx(self, data):
        return await self._io(self._append_wallet_tx, data)

    def _mirror_sale_to_legacy_pos(self, order, tg_user, total_cost):
        try:
            sales_head = self._ws("Sales").get("A1:B1")
            payments_head = self._ws("Payments").get("A1:B1")
        except Exception:
            return
        sh = [str(x).strip().lower() for x in (sales_head[0] if sales_head else [])[:2]]
        ph = [str(x).strip().lower() for x in (payments_head[0] if payments_head else [])[:2]]
        if sh != ["id", "data"] or ph != ["id", "data"]:
            return
        sold_by = {"username": "TG:" + (tg_user.username or tg_user.first_name or str(tg_user.id)),
                   "role": "customer"}
        sale = {
            "invoiceNo": order["invoiceNo"], "date": order["createdAt"],
            "items": [{"id": i["id"], "name": i["name"], "qty": i["qty"],
                       "sellPrice": i["price"], "buyPrice": to_number(i["buyPrice"]),
                       "isFoc": False,
                       "profit": (to_number(i["price"]) - to_number(i["buyPrice"])) * to_number(i["qty"])}
                      for i in order["items"]],
            "total": order["total"], "amountPaid": order["total"], "change": 0,
            "soldBy": sold_by, "discountPercent": 0, "discountAmount": 0,
            "paymentMethod": "Telegram Wallet",
        }
        sales_ws, payments_ws = self._ws("Sales"), self._ws("Payments")
        sales_ws.append_row(["Sale-" + uuid.uuid4().hex[:6].upper(), json.dumps(sale, ensure_ascii=False)])
        payments_ws.append_row(["Payment-" + uuid.uuid4().hex[:6].upper(), json.dumps({
            "date": order["createdAt"], "invoice": order["invoiceNo"],
            "amountPaid": order["total"], "totalSales": order["total"], "change": 0,
            "totalBuy": total_cost,
            "profit": round((order["total"] - total_cost) * 100) / 100,
            "status": "Completed", "paymentMethod": "Telegram Wallet",
            "itemsSold": sum(to_number(i["qty"]) for i in order["items"]),
            "seller": sold_by["username"], "sellerRole": sold_by["role"],
        }, ensure_ascii=False)])

    # -- top-ups ---------------------------------------------------------
    async def create_topup(self, tg_user, amount, telegram_file_id):
        def _do():
            topup_id = "TOP-" + uuid.uuid4().hex[:8].upper()
            topup = {
                "id": topup_id, "telegramId": str(tg_user.id),
                "username": tg_user.username or "", "firstName": tg_user.first_name or "",
                "amount": amount, "telegramFileId": telegram_file_id,
                "status": "PENDING", "createdAt": _now_iso(),
                "reviewedAt": "", "reviewedBy": "",
            }
            ws = self._ensure_ws(TAB["topups"], ["ID", "Data"])
            ws.append_row([topup_id, json.dumps(topup, ensure_ascii=False)])
            return topup
        return await self._io(_do)

    def _find_topup(self, topup_id):
        ws = self._ws(TAB["topups"])
        col_a = ws.col_values(1)
        for i, v in enumerate(col_a):
            if v == topup_id and i + 1 > 1:
                row_no = i + 1
                raw = ws.cell(row_no, 2).value or "{}"
                return row_no, json.loads(raw)
        return None, None

    async def approve_topup(self, topup_id, admin_id):
        async with self._lock:
            return await self._io(self._review_topup, topup_id, admin_id, True)

    async def reject_topup(self, topup_id, admin_id):
        async with self._lock:
            return await self._io(self._review_topup, topup_id, admin_id, False)

    def _review_topup(self, topup_id, admin_id, approve):
        row_no, topup = self._find_topup(topup_id)
        if not topup:
            raise ValueError("Top-up not found.")
        if topup.get("status") == ("APPROVED" if approve else "REJECTED"):
            return {"message": "Already " + topup["status"].lower() + ".", "topup": topup}
        if topup.get("status") != "PENDING":
            raise ValueError("Top-up is already " + str(topup.get("status")) + ".")
        ws = self._ws(TAB["topups"])
        topup["status"] = "APPROVED" if approve else "REJECTED"
        topup["reviewedAt"] = _now_iso()
        topup["reviewedBy"] = str(admin_id)
        ws.update_cell(row_no, 2, json.dumps(topup, ensure_ascii=False))
        if approve:
            customer = self._get_or_create_user(str(topup["telegramId"]), "", "")
            before = to_number(customer["balance"])
            after = round((before + to_number(topup["amount"])) * 100) / 100
            users_ws = self._ws(TAB["users"])
            users_ws.update_cell(customer["row"], 4, after)
            users_ws.update_cell(customer["row"], 6, _now_iso())
            self._append_wallet_tx({
                "telegramId": str(topup["telegramId"]), "type": "TOPUP",
                "amount": to_number(topup["amount"]),
                "balanceBefore": before, "balanceAfter": after,
                "reference": topup_id, "status": "COMPLETED",
                "createdAt": _now_iso(), "reviewedBy": str(admin_id),
            })
            topup["newBalance"] = after
        return {"message": "Top-up approved." if approve else "Top-up rejected.", "topup": topup}

    # -- states ----------------------------------------------------------
    def set_state(self, telegram_id, state):
        self._states[str(telegram_id)] = (time.time() + STATE_TTL, state or {})

    def get_state(self, telegram_id):
        item = self._states.get(str(telegram_id))
        if not item:
            return None
        if item[0] < time.time():
            self._states.pop(str(telegram_id), None)
            return None
        return item[1]

    def clear_state(self, telegram_id):
        self._states.pop(str(telegram_id), None)

    # -- logging ---------------------------------------------------------
    async def log(self, level, event, data):
        def _do():
            try:
                ws = self._ensure_ws(TAB["logs"], ["Timestamp", "Level", "Event", "Data"])
                text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
                ws.append_row([_now_iso(), str(level or "INFO"), str(event or ""), trim(text, 45000)])
            except Exception:
                pass
        await self._io(_do)
