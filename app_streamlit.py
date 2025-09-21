# app_streamlit.py
# Fresh single-file Streamlit POS (Order ▸ Kitchen ▸ Manager ▸ Admin)
# - One checkout button: "Pay with Stripe"
# - Verifies Checkout session on return (marks order Paid without webhooks)
# - FIX: unique widget keys in checkout helper to avoid StreamlitDuplicateElementId
# - menu.json & orders.db auto-create next to this file
# - Works on Streamlit Cloud via st.secrets or locally via .env / env vars
#
# Quickstart (local):
#   pip install streamlit pandas stripe python-dotenv
#   streamlit run app_streamlit.py --server.port 8502
#
# Required secrets / env:
#   STRIPE_SECRET_KEY = sk_test_...
#   PUBLIC_BASE_URL   = https://<your-app-url>  (https://*.streamlit.app when deployed)

from __future__ import annotations
import json
import os
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, List, Optional

import pandas as pd
import streamlit as st
from streamlit.components.v1 import html as st_html

import stripe
from urllib.parse import urlencode, urlsplit, urlunsplit
from dotenv import load_dotenv
import hashlib

APP_DIR = Path(__file__).resolve().parent
ENV_PATH = APP_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)

APP_NAME = "Fireside Pizza POS"
DEFAULT_PIN = "1234"
CURRENCY = "$"
DEFAULT_TAX_RATE = 0.085
DEFAULT_DELIVERY_FEE = 3.00
MENU_FILE = str(APP_DIR / "menu.json")
DB_FILE = str(APP_DIR / "orders.db")

# ---------- Secrets / ENV helpers ----------

def _get_secret_env(name: str, default: str = "") -> str:
    """Prefer Streamlit secrets; fallback to environment."""
    try:
        v = st.secrets.get(name)  # type: ignore[attr-defined]
        if v:
            return str(v).strip()
    except Exception:
        pass
    return (os.getenv(name, default) or "").strip()


def _clean_base_url(url: str) -> str:
    """Strip query/fragment and trailing slashes; keep scheme+host only."""
    if not url:
        return url
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return url
    base = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    return base.rstrip("/")

STRIPE_ENABLED = True
stripe.api_key = _get_secret_env("STRIPE_SECRET_KEY", "")
PUBLIC_BASE_URL = _clean_base_url(_get_secret_env("PUBLIC_BASE_URL", "http://127.0.0.1:8502"))

STATUS_CHOICES = ["new", "in_progress", "ready", "completed"]
SERVICE_TYPES = ["Dine-In", "Takeout", "Delivery"]
PAYMENT_METHODS = ["Card (Stripe)"]

# ---------- Utilities ----------

def money(x: float) -> str:
    try:
        return f"{CURRENCY}{x:,.2f}"
    except Exception:
        return f"{CURRENCY}{x}"


def ensure_menu_file(path: str = MENU_FILE) -> None:
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
                        "available_toppings": [{"name": "Marinara", "price_delta": 0.75}],
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
                    }
                ],
            },
        ]
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(default_menu, f, indent=2)


def load_menu(path: str = MENU_FILE) -> Dict[str, Any]:
    ensure_menu_file(path)
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


# ---------- Database ----------

def get_conn():
    return sqlite3.connect(DB_FILE, check_same_thread=False)


def init_db():
    con = get_conn(); cur = con.cursor()
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
            service_type TEXT,
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
    con.commit(); con.close()


# ---------- Pricing ----------

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
                size_delta = float(s.get("price_delta", 0)); break
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


# ---------- State ----------

def init_state():
    st.session_state.setdefault("cart", [])
    st.session_state.setdefault("admin_unlocked", False)
    st.session_state.setdefault("tax_rate", DEFAULT_TAX_RATE)
    st.session_state.setdefault("delivery_fee", DEFAULT_DELIVERY_FEE)
    st.session_state.setdefault("pin", DEFAULT_PIN)


# ---------- UI helpers ----------

def open_in_new_tab(url: str):
    st_html(f"<script>window.open('{url}', '_blank');</script>", height=0)


def add_item_ui(menu: Dict[str, Any]):
    st.subheader("Add Item")
    cat_names = [c["name"] for c in menu.get("categories", [])]
    if not cat_names:
        st.warning("No categories in menu.json. Use Admin to edit the menu.")
        return
    cat_name = st.selectbox("Category", cat_names)
    cat = next(c for c in menu["categories"] if c["name"] == cat_name)
    item_names = [i["name"] for i in cat.get("items", [])]
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


def cart_summary_ui(menu: Dict[str, Any]) -> float:
    st.subheader("Cart")
    if not st.session_state.cart:
        st.info("Cart is empty.")
        return 0.0
    subtotal = 0.0
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
                line["qty"] = max(1, int(line["qty"]) - 1)
                line["line_total"] = calc_line_total(menu, line["item_id"], line["qty"], line.get("size"), line.get("modifiers", []))
            if cols[1].button("+1", key=f"inc_{i}"):
                line["qty"] = int(line["qty"]) + 1
                line["line_total"] = calc_line_total(menu, line["item_id"], line["qty"], line.get("size"), line.get("modifiers", []))
            if cols[2].button("Remove", key=f"rm_{i}"):
                st.session_state.cart.pop(i)
                st.rerun()
        subtotal += float(line["line_total"])
    st.markdown(f"**Subtotal:** {money(subtotal)}")
    return subtotal


# ---------- Stripe helpers ----------

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


def create_checkout_for_order(order_id: int) -> Optional[str]:
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
        amount_cents = int(round(float(total) * 100))
        if amount_cents <= 0:
            st.error("Total must be > 0 to create checkout.")
            return None

        success_url = f"{PUBLIC_BASE_URL}?checkout=success&order_id={order_id}&session_id={{CHECKOUT_SESSION_ID}}"
        cancel_url  = f"{PUBLIC_BASE_URL}?checkout=canceled&order_id={order_id}"

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


def trigger_checkout(url: str, key_suffix: Optional[str] = None):
    """Open Checkout and render helper controls with unique widget keys."""
    ks = key_suffix or hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    st.session_state["last_checkout_url"] = url
    open_in_new_tab(url)

    try:
        st.link_button("Open secure checkout", url, use_container_width=True, key=f"open_{ks}")
    except Exception:
        st.markdown(f"[Manager (read-only)]({app_url}/?view=manager&readonly=1) · [Order]({app_url}/?view=order) · [Kitchen (KDS)]({app_url}/?view=kitchen)")
")

    if st.button("Try popup again", key=f"retry_{ks}"):
        open_in_new_tab(url)

    st.info("If nothing opened, check your popup blocker.")


# ---------- Order flow ----------

def place_order_ui(menu: Dict[str, Any]):
    st.header("Front Desk · Take Orders")

    with st.container(border=True):
        st.subheader("Build Cart")
        add_item_ui(menu)
        subtotal = cart_summary_ui(menu)

    st.divider()

    with st.form("order_form", border=True):
        cols = st.columns(2)
        customer_name = cols[0].text_input("Customer name")
        customer_phone = cols[1].text_input("Phone (optional)")

        service_type = st.radio("Service type", SERVICE_TYPES, horizontal=True)
        table_number = st.text_input("Table number (optional)" if service_type == "Dine-In" else "Table number (optional)") if service_type == "Dine-In" else None
        delivery_fee = st.number_input("Delivery fee", 0.0, 99.0, float(st.session_state.delivery_fee)) if service_type == "Delivery" else 0.0

        st.divider()
        cols2 = st.columns(3)
        discount = cols2[0].number_input("Discount (amount)", 0.0, 999.0, 0.0)
        tip = cols2[1].number_input("Tip", 0.0, 999.0, 0.0)
        tax_rate = cols2[2].number_input("Tax Rate", 0.0, 0.5, float(st.session_state.tax_rate))

        notes = st.text_area("Order notes (optional)")

        totals = calc_order_totals(subtotal, tax_rate, discount, delivery_fee, tip)
        st.info(f"Total: {money(totals['total'])}  ·  Subtotal {money(totals['subtotal'])} · Tax {money(totals['tax'])}")

        pay_and_submit = st.form_submit_button("Pay with Stripe", type="primary", use_container_width=True)

    if pay_and_submit:
        if not st.session_state.cart:
            st.error("Cart is empty.")
            return
        con = get_conn(); cur = con.cursor()
        created_at = datetime.now().isoformat(timespec="seconds")
        cust_id = create_or_get_customer(con, (customer_name or "").strip(), (customer_phone or "").strip()) if customer_phone else None

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
                "new", 0, "Card (Stripe)", notes, "POS",
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
        con.commit(); con.close()

        st.session_state.cart = []
        st.session_state["last_order_id"] = int(order_id)
        st.success(f"Order #{order_id} placed!")

        if STRIPE_ENABLED and stripe.api_key:
            url = create_checkout_for_order(order_id)
            if url:
                trigger_checkout(url)
            else:
                st.error("Could not start checkout. See Admin → Stripe diagnostics.")
        else:
            st.error("Stripe disabled or API key missing.")


# ---------- Kitchen (KDS) ----------

def kitchen_ui():
    st.header("Kitchen Display (KDS)")
    con = get_conn(); cur = con.cursor()
    cur.execute("SELECT id, created_at, service_type, table_number, status FROM orders WHERE archived=0 AND status IN ('new','in_progress','ready') ORDER BY id DESC")
    orders = cur.fetchall()
    if not orders:
        st.info("No active orders.")
        con.close(); return

    for oid, created_at, service_type, table_number, status in orders:
        with st.container(border=True):
            cols = st.columns([2,2,2,2,3])
            cols[0].markdown(f"**Order #{oid}**")
            try:
                cols[1].write(created_at)
            except Exception:
                cols[1].write(str(created_at))
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
                    try:
                        mods_list = [m.get("name") for m in json.loads(mods or "[]")] or []
                        if mods_list:
                            line += f" · +{', '.join(mods_list)}"
                    except Exception:
                        pass
                st.write(line)
                if notes:
                    st.caption(f"Notes: {notes}")

            c2 = st.columns(3)
            if c2[0].button("Start", key=f"start_{oid}"):
                cur.execute("UPDATE orders SET status='in_progress' WHERE id=?", (oid,)); con.commit(); st.rerun()
            if c2[1].button("Ready", key=f"ready_{oid}"):
                cur.execute("UPDATE orders SET status='ready' WHERE id=?", (oid,)); con.commit(); st.rerun()
            if c2[2].button("Complete", key=f"done_{oid}"):
                cur.execute("UPDATE orders SET status='completed' WHERE id=?", (oid,)); con.commit(); st.rerun()
    con.close()


# ---------- Manager ----------

def manager_ui():
    def _qp1(name):
        v = st.query_params.get(name)
        if isinstance(v, list):
            return (v[0] if v else None)
        return v
    readonly = (_qp1("readonly") or "").lower() in {"1","true","yes"}

    st.header("Manager · Orders & Reports")
    con = get_conn(); cur = con.cursor()

    cols = st.columns(4)
    date_from = cols[0].date_input("From", date.today())
    date_to = cols[1].date_input("To", date.today())
    status_f = cols[2].multiselect("Status", STATUS_CHOICES, default=[])
    paid_filter = cols[3].selectbox("Paid?", ["All","Paid only","Unpaid only"])

    q = "SELECT id, created_at, customer_name, service_type, status, paid, total FROM orders WHERE DATE(created_at) BETWEEN ? AND ?"
    params = [date_from.isoformat(), date_to.isoformat()]
    if status_f:
        q += " AND status IN (%s)" % ",".join(["?"]*len(status_f)); params += status_f
    if paid_filter == "Paid only":
        q += " AND paid=1"
    elif paid_filter == "Unpaid only":
        q += " AND paid=0"
    q += " ORDER BY id DESC"

    cur.execute(q, params)
    rows = cur.fetchall()

    df = pd.DataFrame(rows, columns=["OrderID","Created","Customer","Service","Status","Paid","Total"])
    st.dataframe(df, hide_index=True, use_container_width=True)

    if not df.empty:
        today_str = date.today().isoformat()
        st.download_button("Export CSV", df.to_csv(index=False).encode("utf-8"), file_name=f"orders_{today_str}.csv")

    if not readonly:
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
                    trigger_checkout(url)  # url-based key prevents duplicate id
                else:
                    st.error("Could not create checkout (order not found or Stripe not configured).")

    # Metrics
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


# ---------- Admin ----------

def admin_ui():
    st.header("Admin")
    if not st.session_state.admin_unlocked:
        pin = st.text_input("Enter PIN", type="password")
        if st.button("Unlock"):
            if pin == st.session_state.pin:
                st.session_state.admin_unlocked = True
                st.success("Admin unlocked.")
                st.rerun()
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

    with st.expander("Stripe diagnostics", expanded=False):
        env_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
        active_key = (getattr(stripe, "api_key", "") or "").strip()
        st.caption(f"Loaded key: {'(none)' if not env_key else env_key[:10] + '…' + env_key[-6:]} | Ready: {bool(active_key)}")
        st.caption(f"PUBLIC_BASE_URL: {PUBLIC_BASE_URL}")
        c0, c1, c2 = st.columns(3)
        if c0.button("Reload key from env/secrets"):
            stripe.api_key = _get_secret_env("STRIPE_SECRET_KEY", "")
            st.success("Reloaded into SDK.")
        if c1.button("Ping Stripe (Account.retrieve)"):
            try:
                acct = stripe.Account.retrieve()
                st.success(f"✅ Key valid. Account: {acct.get('id')}")
            except Exception as e:
                st.error(f"❌ {e}")
        if c2.button("Create $1 test Checkout"):
            try:
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
                    success_url=f"{PUBLIC_BASE_URL}?checkout=success&order_id=TEST&session_id={{CHECKOUT_SESSION_ID}}",
                    cancel_url=f"{PUBLIC_BASE_URL}?checkout=canceled&order_id=TEST",
                )
                trigger_checkout(sess.url)  # gets unique keys
            except Exception as e:
                st.error(f"Failed to create test session: {e}")

    st.subheader("Menu Editor (menu.json)")
    menu_text = open(MENU_FILE, "r", encoding="utf-8-sig").read()
    new_menu_text = st.text_area("menu.json", value=menu_text, height=400)
    if st.button("Save Menu JSON"):
        try:
            parsed = json.loads(new_menu_text)
            with open(MENU_FILE, "w", encoding="utf-8") as f:
                json.dump(parsed, f, indent=2, ensure_ascii=False)
            st.success("Menu saved.")
        except Exception as e:
            st.error(f"Invalid JSON: {e}")


# ---------- Return banner + verification ----------

def init_banner():
    qp = st.query_params
    if qp.get("checkout") == ["success"] and qp.get("order_id"):
        order_id = qp["order_id"][0]
        session_id = (qp.get("session_id") or [None])[0]
        if STRIPE_ENABLED and stripe.api_key and session_id and order_id != "TEST":
            try:
                sess = stripe.checkout.Session.retrieve(session_id)
                if sess.get("payment_status") == "paid":
                    con = get_conn(); cur = con.cursor()
                    cur.execute("UPDATE orders SET paid=1 WHERE id=?", (int(order_id),))
                    con.commit(); con.close()
                    st.success(f"Payment confirmed — Order #{order_id} marked paid.")
                else:
                    st.warning(f"Returned from Stripe — payment status: {sess.get('payment_status')}")
            except Exception as e:
                st.error(f"Payment verification failed: {e}")
        elif order_id == "TEST":
            st.success("Returned from test checkout.")
        try:
            st.query_params.clear(); st.rerun()
        except Exception:
            pass
    elif qp.get("checkout") == ["canceled"] and qp.get("order_id"):
        st.warning(f"Checkout canceled for Order #{qp['order_id'][0]}.")
        try:
            st.query_params.clear(); st.rerun()
        except Exception:
            pass


# ---------- App ----------

def main():
    st.set_page_config(page_title=APP_NAME, page_icon="🍕", layout="wide")
    st.title(APP_NAME)
    st.caption("Single-file POS · Streamlit")
    st.caption(f"Stripe ready: {bool(stripe.api_key)} · key starts with: {(stripe.api_key or '')[:7]}")

    # Quick demo links (added)
    c1, c2, c3 = st.columns(3)
    app_url = PUBLIC_BASE_URL or "https://<your-app>.streamlit.app"
    try:
        c1.link_button("Manager (read-only)", f"{app_url}/?view=manager&readonly=1", use_container_width=True, key="demo_mgr_ro")
        c2.link_button("Order (take payment)", f"{app_url}/?view=order", use_container_width=True, key="demo_order")
        c3.link_button("Kitchen (KDS)", f"{app_url}/?view=kitchen", use_container_width=True, key="demo_kitchen")
    except Exception:
        st.markdown(f"[Manager (read-only)]({app_url}/?view=manager&readonly=1) · [Order]({app_url}/?view=order)")

    init_state()
    init_db()
    menu = load_menu()
    init_banner()

    # Presentation routing via query param: ?view=manager|kitchen|order|admin (&readonly=1)
    def _qp1(name):
        v = st.query_params.get(name)
        if isinstance(v, list):
            return v[0] if v else None
        return v
    view = (_qp1("view") or "").lower()
    if view in {"manager","kitchen","order","admin"}:
        if view == "manager": manager_ui(); return
        if view == "kitchen": kitchen_ui(); return
        if view == "order": place_order_ui(menu); return
        if view == "admin": admin_ui(); return

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
    st.caption("Tip: Set PUBLIC_BASE_URL to your deployed https URL for Stripe returns.")


if __name__ == "__main__":
    main()
