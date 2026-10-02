"""Alerts through Amazon SNS: an SNS topic (email subscriptions, works right away) and optional direct SMS
(plus a log you can see in the app).

US numbers: AWS requires an origination identity (e.g. a toll-free number registered in AWS End User Messaging SMS)
and, while the account is in the SMS sandbox, each destination number must be verified first. See SETUP.md.
"""
import json
import os
import re

import db
from util import iso, now_ny


def _settings(sub):
    from views import load_settings
    return (load_settings(sub).get("notify") or {})


def send(sub, text, kind="order"):
    cfg = _settings(sub)
    item = {"PK": db.upk(sub), "SK": f"NOTIFY#{iso(now_ny())}#{kind}", "text": text[:480], "kind": kind, "status": "logged"}
    phone = cfg.get("phone")
    events = cfg.get("events") or ["entry", "fill", "exit", "closed", "cancel", "roll", "ai", "test"]
    if cfg.get("sms") and phone and re.match(r"^\+[1-9]\d{7,14}$", phone) and (kind in events or kind in ("test", "order")):
        try:
            import boto3
            boto3.client("sns").publish(PhoneNumber=phone, Message=text[:480], MessageAttributes={
                "AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"}})
            item["status"] = "sent"
        except Exception as e:
            item["status"] = "failed"
            item["error"] = str(e)[:300]
            print("sms failed", e)
    topic = os.environ.get("ALERT_TOPIC_ARN")
    if topic and cfg.get("email") and (kind in events or kind in ("test", "order")):
        try:
            import boto3
            boto3.client("sns").publish(TopicArn=topic, Subject=("Trade Journal: " + text)[:99], Message=text,
                                        MessageAttributes={"user": {"DataType": "String", "StringValue": sub}})
            item["topic"] = "sent"
            if item["status"] == "logged":
                item["status"] = "sent"
        except Exception as e:
            item["topic"] = "failed"
            item["error"] = (item.get("error", "") + " topic: " + str(e))[:300]
            if item["status"] == "logged":
                item["status"] = "failed"
    db.put(item)
    return item


def subscribe_email(sub, email):
    """Subscribe an email address to the alerts topic, only for this user's messages (filter policy)."""
    import boto3
    topic = os.environ["ALERT_TOPIC_ARN"]
    sns = boto3.client("sns")
    for s_ in sns.list_subscriptions_by_topic(TopicArn=topic).get("Subscriptions", []):
        if s_["Protocol"] == "email" and s_["Endpoint"].lower() == email.lower():
            if s_["SubscriptionArn"].startswith("arn:"):
                return {"email": email, "status": "confirmed"}
            return {"email": email, "status": "pending"}
    sns.subscribe(TopicArn=topic, Protocol="email", Endpoint=email, ReturnSubscriptionArn=True,
                  Attributes={"FilterPolicy": json.dumps({"user": [sub]})})
    return {"email": email, "status": "pending"}


def email_status(email):
    import boto3
    topic = os.environ.get("ALERT_TOPIC_ARN")
    if not topic or not email:
        return None
    for s_ in boto3.client("sns").list_subscriptions_by_topic(TopicArn=topic).get("Subscriptions", []):
        if s_["Protocol"] == "email" and s_["Endpoint"].lower() == email.lower():
            return "confirmed" if s_["SubscriptionArn"].startswith("arn:") else "pending"
    return "not subscribed"


def recent(sub, limit=30):
    return [{k: v for k, v in i.items() if k not in ("PK", "SK")} | {"at": i["SK"].split("#")[1]}
            for i in db.q_prefix(db.upk(sub), "NOTIFY#", desc=True, limit=limit)]
