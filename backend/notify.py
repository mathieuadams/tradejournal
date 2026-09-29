"""Text-message alerts through Amazon SNS (plus a log you can see in the app).

US numbers: AWS requires an origination identity (e.g. a toll-free number registered in AWS End User Messaging SMS)
and, while the account is in the SMS sandbox, each destination number must be verified first. See SETUP.md.
"""
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
    events = cfg.get("events") or ["entry", "fill", "exit", "closed", "cancel", "test"]
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
    db.put(item)
    return item


def recent(sub, limit=30):
    return [{k: v for k, v in i.items() if k not in ("PK", "SK")} | {"at": i["SK"].split("#")[1]}
            for i in db.q_prefix(db.upk(sub), "NOTIFY#", desc=True, limit=limit)]
