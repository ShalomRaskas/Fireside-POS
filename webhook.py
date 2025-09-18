# webhook.py
import os
import sqlite3
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import PlainTextResponse
import stripe

DB_FILE = "orders.db"  # same DB your Streamlit app uses
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

app = FastAPI()

def get_conn():
    return sqlite3.connect(DB_FILE, check_same_thread=False)

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()

    if not WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Webhook secret not set")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload, sig_header=stripe_signature, secret=WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid payload")

    # Mark order paid on successful checkout
    if event["type"] in ("checkout.session.completed", "payment_intent.succeeded"):
        data = event["data"]["object"]
        order_id = (data.get("metadata") or {}).get("order_id")
        if order_id:
            con = get_conn()
            cur = con.cursor()
            cur.execute(
                "UPDATE orders SET paid=1, payment_method='Card' WHERE id=?",
                (int(order_id),)
            )
            con.commit()
            con.close()

    return PlainTextResponse("ok", status_code=200)
