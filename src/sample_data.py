from __future__ import annotations

import random
from datetime import datetime, timedelta

import pandas as pd


EVENT_FLOW = ["view_item", "add_to_cart", "begin_checkout", "purchase"]


def generate_sample_events(n_users: int = 80, seed: int = 42) -> pd.DataFrame:
    random.seed(seed)
    rows = []
    start = datetime.now() - timedelta(hours=2)

    for idx in range(n_users):
        user_id = f"user_{idx:03d}"
        ts = start + timedelta(seconds=random.randint(0, 7200))

        signed = random.random() > 0.1
        has_cart = random.random() > 0.15
        has_purchase = random.random() > 0.25

        if signed:
            rows.append(
                {
                    "event_name": "sign_up",
                    "event_date": ts.date(),
                    "event_timestamp": ts,
                    "user_pseudo_id": user_id,
                    "transaction_id": None,
                    "value": None,
                    "currency": None,
                }
            )

        rows.append(
            {
                "event_name": "view_item",
                "event_date": ts.date(),
                "event_timestamp": ts + timedelta(minutes=1),
                "user_pseudo_id": user_id,
                "transaction_id": None,
                "value": None,
                "currency": "KRW",
            }
        )

        if has_cart:
            rows.append(
                {
                    "event_name": "add_to_cart",
                    "event_date": ts.date(),
                    "event_timestamp": ts + timedelta(minutes=2),
                    "user_pseudo_id": user_id,
                    "transaction_id": None,
                    "value": random.randint(1000, 9000),
                    "currency": "KRW",
                }
            )

        rows.append(
            {
                "event_name": "begin_checkout",
                "event_date": ts.date(),
                "event_timestamp": ts + timedelta(minutes=3),
                "user_pseudo_id": user_id,
                "transaction_id": None,
                "value": random.randint(1000, 9000),
                "currency": "KRW",
            }
        )

        if has_purchase:
            tx_id = f"tx_{random.randint(1, 60):04d}"  # intentional duplicates
            value = random.randint(2000, 12000)
            if random.random() < 0.15:
                value = None
            if random.random() < 0.1:
                value = "bad_value"

            rows.append(
                {
                    "event_name": "purchase",
                    "event_date": ts.date(),
                    "event_timestamp": ts + timedelta(minutes=4),
                    "user_pseudo_id": user_id,
                    "transaction_id": tx_id if random.random() > 0.2 else None,
                    "value": value,
                    "currency": "KRW" if random.random() > 0.1 else None,
                }
            )

    return pd.DataFrame(rows)
