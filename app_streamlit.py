# app_streamlit.py
# Fresh single-file Streamlit POS for a general restaurant (pizza-ready)
# - Tabs: Order, Kitchen (KDS), Manager, Admin
# - SQLite DB auto-creates; menu.json auto-creates if missing
# - Stripe Checkout payments (optional) via .env (see notes below)
#
# Quickstart
#   pip install streamlit pandas stripe python-dotenv
#   streamlit run app_streamlit.py --server.port 8502
#
# .env (must be NEXT TO THIS FILE)
#   STRIPE_SECRET_KEY=sk_test_...
#   STRIPE_WEBHOOK_SECRET=whsec_...   # used by webhook.py (FastAPI)
#   PUBLIC_BASE_URL=http://127.0.0.1:8502

from __future__ import annotations
import json
import os
import sys
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, List, Optional

import pandas as pd
import streamlit as st
from streamlit.components.v1 import html as st_html
import sys

# ------------------------- STRIPE (optional) -------------------------
# If you don't want Stripe, set STRIPE_ENABLED=False below.
import stripe
from urllib.parse import urlencode
from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
ENV_PATH = APP_DIR / ".env"
# Try to load .env right next to this file
load_dotenv(dotenv_path=ENV_PATH, override=True)
# Fallback: also search upward and current working dir if still missing
if not os.getenv("STRIPE_SECRET_KEY"):
    try:
        from dotenv import find_dotenv
        alt = find_dotenv(usecwd=True)
        if alt:
            load_dotenv(alt, override=True)
    except Exception:
        pass  # <- force-load .env next to this file

APP_NAME = "Fireside Pizza POS"
DEFAULT_PIN = "1234"
CURRENCY = "$"
DEFAULT_TAX_RATE = 0.085
DEFAULT_DELIVERY_FEE = 3.00
MENU_FILE = str(APP_DIR / "menu.json")
DB_FILE = str(APP_DIR / "orders.db")

STRIPE_ENABLED = True
stripe.api_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "http://127.0.0.1:8502").strip()

STATUS_CHOICES = ["new", "in_progress", "ready", "completed"]
SERVICE_TYPES = ["Dine-In", "Takeout", "Delivery"]
PAYMENT_METHODS = ["Cash", "Card", "Online", "Other"]

# ------------------------- UTILITIES -------------------------

def money(x: float) -> str:
    try:
        return f"{CURRENCY}{x:,.2f}"
    except Exception:
        return f"{CURRENCY}{x}"


def ensure_menu_file(path: str = MENU_FILE):
    if os.path.exists(path):
        return
    default_menu = {
        "categories": [
            {
                "name": "Pizzas",
                "items": [
                    {
                        "id": "pz_margherita",
                        "name": "Margherita",
                        "base_price": 12.0,
                        "sizes": [
                            {"name": "Small 10\"", "price_delta": 0},
                            {"name": "Medium 12\"", "price_delta": 3},
                            {"name": "Large 16\"", "price_delta": 7},
                        ],
                        "included_toppings": ["Mozzarella", "Tomato Sauce", "Basil"],
                        "available_toppings": [
                            {"name": "Pepperoni", "price_delta": 1.5},
                            {"name": "Mushrooms", "price_delta": 1.0},
                            {"name": "Onions", "price_delta": 0.75},
                            {"name": "Olives", "price_delta": 0.75},
                            {"name": "Extra Cheese", "price_delta": 1.5},
                        ],
                        "dietary": ["dairy"],
                        "allergens": ["dairy", "gluten"],
                        "kashrut_type": "dairy",
                    },
                    {
                        "id": "pz_pepperoni",
                        "name": "Pepperoni",
                        "base_price": 13.0,
                        "sizes": [
                            {"name": "Small 10\"", "price_delta": 0},
                            {"name": "Medium 12\"", "price_delta": 3},
                            {"name": "Large 16\"", "price_delta": 7},
                        ],
                        "included_toppings": ["Mozzarella", "Tomato Sauce", "Pepperoni"],
                        "available_toppings": [
                            {"name": "Mushrooms", "price_delta": 1.0},
                            {"name": "Onions", "price_delta": 0.75},
                            {"name": "Olives", "price_delta": 0.75},
                            {"name": "Jalapeños", "price_delta": 1.0},
                            {"name": "Extra Cheese", "price_delta": 1.5},
                        ],
                        "dietary": ["meat"],
                        "allergens": ["dairy", "gluten"],
                        "kashrut_type": "meat",
                    },
                ],
            },
            {
                "name": "Sides",
                "items": [
                    {
                        "id": "sd_garlic_knots",
                        "name": "Garlic Knots",
                        "base_price": 5.0,
                        "sizes": [],
                        "available_toppings": [
                            {"name": "Marinara", "price_delta": 0.75}
                        ],
                        "dietary": ["pareve"],
                        "allergens": ["gluten", "garlic"],
                        "kashrut_type": "pareve",
                    }
                ],
            },
            {
                "name": "Drinks",
                "items": [
                    {
                        "id": "dr_soda",
                        "name": "Soda",
                        "base_price": 2.5,
                        "sizes": [
                            {"name": "Reg", "price_delta": 0},
                            {"name": "Lg", "price_delta": 0.75},
                        ],
                        "available_toppings": [],
                        "dietary": ["pareve"],
                        "allergens": [],
                        "kashrut_type": "pareve",
                    }
                ],
            },
        ]
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(default_menu, f, indent=2)


def load_menu(path: str = MENU_FILE) -> Dict[str, Any]:
    ensure_menu_file(path)
    with open(path, "r", encoding="utf-8-sig") as f:  # handle BOM
        return json.load(f)


def save_menu(menu: Dict[str, Any], path: str = MENU_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(menu, f, indent=2, ensure_ascii=False)

# ------------------------- DATABASE -------------------------

def get_conn():
    return sqlite3.connect(DB_FILE, check_same_thread=False)


def init_db():
    con = get_conn()
    cur = con.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            phone TEXT UNIQUE,
            total_spent REAL DEFAULT 0,
            orders_count INTEGER DEFAULT 0
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            customer_name TEXT,
            customer_phone TEXT,
            service_type TEXT DEFAULT 'Takeout',
            table_number TEXT,
            status TEXT DEFAULT 'new',
            paid INTEGER DEFAULT 0,
            payment_method TEXT,
            notes TEXT,
            source TEXT DEFAULT 'POS',
            subtotal REAL DEFAULT 0,
            tax REAL DEFAULT 0,
            discount REAL DEFAULT 0,
            delivery_fee REAL DEFAULT 0,
            tip REAL DEFAULT 0,
            total REAL DEFAULT 0,
            archived INTEGER DEFAULT 0
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER,
            item_id TEXT,
            item_name TEXT,
            base_price REAL,
            size TEXT,
            size_delta REAL DEFAULT 0,
            modifiers TEXT,
            qty INTEGER,
            line_total REAL,
            item_notes TEXT,
            voided INTEGER DEFAULT 0,
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )
        """
    )

    def add_col(table: str, col: str, decl: str):
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            con.commit()
        except Exception:
            pass

    add_col("orders", "archived", "INTEGER DEFAULT 0")
    add_col("orders", "source", "TEXT DEFAULT 'POS'")

    con.close()

# ------------------------- PRICING -------------------------

def find_item(menu: Dict[str, Any], item_id: str) -> Optional[Dict[str, Any]]:
    for cat in menu.get("categories", []):
        for it in cat.get("items", []):
            if it.get("id") == item_id:
                return it
    return None


def calc_line_total(menu: Dict[str, Any], item_id: str, qty: int, size_name: Optional[str], modifiers: List[Dict[str, Any]]) -> float:
    it = find_item(menu, item_id)
    if not it:
        return 0.0
    base = float(it.get("base_price", 0))
    size_delta = 0.0
    if size_name and it.get("sizes"):
        for s in it["sizes"]:
            if s["name"] == size_name:
                size_delta = float(s.get("price_delta", 0))
                break
    mods_total = sum(float(m.get("price_delta", 0)) for m in modifiers)
    return (base + size_delta + mods_total) * max(1, int(qty))


def calc_order_totals(subtotal: float, tax_rate: float, discount: float, delivery_fee: float, tip: float) -> Dict[str, float]:
    sub_after_disc = max(0.0, subtotal - (discount or 0))
    tax = round(sub_after_disc * (tax_rate or 0), 2)
    total = round(sub_after_disc + tax + (delivery_fee or 0) + (tip or 0), 2)
    return {
        "subtotal": round(subtotal, 2),
        "tax": tax,
        "discount": round(discount or 0, 2),
        "delivery_fee": round(delivery_fee or 0, 2),
        "tip": round(tip or 0, 2),
        "total": total,
    }

# ------------------------- STATE -------------------------

def init_state():
    if "cart" not in st.session_state:
        st.session_state.cart = []
    if "admin_unlocked" not in st.session_state:
        st.session_state.admin_unlocked = False
    if "tax_rate" not in st.session_state:
        st.session_state.tax_rate = DEFAULT_TAX_RATE
    if "delivery_fee" not in st.session_state:
        st.session_state.delivery_fee = DEFAULT_DELIVERY_FEE
    if "pin" not in st.session_state:
        st.session_state.pin = DEFAULT_PIN

# ------------------------- UI HELPERS -------------------------

def open_in_new_tab(url: str):
    # Try to open Stripe Checkout in a new tab immediately (helps with popup blockers)
    st_html(f"<script>window.open('{url}', '_blank');</script>", height=0)

def trigger_checkout(url: str):
    """Persist URL and immediately render a visible link + attempt a popup once.
    This avoids relying solely on rerun timing / popup blockers.
    """
    st.session_state["checkout_url"] = url
    # Try an immediate popup (may be blocked)
    try:
        st_html(f"<script>window.open('{url}','_blank');</script>", height=0)
    except Exception:
        pass
    # Always render a big, obvious link the user can click
    with st.container(border=True):
        st.subheader("Complete payment")
        st.write("If a new tab didn't open, click the button below.")
        try:
            st.link_button("Open secure checkout", url, use_container_width=True)
        except Exception:
            st.markdown(f"[Open secure checkout]({url})")
        c1, c2 = st.columns([1,1])
        if c1.button("Try popup again"):
            st_html(f"<script>window.open('{url}','_blank');</script>", height=0)
        if c2.button("Dismiss"):
            st.session_state.pop("checkout_url", None)
            st.session_state.pop("checkout_popup_attempted", None)
            st.rerun()

def render_checkout_banner():
    url = st.session_state.get("checkout_url")
    if not url:
        return
    # Attempt a single popup the first time we render this banner
    if not st.session_state.get("checkout_popup_attempted"):
        st_html(f"<script>window.open('{url}','_blank');</script>", height=0)
        st.session_state["checkout_popup_attempted"] = True
    with st.container(border=True):
        st.subheader("Complete payment")
        st.write("If a new tab didn't open, click the button below.")
        try:
            st.link_button("Open secure checkout", url, use_container_width=True)
        except Exception:
            st.markdown(f"[Open secure checkout]({url})")
        c1, c2 = st.columns([1,1])
        if c1.button("Try popup again"):
            st_html(f"<script>window.open('{url}','_blank');</script>", height=0)
        if c2.button("Dismiss"):
            st.session_state.pop("checkout_url", None)
            st.session_state.pop("checkout_popup_attempted", None)
            st.experimental_rerun()


def cart_summary_ui(menu: Dict[str, Any]):
    st.subheader("Cart")
    if not st.session_state.cart:
        st.info("Cart is empty.")
        return 0.0

    total_sub = 0.0
    for i, line in enumerate(st.session_state.cart):
        with st.container(border=True):
            st.markdown(f"**{line['item_name']}** × {line['qty']}")
            if line.get("size"):
                st.caption(f"Size: {line['size']}")
            if line.get("modifiers"):
                mods = ", ".join([f"{m['name']} ({CURRENCY}{m['price_delta']})" for m in line["modifiers"]])
                st.caption(f"Toppings: {mods}")
            st.write(money(line["line_total"]))
            cols = st.columns(3)
            if cols[0].button("−1", key=f"dec_{i}"):
                line["qty"] = max(1, line["qty"] - 1)
                line["line_total"] = calc_line_total(menu, line["item_id"], line["qty"], line.get("size"), line.get("modifiers", []))
            if cols[1].button("+1", key=f"inc_{i}"):
                line["qty"] += 1
                line["line_total"] = calc_line_total(menu, line["item_id"], line["qty"], line.get("size"), line.get("modifiers", []))
            if cols[2].button("Remove", key=f"rm_{i}"):
                st.session_state.cart.pop(i)
                st.experimental_rerun()
        total_sub += float(line["line_total"])

    st.markdown(f"**Subtotal:** {money(total_sub)}")
    return total_sub


def add_item_ui(menu: Dict[str, Any]):
    st.subheader("Add Item")
    cat_names = [c["name"] for c in menu.get("categories", [])]
    if not cat_names:
        st.warning("No categories in menu.json. Use Admin to edit the menu.")
        return

    cat_name = st.selectbox("Category", cat_names)
    cat = next(c for c in menu["categories"] if c["name"] == cat_name)
    item_names = [i["name"] for i in cat.get("items", [])]
    if not item_names:
        st.warning("No items in this category.")
        return

    item_name = st.selectbox("Item", item_names)
    item = next(i for i in cat["items"] if i["name"] == item_name)

    qty = st.number_input("Qty", 1, 99, 1)

    size = None
    size_delta_map = {}
    if item.get("sizes"):
        size = st.selectbox("Size", [s["name"] for s in item["sizes"]])
        size_delta_map = {s["name"]: float(s.get("price_delta", 0)) for s in item["sizes"]}

    chosen_mods = []
    if item.get("available_toppings"):
        mods = [m["name"] for m in item["available_toppings"]]
        selected = st.multiselect("Toppings", mods)
        price_lookup = {m["name"]: float(m["price_delta"]) for m in item["available_toppings"]}
        chosen_mods = [{"name": m, "price_delta": price_lookup[m]} for m in selected]

    item_notes = st.text_input("Item notes (optional)")

    if st.button("Add to Cart", use_container_width=True):
        line_total = calc_line_total(menu, item["id"], qty, size, chosen_mods)
        st.session_state.cart.append({
            "item_id": item["id"],
            "item_name": item["name"],
            "qty": int(qty),
            "size": size,
            "size_delta": float(size_delta_map.get(size, 0)) if size else 0.0,
            "modifiers": chosen_mods,
            "item_notes": item_notes.strip() if item_notes else None,
            "base_price": float(item.get("base_price", 0)),
            "line_total": float(line_total),
        })
        st.success(f"Added {qty} × {item['name']} to cart")

# ------------------------- STRIPE HELPERS -------------------------

def create_checkout_for_order(order_id: int) -> Optional[str]:
    """Create a Stripe Checkout session. Returns URL or None on error."""
    if not (STRIPE_ENABLED and stripe.api_key):
        st.error("Stripe not configured.")
        return None

    try:
        con = get_conn(); cur = con.cursor()
        cur.execute("SELECT customer_name, total FROM orders WHERE id=?", (order_id,))
        row = cur.fetchone(); con.close()
        if not row:
            st.error("Order not found.")
            return None

        customer_name, total = row
        if total is None:
            st.error("Order total missing.")
            return None
        amount_cents = int(round(float(total) * 100))
        if amount_cents <= 0:
            st.error("Total must be greater than $0.00 to start checkout.")
            return None

        success_url = f"{PUBLIC_BASE_URL}?{urlencode({'checkout':'success','order_id':order_id})}"
        cancel_url  = f"{PUBLIC_BASE_URL}?{urlencode({'checkout':'canceled','order_id':order_id})}"

        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"Order #{order_id} - {customer_name or 'Guest'}"},
                    "unit_amount": amount_cents,
                },
                "quantity": 1,
            }],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"order_id": str(order_id)},
        )
        return session.url
    except Exception as e:
        st.error(f"Stripe error: {e}")
        return None

# ------------------------- ORDER FLOW -------------------------

def create_or_get_customer(con, name: str, phone: str):
    cur = con.cursor()
    if not phone:
        return None
    cur.execute("SELECT id FROM customers WHERE phone=?", (phone,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO customers (name, phone) VALUES (?,?)", (name, phone))
    con.commit()
    return cur.lastrowid


def is_order_paid(order_id: int) -> bool:
    try:
        con = get_conn(); cur = con.cursor()
        cur.execute("SELECT paid FROM orders WHERE id=?", (order_id,))
        row = cur.fetchone(); con.close()
        return bool(row and row[0])
    except Exception:
        return False

def place_order_ui(menu: Dict[str, Any]):
    st.header("Front Desk · Take Orders")

    # Persistent payment banner for the most recent order until it's paid or dismissed
    last_oid = st.session_state.get("last_order_id")
    if last_oid and not is_order_paid(int(last_oid)):
        with st.container(border=True):
            st.subheader(f"Recent order #{last_oid}")
            st.caption("Finish payment with Stripe or dismiss.")
            if STRIPE_ENABLED and stripe.api_key:
                if st.button(f"Pay by Card (Stripe) · Order #{last_oid}", key="pay_last", use_container_width=True):
                    url = create_checkout_for_order(int(last_oid))
                    if url:
                        trigger_checkout(url)
            colx, coly = st.columns(2)
            if colx.button("Dismiss", key="dismiss_last"):
                st.session_state.pop("last_order_id", None)
                st.rerun()

    # Cart build (outside form so buttons work)
    with st.container(border=True):
        st.subheader("Build Cart")
        add_item_ui(menu)
        subtotal = cart_summary_ui(menu)

    st.divider()

    # Checkout form
    with st.form("order_form", border=True):
        cols = st.columns(2)
        customer_name = cols[0].text_input("Customer name")
        customer_phone = cols[1].text_input("Phone (optional)")

        service_type = st.radio("Service type", SERVICE_TYPES, horizontal=True)
        table_number = None
        address = None
        delivery_fee = 0.0
        if service_type == "Dine-In":
            table_number = st.text_input("Table number (optional)")
        elif service_type == "Delivery":
            address = st.text_input("Delivery address")
            delivery_fee = st.number_input("Delivery fee", 0.0, 99.0, float(st.session_state.delivery_fee))

        st.divider()
        cols2 = st.columns(4)
        discount = cols2[0].number_input("Discount (amount)", 0.0, 999.0, 0.0)
        tip = cols2[1].number_input("Tip", 0.0, 999.0, 0.0)
        tax_rate = cols2[2].number_input("Tax Rate", 0.0, 0.5, float(st.session_state.tax_rate))
        payment_method = cols2[3].selectbox("Payment", PAYMENT_METHODS)

        notes = st.text_area("Order notes (optional)")
        paid = st.checkbox("Mark as paid now?", value=False)

        totals = calc_order_totals(subtotal, tax_rate, discount, delivery_fee, tip)
        st.info(f"Total: {money(totals['total'])}  ·  Subtotal {money(totals['subtotal'])} · Tax {money(totals['tax'])}")

        colb1, colb2 = st.columns(2)
        submitted = colb1.form_submit_button("Place Order", use_container_width=True)
        pay_and_submit = colb2.form_submit_button("Place & Pay with Stripe", type="primary", use_container_width=True)

    if submitted or pay_and_submit:
        if not st.session_state.cart:
            st.error("Cart is empty.")
            return
        con = get_conn(); cur = con.cursor()
        created_at = datetime.now().isoformat(timespec="seconds")
        cust_id = create_or_get_customer(con, customer_name.strip(), customer_phone.strip()) if customer_name else None

        cur.execute(
            """
            INSERT INTO orders (
                created_at, customer_name, customer_phone, service_type, table_number,
                status, paid, payment_method, notes, source,
                subtotal, tax, discount, delivery_fee, tip, total
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                created_at, customer_name.strip() if customer_name else None,
                customer_phone.strip() if customer_phone else None,
                service_type, table_number,
                "new", 1 if paid else 0, payment_method, notes, "POS",
                totals["subtotal"], totals["tax"], totals["discount"], totals["delivery_fee"], totals["tip"], totals["total"],
            ),
        )
        order_id = cur.lastrowid

        for line in st.session_state.cart:
            cur.execute(
                """
                INSERT INTO order_items (
                    order_id, item_id, item_name, base_price, size, size_delta,
                    modifiers, qty, line_total, item_notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    order_id,
                    line["item_id"], line["item_name"], line["base_price"],
                    line.get("size"), line.get("size_delta", 0.0),
                    json.dumps(line.get("modifiers", []), ensure_ascii=False),
                    int(line["qty"]), float(line["line_total"]),
                    line.get("item_notes"),
                ),
            )

        if cust_id is not None and customer_phone:
            cur.execute("UPDATE customers SET orders_count = orders_count + 1, total_spent = total_spent + ? WHERE id = ?", (totals["total"], cust_id))

        con.commit(); con.close()
        st.session_state.cart = []
        st.success(f"Order #{order_id} placed!")
        st.balloons()

        # Remember this order so the pay button persists across reruns
        st.session_state["last_order_id"] = int(order_id)

        # If user clicked the Stripe option, launch checkout right away
        if pay_and_submit:
            if not (STRIPE_ENABLED and stripe.api_key):
                st.error("Stripe is not configured. Check Admin → Stripe.")
            elif totals["total"] <= 0:
                st.error("Total must be greater than $0.00 to start checkout.")
            else:
                url = create_checkout_for_order(order_id)
                if url:
                    trigger_checkout(url)
                else:
                    st.error("Could not start checkout. See Admin → Stripe diagnostics.")

        # Remember this order so the pay button persists across reruns
        st.session_state["last_order_id"] = int(order_id)

        # Offer Stripe Checkout after placing order
        if STRIPE_ENABLED and stripe.api_key:
            if st.button(f"Pay by Card (Stripe) · Order #{order_id}", use_container_width=True):
                url = create_checkout_for_order(order_id)
                if url:
                    trigger_checkout(url)
                else:
                    st.error("Could not start checkout. Check Stripe keys / .env.")
        else:
            st.info("Stripe disabled or API key missing.")

# ------------------------- KITCHEN (KDS) -------------------------

def _safe_dt(v):
    if v is None:
        return "-"
    try:
        if isinstance(v, str):
            # Handle ISO strings with or without 'T' / 'Z'
            vs = v.replace("Z", "").strip()
            try:
                return datetime.fromisoformat(vs)
            except Exception:
                return pd.to_datetime(vs, errors="coerce").to_pydatetime() if pd.to_datetime(vs, errors="coerce") is not pd.NaT else v
        # Timestamps / datetime-like
        return pd.to_datetime(v, errors="coerce").to_pydatetime()
    except Exception:
        return v

def kitchen_ui():
    st.header("Kitchen Display (KDS)")
    con = get_conn(); cur = con.cursor()
    cur.execute(
        "SELECT id, created_at, service_type, table_number, status FROM orders WHERE archived=0 AND status IN ('new','in_progress','ready') ORDER BY id DESC"
    )
    orders = cur.fetchall()

    if not orders:
        st.info("No active orders.")
        con.close(); return

    for oid, created_at, service_type, table_number, status in orders:
        with st.container(border=True):
            cols = st.columns([2,2,2,2,3])
            cols[0].markdown(f"**Order #{oid}**")
            cols[1].write(_safe_dt(created_at))
            cols[2].write(service_type)
            cols[3].write(table_number or "-")
            cols[4].write(f"Status: **{status}**")

            icur = con.cursor()
            icur.execute("SELECT item_name, qty, size, modifiers, item_notes FROM order_items WHERE order_id=? AND voided=0", (oid,))
            for iname, qty, size, mods, notes in icur.fetchall():
                line = f"{iname} × {qty}"
                if size:
                    line += f" · {size}"
                if mods:
                    mods_list = [m.get("name") for m in json.loads(mods or "[]")] or []
                    if mods_list:
                        line += f" · +{', '.join(mods_list)}"
                st.write(line)
                if notes:
                    st.caption(f"Notes: {notes}")

            c2 = st.columns(3)
            if c2[0].button("Start", key=f"start_{oid}"):
                cur.execute("UPDATE orders SET status='in_progress' WHERE id=?", (oid,)); con.commit(); st.experimental_rerun()
            if c2[1].button("Ready", key=f"ready_{oid}"):
                cur.execute("UPDATE orders SET status='ready' WHERE id=?", (oid,)); con.commit(); st.experimental_rerun()
            if c2[2].button("Complete", key=f"done_{oid}"):
                cur.execute("UPDATE orders SET status='completed' WHERE id=?", (oid,)); con.commit(); st.experimental_rerun()

    con.close()

# ------------------------- MANAGER -------------------------

def manager_ui():
    st.header("Manager · Orders & Reports")
    con = get_conn(); cur = con.cursor()

    st.caption("Showing today's orders by default. Use filters to expand.")
    cols = st.columns(4)
    date_from = cols[0].date_input("From", date.today())
    date_to = cols[1].date_input("To", date.today())
    status_f = cols[2].multiselect("Status", STATUS_CHOICES, default=[])
    show_arch = cols[3].checkbox("Include archived", value=False)

    q = "SELECT id, created_at, customer_name, service_type, status, paid, total FROM orders WHERE DATE(created_at) BETWEEN ? AND ?"
    params = [date_from.isoformat(), date_to.isoformat()]
    if status_f:
        q += " AND status IN (%s)" % ",".join(["?"] * len(status_f))
        params += status_f
    if not show_arch:
        q += " AND archived=0"
    q += " ORDER BY id DESC"

    cur.execute(q, params)
    rows = cur.fetchall()

    df = pd.DataFrame(rows, columns=["OrderID","Created","Customer","Service","Status","Paid","Total"])
    st.dataframe(df, hide_index=True, use_container_width=True)

    if not df.empty:
        today_str = date.today().isoformat()
        st.download_button("Export CSV", df.to_csv(index=False).encode("utf-8"), file_name=f"orders_{today_str}.csv")

    st.subheader("Quick Actions")
    oid = st.number_input("Order ID", min_value=1, step=1)
    cols2 = st.columns(4)
    if cols2[0].button("Toggle Paid"):
        cur.execute("UPDATE orders SET paid = CASE paid WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (int(oid),)); con.commit(); st.success("Paid toggled.")
    if cols2[1].button("Archive"):
        cur.execute("UPDATE orders SET archived=1 WHERE id=?", (int(oid),)); con.commit(); st.success("Archived.")
    if cols2[2].button("Unarchive"):
        cur.execute("UPDATE orders SET archived=0 WHERE id=?", (int(oid),)); con.commit(); st.success("Unarchived.")
    if cols2[3].button("Mark Completed"):
        cur.execute("UPDATE orders SET status='completed' WHERE id=?", (int(oid),)); con.commit(); st.success("Status updated.")

    if STRIPE_ENABLED and stripe.api_key:
        if st.button("Create Stripe Checkout for Order ID above"):
            url = create_checkout_for_order(int(oid))
            if url:
                trigger_checkout(url)
            else:
                st.error("Could not create checkout (order not found or Stripe not configured).")

    # Quick report
    st.subheader("Today at a Glance")
    cur.execute("SELECT COUNT(*), SUM(total) FROM orders WHERE DATE(created_at)=DATE('now') AND archived=0")
    cnt, gross = cur.fetchone()
    colA, colB, colC = st.columns(3)
    colA.metric("Orders Today", cnt or 0)
    colB.metric("Gross Sales", money(gross or 0))
    cur.execute("SELECT AVG(total) FROM orders WHERE DATE(created_at)=DATE('now') AND archived=0")
    avg_t = cur.fetchone()[0] or 0
    colC.metric("Avg Ticket", money(avg_t))

    con.close()

# ------------------------- ADMIN -------------------------

def _stripe_diagnostics_ui():
    st.subheader("Stripe diagnostics")
    env_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    active_key = (getattr(stripe, "api_key", "") or "").strip()
    st.caption(f"Loaded from env: {'(none)' if not env_key else env_key[:10] + '…' + env_key[-6:]} | Active in SDK: {'(none)' if not active_key else active_key[:10] + '…' + active_key[-6:]} | Ready: {bool(active_key)}")
    st.caption(f"PUBLIC_BASE_URL: {PUBLIC_BASE_URL}")

    col0, col1, col2 = st.columns(3)
    if col0.button("Reload key from .env"):
        try:
            stripe.api_key = env_key
            st.success("Stripe key reloaded into SDK.")
        except Exception as e:
            st.error(f"Failed to set key: {e}")

    if col1.button("Ping Stripe (Account.retrieve)"):
        try:
            acct = stripe.Account.retrieve()
            st.success(f"✅ Key valid. Account: {acct.get('id')}")
        except Exception as e:
            st.error(f"❌ Key/Network error: {e}")
    if col2.button("Create $1 test Checkout"):
        try:
            success_url = f"{PUBLIC_BASE_URL}?test=success"
            cancel_url  = f"{PUBLIC_BASE_URL}?test=cancel"
            sess = stripe.checkout.Session.create(
                mode="payment",
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "product_data": {"name": "Test $1 charge"},
                        "unit_amount": 100,
                    },
                    "quantity": 1,
                }],
                success_url=success_url,
                cancel_url=cancel_url,
            )
            st.success("Session created. Open below:")
            try:
                st.link_button("Open $1 test checkout", sess.url, use_container_width=True)
            except Exception:
                st.markdown(f"[Open $1 test checkout]({sess.url})")
        except Exception as e:
            st.error(f"Failed to create test session: {e}")

def admin_ui():
    st.header("Admin")
    if not st.session_state.admin_unlocked:
        pin = st.text_input("Enter PIN", type="password")
        if st.button("Unlock"):
            if pin == st.session_state.pin:
                st.session_state.admin_unlocked = True
                st.success("Admin unlocked.")
                try:
                    st.rerun()
                except Exception:
                    st.experimental_rerun()
            else:
                st.error("Wrong PIN.")
        st.stop()

    st.success("Admin mode active")

    with st.expander("Settings", expanded=True):
        st.session_state.tax_rate = st.number_input("Tax rate", 0.0, 0.5, float(st.session_state.tax_rate))
        st.session_state.delivery_fee = st.number_input("Default delivery fee", 0.0, 50.0, float(st.session_state.delivery_fee))
        new_pin = st.text_input("Set new PIN", value=st.session_state.pin)
        if st.button("Save Settings"):
            st.session_state.pin = new_pin
            st.success("Settings saved.")

    st.subheader("Menu Editor (menu.json)")
    menu_text = open(MENU_FILE, "r", encoding="utf-8-sig").read()
    new_menu_text = st.text_area("menu.json", value=menu_text, height=400)
    if st.button("Save Menu JSON"):
        try:
            parsed = json.loads(new_menu_text)
            save_menu(parsed)
            st.success("Menu saved.")
        except Exception as e:
            st.error(f"Invalid JSON: {e}")

    with st.expander("Reports"):
        con = get_conn(); cur = con.cursor()
        cur.execute("SELECT COUNT(*), SUM(total) FROM orders WHERE archived=0")
        cnt, gross = cur.fetchone()
        st.metric("All-time Orders", cnt or 0)
        st.metric("All-time Gross", money(gross or 0))
        cur.execute("SELECT id, created_at, customer_name, service_type, status, paid, total FROM orders ORDER BY id DESC")
        rows = cur.fetchall()
        df = pd.DataFrame(rows, columns=["OrderID","Created","Customer","Service","Status","Paid","Total"])
        st.download_button("Export All Orders CSV", df.to_csv(index=False).encode("utf-8"), file_name="orders_all.csv")
        con.close()

    with st.expander("Stripe", expanded=False):
        _stripe_diagnostics_ui()

# ------------------------- BANNERS & APP -------------------------

def init_banner():
    qp = st.query_params
    # st.query_params may return strings; handle either strings or list[str]
    def _get(name):
        v = qp.get(name)
        if isinstance(v, list):
            return v[0] if v else None
        return v
    if _get("checkout") == "success" and _get("order_id"):
        st.success(f"Payment received – thank you! (Order #{_get('order_id')})")
    elif _get("checkout") == "canceled" and _get("order_id"):
        st.warning(f"Checkout canceled for Order #{_get('order_id')}")


def main():
    st.set_page_config(page_title=APP_NAME, page_icon="🍕", layout="wide")
    st.title(APP_NAME)
    st.caption("Single-file POS · Streamlit")
    sk = (os.getenv('STRIPE_SECRET_KEY') or '')
    from pathlib import Path as _P
    _env_exists = _P(str(ENV_PATH)).exists()
    st.caption(f"Stripe ready: {bool(stripe.api_key)} · key starts with: {sk[:7]}")
    st.caption(f".env path: {ENV_PATH} · exists: {_env_exists} · PUBLIC_BASE_URL: {PUBLIC_BASE_URL}")

    init_state()
    init_db()
    menu = load_menu()
    init_banner()
    # If we recently created a Checkout session, render banner and attempt popup once
    try:
        render_checkout_banner()
    except Exception:
        pass

    tabs = st.tabs(["Order", "Kitchen", "Manager", "Admin"])
    with tabs[0]:
        place_order_ui(menu)
    with tabs[1]:
        kitchen_ui()
    with tabs[2]:
        manager_ui()
    with tabs[3]:
        admin_ui()

    st.markdown("---")
    st.caption("Tip: Put your logo next to this file and rename APP_NAME at the top.")


if __name__ == "__main__":
    main()

